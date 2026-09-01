"""Phase 1 ingest: Helius WebSocket -> SQLite.

Uses the standard Solana `logsSubscribe` method, which is on the Helius free
tier. `transactionSubscribe` and LaserStream are paid and deliberately not used.

One WebSocket connection carries one subscription per watched program (the
`mentions` filter accepts exactly one address, so several programs means
several subscriptions on the same socket).

Failure model this is built around: the connection does not error, it goes
quiet. So there is a silence watchdog on top of the protocol-level ping, and
reconnect uses exponential backoff with jitter.
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
from .db import STATUS_DECODED, STATUS_PENDING, STATUS_SKIPPED, Database
from .decoders import decode_pumpfun_create, has_create_event, looks_like_creation
from .resolver import ResolveJob, Resolver

log = logging.getLogger("solscanner.ingest")


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
        self.resolver = Resolver(db, self.meter)
        self.programs = config.active_programs()

        self.started_at = time.monotonic()
        self.messages = 0
        self.creations = 0
        self.rows_written = 0
        self.duplicates = 0
        self.failed_txs = 0
        self.layout_warnings = 0
        self.reconnects = 0
        self.run_id: int | None = None

        # subscription id -> WatchedProgram, rebuilt on every connect
        self._subs: dict[int, config.WatchedProgram] = {}
        # our request id -> WatchedProgram, for matching subscribe replies
        self._pending_subs: dict[int, config.WatchedProgram] = {}
        self._stop = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        self.run_id = self.db.start_run(",".join(p.key for p in self.programs))
        self._banner()

        tasks = [
            asyncio.create_task(self._websocket_loop(), name="websocket"),
            asyncio.create_task(self._status_loop(), name="status"),
        ]
        if config.RESOLVE_RAYDIUM_MINTS:
            tasks.append(asyncio.create_task(self.resolver.run(), name="resolver"))

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
        print(f"  endpoint : {config.redact(config.HELIUS_WS_URL)}")
        print(f"  database : {config.DB_PATH}")
        print(f"  log file : {config.LOG_PATH}")
        print("  watching :")
        for program in self.programs:
            mode = "logs only, 0 credits" if program.self_describing else "needs getTransaction, 1 credit each"
            print(f"    - {program.label:<26} {short(program.program_id, 6, 6)}  ({mode})")
        if not config.RESOLVE_RAYDIUM_MINTS:
            print("  note     : mint resolution is OFF; non-pump.fun rows stay 'pending'")
        print(f"  existing rows in database: {self.db.total_tokens()}")
        print("-" * 78)
        print("  Ctrl+C to stop. Status line every "
              f"{config.STATUS_INTERVAL_SECONDS}s.")
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
        print("-" * 78)
        print(f"  stopped after {self._uptime()}")
        print(f"  rows written this run : {self.rows_written} "
              f"(duplicates ignored: {self.duplicates})")
        print(f"  totals in database    : {self.db.total_tokens()}")
        print(f"  credits (estimate)    : {self.meter.summary()}")
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
            # Jitter so a shared outage does not produce a synchronised retry.
            await asyncio.sleep(delay * (1 + random.random() * 0.25))
            delay = min(delay * config.BACKOFF_FACTOR, config.BACKOFF_MAX_SECONDS)

    async def _connect_and_read(self) -> None:
        async with connect(
            config.HELIUS_WS_URL,
            ping_interval=config.WS_PING_INTERVAL_SECONDS,
            ping_timeout=config.WS_PING_TIMEOUT_SECONDS,
            max_size=None,
            open_timeout=30,
        ) as ws:
            await self._subscribe_all(ws)
            print(f"[{now_display()}] connected, {len(self.programs)} subscription(s) requested")
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=config.WS_SILENCE_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError as exc:
                    # No data at all for the timeout window. On these programs
                    # that is not quiet, that is dead.
                    raise ConnectionError(
                        f"no messages for {config.WS_SILENCE_TIMEOUT_SECONDS}s"
                    ) from exc
                self._handle_raw(raw)

    async def _subscribe_all(self, ws) -> None:
        self._subs.clear()
        self._pending_subs.clear()
        for index, program in enumerate(self.programs, start=1):
            request = {
                "jsonrpc": "2.0",
                "id": index,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [program.program_id]},
                    {"commitment": "confirmed"},
                ],
            }
            self._pending_subs[index] = program
            await ws.send(json.dumps(request))

    # -- message handling ---------------------------------------------------

    def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("undecodable frame: %s", config.redact(raw[:200]))
            return

        if "method" not in message:
            self._handle_subscribe_reply(message)
            return

        if message.get("method") != "logsNotification":
            return

        self.messages += 1
        self.meter.record_ws_message()

        params = message.get("params", {})
        result = params.get("result", {})
        value = result.get("value", {})
        subscription = params.get("subscription")

        program = self._subs.get(subscription)
        if program is None:
            log.debug("notification for unknown subscription %s", subscription)
            return

        if value.get("err") is not None:
            # The transaction failed on chain. Not a launch.
            self.failed_txs += 1
            return

        logs = value.get("logs") or []
        signature = value.get("signature")
        slot = result.get("context", {}).get("slot")
        if not signature:
            return

        if program.self_describing:
            # The event in the log data is the authoritative signal, not the
            # instruction name. Matching on the instruction name produced a 30%
            # false positive rate on the first live run.
            self._record_self_describing(program, signature, slot, logs)
        elif looks_like_creation(logs, program.create_markers):
            self._record_opaque(program, signature, slot, logs)

    def _handle_subscribe_reply(self, message: dict) -> None:
        request_id = message.get("id")
        program = self._pending_subs.pop(request_id, None) if request_id is not None else None
        if "error" in message:
            detail = config.redact(json.dumps(message["error"]))
            log.error("subscribe failed: %s", detail)
            print(f"[{now_display()}] SUBSCRIBE FAILED: {detail}")
            return
        subscription = message.get("result")
        if program is not None and isinstance(subscription, int):
            self._subs[subscription] = program
            log.info("subscribed to %s as %s", program.key, subscription)

    def _record_self_describing(
        self, program: config.WatchedProgram, signature: str, slot: int | None, logs: list[str]
    ) -> None:
        """pump.fun: the launch record is in the logs or it is not a launch."""
        decoded = decode_pumpfun_create(logs)

        if decoded is None:
            if not has_create_event(logs):
                return  # an ordinary buy, sell or fee transaction
            # The event is there but its body did not parse. That is a layout
            # change, not a non-event: keep the observation and make noise.
            self.layout_warnings += 1
            log.error(
                "CreateEvent present but undecodable in %s - pump.fun layout may have changed",
                signature,
            )
            print(f"[{now_display()}] WARNING: undecodable CreateEvent in {short(signature, 6, 6)}"
                  " - check DEVLOG, the pump.fun event layout may have changed")
            self.creations += 1
            self._store_pending(program, signature, slot, logs)
            return

        self.creations += 1
        row_id = self.db.insert_token(
            signature=signature,
            program_id=program.program_id,
            source=program.source,
            resolution_status=STATUS_DECODED,
            mint=decoded.mint,
            name=decoded.name,
            symbol=decoded.symbol,
            uri=decoded.uri,
            deployer=decoded.creator,
            slot=slot,
            raw_logs=logs,
            raw_event=decoded.raw_base64,
        )
        if row_id is None:
            self.duplicates += 1
            return
        self.rows_written += 1
        self._print_token(
            program, decoded.mint, decoded.symbol, decoded.name, decoded.creator
        )

    def _record_opaque(
        self, program: config.WatchedProgram, signature: str, slot: int | None, logs: list[str]
    ) -> None:
        """Raydium: the logs say a pool was created but not which token for."""
        self.creations += 1
        self._store_pending(program, signature, slot, logs)

    def _store_pending(
        self, program: config.WatchedProgram, signature: str, slot: int | None, logs: list[str]
    ) -> None:
        # Store the observation now, look up the mint after. Never the other way
        # round: a failed lookup must not be able to lose the observation.
        status = STATUS_PENDING if config.RESOLVE_RAYDIUM_MINTS else STATUS_SKIPPED
        row_id = self.db.insert_token(
            signature=signature,
            program_id=program.program_id,
            source=program.source,
            resolution_status=status,
            slot=slot,
            raw_logs=logs,
        )
        if row_id is None:
            self.duplicates += 1
            return
        self.rows_written += 1
        self._print_token(program, None, None, None, None)
        if config.RESOLVE_RAYDIUM_MINTS:
            self.resolver.submit(
                ResolveJob(row_id=row_id, signature=signature, source=program.source)
            )

    def _print_token(
        self,
        program: config.WatchedProgram,
        mint: str | None,
        symbol: str | None,
        name: str | None,
        deployer: str | None,
    ) -> None:
        print(
            f"[{now_display()}] #{self.rows_written:<5} {program.source:<15} "
            f"mint {short(mint, 5, 5):<12} "
            f"{('$' + safe(symbol, 10)) if symbol else '$-':<12} "
            f"{safe(name, 24):<24} dep {short(deployer)}"
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
            by_source = self.db.counts_by_source()
            breakdown = " ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "none yet"
            print(
                f"[status {now_display()}] up {self._uptime()} | "
                f"msgs {self.messages} ({self.messages / elapsed:.1f}/s) | "
                f"creations {self.creations} | rows {self.rows_written} | "
                f"dupes {self.duplicates} | layout_warn {self.layout_warnings} | "
                f"db total {self.db.total_tokens()} [{breakdown}] | "
                f"resolved {self.resolver.resolved} failed {self.resolver.failed} "
                f"queued {self.resolver.queue.qsize()} | "
                f"reconnects {self.reconnects} | {self.meter.summary()}"
            )
