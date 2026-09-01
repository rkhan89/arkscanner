"""Diff the two feeds captured by feed_diff.py and verify the discrepancies.

The dangerous answer here is a false one. A launch that Helius caught and
PumpPortal did not is only evidence of a gap if it was a real token creation, and
session 1 established that Helius log-marker matching produces false positives.
So every discrepancy is checked against the chain: a transaction counts as a
genuine launch only if it contains an initializeMint / initializeMint2
instruction, which is what actually brings a token into existence.

Helius LaunchLab rows carry no mint in their logs, so those are resolved by the
same RPC call that verifies them.

Usage:
    python tools/diff_report.py [--trim SECONDS]
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from solscanner import config

DB_PATH = config.PROJECT_ROOT / "feed_diff.db"
MINT_INIT_TYPES = {"initializeMint", "initializeMint2"}


def parsed_instructions(tx: dict):
    msg = tx.get("transaction", {}).get("message", {})
    for ix in msg.get("instructions", []) or []:
        yield ix
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in inner.get("instructions", []) or []:
            yield ix


def analyse_tx(tx: dict | None) -> dict:
    """What did this transaction actually do?"""
    if tx is None:
        return {"exists": False, "created_mint": None, "is_creation": False, "programs": []}
    created = []
    programs = Counter()
    for ix in parsed_instructions(tx):
        pid = ix.get("programId")
        if pid:
            programs[pid] += 1
        parsed = ix.get("parsed")
        if isinstance(parsed, dict) and parsed.get("type") in MINT_INIT_TYPES:
            mint = (parsed.get("info") or {}).get("mint")
            if mint:
                created.append(mint)
    return {
        "exists": True,
        "created_mint": created[0] if created else None,
        "is_creation": bool(created),
        "programs": [p for p, _ in programs.most_common()],
        "err": (tx.get("meta") or {}).get("err"),
    }


async def fetch_many(signatures: list[str], concurrency: int = 4) -> dict[str, dict | None]:
    """getTransaction for each signature, rate limited under the 10 req/sec cap."""
    out: dict[str, dict | None] = {}
    sem = asyncio.Semaphore(concurrency)
    calls = 0

    async with httpx.AsyncClient(timeout=30) as client:
        async def one(sig: str):
            nonlocal calls
            async with sem:
                await asyncio.sleep(0.2)  # ~5 req/sec across the semaphore
                for attempt in range(3):
                    try:
                        r = await client.post(config.HELIUS_RPC_URL, json={
                            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                            "params": [sig, {"encoding": "jsonParsed",
                                             "commitment": "confirmed",
                                             "maxSupportedTransactionVersion": 0}],
                        })
                        calls += 1
                        if r.status_code == 429:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        body = r.json()
                        out[sig] = body.get("result")
                        return
                    except Exception:
                        await asyncio.sleep(1.0 * (attempt + 1))
                out[sig] = None

        await asyncio.gather(*(one(s) for s in signatures))
    print(f"  ({calls} getTransaction calls = ~{calls} credits)")
    return out


async def main() -> int:
    trim = 0.0
    if "--trim" in sys.argv:
        trim = float(sys.argv[sys.argv.index("--trim") + 1])

    if not DB_PATH.exists():
        print(f"No {DB_PATH}. Run tools/feed_diff.py first.")
        return 1

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    run = con.execute("SELECT * FROM diff_runs ORDER BY id DESC LIMIT 1").fetchone()
    rows = con.execute("SELECT * FROM observations ORDER BY id").fetchall()

    start = datetime.fromisoformat(run["started_utc"])
    end = datetime.fromisoformat(run["ended_utc"]) if run["ended_utc"] else None
    lo = start + timedelta(seconds=trim)
    hi = (end - timedelta(seconds=trim)) if end else None

    def in_window(r) -> bool:
        t = datetime.fromisoformat(r["seen_utc"])
        return t >= lo and (hi is None or t <= hi)

    windowed = [r for r in rows if in_window(r)]

    print("=" * 78)
    print("  CONCURRENT FEED DIFF - REPORT")
    print("=" * 78)
    print(f"  window        : {run['started_utc']}  ->  {run['ended_utc']}")
    if trim:
        print(f"  trimmed by    : {trim:.0f}s at each end (start/stop skew)")
    print(f"  observations  : {len(rows)} total, {len(windowed)} inside the window")
    print(f"  helius volume : {run['helius_messages']:,} messages, "
          f"{run['helius_bytes']/1e6:.1f} MB")
    print(f"  helius cost   : ~{run['helius_credits']:,.0f} credits "
          f"(2 per 0.1MB, published rate)")
    print(f"  notes         : {run['notes']}")

    pp = {r["signature"]: r for r in windowed if r["feed"] == "pumpportal"}
    hl = {r["signature"]: r for r in windowed if r["feed"] == "helius"}
    print()
    print(f"  pumpportal observations : {len(pp)}")
    print(f"  helius observations     : {len(hl)}")

    # Helius LaunchLab rows have no mint in their logs. Resolve them, and at the
    # same time verify every signature only one feed saw.
    unresolved = [s for s, r in hl.items() if not r["mint"]]
    hl_only_sigs = [s for s in hl if s not in pp]
    pp_only_sigs = [s for s in pp if s not in hl]
    to_fetch = sorted(set(unresolved) | set(hl_only_sigs) | set(pp_only_sigs))

    print()
    print(f"  signatures needing chain verification: {len(to_fetch)}")
    print(f"    helius rows with no mint in logs : {len(unresolved)}")
    print(f"    seen by helius only              : {len(hl_only_sigs)}")
    print(f"    seen by pumpportal only          : {len(pp_only_sigs)}")
    if to_fetch and not config.HELIUS_API_KEY:
        print("  HELIUS_API_KEY empty; cannot verify.")
        return 1

    facts: dict[str, dict] = {}
    if to_fetch:
        print()
        print("  verifying against chain...")
        txs = await fetch_many(to_fetch)
        facts = {sig: analyse_tx(tx) for sig, tx in txs.items()}

    def mint_of(sig: str, row) -> str | None:
        if row["mint"]:
            return row["mint"]
        f = facts.get(sig) or {}
        return f.get("created_mint")

    pp_mints = {m for s, r in pp.items() if (m := mint_of(s, r))}
    hl_mints = {m for s, r in hl.items() if (m := mint_of(s, r))}

    # Only count a Helius observation as a real launch if the chain agrees.
    genuine_hl_only = []
    false_hl_only = []
    for sig in hl_only_sigs:
        f = facts.get(sig) or {}
        m = mint_of(sig, hl[sig])
        if f.get("is_creation") or (m and m in pp_mints):
            genuine_hl_only.append((sig, m, f))
        else:
            false_hl_only.append((sig, m, f))

    # Some of those "helius only" signatures may be the same token PumpPortal saw
    # under a different signature; compare on mints, not just signatures.
    missed_mints = sorted({m for _, m, _ in genuine_hl_only if m and m not in pp_mints})

    print()
    print("=" * 78)
    print("  RESULT")
    print("=" * 78)
    print(f"  distinct mints, pumpportal : {len(pp_mints)}")
    print(f"  distinct mints, helius     : {len(hl_mints)}")
    print(f"  captured by both           : {len(pp_mints & hl_mints)}")
    print(f"  pumpportal only            : {len(pp_mints - hl_mints)}")
    print(f"  helius only                : {len(hl_mints - pp_mints)}")
    print()
    print(f"  helius-only signatures examined : {len(hl_only_sigs)}")
    print(f"    verified genuine creations    : {len(genuine_hl_only)}")
    print(f"    NOT creations (false positive): {len(false_hl_only)}")
    print(f"  => launches PumpPortal genuinely missed: {len(missed_mints)}")

    if missed_mints:
        print()
        print("  MISSED LAUNCHES:")
        for m in missed_mints[:40]:
            sig = next(s for s, mm, _ in genuine_hl_only if mm == m)
            progs = (facts.get(sig) or {}).get("programs", [])
            known = {p.program_id: p.key for p in config.PROGRAM_CATALOGUE.values()}
            label = ", ".join(known.get(p, "") for p in progs if known.get(p)) or "unknown program"
            print(f"    {m}  {label}")
            print(f"      https://solscan.io/tx/{sig}")

    if false_hl_only:
        print()
        print("  helius-only signatures that were NOT token creations "
              "(these are why raw counts mislead):")
        for sig, m, f in false_hl_only[:10]:
            print(f"    {sig[:44]}...  exists={f.get('exists')} "
                  f"created_mint={m} err={f.get('err')}")

    pp_only_mints = sorted(pp_mints - hl_mints)
    if pp_only_mints:
        print()
        print(f"  pumpportal-only mints ({len(pp_only_mints)}), sample:")
        for m in pp_only_mints[:10]:
            print(f"    {m}")

    venues = Counter(r["venue"] for r in windowed if r["feed"] == "pumpportal")
    print()
    print(f"  pumpportal launchpad split: {dict(venues)}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
