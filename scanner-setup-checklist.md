# Solana Token Scanner: End-to-End Setup Checklist

Verified against live pricing and docs, September 2026. Anything likely to change or trip you up is flagged.

**Revision note:** v1 omitted wallet setup entirely. An audit found nine further gaps: git authentication, exchange withdrawal locks, backtest data sourcing, heartbeat monitoring, database backup, deploy persistence, log rotation, disk budgeting for recordings, and the content toolchain. All are now included. Section 12 lists what I still consider genuinely uncertain rather than pretending otherwise.

---

## 0. Two decisions that shape everything

### Decision 1: Claude Code, not this chat

Multi-file Python project with dependencies, environment variables, a database, git, and a deploy step. In chat I can write code but you become the clipboard between browser and terminal, and I never see your actual error output. Claude Code reads your files, runs the code, sees the traceback, fixes it.

Use this chat for: architecture, interpreting results, deciding what to build next, reviewing paper-trade data.

### Decision 2: WebSockets first, webhooks later

Helius webhooks are on the free tier, but a webhook needs a public HTTPS URL to push to, which your laptop doesn't have. You'd need ngrok or a deployed server before you could test anything.

Standard Solana WebSocket subscriptions work outbound from your laptop with no public endpoint. Build on those. This means you can build and test locally, for free, before touching hosting.

---

## 1. Accounts to register

### Required

| # | Service | URL | Cost | Notes |
|---|---------|-----|------|-------|
| 1 | GitHub | github.com | Free | Version control. See 2.4 for auth, the bit that catches people. |
| 2 | Helius | helius.dev | Free | Solana RPC + WebSocket. Sign up, create a project, copy the API key. |
| 3 | Birdeye | bds.birdeye.so | Free | Holder distribution, OHLCV, historical data for the backtest. |
| 4 | Telegram | have it | Free | Bot setup in section 4. |
| 5 | Anthropic | claude.com | Varies | Claude Code auth. Claude subscription or Console API credits. |
| 6 | On-ramp | see 5.2 | Free | Phantom Buy flow. Light KYC with the provider, minutes not days. |

### No signup needed

- **DexScreener** (`api.dexscreener.com`) — no key, ~300 req/min
- **Jupiter** (`lite-api.jup.ag`) — no key for quotes and swap routing
- **RugCheck** (`api.rugcheck.xyz`) — free public endpoints
- **Solana public RPC** — exists, heavily rate limited, don't rely on it

### Hosting: Railway Hobby

Decided. You already pay $5/month and it includes $5 of usage credit; one always-on Python process fits inside that comfortably. Oracle stays as a fallback if usage ever creeps past the credit, but managing a VM is a day of work to save $5.

- [ ] **Attach a Volume before your first deploy.** Railway's filesystem is ephemeral, so your SQLite database is wiped on every redeploy without one. This is your entire captured dataset.
- [ ] Set a hard usage limit in Railway settings so a runaway loop can't bill you.
- [ ] Develop locally first. Deploy at the end of phase 1, once ingest works.

---

## 2. Local machine setup

### 2.1 Toolchain

- [ ] **Python 3.11+**. `python3 --version`. Install from python.org if missing.
- [ ] **Git**. `git --version`. On Windows install Git for Windows, which Claude Code requires on native Windows.
- [ ] **Claude Code**. Per Anthropic's docs: macOS 13.0+, Windows 10 1809+, or Ubuntu 20.04+/Debian 10+; 4 GB+ RAM; x64 or ARM64. The native installer ships its own runtime, so Node.js is not required. Only the legacy npm path needs Node 18+.
- [ ] Run `claude doctor` to confirm it's healthy.
- [ ] **VS Code** optional but useful for eyeballing the database.
- [ ] **DB Browser for SQLite** optional, but makes checking captured data far easier than the CLI.

**Windows:** if Claude Code hits a permissions error early, close PowerShell, right-click, Run as administrator, retry. Common, not a real problem.

### 2.2 Project folder

- [ ] `mkdir -p ~/projects/solscanner && cd ~/projects/solscanner`
- [ ] Drop `CLAUDE.md` in the root before your first Claude Code session

### 2.3 Virtual environment

Claude Code handles this, but know what it's doing so you can debug it.

- [ ] `python3 -m venv venv`
- [ ] Activate: `source venv/bin/activate` (macOS/Linux) or `venv\Scripts\activate` (Windows)
- [ ] Prompt should show `(venv)`. If it doesn't, nothing else works as expected.

Expected dependencies: `solana`, `solders`, `websockets`, `httpx`, `python-dotenv`, `apscheduler`, `pandas`.

### 2.4 Git authentication

**This is the one that stops people on their first commit.** GitHub removed password authentication for git operations. You need one of:

