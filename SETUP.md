# Daily Paper-Trading Setup (Ubuntu 24.04)

> **Active broker: Alpaca** (paper). IBKR stays supported via `broker: ibkr`
> in `watchlist.yaml` for when you want the extra fill fidelity of a paper
> Gateway — see the IBKR section at the bottom.

## Keys & broker setup (do this first)

### 1. OpenRouter API key (~2 min)
1. Sign up / log in at https://openrouter.ai (Google or GitHub works).
2. Go to https://openrouter.ai/keys → **Create Key** → name it e.g. `daily-trading` → copy the `sk-or-v1-...` value.
3. Add ~$10 credit at https://openrouter.ai/settings/credits (the DeepSeek V4 pairing is ~$0.10–0.50/stock/day, so $10 covers weeks of a 5–10 ticker watchlist). You can set a monthly limit cap.
4. Paste into `.env`: `OPENROUTER_API_KEY=sk-or-v1-...`

### 2. FRED API key (~2 min, free)
1. Go to https://fred.stlouisfed.org/docs/api/api_key.html → **Request API key** → register → the key arrives in your email instantly.
2. Paste into `.env`: `FRED_API_KEY=<32 hex chars>`.

### 3. Alpaca paper trading (~5 min, no approval wait)
1. Sign up at https://alpaca.markets (email + verify; no ID approval needed for paper).
2. **Paper Trading → API Keys** — you get a key pair (`PK...` = API key, `SK...` = secret). Paper accounts are funded with $100k fake cash.
3. Paste into `.env`: `ALPACA_API_KEY=PK...` and `ALPACA_SECRET_KEY=SK...`.
4. Verify: `.venv/bin/python daily_run.py --healthcheck` → prints `broker (alpaca) reachable`.

> Alpaca is US-equities-only — exactly the system's scope. Orders are placed
> for the regular session (`extended_hours=false`), so orders submitted at
> 09:00 queue for the 09:30 open.
>
> **Entry protection (gap-up guard):** buys are plain limit orders at
> `prev_close × 1.05` (`entry_protection_pct`). If the open gaps above the
> cap the order never fills and is cancelled after the fill timeout — never
> overpaid.
>
> **Two-step stop-loss (not an OTO bracket):** Alpaca's paper engine inverts
> the OTO leg creation at the open (stop leg lands before the limit, no
> parent linkage) so OTO entries never fill — verified live 2026-09-01 (IT
> and CRWD both sat unfilled and were cancelled). The broker therefore
> submits the plain capped limit first, polls the fill, and only then
> attaches the GTC stop (`prev_close × 0.92`, `stop_loss_pct`) as a separate
> order. The unprotected window between fill and stop is the 5s poll
> interval.
>
> **Gap-down guard:** if the fill price is at/below the stop level
> (`prev_close × 0.92`), the stock gapped through the stop at the open — the
> position would be dead on arrival, so the entry is immediately sold back
> and no stop is attached (no wasted position slot, no guaranteed-loss stop).
> If that undo sell itself fails, the position is left naked and logged
> loudly.

Verify all keys: `cd ~/workplace/TradingAgents && .venv/bin/python daily_run.py --analyze --tickers AAPL`
should start printing per-agent analysis.

### 4. Reddit OAuth (optional, 10x the rate limit — recommended)
The anonymous RSS path is rate-limited to ~10 req/min (429s under parallel
analysis). A free script app raises that to 100 req/min and returns real
scores/comment counts.

1. Log in to Reddit, then open **https://www.reddit.com/prefs/apps** (if it
   says "page not found", you're logged out — sign in first, the page lives
   under your account settings).
2. Scroll to the bottom → **"are you a developer? create an app..."**.
3. Fill in: name `daily-trading`, **type: script**, description anything,
   redirect uri `http://localhost:8080` (unused for script apps but required).
   → **create app**.
4. Under your new app: the line under "personal use script" is the
   **client ID** (~14 chars, not secret); **secret** is the other field.
