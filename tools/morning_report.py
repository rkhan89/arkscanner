"""Plain-English summary of an overnight run. No code knowledge required.

    python tools/morning_report.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solscanner import config
from solscanner.credits import FREE_TIER_MONTHLY_CREDITS


def gst(iso: str) -> str:
    return (datetime.fromisoformat(iso)
            .astimezone(timezone(timedelta(hours=config.DISPLAY_TZ_OFFSET_HOURS)))
            .strftime("%a %d %b %H:%M:%S"))


def humanise(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def main() -> int:
    if not config.DB_PATH.exists():
        print(f"No database at {config.DB_PATH}.")
        return 1
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row

    run = con.execute(
        "SELECT * FROM runs WHERE programs LIKE 'pumpportal%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        print("No PumpPortal run found.")
        return 1

    start_iso = run["started_utc"]
    end_iso = run["ended_utc"]
    end_dt = datetime.fromisoformat(end_iso) if end_iso else datetime.now(timezone.utc)
    elapsed = (end_dt - datetime.fromisoformat(start_iso)).total_seconds()

    rows = con.execute(
        "SELECT * FROM tokens WHERE first_seen_utc >= ? ORDER BY id", (start_iso,)
    ).fetchall()
    events = con.execute(
        "SELECT * FROM connection_events WHERE run_id = ? ORDER BY id", (run["id"],)
    ).fetchall()

    print("=" * 74)
    print("  OVERNIGHT SCANNER REPORT")
    print("=" * 74)
    print(f"  Started : {gst(start_iso)} {config.DISPLAY_TZ_NAME}")
    print(f"  {'Ended   : ' + gst(end_iso) if end_iso else 'Status  : STILL RUNNING'}"
          f"  {config.DISPLAY_TZ_NAME if end_iso else ''}")
    print(f"  Ran for : {humanise(elapsed)}")
    print()

    # 2. connection
    print("-" * 74)
    print("  DID THE CONNECTION EVER DROP?")
    print("-" * 74)
    drops = [e for e in events if e["event"] in ("disconnected", "watchdog_fired")]
    recoveries = [e for e in events if e["event"] == "connected" and e["downtime_seconds"]]
    watchdogs = [e for e in events if e["event"] == "watchdog_fired"]
    if not drops:
        print("  No. The connection held for the whole run with no interruptions.")
    else:
        total_down = sum(e["downtime_seconds"] or 0 for e in recoveries)
        print(f"  Yes - {len(drops)} time(s). It recovered {len(recoveries)} time(s).")
        print(f"  Total time disconnected: {humanise(total_down)} "
              f"({100*total_down/max(elapsed,1):.2f}% of the run)")
        if len(recoveries) < len(drops):
            print(f"  WARNING: {len(drops) - len(recoveries)} drop(s) with no recorded recovery.")
        print(f"  Silence watchdog fired: {len(watchdogs)} time(s)")
        print()
        print("  Timeline:")
        for e in events:
            if e["event"] == "connected" and e["downtime_seconds"]:
                print(f"    {gst(e['ts_utc'])}  back online after "
                      f"{e['downtime_seconds']:.1f}s")
            elif e["event"] in ("disconnected", "watchdog_fired"):
                print(f"    {gst(e['ts_utc'])}  {e['event']}: {(e['cause'] or '')[:70]}")
    print()

    # 3. launches
    print("-" * 74)
    print("  HOW MANY LAUNCHES?")
    print("-" * 74)
    print(f"  Total captured overnight: {len(rows)}")
    if elapsed > 0:
        print(f"  Average rate            : {len(rows)/elapsed*60:.1f} per minute")
    venues = Counter(r["source"] for r in rows)
    print()
    print("  By launchpad:")
    for venue, n in venues.most_common():
        pct = 100.0 * n / max(len(rows), 1)
        label = venue
        if venue.startswith("pumpportal:"):
            label = f"{venue}  (UNRECOGNISED - needs checking)"
        print(f"    {label:<44} {n:>6}  ({pct:5.1f}%)")
    print()
    hourly = Counter()
    for r in rows:
        t = datetime.fromisoformat(r["first_seen_utc"]).astimezone(
            timezone(timedelta(hours=config.DISPLAY_TZ_OFFSET_HOURS)))
        hourly[t.strftime("%H:00")] += 1
    if hourly:
        print("  By hour (GST):")
        peak = max(hourly.values())
        for hour in sorted(hourly):
            bar = "#" * int(28 * hourly[hour] / peak)
            print(f"    {hour}  {hourly[hour]:>5}  {bar}")
    print()

    # 4. payload shapes
    print("-" * 74)
    print("  ANY NEW PAYLOAD SHAPES?")
    print("-" * 74)
    shapes = con.execute("SELECT * FROM payload_shapes ORDER BY first_seen_utc").fetchall()
    new_tonight = [s for s in shapes if s["first_seen_utc"] >= start_iso]
    if not new_tonight:
        print("  No new shapes. Every payload matched a field layout already seen.")
    else:
        print(f"  Yes - {len(new_tonight)} new field layout(s) appeared:")
        for s in new_tonight:
            fields = s["field_signature"].split(",")
            print(f"\n    pool '{s['pool']}' first seen {gst(s['first_seen_utc'])}, "
                  f"{s['seen_count']} event(s)")
            print(f"      fields: {fields}")
            print(f"      example: https://solscan.io/tx/{s['example_signature']}")
    print()
    print(f"  All shapes on record ({len(shapes)}):")
    for s in shapes:
        print(f"    {s['pool']:<12} {s['seen_count']:>6} events  "
              f"{len(s['field_signature'].split(','))} fields")
    print()

    # data quality
    print("-" * 74)
    print("  DATA QUALITY")
    print("-" * 74)
    nulls = {c: sum(1 for r in rows if not r[c]) for c in ("mint", "signature", "deployer", "name", "symbol")}
    print(f"  Rows missing a mint      : {nulls['mint']}")
    print(f"  Rows missing a signature : {nulls['signature']}")
    print(f"  Rows missing a deployer  : {nulls['deployer']}")
    print(f"  Tokens with no name      : {nulls['name']}")
    print(f"  Distinct mints           : {len({r['mint'] for r in rows})} "
          f"(of {len(rows)} rows)")
    print(f"  Distinct deployer wallets: {len({r['deployer'] for r in rows})}")
    print()

    # 5. credits
    print("-" * 74)
    print("  HELIUS CREDITS")
    print("-" * 74)
    spend = con.execute(
        "SELECT method, SUM(calls), SUM(estimated_credits) FROM credit_usage"
        " WHERE ts_utc >= ? GROUP BY method", (start_iso,)
    ).fetchall()
    total = sum(r[2] for r in spend)
    if not spend or total == 0:
        print("  Zero. Ingest runs entirely on PumpPortal, which is free.")
    else:
        for method, calls, credits in spend:
            print(f"    {method:<20} {calls:>8} calls   {credits:>10,.0f} credits")
    print()
    print(f"  Spent by ingest this run : {total:,.0f}")
    print(f"  Free tier                : {FREE_TIER_MONTHLY_CREDITS:,} credits/month")
    print()
    print("  NOTE: this counts only what this scanner spent. Helius does not")
    print("  publish a balance endpoint, so the dashboard at")
    print("  https://dashboard.helius.dev is the authority on remaining balance.")
    print("=" * 74)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
