"""SQLite storage for phase 1.

Phase 1 captures everything and filters nothing. Rows are written the moment a
creation event is seen, even when the mint is not yet known, so that a failed
lookup later can never cost us the observation.

All timestamps are stored as ISO-8601 UTC strings. Conversion to GST happens at
display time only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mint              TEXT,
    first_seen_utc    TEXT NOT NULL,
    block_time_utc    TEXT,
    slot              INTEGER,
    name              TEXT,
    symbol            TEXT,
    uri               TEXT,
    deployer          TEXT,
    source            TEXT NOT NULL,
    program_id        TEXT NOT NULL,
    signature         TEXT NOT NULL,
    quote_mint        TEXT,
    resolution_status TEXT NOT NULL,
    raw_logs          TEXT,
    raw_event         TEXT,
    UNIQUE (signature, program_id)
);

CREATE INDEX IF NOT EXISTS idx_tokens_first_seen ON tokens (first_seen_utc);
CREATE INDEX IF NOT EXISTS idx_tokens_mint       ON tokens (mint);
CREATE INDEX IF NOT EXISTS idx_tokens_source     ON tokens (source);
CREATE INDEX IF NOT EXISTS idx_tokens_status     ON tokens (resolution_status);

CREATE TABLE IF NOT EXISTS credit_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc            TEXT NOT NULL,
    method            TEXT NOT NULL,
    calls             INTEGER NOT NULL,
    estimated_credits REAL NOT NULL,
    note              TEXT
);

CREATE INDEX IF NOT EXISTS idx_credit_ts ON credit_usage (ts_utc);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_utc    TEXT NOT NULL,
    ended_utc      TEXT,
    programs       TEXT NOT NULL,
    events_seen    INTEGER NOT NULL DEFAULT 0,
    tokens_written INTEGER NOT NULL DEFAULT 0,
    reconnects     INTEGER NOT NULL DEFAULT 0,
    notes          TEXT
);
"""

# Row states. STATUS_DECODED means everything came out of the log stream itself
# and cost nothing. STATUS_PENDING means an RPC lookup is needed for the mint.
STATUS_DECODED = "decoded"
STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL lets you open the file in another tool while the scanner runs.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive-only schema changes. Existing rows are never rewritten."""
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(tokens)")}
        if "ingest_source" not in columns:
            # Where the row came from, as opposed to `source`, which is the venue
            # the token launched on. Rows captured before PumpPortal came from the
            # Helius log stream; the DEFAULT makes them read back as such without
            # an UPDATE touching a single existing row.
            self.conn.execute(
                "ALTER TABLE tokens ADD COLUMN ingest_source TEXT NOT NULL"
                " DEFAULT 'helius_logs'"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tokens_ingest ON tokens (ingest_source)"
            )

    def close(self) -> None:
        self.conn.close()

    # -- tokens -------------------------------------------------------------

    def insert_token(
        self,
        *,
        signature: str,
        program_id: str,
        source: str,
        resolution_status: str,
        ingest_source: str,
        mint: str | None = None,
        name: str | None = None,
        symbol: str | None = None,
        uri: str | None = None,
        deployer: str | None = None,
        slot: int | None = None,
        raw_logs: Iterable[str] | None = None,
        raw_event: str | None = None,
    ) -> int | None:
        """Insert one observation.

        Returns the new row id, or None if this was a duplicate: the log stream
        can repeat a signature across a reconnect.
        """
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO tokens (
                mint, first_seen_utc, slot, name, symbol, uri, deployer,
                source, program_id, signature, resolution_status,
                raw_logs, raw_event, ingest_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mint,
                utcnow_iso(),
                slot,
                name,
                symbol,
                uri,
                deployer,
                source,
                program_id,
                signature,
                resolution_status,
                json.dumps(list(raw_logs)) if raw_logs is not None else None,
                raw_event,
                ingest_source,
            ),
        )
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def update_resolution(
        self,
        row_id: int,
        *,
        status: str,
        mint: str | None = None,
        deployer: str | None = None,
        block_time_utc: str | None = None,
        slot: int | None = None,
        quote_mint: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE tokens SET
                resolution_status = ?,
                mint           = COALESCE(?, mint),
                deployer       = COALESCE(?, deployer),
                block_time_utc = COALESCE(?, block_time_utc),
                slot           = COALESCE(?, slot),
                quote_mint     = COALESCE(?, quote_mint)
            WHERE id = ?
            """,
            (status, mint, deployer, block_time_utc, slot, quote_mint, row_id),
        )
        self.conn.commit()

    def counts_by_ingest_source(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT ingest_source, COUNT(*) AS n FROM tokens GROUP BY ingest_source"
        ).fetchall()
        return {r["ingest_source"]: r["n"] for r in rows}

    def counts_by_source(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*) AS n FROM tokens GROUP BY source"
        ).fetchall()
        return {r["source"]: r["n"] for r in rows}

    def total_tokens(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0])

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tokens ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # -- credits ------------------------------------------------------------

    def record_credits(
        self, method: str, calls: int, estimated_credits: float, note: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO credit_usage (ts_utc, method, calls, estimated_credits, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (utcnow_iso(), method, calls, estimated_credits, note),
        )
        self.conn.commit()

    def credits_since(self, since_utc: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(estimated_credits), 0) FROM credit_usage WHERE ts_utc >= ?",
            (since_utc,),
        ).fetchone()
        return float(row[0])

    # -- runs ---------------------------------------------------------------

    def start_run(self, programs: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_utc, programs) VALUES (?, ?)",
            (utcnow_iso(), programs),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_run(
        self, run_id: int, *, events_seen: int, tokens_written: int, reconnects: int
    ) -> None:
        """Keep the run row current so a hard kill still leaves useful numbers."""
        self.conn.execute(
            "UPDATE runs SET events_seen = ?, tokens_written = ?, reconnects = ? WHERE id = ?",
            (events_seen, tokens_written, reconnects, run_id),
        )
        self.conn.commit()

    def finish_run(
        self,
        run_id: int,
        *,
        events_seen: int,
        tokens_written: int,
        reconnects: int,
        notes: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET ended_utc = ?, events_seen = ?, tokens_written = ?,"
            " reconnects = ?, notes = ? WHERE id = ?",
            (utcnow_iso(), events_seen, tokens_written, reconnects, notes, run_id),
        )
        self.conn.commit()