5. Paste both into `.env`: `REDDIT_CLIENT_ID=...` and `REDDIT_SECRET=...`.
6. Verify (uses the live API, reads only):
   `cd ~/workplace/TradingAgents && .venv/bin/python -c "
   import reddit_auth; print(reddit_auth.fetch_reddit_posts('AAPL', subreddits=('stocks',)))"`

With creds set, the analysis uses the OAuth fetcher automatically (you'll see
`Reddit: using OAuth fetcher (100 QPM)` in the logs); without them it falls
back to the paced RSS path.

### 3. IBKR paper trading + API (~30 min, account approval can take a day)
1. **Account**: create an individual account at https://www.interactivebrokers.com (needs government ID; approval usually 1–3 days). Skip if you already have one.
2. **Paper account**: log into the IBKR Client Portal → **Settings → Account Settings → Trading Configuration → Paper Trading Account** → enable it. IBKR mirrors your account as a paper environment (funded with $1M fake cash, no live money ever).
3. **Install IB Gateway on the Ubuntu host** (NOT TWS — Gateway is the headless server app):
   - Download the Linux (64-bit) package from https://www.interactivebrokers.com/en/trading/ibgateway-stable.php (`.sh` installer; works on Ubuntu 24.04).
   - Run the installer, launch Gateway, and log in with your **paper account credentials** (login window: pick "Paper" / the paper username — paper logins usually look like your username; TWS/Gateway shows a paper/live toggle at login).
4. **Enable API** in Gateway: **Configure → Settings → API → Settings** → check **Enable ActiveX and Socket Clients**, set the socket port to **7497** (paper), and uncheck "Read-Only API" only if you want the API to place orders — for this system, leave **Read-Only API unchecked** (we need order placement) but keep **"Trusted IPs"** set to `127.0.0.1`.
5. **Auto-login + auto-restart** (so a reboot doesn't kill the morning run):
   - In Gateway's login window, tick **Save user ID and password** (credentials are stored in `~/IBJts/jts.ini`).
   - Create a systemd unit so Gateway starts on boot — see below.
6. **Verify the link**: with Gateway running, run
   `.venv/bin/python daily_run.py --healthcheck` → prints **`IBKR reachable`**.

> Paper accounts mirror your real account's market-data subscriptions. Delayed data is
> free and sufficient: all price data for decisions comes from yfinance; the Gateway is
> only used for order placement at the open. No paid data subscription needed to run.

## Switching to IBKR paper later (kept backend)
The IBKR code stays in the repo; the flip is: set `broker: ibkr` in
`watchlist.yaml`, install + log into IB Gateway (paper, port 7497, API
enabled, auto-login + the systemd unit below), and `--healthcheck` again.
IBKR paper is the most realistic free simulator (real routing simulation,
opening-auction modeling) at the cost of running the Gateway daemon.

### systemd unit for IB Gateway (auto-start on boot)
```ini
# /etc/systemd/system/ibgateway.service
[Unit]
Description=IBKR Gateway (paper)
After=network-online.target
Wants=network-online.target

[Service]
User=trading
ExecStart=/opt/IBGateway/ibgateway
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```
(Adjust `User` and the `ExecStart` path to your install. On first boot, log in once
interactively to store credentials; the service restarts the saved session afterwards.)

## One-time install
1. `sudo apt install -y python3.12 python3.12-venv cron`
2. `git clone <repo> ~/workplace/TradingAgents && cd ~/workplace/TradingAgents`
3. `python3.12 -m venv .venv && .venv/bin/pip install . ib_async pyyaml`
4. Copy the `.env` template into place and fill in `OPENROUTER_API_KEY` + `FRED_API_KEY` (dotenv is loaded by the framework).
5. Copy `watchlist.yaml` into place; verify `trading_enabled: true`.

## Cron + power schedule (host LOCAL timezone — America/Edmonton)

**Important: Ubuntu's cron ignores `CRON_TZ`** (verified 2026-08-31 — jobs silently
run 2h late). Edmonton and New York share DST dates, so the offset is a stable 2
hours year-round: the ET schedule is expressed as local times minus 2h. Cron jobs
`cd` into the repo first (the framework loads `.env` from the working directory).

