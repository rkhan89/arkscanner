"""Look at what the scanner has captured, without needing a SQLite client.

    python show_db.py            # last 20 rows plus totals
    python show_db.py 100        # last 100 rows
"""

from __future__ import annotations

import sys
from datetime import datetime

from solscanner import config
from solscanner.db import Database
from solscanner.ingest import safe, short, to_display_tz


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    if not config.DB_PATH.exists():
        print(f"No database at {config.DB_PATH}. Run the scanner first.")
        return 1

    db = Database(config.DB_PATH)
    total = db.total_tokens()
    print(f"database : {config.DB_PATH}")
    print(f"rows     : {total}")
    print(f"by source: {db.counts_by_source() or 'none'}")
    print(f"by ingest: {db.counts_by_ingest_source() or 'none'}")

    statuses = db.conn.execute(
        "SELECT resolution_status, COUNT(*) n FROM tokens GROUP BY resolution_status"
    ).fetchall()
    print(f"by status: {{{', '.join(f'{r[0]}: {r[1]}' for r in statuses)}}}")

    credits = db.conn.execute(
        "SELECT method, SUM(calls), SUM(estimated_credits) FROM credit_usage GROUP BY method"
    ).fetchall()
    print("credits (local estimate):")
    for method, calls, est in credits:
        print(f"  {method:<20} calls={calls} est_credits={est:.0f}")
    if not credits:
        print("  none recorded yet")

    print()
    print(f"last {limit} rows (times in {config.DISPLAY_TZ_NAME}):")
    header = (f"{'seen':<9} {'ingest':<12} {'source':<15} {'mint':<48} "
              f"{'symbol':<12} {'name':<20} deployer")
    print(header)
    print("-" * len(header))
    for row in reversed(db.recent(limit)):
        seen = to_display_tz(datetime.fromisoformat(row["first_seen_utc"])).strftime("%H:%M:%S")
        print(
            f"{seen:<9} {row['ingest_source']:<12} {row['source']:<15} "
            f"{(row['mint'] or '-'):<48} "
            f"{safe(row['symbol'], 11):<12} {safe(row['name'], 19):<20} "
            f"{short(row['deployer'])}"
        )

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
