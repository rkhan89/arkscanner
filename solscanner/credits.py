"""Helius credit accounting.

Helius does not return a per-response credit cost, so this is a local estimate,
not a reading. It exists so that a careless loop shows up in the console within
minutes instead of on the dashboard three weeks later.

Free tier is 1,000,000 credits/month and 10 requests/second.

Measured, not assumed: the Helius dashboard reported 35,989 credits in under an
hour of logsSubscribe running - 99.3% of it WebSocket delivery, 0.7% RPC. At the
observed ~1,100 msg/sec that is roughly 0.01 credits per WebSocket message, and
it exhausts the 1M/month free tier in under a day. That measurement is why ingest
moved to PumpPortal.

What remains:
  - A standard JSON-RPC call such as getTransaction is billed at 1 credit
    (config.RPC_CALL_CREDIT_COST). RPC was 0.7% of the bill - 236 credits for a
    whole session - so phase 2 enrichment on Helius RPC is affordable.
  - Ingest no longer holds a Helius WebSocket, so this meter should now report
    zero for a phase 1 run. If it does not, something is calling Helius that
    should not be.
  - PumpPortal is free and unmetered. Its messages are counted for rate
    monitoring but carry no credit cost.
"""

from __future__ import annotations

from collections import defaultdict

from . import config
from .db import Database

FREE_TIER_MONTHLY_CREDITS = 1_000_000


class CreditMeter:
    """Counts calls in memory, flushes totals to SQLite periodically."""

    def __init__(self, db: Database):
        self.db = db
        self._pending_calls: dict[str, int] = defaultdict(int)
        self._pending_credits: dict[str, float] = defaultdict(float)
        self.session_calls: dict[str, int] = defaultdict(int)
        self.session_credits = 0.0

    def record(self, method: str, calls: int = 1, credit_cost: float | None = None) -> None:
        cost = config.RPC_CALL_CREDIT_COST if credit_cost is None else credit_cost
        credits = calls * cost
        self._pending_calls[method] += calls
        self._pending_credits[method] += credits
        self.session_calls[method] += calls
        self.session_credits += credits

    def record_ws_message(self, count: int = 1) -> None:
        self.record(
            "logsNotification", calls=count, credit_cost=config.WS_MESSAGE_CREDIT_COST
        )

    def flush(self) -> None:
        """Write the accumulated counts to the credit_usage table."""
        for method, calls in list(self._pending_calls.items()):
            if calls == 0:
                continue
            self.db.record_credits(
                method,
                calls,
                self._pending_credits[method],
                note="local estimate" if method == "logsNotification" else None,
            )
        self._pending_calls.clear()
        self._pending_credits.clear()

    def summary(self) -> str:
        rpc_calls = sum(n for m, n in self.session_calls.items() if m != "logsNotification")
        ws_messages = self.session_calls.get("logsNotification", 0)
        pct = 100.0 * self.session_credits / FREE_TIER_MONTHLY_CREDITS
        return (
            f"rpc_calls={rpc_calls} ws_msgs={ws_messages} "
            f"est_credits={self.session_credits:.0f} ({pct:.3f}% of monthly free tier)"
        )
