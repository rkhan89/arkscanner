# DEVLOG

Build log for the Solana token scanner. Append only. Entries are a record of
what actually happened, including the parts that did not work.

---

## [2026-09-01] Session 1: Project setup and phase 1 ingest

**Goal:** Get a scanner running that listens to new token launches on Solana and writes every one of them into a local database, with no filtering.

**What we built:** A program that keeps an always-open connection to the Solana blockchain and watches two of the places where new coins get created — pump.fun and Raydium. Every time a new coin appears, it writes down the coin's ID, name, symbol, who created it, and the exact time, into a file on the laptop, and prints a line to the screen so you can watch it happen.

### What exists now that didn't before

```
.gitignore              secrets, database, logs and venv all excluded
.env                    four empty credential slots, each with a comment saying where to get it
.env.example            the same file, committed, so the public repo documents what's needed
requirements.txt        four dependencies, phase 1 only
solscanner/config.py    env loading, program IDs, a redact() that strips the API key from anything printed
solscanner/db.py        SQLite schema: tokens, credit_usage, runs
solscanner/decoders.py  pulls name/symbol/mint/creator straight out of pump.fun log data
solscanner/credits.py   local Helius credit estimate, flushed to the database
solscanner/resolver.py  rate-limited getTransaction lookups for Raydium pools
solscanner/ingest.py    the WebSocket loop, reconnect/backoff, console output
run_scanner.py          entry point
show_db.py              read the captured data without needing a SQLite client
```

Setup order was deliberate: `.gitignore` was the first file created, before `git init`, before `.env` existed. `git status` has never at any point listed `.env`.

```
$ git check-ignore -v .env
.gitignore:2:.env	.env

$ git status --short
?? .env.example
?? .gitignore
?? CLAUDE.md
?? requirements.txt
?? run_scanner.py
?? scanner-setup-checklist.md
?? show_db.py
?? solscanner/
```

### Errors hit

**1. `python` does not exist inside Git Bash on this machine.**

```
Python was not found; run without arguments to install from the Microsoft Store, or disable this shortcut from Settings > Apps > Advanced app settings > App execution aliases.
```

Cause: Windows ships a stub `python.exe` in `WindowsApps` that exists only to redirect you to the Microsoft Store. The real interpreter is reachable as `py -3` from PowerShell, and inside the project as `./venv/Scripts/python.exe`. Fixed by using the venv interpreter explicitly for everything. Cost about 2 minutes. Worth knowing because every command in this project needs the venv interpreter anyway, not the system one.

**2. Bash heredoc blew up part way through writing a source file.**

