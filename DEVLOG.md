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

---

## [2026-09-01] Session 2: Replacing the ingest architecture

**Goal:** The Helius firehose is not affordable. Move ingest to PumpPortal and prove the launch capture rate holds.

**What we built:** The scanner used to listen to every single trade on Solana in order to spot the handful that were new coins being created — about one useful message in every fifteen hundred. That turned out to cost so much that our free monthly allowance would have run out in a day. We swapped it for a free service that only sends the new-coin announcements, so the scanner now receives roughly one message per coin instead of fifteen hundred, and costs nothing.

### The measurement that forced the change

The Helius dashboard, after under an hour of running:

```
35,989 credits
  99.3%  LaserStream WebSocket delivery
   0.7%  RPC
```

The free tier is 1M credits/month. At that burn rate it is gone in about a day of
continuous running. Session 1 flagged `WS_MESSAGE_CREDIT_COST` as the single most
important unknown in the project and defaulted it to zero pending a real reading.
The real reading came back at roughly **0.01 credits per WebSocket message**, and
zero was wrong in the direction that mattered.

The 0.7% is the more interesting half. RPC — the `getTransaction` calls — came to
236 credits for an entire session. **RPC was never the problem. Delivery was.**
So Helius stays in the project as an RPC client for phase 2 enrichment, and only
the subscription architecture goes.

### Capturing the payload shape before writing any code

Session 1's worst bug came from assuming a log format. So this time, before a line
of ingest code changed, ten real payloads were pulled off the PumpPortal socket
and read.

```
connected to wss://pumpportal.fun/api/data
sent: {"method": "subscribeNewToken"}
  non-token message: {"message": "Successfully subscribed to token creation events."}
  [ 1/10] 'TeAmo'
  [ 2/10] 'Killer'
  ...
```

One real event, verbatim:

```json
{
  "signature": "3vJ8ywXdaJJzP9xP8FBRrEgidxyPCoPfEJG7VbJrcZ1nnZmhVJtt1NiJ2JyyNSQvJ4DyFkwH3eBTkhMMYyVYg8gi",
  "mint": "Btz83wg2AK3eng1R6HYBYBDie3JmxbbiGmmqj1TZpump",
  "traderPublicKey": "5MTjkAZMe1ur64ADTgnaYFN1ByTapu2RqcKYUdQNaE2E",
  "txType": "create",
  "initialBuy": 97545454.545454,
  "solAmount": 3,
  "bondingCurveKey": "3aeLfDXUhz5JVdLxFfPwpmMJV9giPsW4GPjLtEf63jiu",
  "vTokensInBondingCurve": 975454545.454546,
  "vSolInBondingCurve": 32.999999999999986,
  "marketCapSol": 33.830382106244144,
  "name": "Solana Te Amo",
  "symbol": "TeAmo",
  "uri": "https://metadata.j7tracker.io/metadata/DL2kT756b9.json",
  "is_mayhem_mode": false,
  "pool": "pump"
}
```

What the capture established that documentation would not have:

- 14 fields, all present in all 10 events.
- **No `slot` and no `blockTime`.** The Helius log stream gave us a slot for free;
  this does not. Those columns are now null, and deliberately not invented.
- `initialBuy` and `solAmount` arrive as `int` *or* `float` depending on the value
  (`"solAmount": 3` in one event, `0.989824717` in another). Anything doing
  arithmetic on them in phase 2 needs to expect both.
- `traderPublicKey` is the creator on a `create` event, so it maps to `deployer`.
- A `{"message": "Successfully subscribed..."}` acknowledgement arrives first and
  is not a token event.

### Decisions made

**The payload maps onto the existing columns; the schema does not bend to the
feed.** `signature`, `mint`, `name`, `symbol`, `uri` map directly.
`traderPublicKey` becomes `deployer`. `pool` becomes `source`. The seven fields
with no column of their own — `bondingCurveKey`, `initialBuy`, `solAmount`,
`vTokensInBondingCurve`, `vSolInBondingCurve`, `marketCapSol`, `is_mayhem_mode` —
go into `raw_event` as the complete original JSON, so nothing is lost and phase 2
can promote whichever of them turn out to matter.

