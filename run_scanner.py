"""Entry point for the phase 1 ingest scanner.

    python run_scanner.py

Stop it with Ctrl+C. It writes to scanner.db and logs/scanner.log.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys

from solscanner import config
from solscanner.db import Database
from solscanner.ingest import Scanner


def setup_logging() -> None:
    config.LOG_DIR.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_PATH, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)sZ %(levelname)-7s %(name)s %(message)s")
    )
    logging.Formatter.converter = __import__("time").gmtime  # log file is UTC
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.addHandler(handler)
    # httpx logs every request line at INFO, which would drown the file.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def check_credentials() -> None:
    """Ingest needs no credentials at all now that it runs off PumpPortal, which
    is free and unauthenticated. A missing Helius key is therefore a warning, not
    a blocker: it only matters once phase 2 enrichment starts making RPC calls."""
    if config.missing_credentials():
        print("Note: HELIUS_API_KEY is empty.")
        print("  Ingest does not need it - PumpPortal is free and unauthenticated.")
        print("  Phase 2 enrichment will need it.")
        print()


async def main_async() -> int:
    db = Database(config.DB_PATH)
    scanner = Scanner(db)

    loop = asyncio.get_running_loop()

    def request_stop(*_args) -> None:
        loop.call_soon_threadsafe(scanner.stop)

    # SIGBREAK is Windows only and is what Ctrl+Break and GenerateConsoleCtrlEvent
    # deliver. It matters because a scanner started in the background cannot be
    # sent a Ctrl+C, and taskkill without /F does not stop a console process at
    # all: without this, the only way to stop a backgrounded run is a hard kill,
    # which loses the run summary and up to 30s of credit counts.
    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, scanner.stop)
        except (NotImplementedError, AttributeError, ValueError):
            # Windows: the asyncio loop does not support signal handlers, so
            # fall back to the plain signal module.
            try:
                signal.signal(sig, request_stop)
            except (OSError, ValueError):
                pass

    try:
        await scanner.run()
    except KeyboardInterrupt:
        scanner.stop()
    finally:
        db.close()
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    setup_logging()
    check_credentials()

    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