```
/usr/bin/bash: -c: line 70: unexpected EOF while looking for matching `''
```

Cause: writing a ~230 line Python file through a shell heredoc, where the file content contains quote characters. The shell tried to parse something inside the file as shell syntax. The file was written truncated, which is worse than not written at all — a half-written module that still imports is a genuinely nasty failure mode. Fixed by deleting the partial file and writing source files directly instead of piping them through a shell. About 3 minutes.

**3. Dry-run harness reported zero rows written.**

```
[dryrun] active subscriptions: []
...
AssertionError: 0
```

Cause: this was a bug in the test harness, not the scanner. Solana's WebSocket does not tell you which subscription a message belongs to by name — it gives you a numeric subscription ID that the server assigns in its reply to your subscribe request. The scanner keeps two maps: request-ID to program (filled when we send the request), and subscription-ID to program (filled when the reply comes back). The harness skipped the first step and fed in only the replies, so every incoming message arrived tagged with a subscription ID the scanner had never heard of and was correctly discarded. Fixed by having the harness populate the pending-request map first. About 5 minutes.

Worth writing down because it surfaced a real property of the design: **if a subscribe reply is ever lost, the scanner sits there connected and silent forever.** That is exactly the 3am failure mode CLAUDE.md warns about. It is covered by the silence watchdog described below, but it took a broken test to make the risk concrete.

**4. Temp directory cleanup failed on Windows.**

```
PermissionError: [WinError 32] The process cannot access the file because it is being used by another process: 'C:\\Users\\rkhan\\AppData\\Local\\Temp\\tmp8gaxwahi\\dry.db'
```

Cause: the database is opened in WAL mode, which keeps `-wal` and `-shm` sidecar files open. The assertion above fired before `db.close()` ran, so the connection was still holding the file when the temp directory tried to delete itself. The `PermissionError` then buried the actual `AssertionError` under two more tracebacks. Fixed in the harness by closing the database before asserting. Under a minute, but it is a good reminder that on Windows an unclosed SQLite handle blocks file deletion in a way it does not on Linux — relevant later when the deploy step starts rotating or backing up the database file.

### Decisions made

**pump.fun launches cost zero API credits.** This is the significant finding of the session. pump.fun emits an Anchor event as a base64 `Program data:` line inside the log stream, and that event contains the name, symbol, metadata URI, mint address and creator wallet. All of it is already in the WebSocket message. No `getTransaction` call, no credit spent, no rate limit pressure. Given pump.fun is the overwhelming majority of launch volume, the free tier looks far less tight than expected.

**Raydium does cost one credit per pool.** Raydium's logs say a pool was initialised but not which token it is for. That needs one `getTransaction`. Pool creations are rare relative to trades, so this is affordable, but it is the only thing in phase 1 that spends anything. It runs behind a bounded queue and a client-side rate limiter set to 5 req/sec against a 10 req/sec ceiling, and it can be switched off with `RESOLVE_RAYDIUM_MINTS=false`.

**Rows are written before the lookup, never after.** A Raydium pool is inserted immediately with a null mint and status `pending`, then updated to `resolved` or `failed`. Rejected the obvious alternative of looking the mint up first and inserting a complete row, because a failed or rate-limited lookup would then silently lose the observation. Phase 1 is supposed to capture everything, and an incomplete row is a real data point while a missing row is not. This also matters for phase 5: a dataset that quietly drops the launches we failed to enrich is a survivorship-biased dataset.

**Derive the Anchor discriminator instead of hardcoding it.** Anchor computes an event's 8-byte tag as `sha256("event:CreateEvent")[:8]`. Copying that constant off a forum post is a great way to silently match nothing. Computing it at runtime makes it correct by construction. It comes out as `1b72a94ddeeb6376`.

**Read the mint from token balances, not from account layouts.** Raydium AMM v4, CPMM and LaunchLab all have different account orderings for their init instruction, and those orderings change between versions. Instead of decoding three layouts, the resolver reads the token balances the transaction touched and takes the mint that is not WSOL, USDC or USDT. Same code path works for all three and does not break on a version bump.

**Silence watchdog on top of the protocol ping.** websockets already sends a ping every 20 seconds. That is not enough — the specific failure we care about is a socket that stays technically open while delivering nothing. So there is a second layer: if no message of any kind arrives for 90 seconds, treat the connection as dead and reconnect. On programs this busy, 90 seconds of total silence is not a quiet period.

**Backoff resets on a healthy connection.** Exponential backoff, 1s doubling to a 60s cap, with up to 25% jitter. But if a connection survived longer than 60 seconds before dropping, the backoff resets to 1s — that is a one-off blip, not an endpoint that is refusing us, and there is no reason to sit out 60 seconds of launches for it.

**Rejected:** `transactionSubscribe` and LaserStream (both $49/month, per the setup checklist), webhooks (need a public HTTPS endpoint we do not have), and `getTransaction` on every pump.fun event (unnecessary, since the data is already in the logs).

### Surprises

- **The pump.fun event carries the entire launch record.** Expected to need an RPC call per launch and to be budgeting credits carefully from day one. Not the case.
- **Helius does not tell you what anything cost.** There is no credit figure in a JSON-RPC response and nothing itemised for WebSocket traffic. Anything a scanner reports about credits is a local estimate. The meter is built and labelled as an estimate: `getTransaction` is counted at 1 credit, and WebSocket messages are counted separately at a configured cost that currently defaults to **0**, because whether Helius bills per WebSocket message is genuinely unknown. The message count is recorded regardless, so after an hour of live running the real dashboard figure can be divided by the message count and `WS_MESSAGE_CREDIT_COST` set correctly. Flagged rather than guessed.
- **Token names contain emoji.** Writing one to a Windows console raises a `UnicodeEncodeError` and kills the process. Handled: full unicode goes into the database, the console gets an ASCII-flattened copy. A scanner that dies because someone launched a coin with a rocket in its name would be a stupid way to lose a night of data.
- **Everything installed clean on Python 3.14.4**, including `websockets` 17.1 with a `cp314` wheel. Was expecting to have to fall back to 3.12.

### Verification

No live run yet — the Helius key is not filled in, and nothing that needs a credential has been executed. Everything below is against synthetic data.

Offline self-test, decode and storage path, 7 cases:

```
1. pump.fun CreateEvent, old layout (no creator field)
   -> TEST Test Token 4vJ9JU1b creator CktRuQ2m OK