**One new column, added without touching a single existing row.**
`ingest_source` distinguishes PumpPortal rows from the 129 Helius rows already
captured. It went in as `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT
'helius_logs'`, which means SQLite reports the old rows as `helius_logs` on read
without rewriting them. No `UPDATE`, no rebuild, no migration of existing data.
Note this is separate from `source`, which stays what it always was: the venue the
token launched on, not how we heard about it.

**Do not guess a pool-to-program mapping.** Only pools actually seen on the wire
get mapped. Anything else is stored under a `pumpportal:<pool>` sentinel and logs
a warning, rather than being silently attributed to the wrong program. This
mattered within eight seconds of going live — see below.

**Do not use `mint` to tell an event from an acknowledgement.** The first version
did, and a test caught it: a create event that lost its `mint` field would have
been quietly filed as an unrecognised control message. It now keys off the
presence of any of `txType`/`mint`/`signature`, so a payload that loses a required
field gets counted as malformed and shouted about instead of disappearing.

**Watchdog retuned from 90s to 600s.** Under Helius this watched a 1,100 msg/sec
firehose, where 90 seconds of silence was unambiguously a dead socket. PumpPortal
delivers about 28 creations a minute. Treating arrivals as Poisson, 90s of silence
at the observed rate has probability e^-42, but the rate is not constant — quiet
stretches and overnight lulls drop it to a few per minute, and at 2/min a 90s
watchdog would false-trigger about 5% of the time and reconnect for no reason. At
600s, even a 1/min rate gives a 0.005% false-trigger probability. The
protocol-level ping (20s interval, 20s timeout) still catches a genuinely dead TCP
connection in about 40 seconds, so the longer backstop does not slow down real
failure detection — it only covers the open-but-not-delivering case.

**Rejected:** narrowing the Helius subscription set to stay under budget. There is
no subscription narrow enough. The cheapest possible logsSubscribe still delivers
every trade on whatever program it watches, and the launches are a rounding error
inside that. The architecture was the cost, not the tuning.

### Errors hit

No new errors this session. The replay test did fail twice, both times because a
test expectation was wrong rather than the code:

```
AssertionError: 2
```

Expected three malformed payloads, got two. That one was worth having: it exposed
the `mint`-as-discriminator flaw described above, so a wrong test found a real
weakness. Fixed in the code, not the test.

```
AssertionError
  assert db.counts_by_ingest_source() == {"helius_logs": pre_count}
```

The migration test copies the live database, and by then the live database
contained PumpPortal rows too, so the expectation of a Helius-only table was
stale. Fixed in the test.

### The bonk discovery

Eight seconds into the live run:

```
2026-09-01 18:57:00,700Z WARNING solscanner.ingest unmapped PumpPortal pool 'bonk', stored as 'pumpportal:bonk'
```

**`subscribeNewToken` is not a pump.fun feed. It covers multiple launchpads.** The
ten-payload sample happened to contain only `pool: "pump"`, and had that sample
been treated as the whole truth, this launch would have been silently recorded
against the pump.fun program. It was not, because unmapped pools get a sentinel.

The `bonk` payload also has a **different shape**:

```json
{
  "signature": "pZJ4sWQax8k3yUw2YVACn1irsxHKvNFC5aQKiDPoTcUaZAEAu4zRg4hw5cTRerd5xGdyDXhRjT3VcATLfnPJoVG",
  "mint": "3ksbqvVHUCSqtCieUYXPrJ2MySNwzqG4bspCBMSqiray",
  "solInPool": 0.992,
  "tokensInPool": 965739053.104158,
  "newTokenBalance": 34260946.895842,
  "pool": "bonk"
}
```