```cron
# Local (MDT/MST) mapping: 04:00=06:00ET screen | 04:50=06:50ET healthcheck
#                           05:00=07:00ET analyze | 07:00=09:00ET execute
#                           03:50=05:50ET power arm | 08:00=10:00ET power off
50 4 * * 1-5  cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python daily_run.py --healthcheck >> logs/health.log 2>&1
0 4 * * 1-5   cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python screener.py --screen >> logs/screener.log 2>&1
0 5 * * 1-5   cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python daily_run.py --analyze >> logs/cron.log 2>&1
0 7 * * 1-5   cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python daily_run.py --execute >> logs/orders.log 2>&1
50 3 * * 1-5 cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python power_schedule.py --arm >> logs/power.log 2>&1
0 8 * * 1-5 cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python power_schedule.py --shutdown >> logs/power.log 2>&1
@reboot cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python power_schedule.py --arm >> logs/power.log 2>&1
```
Every job `cd`s into the repo first: cron runs commands from the user's home,
and the framework loads `.env` from the working directory — without the `cd`
the API keys would never load. Do NOT add `CRON_TZ=America/New_York`: Ubuntu
cron silently ignores it and everything would run 2h late (verified twice —
2026-08-31 and 2026-09-01 when the power entries were first added in ET).

The 06:00 screen refreshes the momentum ranking **every trading day** before the
07:00 analysis (the scores are deterministic but prices move daily, so a weekly
snapshot goes stale). It's free (yfinance only, no LLM cost), takes ~10 min, and
if it fails the analysis falls back to the last cached pool — never blocks the
run.
The 09:00 execute pass **waits until 09:30 ET** (the open) before submitting
orders — a fill can't happen before the open, and the 60s fill poll would
otherwise cancel pre-open orders. Dry-runs (`--dry-run`) never wait.

### Self-managed power (RTC wake/shutdown) — one-time setup

The machine powers itself off after the run (10:00 ET) and wakes itself before
the next one (05:45 ET) via the RTC alarm, so it is not always on.

1. **BIOS — enable RTC wake (one-time, manual):** on this Gigabyte B550
   (AORUS ELITE AX V2) both relevant settings are already active, but if you
   ever reset the BIOS: enable **"Resume by Alarm" / "Wake on RTC Alarm"**
   and **"Restore on AC Power Loss"** (Power Management section). The RTC
   alarm alone wakes a gracefully-shut-down machine; Restore-on-AC is what
   makes a plug power-cycle boot it.
2. **Passwordless rtcwake for the cron user** (rtcwake writes
   `/sys/class/rtc/rtc0/wakealarm`, root-only):
   ```bash
   echo "harsh-amin ALL=(ALL) NOPASSWD: /usr/sbin/rtcwake" | sudo tee /etc/sudoers.d/rtcwake
   sudo chmod 440 /etc/sudoers.d/rtcwake
   ```
3. Install the three power cron lines above (`--arm` at 05:50 ET, `--shutdown`
   at 10:00 ET, `@reboot --arm`).

**How it works** (all times ET; the alarm is stored as an absolute epoch in the
RTC chip, which runs UTC — immune to the Edmonton/ET confusion):

- 05:45 — RTC alarm wakes the machine (alarm armed the previous day).
- 05:50 — `--arm` (cron 03:50 local) re-arms the alarm for the next weekday.
- 10:00 — `--shutdown` (cron 08:00 local) powers off via `rtcwake -m off`;
  the same command arms the next day's alarm first.
- Any boot — `@reboot --arm` re-arms. **This is load-bearing:** this BIOS
  *clears the armed alarm on any boot* (verified live — plug cuts and even a
  graceful reboot wipe `/sys/class/rtc/rtc0/wakealarm`), so without `@reboot`
  a plug-flip between runs would kill the next morning's wake.

**Verified behaviors (2026-09-01, live on this machine):**
- RTC wake from a graceful shutdown: works (powered off, booted itself on the
  alarm, no plug involved).
