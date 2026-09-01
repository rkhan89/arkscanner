"""Run PumpPortal and Helius logsSubscribe against the same wall-clock window and
diff the mint sets.

The question this answers: does PumpPortal drop launches? If it does, every
dataset built on it inherits a survivorship problem, and CLAUDE.md is explicit
that survivorship bias invalidates the backtest.

Both feeds start together, run for a fixed window, and stop together. Everything
is written to its own database (feed_diff.db) with a `feed` marker, so the
production tokens table is untouched by the experiment.

Usage:
    python tools/feed_diff.py [minutes]

Helius side uses the corrected session-1 detection: the pump.fun CreateEvent
discriminator, and word-boundary marker matching for LaunchLab. It is billed by
volume, so bytes received are counted and the run aborts the Helius side if the
credit budget is hit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from websockets.asyncio.client import connect

from solscanner import config
from solscanner.decoders import decode_pumpfun_create, has_create_event, looks_like_creation

log = logging.getLogger("feed_diff")

DB_PATH = config.PROJECT_ROOT / "feed_diff.db"

# Helius programs to watch. PumpPortal has been observed covering pump.fun and
# letsbonk (Raydium LaunchLab), so those are the two the comparison needs.
HELIUS_PROGRAMS = ["pumpfun", "raydium_launchlab"]

# Hard stop on Helius spend. The run aborts the Helius side rather than
# overshooting; PumpPortal keeps going and the diff uses the overlap window.
CREDIT_BUDGET = 36_000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    feed       TEXT NOT NULL,
    seen_utc   TEXT NOT NULL,
    mint       TEXT,
    signature  TEXT NOT NULL,
    venue      TEXT,
    raw        TEXT,
    UNIQUE (feed, signature)
);
CREATE TABLE IF NOT EXISTS diff_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_utc       TEXT NOT NULL,
    ended_utc         TEXT,
    helius_stopped_utc TEXT,
    helius_bytes      INTEGER NOT NULL DEFAULT 0,
    helius_messages   INTEGER NOT NULL DEFAULT 0,
    helius_credits    REAL NOT NULL DEFAULT 0,
    notes             TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DiffRecorder:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.counts = {"pumpportal": 0, "helius": 0}

    def add(self, feed: str, signature: str, mint: str | None, venue: str, raw: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO observations (feed, seen_utc, mint, signature, venue, raw)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (feed, utcnow(), mint, signature, venue, raw),
        )
        self.conn.commit()
        if cur.rowcount:
            self.counts[feed] += 1
            return True
        return False

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO diff_runs (started_utc) VALUES (?)", (utcnow(),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE diff_runs SET ended_utc = ?, {sets} WHERE id = ?",
            (utcnow(), *fields.values(), run_id),
        )
        self.conn.commit()


class PumpPortalFeed:
    name = "pumpportal"

    def __init__(self, rec: DiffRecorder, stop: asyncio.Event):
        self.rec = rec
        self.stop = stop
        self.messages = 0
        self.reconnects = 0

    async def run(self) -> None:
        while not self.stop.is_set():
            try:
                async with connect(config.PUMPPORTAL_WS_URL, ping_interval=20,
                                   ping_timeout=20, max_size=None, open_timeout=30) as ws:
                    await ws.send(json.dumps({"method": config.PUMPPORTAL_SUBSCRIBE_METHOD}))
                    print(f"[{utcnow()}] pumpportal: connected")
                    while not self.stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop.is_set():
                    return
                self.reconnects += 1
                print(f"[{utcnow()}] pumpportal: dropped ({type(exc).__name__}), retrying")
                await asyncio.sleep(2)

    def _handle(self, raw) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        self.messages += 1
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict) or msg.get("txType") != "create":
            return
        sig, mint = msg.get("signature"), msg.get("mint")
        if not sig or not mint:
            return
        venue, _, _ = config.venue_for_pool(msg.get("pool"))
        self.rec.add(self.name, sig, mint, venue, raw)


class HeliusFeed:
    name = "helius"

    def __init__(self, rec: DiffRecorder, stop: asyncio.Event):
        self.rec = rec
        self.stop = stop
        self.messages = 0
        self.bytes = 0
        self.reconnects = 0
        self.stopped_utc: str | None = None
        self.budget_hit = False
        self.programs = [config.PROGRAM_CATALOGUE[k] for k in HELIUS_PROGRAMS]
        self._subs: dict[int, config.WatchedProgram] = {}
        self._pending: dict[int, config.WatchedProgram] = {}

    @property
    def credits(self) -> float:
        return self.bytes / config.WS_BILLED_CHUNK_BYTES * config.WS_CREDITS_PER_CHUNK

    async def run(self) -> None:
        while not self.stop.is_set() and not self.budget_hit:
            try:
                async with connect(config.HELIUS_WS_URL, ping_interval=20, ping_timeout=20,
                                   max_size=None, open_timeout=30) as ws:
                    self._subs.clear()
                    self._pending.clear()
                    for i, prog in enumerate(self.programs, start=1):
                        self._pending[i] = prog
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                            "params": [{"mentions": [prog.program_id]},
                                       {"commitment": "confirmed"}],
                        }))
                    print(f"[{utcnow()}] helius: connected, {len(self.programs)} subs")
                    while not self.stop.is_set() and not self.budget_hit:
                        raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        self._handle(raw)
                        if self.credits >= CREDIT_BUDGET:
                            self.budget_hit = True
                            print(f"[{utcnow()}] helius: CREDIT BUDGET {CREDIT_BUDGET:.0f} "
                                  f"REACHED, stopping helius side")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop.is_set() or self.budget_hit:
                    break
                self.reconnects += 1
                print(f"[{utcnow()}] helius: dropped ({type(exc).__name__}), retrying")
                await asyncio.sleep(2)
        self.stopped_utc = utcnow()

    def _handle(self, raw) -> None:
        if isinstance(raw, bytes):
            self.bytes += len(raw)
            raw = raw.decode("utf-8", "replace")
        else:
            self.bytes += len(raw.encode("utf-8"))
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if "method" not in msg:
            rid = msg.get("id")
            prog = self._pending.pop(rid, None) if rid is not None else None
            if prog is not None and isinstance(msg.get("result"), int):
                self._subs[msg["result"]] = prog
            elif "error" in msg:
                print(f"[{utcnow()}] helius: SUBSCRIBE FAILED {msg['error']}")
            return
        if msg.get("method") != "logsNotification":
            return
        self.messages += 1
        params = msg.get("params", {})
        result = params.get("result", {})
        value = result.get("value", {})
        prog = self._subs.get(params.get("subscription"))
        if prog is None or value.get("err") is not None:
            return
        logs = value.get("logs") or []
        sig = value.get("signature")
        if not sig:
            return

        if prog.self_describing:
            decoded = decode_pumpfun_create(logs)
            if decoded is None:
                if has_create_event(logs):
                    self.rec.add(self.name, sig, None, prog.source, json.dumps(logs))
                return
            self.rec.add(self.name, sig, decoded.mint, prog.source, json.dumps(logs))
        elif looks_like_creation(logs, prog.create_markers):
            # LaunchLab logs do not carry the mint; record the signature and
            # resolve it afterwards so nothing is dropped.
            self.rec.add(self.name, sig, None, prog.source, json.dumps(logs))


async def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    logging.basicConfig(level=logging.WARNING)

    if not config.HELIUS_API_KEY:
        print("HELIUS_API_KEY is empty; the Helius half of this comparison cannot run.")
        return 1

    rec = DiffRecorder(DB_PATH)
    run_id = rec.start_run()
    stop = asyncio.Event()
    pp = PumpPortalFeed(rec, stop)
    hl = HeliusFeed(rec, stop)

    print("=" * 78)
    print(f"  CONCURRENT FEED DIFF - {minutes:.0f} minutes")
    print(f"  database : {DB_PATH}")
    print(f"  helius   : {', '.join(HELIUS_PROGRAMS)} (budget {CREDIT_BUDGET:,.0f} credits)")
    print(f"  started  : {utcnow()}")
    print("=" * 78)

    started = time.monotonic()
    tasks = [asyncio.create_task(pp.run()), asyncio.create_task(hl.run())]

    async def ticker():
        while not stop.is_set():
            await asyncio.sleep(60)
            el = time.monotonic() - started
            print(f"[{utcnow()}] t+{el/60:5.1f}m  pumpportal={rec.counts['pumpportal']:<5} "
                  f"helius={rec.counts['helius']:<5} "
                  f"helius_msgs={hl.messages:<8} helius_mb={hl.bytes/1e6:6.1f} "
                  f"est_credits={hl.credits:,.0f}")

    tick = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(minutes * 60)
    finally:
        stop.set()
        for t in (*tasks, tick):
            t.cancel()
        await asyncio.gather(*tasks, tick, return_exceptions=True)

    rec.finish_run(
        run_id,
        helius_stopped_utc=hl.stopped_utc or utcnow(),
        helius_bytes=hl.bytes,
        helius_messages=hl.messages,
        helius_credits=hl.credits,
        notes=f"budget_hit={hl.budget_hit} pp_reconnects={pp.reconnects} hl_reconnects={hl.reconnects}",
    )

    print()
    print("=" * 78)
    print("  RUN COMPLETE")
    print("=" * 78)
    print(f"  pumpportal observations : {rec.counts['pumpportal']}")
    print(f"  helius observations     : {rec.counts['helius']}")
    print(f"  helius messages         : {hl.messages:,}")
    print(f"  helius bytes            : {hl.bytes:,} ({hl.bytes/1e6:.1f} MB)")
    print(f"  helius est credits      : {hl.credits:,.0f}  (2 per 0.1MB, published rate)")
    print(f"  budget hit              : {hl.budget_hit}")
    print(f"  reconnects              : pumpportal={pp.reconnects} helius={hl.reconnects}")
    rec.conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