2. pump.fun CreateEvent, newer layout (creator appended)
   -> creator picked up from appended field: GgBaCs3N OK
3. a pump.fun trade (TradeEvent, different discriminator) is ignored
   -> ignored OK
4. truncated / corrupt event does not raise
   -> returned None instead of raising OK
5. creation markers
   -> OK
6. Raydium pool details from a getTransaction result
   -> US517G59 quote So1111 2025-08-24T01:46:40.000+00:00 OK
7. database write + duplicate suppression
   -> insert ok, replay of same signature ignored, count stays 1  OK

ALL SELF-TESTS PASSED
```

Dry run, seven synthetic WebSocket frames pushed through the real message handler:

```
[dryrun] active subscriptions: ['pumpfun', 'raydium_amm_v4', 'raydium_cpmm']
[21:46:50] #1     pumpfun         mint k7FaK..bDfYn $WIFSCARF    Dogwifscarf              dep swqr..iLCC
[21:46:50] #2     pumpfun         mint 2RJD1..agzmr $MOON        ? To The Moon ?          dep 2Z8o..BfRG
[21:46:50] #3     raydium_amm_v4  mint ------------ $-           -                        dep ----------

[dryrun] messages seen   : 7
[dryrun] creations       : 4
[dryrun] rows written    : 3
[dryrun] duplicates      : 1
[dryrun] failed txs      : 1
[dryrun] rows in db      : 3
[dryrun] by source       : {'pumpfun': 2, 'raydium_amm_v4': 1}
```

Seven frames in, three rows out. A buy on the same program was ignored, a Raydium swap was ignored, a failed on-chain transaction was ignored, and a replayed signature was suppressed as a duplicate.

The credential guard was also confirmed to block startup:

```
$ python run_scanner.py
Cannot start: missing credentials in .env
  - HELIUS_API_KEY is empty

Open .env, paste the value in, save, and run this again.
Nothing else in the project reads a key from anywhere else.
exit code: 1
```

### Content

`[CONTENT]` **The launch data was already in the message.** The premise going in was that watching Solana launches costs API credits and the free tier would be the binding constraint. It is not — for pump.fun, which is most of the volume. The WebSocket message already contains the token's name, symbol, mint and creator wallet, base64-encoded in a log line. On screen: the `decoders.py` block that base64-decodes the `Program data:` line, next to the dry-run output showing `#1 pumpfun mint k7FaK..bDfYn $WIFSCARF Dogwifscarf dep swqr..iLCC` — a fully populated row that cost zero credits. Contrast with the line directly under it, `#3 raydium_amm_v4 mint ------------ $-`, which is a Raydium pool where the mint is blank and one credit has to be spent to find out what token it is.

`[CONTENT]` **The provider will not tell you what you are spending.** Helius bills in credits, 1,000,000 free per month, and returns no credit figure in any response. The credit meter that got built is honest about this: `getTransaction` is counted at a known 1 credit, WebSocket messages are counted at a cost that defaults to zero because nobody actually knows. On screen: `credits.py`, specifically the docstring section headed "What is known vs assumed", and the status line format `est_credits=0 (0.000% of monthly free tier)`. The point is that the number in the status bar is an estimate wearing a suit, and it is labelled as one.

