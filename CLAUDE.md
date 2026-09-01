# Project: Solana Token Scanner

## What this is

A personal Solana token scanner. Ingests new token launches, enriches them with on-chain and market data, applies a disqualification ruleset, and pushes surviving candidates to Telegram as alerts. Paper-trades and backtests every signal before any real execution.

Python 3.11+. SQLite. Local development, deployed later to a small always-on VM.

## Documentation mandate

This project is being documented publicly as a build-in-public series. Documentation is not optional and is not a post-hoc task. Treat it as part of the definition of done for every session.

### DEVLOG.md

Maintain `DEVLOG.md` in the project root. Append a new entry at the end of every working session. Never rewrite or tidy earlier entries. The log is a record, not a summary.

Each entry uses this structure:

```
## [YYYY-MM-DD] Session N: <what we set out to do>

**Goal:** one line

**What we built:** plain-English description of what now exists that didn't before.
No jargon that a non-developer wouldn't follow.

**Errors hit:**
- Verbatim error message in a code block
- What caused it
- How it was fixed
- How long it took to work out

**Decisions made:** what we chose, what we rejected, and why.

**Surprises:** anything that did not work the way the docs said it would.

**Time:** roughly how long this session ran.

**Content:** flagged moments worth cutting into video, with exact on-screen detail. See below.
```

### Error capture rules

- Record every error **verbatim**, in a fenced code block, before it gets fixed. Truncated or paraphrased errors are useless.
- Record failed approaches, not just the one that worked. The dead ends are the most valuable content in the whole log.
- If something took more than 20 minutes to solve, write a short paragraph explaining what was actually going on. Assume the reader is technical but has never touched Solana.
- Never quietly fix something and move on. If it broke, it goes in the log.

### Content flags

At the end of each session, flag anything that would make good short-form video. Use the tag `[CONTENT]` inline in the devlog entry. Qualifying moments:

- First time any new thing works (first data row, first alert, first successful swap)
- Any error that took more than 20 minutes to resolve
- Any moment where documented behaviour and actual behaviour disagreed
- Any number that is surprising in either direction
- Any point where the honest answer is "this was harder than it should have been"

For each flag, record **exactly what was on screen at that moment**: the command run, the full output, the values displayed, the dashboard state. Detail matters here because these entries are the only record. Write enough that the moment can be reconstructed accurately later without guessing.

No screenshots or screen recordings are required. The devlog is the capture mechanism.

### Plain-English translation

At the end of each session, write a two-sentence explanation of what was built, aimed at somebody with no coding background. Goes under `**What we built:**`. It becomes the voiceover.

## Engineering rules

- Load all credentials from `.env`. Never hardcode a key, never print one to console, never write one to a log file.
- `.gitignore` must cover `.env`, `*.db`, `__pycache__/`, `venv/`. Verify before the first commit.
- All timestamps stored in UTC. Display in GST (UTC+4).
- WebSocket connections must have automatic reconnect with backoff. A silent death at 3am is the primary failure mode of this whole project.
- Log API credit consumption where the provider exposes it. Helius free tier is 1M credits/month and it is easy to burn through it with a careless loop.
- No live trading execution code until the backtest and paper-trade phases are complete and have produced an expectancy number.
- When execution is eventually added: hard-code a maximum position size cap. It must not be configurable from an environment variable.

## Build phases

1. Ingest: WebSocket subscription, write new tokens to SQLite
2. Enrichment: authorities, LP status, holder concentration, liquidity, bundled-buy detection
3. Deployer history: reputation scoring per deployer wallet
4. Rules engine: disqualification ruleset plus Telegram alerts
5. Backtest harness: run rules against 60 to 90 days of historical launches
6. Paper trade logger: forward-test every live signal
7. Execution: only if 5 and 6 justify it

Do not skip ahead. Do not build execution early.

## Backtest integrity

The backtest is the point of the project. Two failure modes will silently invalidate it:

- **Lookahead bias.** Rules may only use data that existed at the decision timestamp. Holder count at T+0, not final holder count. Liquidity at T+0, not peak liquidity. Enforce this in code, not by convention.
- **Survivorship bias.** The historical dataset must include tokens that died. A dataset built from currently-indexed tokens is not a valid sample.

If either is present, say so explicitly rather than reporting a result.