`solInPool` / `tokensInPool` / `newTokenBalance` instead of `bondingCurveKey` /
`vSolInBondingCurve` / `vTokensInBondingCurve`, and no `is_mayhem_mode`. Any phase
2 code that reads liquidity out of `raw_event` has to handle both shapes. Storing
the whole payload rather than a chosen subset is what makes that recoverable.

Rather than guess which program `bonk` means, one `getTransaction` was spent on it:

```
signature : pZJ4sWQax8k3yUw2YVACn1irsxHKvNFC5aQKiDPo...
slot      : 443496916  blockTime: 1788289066
programs invoked:
   16x  TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA
   10x  11111111111111111111111111111111
    4x  LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj  raydium_launchlab
    2x  ComputeBudget111111111111111111111111111111
    2x  ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL
    1x  metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s
```

letsbonk.fun runs on Raydium LaunchLab, which was already in the program
catalogue. `bonk` is now mapped, with the signature that proves it recorded in the
comment. Cost: 1 credit. That is exactly the job the retained Helius RPC client
exists to do.

The one row captured under the sentinel was corrected to `raydium_launchlab` once
the venue was verified. That is a correction to a row captured minutes earlier in
this session, not a migration of the historical Helius rows, which remain
untouched at 129.

### The 10-minute comparison

```
  stopped after 9m59s
  launches captured     : 276 (27.6/min)
  duplicates ignored    : 0
  malformed payloads    : 0
  totals in database    : 405 {'helius_logs': 129, 'pumpportal': 276}
  helius credits        : rpc_calls=0 ws_msgs=0 est_credits=0 (0.000% of monthly free tier)
```

| | Helius logsSubscribe | PumpPortal |
|---|---|---|
| Launches captured | 129 | 276 |
| Window | 249 s | 599 s |
| Rate | 31.1/min | 27.6/min |
| Messages received | 192,026 | 277 |
| **Messages per launch** | **1,489** | **1.004** |
| Helius credits | ~0.01/msg | **0** |
| Reconnects | 0 | 0 |
| Duplicates | 0 | 0 |

**Helius credit spend during the PumpPortal run was zero**, confirmed by the meter
reading `rpc_calls=0 ws_msgs=0 est_credits=0` and by nothing in the ingest path
holding a Helius connection.

**On the rate difference: 27.6/min versus 31.1/min is an 11% gap, and this data
cannot tell you whether that gap is real.** The per-minute counts within the
PumpPortal run alone were:

```
[31, 23, 32, 36, 21, 27, 31, 21, 24, 30]
```

21 to 36 launches per minute — a 1.7x spread inside a single ten-minute run. The
Helius figure comes from a 249-second sample, which is one and a half of those
buckets. An 11% difference between a 4-minute sample and a 10-minute sample taken
20 minutes apart sits comfortably inside that variance, so claiming either
equivalence or a coverage gap from these numbers would be overreading them.

The measurement that would actually settle it: run both ingest paths simultaneously
against the same wall-clock window and diff the sets of mint addresses. That is a
real experiment and it is worth doing before the backtest depends on PumpPortal
being complete, because a feed that silently drops 11% of launches is a
survivorship problem, and CLAUDE.md is explicit that survivorship bias invalidates
the whole exercise. Logged as the next task rather than waved through.

### Data quality of the 276 rows

```
null/empty mint      : 0
null/empty signature : 0
null/empty name      : 1
null/empty symbol    : 1
null/empty uri       : 0
null/empty deployer  : 0
null/empty raw_event : 0
distinct mints    : 276
distinct deployers: 211
slot always null  : True
```

276 launches, 276 distinct mints, no duplicates. One token launched with an empty
name and empty symbol (`BAiQT5CLLi5gQGFrcMveFgDEZvVbdbZ26gqPLeeopump`) — stored as
sent rather than rejected, because an unnamed token is a real observation and quite
possibly an interesting one.