`[CONTENT]` **A rocket emoji in a coin name can kill the scanner.** Windows consoles default to a codepage that cannot encode emoji, and `print()` on an unencodable character raises and takes the process down with it. On screen: the dry-run line `#2 pumpfun mint 2RJD1..agzmr $MOON ? To The Moon ?` — the two question marks are where the rockets were flattened out for display. The full string with both rockets intact is what went into the database. The framing: the scanner survived a coin named with emoji, which sounds trivial until it is 3am and that is why there is a nine-hour hole in the dataset.

`[CONTENT]` **The bug in the test that found a real weakness.** The dry run printed `[dryrun] active subscriptions: []` and wrote zero rows. The harness was wrong, not the scanner — but the reason it produced silence rather than an error is that Solana's WebSocket identifies subscriptions by a number the server assigns you in a reply. Miss that reply and you are connected, healthy, and receiving nothing, forever. On screen: the failing output `[dryrun] active subscriptions: []` followed by `AssertionError: 0`, then the silence watchdog in `ingest.py` that raises `ConnectionError("no messages for 90s")` and forces a reconnect. This is the exact failure CLAUDE.md calls the primary risk of the whole project, and it showed up in session one via a broken test.

**Time:** roughly 45 minutes.

**Next:** fill in `HELIUS_API_KEY`, run the scanner live for an hour, and record the real numbers — launches per minute, message rate, and the actual Helius dashboard credit figure so `WS_MESSAGE_CREDIT_COST` can be set to something true. Nothing in phase 1 has touched the network yet.

---

## [2026-09-01] Session 1 (continued): first live run, and the bug it exposed

The entry above was written before the Helius key was filled in. Everything in it
was verified against synthetic data. The keys went in, the scanner went live, and
the first three minutes invalidated a third of what it captured. Leaving the
original entry as written, because being wrong in a documented way is the point.

**Goal:** Run phase 1 against live mainnet and record real numbers.

**What we built:** We turned the scanner on for real and watched actual coins appear as they were created, roughly twenty a minute. Then we discovered it had been recording a lot of things that were not new coins at all — ordinary trades that happened to look similar — so we tightened up how it decides what counts as a launch, and checked the fix by replaying everything it had already captured.

### The numbers, live

```
[status 22:28:43] up 1m00s | msgs 74144 (1235.3/s) | creations 60 | rows 43 | db total 249 [pumpfun=215 raydium_cpmm=34] | resolved 12 failed 2 queued 0 | reconnects 0 | rpc_calls=14 ws_msgs=74144 est_credits=14 (0.001% of monthly free tier)
[status 22:31:13] up 3m30s | msgs 231171 (1100.5/s) | creations 184 | rows 130 | db total 373 [pumpfun=324 raydium_cpmm=49] | resolved 41 failed 2 queued 0 | reconnects 0 | rpc_calls=43 ws_msgs=231171 est_credits=43 (0.004% of monthly free tier)
```

**1,100 to 1,400 WebSocket messages per second.** 231,171 messages in three and a
half minutes. That is the number that reframes the whole credit question:
subscribing to the pump.fun program means receiving every buy and sell on it, not
just launches. Extrapolated, that is on the order of three billion messages a
month. If Helius bills anything at all per WebSocket message, the 1M free tier is
gone in minutes, and `WS_MESSAGE_CREDIT_COST` is not a rounding detail — it is
the single most important unknown in the project. Zero reconnects in fourteen
minutes of total runtime, so the connection itself is stable.

### Errors hit

**5. A third of everything captured was not a token launch.**

No exception, no error message. That is what made it bad. The console looked
perfect — rows scrolling, mints populated, plausible symbols. The database:

```
  pumpfun          decoded   230
  pumpfun          failed    18
  pumpfun          resolved  103
  raydium_cpmm     resolved  50
```

