"""Resolving pool creations that the logs alone cannot describe.

Raydium's logs tell us a pool was initialised but not which token it is for.
That needs one getTransaction call, which is one Helius credit. Pool creations
are rare compared to trades, so this is affordable, but it is still the only
thing in phase 1 that spends anything: it runs behind a bounded queue and a
client-side rate limiter, and it can be switched off entirely with
RESOLVE_RAYDIUM_MINTS=false.

If resolution is off or fails, the row stays in the database with a null mint
and a status of 'pending' / 'failed'. An observation is never dropped.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from . import config
from .credits import CreditMeter
from .db import STATUS_FAILED, STATUS_RESOLVED, Database

log = logging.getLogger("solscanner.resolver")


@dataclass
class ResolveJob:
    row_id: int
    signature: str
    source: str


class RateLimiter:
    """Simple token bucket. Helius free tier allows 10 req/sec; we sit under it."""

    def __init__(self, rate_per_sec: float):
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._last + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = asyncio.get_running_loop().time()
            self._last = now


class Resolver:
    def __init__(self, db: Database, meter: CreditMeter):
        self.db = db
        self.meter = meter
        self.queue: asyncio.Queue[ResolveJob] = asyncio.Queue(
            maxsize=config.RESOLVER_QUEUE_MAX
        )
        self.limiter = RateLimiter(config.RPC_MAX_REQUESTS_PER_SEC)
        self.resolved = 0
        self.failed = 0
        self.dropped = 0
        self._client: httpx.AsyncClient | None = None

    def submit(self, job: ResolveJob) -> None:
        """Queue a lookup. Drops rather than blocks the WebSocket reader if the
        queue is full: keeping the socket drained matters more."""
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning(
                "resolver queue full (%d), leaving row %d unresolved",
                config.RESOLVER_QUEUE_MAX,
                job.row_id,
            )

    async def run(self) -> None:
        self._client = httpx.AsyncClient(timeout=config.RPC_TIMEOUT_SECONDS)
        try:
            while True:
                job = await self.queue.get()
                try:
                    await self._handle(job)
                except Exception:
                    self.failed += 1
                    log.exception("resolver failed on row %d", job.row_id)
                finally:
                    self.queue.task_done()
        finally:
            await self._client.aclose()
            self._client = None

    async def _handle(self, job: ResolveJob) -> None:
        await self.limiter.acquire()
        result = await self._get_transaction(job.signature)
        if result is None:
            self.failed += 1
            self.db.update_resolution(job.row_id, status=STATUS_FAILED)
            return

        details = extract_pool_details(result)
        if details.get("mint") is None:
            self.failed += 1
            self.db.update_resolution(job.row_id, status=STATUS_FAILED)
            log.debug("no non-quote mint found in %s", job.signature)
            return

        self.resolved += 1
        self.db.update_resolution(job.row_id, status=STATUS_RESOLVED, **details)

    async def _get_transaction(self, signature: str) -> dict | None:
        assert self._client is not None
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
        response = await self._client.post(config.HELIUS_RPC_URL, json=payload)
        # Count the credit whether or not the call succeeded: Helius bills it.
        self.meter.record("getTransaction")
        if response.status_code != 200:
            log.warning(
                "getTransaction HTTP %s: %s",
                response.status_code,
                config.redact(response.text[:200]),
            )
            return None
        body = response.json()
        if "error" in body:
            log.warning("getTransaction error: %s", body["error"])
            return None
        return body.get("result")


def extract_pool_details(tx: dict) -> dict:
    """Work out which token a pool creation was for.

    Rather than decoding each Raydium program's account layout (which differs
    per program and changes between versions), read the token balances the
    transaction touched and take the mint that is not a quote asset. That works
    the same way for AMM v4, CPMM and LaunchLab.
    """
    details: dict = {
        "mint": None,
        "deployer": None,
        "block_time_utc": None,
        "slot": None,
        "quote_mint": None,
    }

    slot = tx.get("slot")
    if isinstance(slot, int):
        details["slot"] = slot

    block_time = tx.get("blockTime")
    if isinstance(block_time, int):
        details["block_time_utc"] = (
            datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat(timespec="milliseconds")
        )

    message = tx.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys") or []
    for key in account_keys:
        # jsonParsed gives dicts; older/raw encodings give plain strings.
        if isinstance(key, dict):
            if key.get("signer"):
                details["deployer"] = key.get("pubkey")
                break
        elif isinstance(key, str):
            details["deployer"] = key
            break

    meta = tx.get("meta") or {}
    mints: list[str] = []
    for balances in (meta.get("postTokenBalances") or [], meta.get("preTokenBalances") or []):
        for entry in balances:
            mint = entry.get("mint")
            if mint and mint not in mints:
                mints.append(mint)

    quotes = [m for m in mints if m in config.QUOTE_MINTS]
    bases = [m for m in mints if m not in config.QUOTE_MINTS]
    if quotes:
        details["quote_mint"] = quotes[0]
    if len(bases) == 1:
        details["mint"] = bases[0]
    elif len(bases) > 1:
        # A pool creation touching several non-quote mints is ambiguous. Record
        # the first and let phase 2 enrichment sort it out rather than guessing
        # silently.
        details["mint"] = bases[0]

    return details