211 distinct deployers across 276 launches: 65 launches came from wallets that
launched more than once inside ten minutes. Phase 3's premise again, now with a
bigger sample.

### Surprises

- **RPC was never the expensive part.** 236 credits for a whole session against 35,753 for delivery. The instinct going into session 1 was to be careful about `getTransaction` calls and relaxed about the subscription. Exactly backwards.
- **The feed is almost pure signal.** 277 messages produced 276 launches. Against 1,489 messages per launch on Helius, that is a 1,483x reduction in traffic to do the same job.
- **`subscribeNewToken` spans launchpads.** Expected a pump.fun feed, got pump.fun plus letsbonk plus presumably others not seen in ten minutes. That is more coverage than intended, which is good, but it means `pool` is a field to handle rather than a constant to ignore.
- **Different launchpads send differently shaped payloads on the same feed.** No version marker, no discriminator, just different keys. Storing the whole payload is the only thing that makes this survivable.
- **Zero duplicates in 276 events.** Helius redelivered constantly. PumpPortal did not repeat a single signature.

### Content

`[CONTENT]` **The number that killed the architecture.** On screen: the Helius
dashboard showing `35,989 credits` with the 99.3% / 0.7% split, next to session
1's status line confidently reporting `est_credits=0 (0.000% of monthly free
tier)`. The scanner had been reporting zero spend while burning a month's
allowance in a day, because the provider does not itemise it and the meter
defaulted to zero pending a real reading. The lesson is not "we got the number
wrong", it is "we shipped a meter that could only ever have read zero and then
trusted it".

`[CONTENT]` **1,489 messages per launch, versus 1.004.** On screen: the two status
lines side by side. Helius: `msgs 231171 (1100.5/s) | creations 184`. PumpPortal:
`msgs 266 (28.0/min) | launches 265`. Same job, same launches, three orders of
magnitude difference in traffic. The visual is the raw scroll rate — one is
unreadable, the other is one line per coin.

`[CONTENT]` **The guess that was refused, eight seconds in.** On screen: the
warning line `unmapped PumpPortal pool 'bonk', stored as 'pumpportal:bonk'`, then
the bonk payload with its completely different field names next to the pump
payload, then the `getTransaction` output showing `4x LanMV9sAd7...
raydium_launchlab`. The point: the ten-sample capture contained only `pump`, and a
reasonable person would have hardcoded that. The rule that saved it was written
because of last session's bug, and it paid out within eight seconds of the first
live run.

`[CONTENT]` **The honest non-answer on coverage.** 27.6/min versus 31.1/min looks
like an 11% shortfall and the tempting move is to report it as one. On screen: the
per-minute bucket list `[31, 23, 32, 36, 21, 27, 31, 21, 24, 30]` — a 1.7x spread
inside one run — and the observation that the Helius baseline was only 249 seconds
long. The finding is that the data does not support a conclusion either way, and
the follow-up is to run both feeds against the same window and diff the mints.
Worth cutting because "we do not know yet, and here is the experiment that would
tell us" is the opposite of what this genre usually shows.

`[CONTENT]` **A test that was wrong found a bug that was real.** On screen:
`AssertionError: 2` where 3 was expected, then the diff that changed the event
discriminator from "has a mint field" to "has any of txType/mint/signature". The
failing expectation was mine and the count was fine — but chasing it revealed that
a create event missing its `mint` would have been silently filed as an
unrecognised control message and never counted as an error at all.

**Time:** roughly 45 minutes.

**Next:** run Helius logsSubscribe and PumpPortal concurrently for a fixed window
and diff the captured mint sets, to establish whether PumpPortal misses launches.
That is a bounded experiment with a known cost — one short Helius run, a few
thousand credits — and it needs settling before phase 5 treats this feed as a
complete record. After that, a long unattended run to finally exercise the
reconnect path, which has still never fired: `reconnects 0` across every run in
both sessions.
