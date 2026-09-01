"""Phase 1 ingest: PumpPortal WebSocket -> SQLite.

Ingest used to hold three Helius `logsSubscribe` subscriptions. That worked, but
it meant receiving every trade on every watched program to find the launches
hidden in them: about one message in 3,500. The Helius dashboard put the cost at
35,989 credits in under an hour, 99.3% of it WebSocket delivery, which exhausts
the 1M/month free tier in well under a day. The architecture was the problem, not
the tuning.

PumpPortal publishes creation events only, free and unauthenticated, so the same
launches arrive without the firehose. Helius stays in the project as an RPC
client for phase 2 enrichment, where it costs a couple of hundred credits per
session rather than tens of thousands.

The field mapping below was built by capturing ten real payloads off the socket
and reading them, not from documentation. Nothing here assumes a shape.

Failure model is unchanged: the connection does not error, it goes quiet. Ping
plus a silence watchdog, and reconnect with exponential backoff and jitter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

from websockets.asyncio.client import connect

from . import config
from .credits import CreditMeter
from .db import STATUS_DECODED, Database
from .resolver import Resolver

log = logging.getLogger("solscanner.ingest")

# Fields a create event must carry to be storable at all.
REQUIRED_FIELDS = ("signature", "mint")


def to_display_tz(ts: datetime) -> datetime:
    return ts.astimezone(timezone(timedelta(hours=config.DISPLAY_TZ_OFFSET_HOURS)))


def now_display() -> str:
    return to_display_tz(datetime.now(timezone.utc)).strftime("%H:%M:%S")


def safe(text: str | None, limit: int = 28) -> str:
    """Token names contain arbitrary unicode, including emoji. Console encodings
    on Windows do not. Flatten for display only; the database keeps the real
    string."""
    if not text:
        return "-"
    flattened = text.encode("ascii", "replace").decode("ascii")
    flattened = "".join(c if c.isprintable() else "?" for c in flattened)
    return flattened[:limit]


def short(address: str | None, head: int = 4, tail: int = 4) -> str:
    if not address:
        return "-" * (head + tail + 2)
    if len(address) <= head + tail + 2:
        return address
    return f"{address[:head]}..{address[-tail:]}"


class Scanner:
    def __init__(self, db: Database):
        self.db = db
        self.meter = CreditMeter(db)
        # Kept for phase 2 enrichment. Nothing in phase 1 ingest submits to it
        # any more: PumpPortal payloads are already complete.
        self.resolver = Resolver(db, self.meter)

        self.started_at = time.monotonic()
        self.messages = 0
        self.creations = 0
        self.rows_written = 0
        self.duplicates = 0
        self.malformed = 0
        self.unknown_pool = 0
        self.other_messages = 0
        self.reconnects = 0
        self.subscribed = False
        self.run_id: int | None = None

        self._stop = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        self.run_id = self.db.start_run(f"pumpportal:{config.PUMPPORTAL_SUBSCRIBE_METHOD}")
        self._banner()

        tasks = [
            asyncio.create_task(self._websocket_loop(), name="websocket"),
            asyncio.create_task(self._status_loop(), name="status"),
        ]
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()

    def _banner(self) -> None:
        print("=" * 78)
        print(f"  Solana token scanner - phase 1 (ingest)   {now_display()} {config.DISPLAY_TZ_NAME}")
        print("=" * 78)
        print(f"  source   : {config.PUMPPORTAL_WS_URL}")
        print(f"  method   : {config.PUMPPORTAL_SUBSCRIBE_METHOD} (free, no key, creations only)")
        print(f"  database : {config.DB_PATH}")
        print(f"  log file : {config.LOG_PATH}")
        print(f"  helius   : RPC only, not used by ingest (phase 2 enrichment)")
        print(f"  watchdog : reconnect after {config.WS_SILENCE_TIMEOUT_SECONDS}s of silence")
        existing = self.db.counts_by_ingest_source()
        print(f"  existing rows in database: {self.db.total_tokens()} {existing or ''}")
        print("-" * 78)
        print(f"  Ctrl+C to stop. Status line every {config.STATUS_INTERVAL_SECONDS}s.")
        print("-" * 78)

    def _shutdown(self) -> None:
        self.meter.flush()
        if self.run_id is not None:
            self.db.finish_run(
                self.run_id,
                events_seen=self.messages,
                tokens_written=self.rows_written,
                reconnects=self.reconnects,
            )
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        print("-" * 78)
        print(f"  stopped after {self._uptime()}")
        print(f"  launches captured     : {self.rows_written} "
              f"({self.rows_written / elapsed * 60:.1f}/min)")
        print(f"  duplicates ignored    : {self.duplicates}")
        print(f"  malformed payloads    : {self.malformed}")
        print(f"  totals in database    : {self.db.total_tokens()} "
              f"{self.db.counts_by_ingest_source()}")
        print(f"  helius credits        : {self.meter.summary()}")
        print("=" * 78)

    # -- websocket ----------------------------------------------------------

    async def _websocket_loop(self) -> None:
        delay = config.BACKOFF_INITIAL_SECONDS
        while not self._stop.is_set():
            connected_at = time.monotonic()
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                lived = time.monotonic() - connected_at
                log.warning(
                    "websocket dropped after %.0fs: %s: %s",
                    lived,
                    type(exc).__name__,
                    config.redact(str(exc)),
                )
                print(f"[{now_display()}] connection lost ({type(exc).__name__}), "
                      f"reconnecting in {delay:.0f}s")
                if lived >= config.BACKOFF_RESET_AFTER_SECONDS:
                    # The connection was healthy; this is a one-off drop, not an
                    # endpoint problem. Start the backoff from scratch.
                    delay = config.BACKOFF_INITIAL_SECONDS

            if self._stop.is_set():
                return
            self.reconnects += 1
            self.subscribed = False
            # Jitter so a shared outage does not produce a synchronised retry.
            await asyncio.sleep(delay * (1 + random.random() * 0.25))
            delay = min(delay * config.BACKOFF_FACTOR, config.BACKOFF_MAX_SECONDS)

    async def _connect_and_read(self) -> None:
        async with connect(
            config.PUMPPORTAL_WS_URL,
            ping_interval=config.WS_PING_INTERVAL_SECONDS,
            ping_timeout=config.WS_PING_TIMEOUT_SECONDS,
            max_size=None,
            open_timeout=30,
        ) as ws:
            await ws.send(json.dumps({"method": config.PUMPPORTAL_SUBSCRIBE_METHOD}))
            print(f"[{now_display()}] connected, sent {config.PUMPPORTAL_SUBSCRIBE_METHOD}")
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=config.WS_SILENCE_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError as exc:
                    raise ConnectionError(
                        f"no messages for {config.WS_SILENCE_TIMEOUT_SECONDS}s"
                    ) from exc
                self._handle_raw(raw)

    # -- message handling ---------------------------------------------------

    def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            self.malformed += 1
            log.warning("undecodable frame: %s", raw[:200])
            return

        self.messages += 1

        if not isinstance(message, dict):
            self.other_messages += 1
            log.debug("non-object message: %s", raw[:200])
            return

        # Control messages carry a message/error and none of the event keys, e.g.
        # {"message": "Successfully subscribed to token creation events."}
        #
        # Deliberately NOT keyed on the presence of "mint": if a create event
        # ever arrives without one, that is a payload change we need shouted
        # about, not quietly filed as an unrecognised control message.
        looks_like_event = bool(message.keys() & {"txType", "mint", "signature"})
        if not looks_like_event:
            self.other_messages += 1
            text = message.get("message") or message.get("error") or raw[:200]
            if not self.subscribed and message.get("message"):
                self.subscribed = True
                print(f"[{now_display()}] {text}")
            else:
                log.info("non-token message: %s", str(text)[:300])
            return

        if message.get("txType") != "create":
            # subscribeNewToken should only ever deliver creations. If that
            # changes, do not silently record trades as launches.
            self.other_messages += 1
            log.warning("unexpected txType on new-token feed: %r", message.get("txType"))
            return

        self._record_launch(message)

    def _record_launch(self, event: dict) -> None:
        self.creations += 1

        missing = [f for f in REQUIRED_FIELDS if not isinstance(event.get(f), str) or not event[f]]
        if missing:
            self.malformed += 1
            log.error("create event missing %s: %s", missing, json.dumps(event)[:400])
            print(f"[{now_display()}] WARNING: create event missing {missing} - "
                  "PumpPortal payload shape may have changed, see log")
            return

        source, program_id, recognised = config.venue_for_pool(event.get("pool"))
        if not recognised:
            self.unknown_pool += 1
            log.warning(
                "unmapped PumpPortal pool %r, stored as %r", event.get("pool"), source
            )

        deployer = event.get("traderPublicKey")
        row_id = self.db.insert_token(
            signature=event["signature"],
            program_id=program_id,
            source=source,
            ingest_source=config.INGEST_PUMPPORTAL,
            resolution_status=STATUS_DECODED,
            mint=event["mint"],
            name=event.get("name"),
            symbol=event.get("symbol"),
            uri=event.get("uri"),
            deployer=deployer if isinstance(deployer, str) else None,
            # PumpPortal carries no slot or block time. Left null rather than
            # invented; phase 2 can fill them from the signature if needed.
            slot=None,
            raw_logs=None,
            # Everything the feed sent, including the bonding-curve and initial
            # buy figures that have no column yet.
            raw_event=json.dumps(event, separators=(",", ":")),
        )
        if row_id is None:
            self.duplicates += 1
            return

        self.rows_written += 1
        print(
            f"[{now_display()}] #{self.rows_written:<5} {source:<15} "
            f"mint {short(event['mint'], 5, 5):<12} "
            f"{('$' + safe(event.get('symbol'), 10)):<12} "
            f"{safe(event.get('name'), 24):<24} dep {short(deployer)}"
        )

    # -- status -------------------------------------------------------------

    def _uptime(self) -> str:
        seconds = int(time.monotonic() - self.started_at)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:d}h{minutes:02d}m{seconds:02d}s" if hours else f"{minutes:d}m{seconds:02d}s"

    async def _status_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(config.STATUS_INTERVAL_SECONDS)
            self.meter.flush()
            if self.run_id is not None:
                self.db.update_run(
                    self.run_id,
                    events_seen=self.messages,
                    tokens_written=self.rows_written,
                    reconnects=self.reconnects,
                )
            elapsed = max(time.monotonic() - self.started_at, 1e-9)
            by_source = self.db.counts_by_ingest_source()
            breakdown = " ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "none yet"
            print(
                f"[status {now_display()}] up {self._uptime()} | "
                f"msgs {self.messages} ({self.messages / elapsed * 60:.1f}/min) | "
                f"launches {self.rows_written} ({self.rows_written / elapsed * 60:.1f}/min) | "
                f"dupes {self.duplicates} | malformed {self.malformed} | "
                f"other {self.other_messages} | "
                f"db total {self.db.total_tokens()} [{breakdown}] | "
                f"reconnects {self.reconnects} | {self.meter.summary()}"
            )