Those `resolved` rows are ones where the mint could not be read from the event
and had to be fetched with a `getTransaction`. For a pump.fun launch that should
never happen — the event is always there. So what were they? Dumping the
instruction names out of the captured log blocks:

```
=== pumpfun rows whose mint came from RPC, not the event ===
count: 108
instructions in those log blocks: {'TransferChecked': 241, 'CreateTokenAccount': 129, 'GetFees': 106, 'InitializeAccount3': 96, 'GetAccountDataSize': 95, 'InitializeImmutableOwner': 95, 'SwapTob': 83, 'BuyExactSolIn': 65}

=== raydium_cpmm: instruction lines ===
rows: 55
    128  TransferChecked
     56  GetAccountDataSize
     56  InitializeAccount3
     56  SwapBaseInput
     54  InitializeImmutableOwner
     27  CloseAccount
     23  Swap
     21  SwapV2
```

`SwapBaseInput`. `BuyExactSolIn`. `Swap`. These are **trades**, being recorded as
token launches.

The cause is one line. Creation detection was a substring test:

```python
return any(marker in line for line in logs for marker in markers)
```

with markers `"Instruction: Create"` for pump.fun and `"Instruction: Initialize"`
for Raydium CPMM. Both are substrings of instruction names that appear in
completely ordinary transactions:

- `Instruction: Create` matches `CreateTokenAccount`, `CreateFeeSharingConfig`,
  `CreateDonationFeePda`, `CreateSocialFeePda`
- `Instruction: Initialize` matches `InitializeAccount3`,
  `InitializeImmutableOwner`, `InitializeMint2` — which the SPL token program
  emits inside virtually every swap that touches a new token account

For Raydium CPMM this was not a partial failure, it was total: replaying the
captured blocks through the fixed logic, **67 of 68 CPMM rows were swaps**. One
was a real pool. The one real one is unmistakable once you see it next to the
others:

```
KEPT ROW -> mint 8hJAuDTv2VkG4dXSKtjdBwZaWp32FzaYhVVQ94NpPY8q quote So111...112 status resolved
program log lines:
    Program log: Instruction: Initialize
    Program log: Create
    Program log: Initialize the associated token account
    Program log: liquidity:83666002653, lock_lp_amount:100, vault_0_amount:7000000000,vault_1_amount:100
```

**And there is a second, worse layer.** Checking how many captured pump.fun blocks
contained an exact `Program log: Instruction: Create` line:

```
  status=decoded   exact_create_line=False  246
  status=failed    exact_create_line=False  23
  status=resolved  exact_create_line=False  108
```

Zero. Not one. Including the 246 that decoded a genuine launch event. So how were
real launches being caught at all? Dumping instruction names from the blocks that
*did* decode a real event:

```
    262  CreateV2
    262  InitializeMint2
    262  SetAuthority
    317  MintTo
```

**The pump.fun create instruction is now `CreateV2`.** The marker
`Instruction: Create` was matching it as a substring, by accident. The detection
logic was not working — it was coincidentally not failing. Rename that instruction
to anything not beginning with "Create" and the scanner captures nothing, reports
no error, and prints a happy status line with `creations 0` forever.

Total damage in fourteen minutes: **231 of 565 rows were false positives, and
232 `getTransaction` calls — 232 credits — were spent on transactions that were
not launches.** About 40 minutes to find, understand and fix.

Two fixes:

1. Marker matching is now word-boundary aware. A marker matches only at the start
   of a `Program log: ` line and only if the next character is not alphanumeric,
   so `Instruction: Initialize` no longer matches `InitializeAccount3`.
2. **For pump.fun, instruction names are not used for detection at all.** The
   Anchor `CreateEvent` in the log data is the only signal. It is derived from
   `sha256("event:CreateEvent")[:8]`, it is what actually carries the launch
   record, and it does not change when the instruction gets renamed. If the event
   is present but its body will not parse, that is a layout change: the row is
   still stored and the console prints a loud warning, because silence is the
   failure we are trying to avoid.

Replaying all 565 captured blocks through the fixed logic:

```
  now KEPT   : {'pumpfun': 333, 'raydium_cpmm': 1}
  now DROPPED: {'pumpfun': 164, 'raydium_cpmm': 67}

  pump.fun rows that decoded a real CreateEvent before the fix : 333
  pump.fun rows the fixed logic keeps                          : 333

REGRESSION CHECKS PASSED
  231 of 565 captured rows were false positives
  wasted getTransaction calls they caused: 232
```

Exactly the rows with a genuine launch event survive, and nothing genuine was
lost. The contaminated database was moved to `scanner-session1-contaminated.db`
rather than deleted — it is the evidence, and it is the regression fixture.

**6. Four scanners were running at once without anyone noticing.**

```
ProcessId : 7612    Cmd : ...\venv\Scripts\python.exe run_scanner.py
ProcessId : 35568   Cmd : ...\venv\Scripts\python.exe run_scanner.py
ProcessId : 21376   Cmd : ...\venv\Scripts\python.exe -u run_scanner.py
ProcessId : 29140   Cmd : ...\venv\Scripts\python.exe -u run_scanner.py
```

Noticed only because moving the database failed:

```
mv: cannot move 'scanner.db' to 'scanner-session1-contaminated.db': Device or resource busy
```

Cause: the test runs were wrapped in `timeout -s INT 120 ...`. GNU `timeout`
under Git Bash sends a POSIX signal that does not reach a native Windows process
as a console control event. The wrapper died on schedule; the Python process
underneath did not, and kept running, kept writing, kept consuming. Two runs left
two orphan pairs. The database row count kept climbing between runs and it looked
like normal capture.

Worth stating plainly: **on Windows, a scanner started in a terminal cannot be
stopped except by a real Ctrl+C in that terminal, or a force kill.** `taskkill`
without `/F` does not work on a console process:

```
ERROR: The process with PID 34640 could not be terminated.
Reason: This process can only be terminated forcefully (with /F option).
```

Fixed in `run_scanner.py` by also handling `SIGBREAK` (Windows Ctrl+Break) and
falling back to `signal.signal` when the asyncio loop refuses to register a
handler, which it does on Windows. Verified with a real console control event
rather than assumed:

```
started pid 27672, capturing for 40s
sending CTRL_BREAK_EVENT (what Ctrl+Break sends)
process exited on its own, returncode=0
...
  stopped after 0m39s
  rows written this run : 25 (duplicates ignored: 0)
  totals in database    : 129
  credits (estimate)    : rpc_calls=0 ws_msgs=24564 est_credits=0 (0.000% of monthly free tier)
```

**7. The emoji problem, confirmed live.**

```
UnicodeEncodeError: 'charmap' codec can't encode character '\U0001fa99' in position 49: character maps to <undefined>
```

That is a coin emoji in a token name, and it came from an ad-hoc analysis script
that printed a symbol directly. The scanner itself ran fourteen minutes through
the same tokens without a flicker, because it routes every display string through
`safe()`. The guard written on a hunch in the first half of the session turned
out to be load-bearing within the hour. Position 49 of a token symbol is not a
place anyone would think to look for a crash.

**8. The heredoc failure from error 2 happened again, writing this entry.**

```
/usr/bin/bash: -c: line 174: unexpected EOF while looking for matching `''
```

Same cause as before: an apostrophe in ordinary prose, inside a shell heredoc.
Recorded because it is the second occurrence of a mistake already written down
once, which is its own kind of data point.

### Surprises

- **The instruction name had already changed and nobody noticed, including the code that depended on it.** `Create` became `CreateV2`. A substring match papered over it. This is the strongest argument in the project so far for matching on structure — a hashed event discriminator — rather than on strings that humans rename.
- **The firehose is enormous.** 1,200 messages/sec sustained on three subscriptions. Roughly 20 real launches per minute out of ~70,000 messages per minute: about **one message in 3,500 is a launch.** Everything else is trading noise being received, parsed and discarded.
- **A tenth of pump.fun mints do not end in `pump`.** 34 of 333 genuine launches had ordinary addresses. The `pump` suffix is a vanity address ground out by the launcher UI, not a protocol rule. Anything that identifies a pump.fun token by its address suffix will silently miss 10% of them. Nearly wrote that assumption into a test.
- **Raydium pool creations are genuinely rare.** One real CPMM pool in fourteen minutes, against roughly 280 pump.fun launches in the same window. Zero Raydium AMM v4 pools. Most of what is worth watching is on pump.fun.
- **105 distinct deployers across 129 launches** in the clean run, so wallets are already launching multiple tokens within minutes of each other. That is phase 3's whole premise, visible in four minutes of data.
- **The duplicate rate was an artifact of the bug.** The first run showed roughly 29% of signatures suppressed as duplicates. After the fix: `dupes 0`. The duplicates were high-frequency swap transactions being redelivered, not launches.

### Verification, clean run

```
rows: 129
  pumpfun         decoded   129