- [ ] **Personal Access Token**: GitHub → Settings → Developer settings → Personal access tokens → Fine-grained. Scope to the one repo, `Contents: read/write`. Use the token as your password when git prompts.
- [ ] **Or SSH key**: `ssh-keygen -t ed25519 -C "your@email.com"`, then paste `~/.ssh/id_ed25519.pub` into GitHub → Settings → SSH keys.

Also required before your first commit or it fails:

- [ ] `git config --global user.name "Your Name"`
- [ ] `git config --global user.email "your@email.com"`

- [ ] Create a **private** repo. Not public. Your commit history will contain mistakes you don't want indexed, and a `.gitignore` slip on a public repo means a drained wallet.

---

## 3. Credentials and security

- [ ] Create `.env` in the project root
- [ ] Create `.gitignore` containing `.env`, `*.db`, `__pycache__/`, `venv/`, `*.log` **before your first commit**
- [ ] Verify: `git status` must not list `.env`
- [ ] Never paste an API key into a source file

Wallet-specific rules are in 5.4.

---

## 4. Telegram bot

- [ ] Telegram → `@BotFather` → `/newbot`
- [ ] Name it, username must end in `bot`
- [ ] Token into `.env` as `TELEGRAM_BOT_TOKEN`
- [ ] Send any message to your bot (it won't reply)
- [ ] Open `https://api.telegram.org/bot<TOKEN>/getUpdates`
- [ ] Find `"chat":{"id":123456789}`, save as `TELEGRAM_CHAT_ID`
- [ ] Test: `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>&text=test`

Phone buzzes, notification pipeline is done.

---

## 5. Wallet and funding

**You do all of this yourself. I can't and won't create a wallet, hold a key, or move funds.**

### 5.1 Create the trading wallet

**Recommended: generate the keypair in code.** Have Claude Code write a one-off `solders` script that generates a fresh keypair, prints the public address, and writes the private key into `.env`. The key never touches a browser, clipboard, or screen, which matters when you're recording everything.

The alternative, exporting from Phantom, puts the key on screen and in your clipboard. Avoid.

- [ ] Generate the keypair
- [ ] Confirm `PRIVATE_KEY` is in `.env` and `.env` is gitignored
- [ ] Save the public address
- [ ] Add it as watch-only in Phantom, or bookmark on Solscan, so you can check the balance without touching the key

### 5.2 Fund the wallet

Use Phantom's built-in Buy flow. No exchange account needed.

- [ ] Phantom → Buy → SOL (mobile: tap Cash Buy; extension: More → Cash Buy)
- [ ] Enter the AED amount, pick a provider from the offers shown
- [ ] Complete the provider's identity verification, usually ID photo and selfie, minutes not days
- [ ] Pay by card, Apple Pay, or bank transfer

**Fees.** MoonPay, the most common provider, runs around 1% on bank transfer and up to 4.5% on card, plus spread. On $30 that's one to two dollars. Buying on an exchange and withdrawing is cheaper, but it costs you a full KYC and a possible withdrawal hold, which is not worth it at this size.

**If it fails.** Provider availability varies by region, so check what UAE actually offers before assuming MoonPay. Some banks block crypto card purchases. If your card declines, try a different provider or payment method. Phantom cannot help with declines, refunds, stuck orders, or verification problems, you contact the provider directly.

**Optional later.** If you scale past experiment size, a VARA-licensed exchange (Binance FZE, OKX, BitOasis, Rain, Crypto.com) is cheaper per transaction. Check the VARA public register rather than any list including this one. At that point also note that most exchanges hold withdrawals 24 to 48 hours after you add a new address or change a security setting, so whitelist early.

### 5.3 How much and why

| Purpose | Amount |
|---------|--------|
| Trading capital | ~$20 |
| Gas and rent buffer | ~$10 in SOL |
| **Total** | **~$30** |

**Account rent.** Every distinct token you buy creates an Associated Token Account costing roughly 0.002 SOL in rent deposit. Refundable when you close the account, but twenty tokens means ~0.04 SOL locked in accounts you've forgotten. Build a cleanup script in phase 7 that closes empty token accounts and reclaims rent.

Swap gas is about $0.01. Rent deposits and slippage are the real costs.

### 5.4 Rules for this wallet

- [ ] Holds nothing but trading capital and gas. Ever.
- [ ] Not connected to any other wallet, exchange account, or identity you care about
- [ ] Never sign a transaction from a website with it. It exists for your own code only.
- [ ] The private key never appears in a commit, screenshot, video frame, chat window, or message to me
- [ ] If you suspect a leak, move funds and regenerate immediately. Bots watch for exposed keys and drain them in minutes.
- [ ] Max position size hard-coded in the execution code. Not an env var, not a config file. In code, so a bug can't overspend.

---

## 6. Build phases

### Phase 1: Ingest (1 to 2 sessions)

Connect to Helius WebSocket, subscribe to new pool creation events, write every new token to SQLite. No filtering, no alerts.

Done when: an hour of running produces hundreds of rows.

### Phase 2: Enrichment (2 to 3 sessions)

Per token: mint authority status, freeze authority status, LP burned or locked, top 10 holder concentration, holder count, liquidity USD, deployer address, bundled-buy detection.

Sources: Helius for on-chain state, Birdeye for holders, RugCheck as second opinion, DexScreener for liquidity and price.

Done when: you can query any token and see all of it.

### Phase 3: Deployer history (1 to 2 sessions)

Per deployer wallet: how many tokens launched, what happened to them, reputation score.

Done when: you can see "this deployer launched 14 tokens, 13 died within 48 hours."

### Phase 4: Rules engine and alerts (1 to 2 sessions)

Disqualification ruleset. Reasons to reject, not reasons to buy. Two tiers: **Watch** (passes structure, logged silently) and **Alert** (passes structure and traction, phone buzzes).

Done when: 5 to 15 alerts a day, not hundreds.

### Phase 5: Operational hardening (1 session, do not skip)

Section 7. This is the phase that stops the project dying quietly.

### Phase 6: Backtest (2 to 3 sessions, hardest part)

Section 8.

### Phase 7: Execution (only if 6 justifies it)

Jupiter swap API, burner wallet, hard-coded position cap, slippage tolerance, priority fee handling, ATA cleanup script.

**Set slippage explicitly.** Defaults on thin memecoin liquidity fill you at prices you did not intend. And on a congested network a transaction with no priority fee simply doesn't land, so you need a fee strategy or your sells fail exactly when you need them.

---

## 7. Operational hardening (phase 5 detail)

Every item exists because without it the project silently stops working and you don't notice.

- [ ] **Heartbeat.** Daily Telegram message: "alive, N tokens seen in 24h, N alerts, N credits remaining." Without this your scanner dies at 3am Tuesday and you find out Friday.
- [ ] **WebSocket reconnect with exponential backoff.** Connections drop constantly. Most likely failure mode by far.
- [ ] **Credit exhaustion alert.** Telegram warning at 80% of Helius monthly credits. Otherwise you hit the ceiling mid-month and everything stops with no error you'd notice.
- [ ] **Database backup.** Your paper-trade log is the entire asset of this project. Nightly copy of the `.db` to a second location. Three months of data on one laptop that could be stolen or die is not a plan.
- [ ] **Log rotation.** Cap log files by size or they fill the disk on a 1 GB Oracle instance and take everything down.
- [ ] **Kill switch.** One command that stops everything. Know it before you need it.
- [ ] **Smoke test.** Before trusting any output, pick three tokens you can verify manually on Solscan and confirm your pipeline reports the same numbers. A pipeline that runs cleanly but reports wrong data is worse than one that crashes.

### Deploy specifics (when you move off the laptop)

- [ ] SSH key generated and added to Oracle **at instance creation**. You cannot add it later without console recovery.
- [ ] Run under `systemd` (or `tmux`/`screen` at minimum) so the process survives your SSH session closing. A plain terminal process dies when you disconnect.
- [ ] `systemd` restart policy `always` so it recovers from crashes
- [ ] No inbound firewall ports need opening. Outbound connections only. Don't open anything.
- [ ] Server timezone UTC, and leave it there

---

## 8. The backtest (phase 6 detail)

I flagged this as the fastest route to a real answer. It's also the hardest thing in the project and I under-described it before.

**The problem:** you need historical data for tokens that *died*. Most APIs deprioritise or drop dead tokens, which is precisely the survivorship bias you're trying to avoid. A dataset built from currently-indexed tokens makes any ruleset look brilliant.

**Where the data comes from:**

- Birdeye historical OHLCV, your primary external source and the main reason to have that account
- **Your own ingest database**, once it's been running. Every token you captured is a data point, including the dead ones.
- Helius historical transaction queries for reconstructing deployer behaviour

**Two ways it silently lies to you:**

1. **Lookahead bias.** Rules may only use data that existed at the decision timestamp. Holder count at T+0, not final. Liquidity at T+0, not peak. Enforce in code, not by convention.
2. **Survivorship bias.** The dataset must include tokens that died.

**Honest recommendation:** your own ingest database is the cleanest source, because you captured everything including the failures, at the timestamps you actually saw them. That means the highest-quality backtest is available after two to three weeks of running phase 1, not on day three. Run the external-data version first for a rough read, then re-run against your own data. Trust the second one.

---

## 9. Known gotchas

1. **Helius free is 1M credits/month, 10 RPC req/sec.** A naive loop calling `getTransaction` per event burns it fast. Watch the dashboard in week one.
2. **Helius LaserStream and enhanced WebSockets (`transactionSubscribe`) are $49/month.** Standard `logsSubscribe` works on free. Build on standard.
3. **Webhooks need a public HTTPS endpoint.** WebSockets first.
4. **SQLite on Railway is wiped on redeploy** without a volume.
5. **DexScreener rate limits around 300 req/min.** Batch lookups.
6. **Store all timestamps in UTC**, display in GST.
7. **X/Twitter API is ~$100/month.** Skip it. On-chain behaviour is a better free signal: unique buyers per minute, holder growth, buy/sell ratio, fresh-wallet share.
8. **Free tiers change.** Verify Helius, Birdeye, Railway pricing on their own sites before architecting around any number here.
9. **A clean token still goes to zero.** Passing every structural check means the deployer didn't use the detectable methods of stealing from you. Most tokens die because attention moved on, which nothing detects.

---

## 10. Content capture

No screenshots, no screen recording. The devlog is the capture mechanism.

`CLAUDE.md` mandates that Claude Code logs every session with verbatim errors, exact on-screen values, and `[CONTENT]` flags on anything worth filming. That gives you an accurate written record to build video from whenever you get to it, without slowing the build down now.

**What that means in practice:**

- [ ] Nothing to install, nothing to run alongside your sessions
- [ ] Read `DEVLOG.md` when you're ready to make content, not before
- [ ] The `[CONTENT]` flags are your shot list
- [ ] Episode scripts are in `tiktok-series-scripts.md`, with blanks for real numbers

**One thing worth keeping accurate:** recreating a visual of an error you actually hit is fine, and the devlog gives you the exact output to recreate it from. Numbers are different. Your backtest result and your trade P&L are the substance of the series, and the whole reason it's worth watching is that they're real. Pull those from the database.

**Never on camera, whenever you make it:**

- API keys: Helius, Birdeye, Telegram bot token
- Private keys or seed phrases, in any form, ever
- `.env` contents
- Telegram chat ID
- Full wallet address, unless you're content with people watching your trades

---

## 11. Division of labour

**You:** all signups, local setup, git auth, Telegram bot, wallet creation and funding, running Claude Code, keeping the paper-trade log honest, never sharing a private key with anyone including me.

**Claude Code:** writes and debugs every line, manages dependencies and venv, runs scripts, fixes breakage, sets up git and deploy config, maintains `DEVLOG.md`.

**This chat:** architecture, interpreting results, deciding what's next, sanity-checking a token or pattern, telling you honestly when the data says stop.

---

## 12. What I'm still uncertain about

Listed here rather than discovered mid-build.

- **On-ramp provider availability and fees in the UAE.** Verify in the Phantom Buy flow rather than trusting the numbers here.
- **Whether Birdeye's free tier covers enough historical depth** for the backtest. Verify before designing around it. Most likely thing to force a paid tier.
- **Bundled-buy detection** is a heuristic, not a solved problem. Yours will have false positives.
- **Oracle ARM capacity** in your region. Unknowable until you try.
- **Whether 5 to 15 alerts a day is the right target.** My estimate, not a measured number. You'll tune it from real data.
- **Helius credit consumption per token processed.** Depends entirely on how the code is written. Measure it in week one rather than assuming.

---

## 13. Immediate next actions, in order

Done: Railway, Helius, Oracle, Birdeye, Phantom, Telegram bot, GitHub, Claude Code.

1. **Pick one host.** You have Railway Hobby and Oracle, which is redundant. Railway Hobby is simpler to deploy to; if you take it, attach a Volume before your first deploy or every redeploy wipes the database.
2. **Decide the wallet approach.** Use your existing Phantom account, or generate a fresh keypair and import it into Phantom for genuine isolation from your seed. Either is fine at $30.
3. **Export the Solana private key** into `.env`. Not the seed phrase. Make sure you're on the Solana account, Phantom is multi-chain.
4. **Fund it.** Phantom → Buy → SOL, roughly $30.
5. **Set up the project:** folder, `CLAUDE.md` in the root, `.gitignore` with `.env` in it, private GitHub repo, `git config` set.
6. **Run `claude`** and give it the first prompt:

*"Read CLAUDE.md. Then build phase 1: connect to Helius WebSocket, subscribe to new pool creation events on Raydium and pump.fun, write each new token to a local SQLite database with mint address, timestamp, name, symbol, and deployer. Load credentials from .env. Set up .gitignore and verify .env is excluded before the first commit."*

Come back here when phase 1 is running and we'll design the disqualification rules against real captured data.
