"""Configuration and credential loading.

Every secret comes from .env via python-dotenv. Nothing is hardcoded, and no
key is ever printed or written to a log file: any URL that carries the API key
goes through redact() first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _get_bool(name: str, default: bool) -> bool:
    return _get(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


HELIUS_API_KEY = _get("HELIUS_API_KEY", "")

HELIUS_WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

DB_PATH = PROJECT_ROOT / _get("DB_PATH", "scanner.db")
LOG_DIR = PROJECT_ROOT / "logs"
LOG_PATH = LOG_DIR / "scanner.log"
LOG_LEVEL = _get("LOG_LEVEL", "INFO").upper()

# Display timezone. Storage is always UTC (see CLAUDE.md).
DISPLAY_TZ_OFFSET_HOURS = 4  # GST
DISPLAY_TZ_NAME = "GST"

WATCH_PROGRAMS = [
    p.strip() for p in _get("WATCH_PROGRAMS", "pumpfun,raydium_amm_v4,raydium_cpmm").split(",") if p.strip()
]

RESOLVE_RAYDIUM_MINTS = _get_bool("RESOLVE_RAYDIUM_MINTS", True)
RPC_MAX_REQUESTS_PER_SEC = _get_float("RPC_MAX_REQUESTS_PER_SEC", 5.0)
RESOLVER_QUEUE_MAX = _get_int("RESOLVER_QUEUE_MAX", 500)
RPC_TIMEOUT_SECONDS = _get_float("RPC_TIMEOUT_SECONDS", 20.0)

STATUS_INTERVAL_SECONDS = _get_int("STATUS_INTERVAL_SECONDS", 30)
WS_SILENCE_TIMEOUT_SECONDS = _get_int("WS_SILENCE_TIMEOUT_SECONDS", 90)
WS_PING_INTERVAL_SECONDS = _get_int("WS_PING_INTERVAL_SECONDS", 20)
WS_PING_TIMEOUT_SECONDS = _get_int("WS_PING_TIMEOUT_SECONDS", 20)

BACKOFF_INITIAL_SECONDS = _get_float("BACKOFF_INITIAL_SECONDS", 1.0)
BACKOFF_MAX_SECONDS = _get_float("BACKOFF_MAX_SECONDS", 60.0)
BACKOFF_FACTOR = _get_float("BACKOFF_FACTOR", 2.0)
# A connection that survives this long is considered healthy: reset the backoff.
BACKOFF_RESET_AFTER_SECONDS = _get_float("BACKOFF_RESET_AFTER_SECONDS", 60.0)

# Estimated Helius credits per WebSocket notification.
# UNVERIFIED. Helius does not itemise WebSocket usage in the response, so this
# is a local estimate only. Check the dashboard after an hour and correct it.
WS_MESSAGE_CREDIT_COST = _get_float("WS_MESSAGE_CREDIT_COST", 0.0)
# getTransaction is a standard RPC call: 1 credit each on the free tier.
RPC_CALL_CREDIT_COST = _get_float("RPC_CALL_CREDIT_COST", 1.0)


@dataclass(frozen=True)
class WatchedProgram:
    """A program we hold a logsSubscribe subscription against."""

    key: str
    label: str
    program_id: str
    source: str
    # Substrings that mark a pool/token creation in the log lines.
    create_markers: tuple[str, ...]
    # True when the mint can be decoded straight out of the logs (no RPC call).
    self_describing: bool


PROGRAM_CATALOGUE: dict[str, WatchedProgram] = {
    "pumpfun": WatchedProgram(
        key="pumpfun",
        label="pump.fun bonding curve",
        program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source="pumpfun",
        # Diagnostic only. Detection uses the CreateEvent in the log data, which
        # is authoritative; the instruction name has already moved from Create to
        # CreateV2 once and will move again.
        create_markers=("Instruction: CreateV2", "Instruction: Create"),
        self_describing=True,
    ),
    "pumpswap": WatchedProgram(
        key="pumpswap",
        label="pump.fun AMM (PumpSwap)",
        program_id="pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        source="pumpswap",
        create_markers=("Instruction: CreatePool", "Instruction: Initialize"),
        self_describing=False,
    ),
    "raydium_amm_v4": WatchedProgram(
        key="raydium_amm_v4",
        label="Raydium AMM v4",
        program_id="675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        source="raydium_amm_v4",
        create_markers=("initialize2: InitializeInstruction2",),
        self_describing=False,
    ),
    "raydium_cpmm": WatchedProgram(
        key="raydium_cpmm",
        label="Raydium CPMM",
        program_id="CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
        source="raydium_cpmm",
        create_markers=("Instruction: Initialize",),
        self_describing=False,
    ),
    "raydium_launchlab": WatchedProgram(
        key="raydium_launchlab",
        label="Raydium LaunchLab",
        program_id="LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
        source="raydium_launchlab",
        create_markers=("Instruction: Initialize",),
        self_describing=False,
    ),
}

# Quote assets. When a pool is resolved, the mint that is NOT one of these is
# the newly launched token.
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112": "WSOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}


def active_programs() -> list[WatchedProgram]:
    """The programs named in WATCH_PROGRAMS, in order."""
    out: list[WatchedProgram] = []
    for key in WATCH_PROGRAMS:
        program = PROGRAM_CATALOGUE.get(key)
        if program is None:
            raise ValueError(
                f"Unknown program '{key}' in WATCH_PROGRAMS. "
                f"Valid keys: {', '.join(sorted(PROGRAM_CATALOGUE))}"
            )
        out.append(program)
    return out


def redact(text: str) -> str:
    """Strip the API key out of anything on its way to a console or log file."""
    if HELIUS_API_KEY and HELIUS_API_KEY in text:
        text = text.replace(HELIUS_API_KEY, "<HELIUS_API_KEY>")
    return text


def missing_credentials() -> list[str]:
    """Credentials phase 1 needs but does not have."""
    return [] if HELIUS_API_KEY else ["HELIUS_API_KEY"]