null mints: 0
null symbols: 0
null deployers: 0
distinct mints: 129
distinct deployers: 105

credit usage recorded:
   logsNotification calls= 192026 est_credits= 0.0
```

129 launches captured, every field populated, **zero RPC calls and zero credits
spent.** Every row came out of the log stream.

### Content

`[CONTENT]` **The bug that produced no error.** Best moment of the session. The
console was scrolling perfect-looking rows. Symbols, mints, deployer wallets, all
populated, all plausible. A third of them were people buying and selling, not
launching. On screen: the console output that looks completely healthy, then the
query result `SwapBaseInput: 56, Swap: 23, SwapV2: 21` from the rows it had
saved as "new token launches", then `now DROPPED: {'pumpfun': 164,
'raydium_cpmm': 67}`. The line to land on: 67 of 68 Raydium rows were swaps, and
nothing anywhere printed a warning. This is exactly the failure mode that
silently invalidates a backtest, caught in the first fifteen minutes of live
running only because a number looked slightly off.

`[CONTENT]` **The code was never working — it was coincidentally not failing.**
On screen: the query returning `exact_create_line=False` for all 377 pump.fun
rows *including the 246 that captured real launches*, followed by the instruction
dump showing `262 CreateV2`. The reveal is that pump.fun renamed its create
instruction, the substring `Create` still matched `CreateV2` by luck, and the
scanner appeared to work. The fix — deriving the event tag from
`sha256("event:CreateEvent")` instead of matching a name someone can rename — is
the actual lesson.

`[CONTENT]` **One message in 3,500 is a launch.** On screen: the status line
`msgs 231171 (1100.5/s) | creations 184`. Sustained 1,200 messages per second to
find roughly 20 launches a minute. Everything else is trades, received and thrown
away. Good visual: the raw scroll rate against the launch counter creeping up.

`[CONTENT]` **Four copies of the scanner running at once, found by accident.**
On screen: `mv: cannot move 'scanner.db': Device or resource busy`, then the
process list with four `run_scanner.py` PIDs, then
`ERROR: The process with PID 34640 could not be terminated. Reason: This process
can only be terminated forcefully`. The point: `timeout` killed the wrapper, not
the program, and on Windows there was no polite way to stop a background scanner
at all. Ends with the fix verified by an actual console control event —
`process exited on its own, returncode=0` — rather than assumed.

`[CONTENT]` **The guard that earned its place within the hour.**
`UnicodeEncodeError: 'charmap' codec can't encode character '\U0001fa99' in
position 49`. A coin emoji in a token symbol. The scanner survived it; a
throwaway analysis script written twenty minutes later did not, because it
skipped `safe()`. Same tokens, same machine, one line of difference.

**Time:** roughly 50 minutes on top of the earlier 45. About 1h35m total.

**Next:** the credit question is now the priority. `ws_msgs` reached 192,026 in a
few minutes of clean running against a 1M monthly free tier — if WebSocket
messages are billed at all, the current subscription set is not viable and the
firehose needs narrowing. Read the Helius dashboard, divide by the recorded
message count, set `WS_MESSAGE_CREDIT_COST` to a real number. After that, a long
unattended run to exercise the reconnect path, which has still never actually
fired: `reconnects 0` across every run so far.