- Alarm firing while the machine is already ON: harmless no-op — it fires and
  clears; the 05:50 arm re-arms afterwards either way.
- Plug power-cut while the machine is OFF: safe (nothing running).
- Plug power-cut while the machine is RUNNING: **risky** — this filesystem
  loses recent writes on hard cuts (ext4 delayed allocation). Seen live: a
  plug cut corrupted the repo's `.git` object store and truncated a freshly
  cloned file to 0 bytes. Only ever flip the plug when the machine is fully
  off; prefer `sudo shutdown` + RTC wake. Manual boots are fine — the
  `@reboot` arm covers them.
- Safety checks: `power_schedule.py --arm --dry-run` prints the command and
  target wake time without touching the machine; verify an armed alarm with
  `cat /sys/class/rtc/rtc0/wakealarm` (epoch) →
  `date -u -d @$(cat /sys/class/rtc/rtc0/wakealarm)`.
- Manual power on any time from the Kasa app / `ensure_power.py`; the plug is
  never used to cut power automatically.

## Kill switch
`touch ~/workplace/TradingAgents/DISABLE_TRADING`   # analysis runs, no orders
`rm ~/workplace/TradingAgents/DISABLE_TRADING`      # re-enable

## Smoke test (before trusting cron)
1. `cd ~/workplace/TradingAgents && .venv/bin/python screener.py --screen`   # full weekly screen
2. `.venv/bin/python daily_run.py --analyze --tickers AAPL`           # one-ticker analysis (~25 min; watch the per-ticker structured log appear)
3. `.venv/bin/python daily_run.py --execute --dry-run`                # prints orders, places none
4. `.venv/bin/python daily_run.py --healthcheck`                      # broker (alpaca) reachable
5. `.venv/bin/python power_schedule.py --arm --dry-run`               # prints the wake command/time
6. Watch `logs/` + `~/.tradingagents/logs/structured/` for a full week before enabling real orders.

## Artifacts
- `~/.tradingagents/logs/ratings_YYYY-MM-DD.json` — morning ratings
- `~/.tradingagents/logs/executed_YYYY-MM-DD.json` — order log (idempotency guard)
- `~/.tradingagents/logs/pool_YYYY-WW.json` — weekly candidate pool
- `~/.tradingagents/logs/structured/YYYY-MM-DD/{ticker}.jsonl` — **per-ticker structured log**: one JSON object per line for every LLM turn (agent via `langgraph_node`, model, provider, token usage, latency), plus a `run_end` event with the rating and a per-day `summary.json`
- `~/.tradingagents/logs/reddit_archive_cache/` — Arctic Shift subreddit cache (complete Reddit coverage, no 429s)
- `~/.tradingagents/logs/stocktwits_cache/` — StockTwits retry cache
- `~/.tradingagents/memory/trading_memory.md` — framework decision memory

## Recent behavior changes (2026-09-01)
- **OpenRouter provider pins** (`watchlist.yaml → openrouter_provider_pins`):
  flash → **Relace**, pro → **StreamLake**, with `allow_fallbacks: true` (the
  pin is a preference, not a hard lock — OpenRouter fails over to another
  healthy host when the pinned one 429s; Fireworks' shared pool rate-limited
  the whole run on 2026-09-01, which is what motivated the switch).
- **Buy-quota expansion** (`watchlist.yaml → screener:`): `min_buy_quota: 5`
  keeps analyzing deeper pool candidates until 5 agent-approved buys
  (Buy/Overweight) are found; `max_analyze: 24` caps the cost. Skipped under
  a STRESS regime (buys are paused anyway).
- **Reddit:** the Arctic Shift archive (`reddit_archive.py`) is tried first
  (keyless, complete 3-subreddit coverage, cached per-subreddit, filtered
  locally per ticker); the paced RSS path is now only the fallback. OAuth
  (`REDDIT_CLIENT_ID`/`REDDIT_SECRET`) still upgrades the fallback when set.
- **Capital** in `watchlist.yaml` is 10,000 — the real paper-account cash
  (Alpaca funds paper with $100k, but this account was trimmed to $10k).
