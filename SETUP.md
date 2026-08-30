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
> for the regular session (`extended_hours=false`), so market orders submitted
> at 09:00 queue for the 09:30 open; buys carry a limit at `prev_close × 1.02`
> and are cancelled if the open gaps beyond it — never overpaid. This mirrors
> the IBKR path's protection-cap semantics.

Verify all keys: `cd ~/workplace/TradingAgents && .venv/bin/python daily_run.py --analyze --tickers AAPL`
should start printing per-agent analysis.

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

## Cron (CRON_TZ avoids DST bugs)
Run `crontab -e` and add:
```cron
CRON_TZ=America/New_York
50 6 * * 1-5  /home/harsh-amin/workplace/TradingAgents/.venv/bin/python /home/harsh-amin/workplace/TradingAgents/daily_run.py --healthcheck >> /home/harsh-amin/workplace/TradingAgents/logs/health.log 2>&1
0 6 * * 1-5   /home/harsh-amin/workplace/TradingAgents/.venv/bin/python /home/harsh-amin/workplace/TradingAgents/screener.py --screen >> /home/harsh-amin/workplace/TradingAgents/logs/screener.log 2>&1
0 7 * * 1-5   /home/harsh-amin/workplace/TradingAgents/.venv/bin/python /home/harsh-amin/workplace/TradingAgents/daily_run.py --analyze >> /home/harsh-amin/workplace/TradingAgents/logs/cron.log 2>&1
0 9 * * 1-5   /home/harsh-amin/workplace/TradingAgents/.venv/bin/python /home/harsh-amin/workplace/TradingAgents/daily_run.py --execute >> /home/harsh-amin/workplace/TradingAgents/logs/orders.log 2>&1
```
The 06:00 screen refreshes the momentum ranking **every trading day** before the
07:00 analysis (the scores are deterministic but prices move daily, so a weekly
snapshot goes stale). It's free (yfinance only, no LLM cost), takes ~10 min, and
if it fails the analysis falls back to the last cached pool — never blocks the
run.
The 09:00 execute pass **waits until 09:30 ET** (the open) before submitting
orders — a fill can't happen before the open, and the 60s fill poll would
otherwise cancel pre-open orders. Dry-runs (`--dry-run`) never wait.

## Kill switch
`touch ~/workplace/TradingAgents/DISABLE_TRADING`   # analysis runs, no orders
`rm ~/workplace/TradingAgents/DISABLE_TRADING`      # re-enable

## Smoke test (before trusting cron)
1. `cd ~/workplace/TradingAgents && .venv/bin/python screener.py --screen`   # full weekly screen
2. `.venv/bin/python daily_run.py --analyze --tickers AAPL`           # one-ticker analysis
3. `.venv/bin/python daily_run.py --execute --dry-run`                # prints orders, places none
4. `.venv/bin/python daily_run.py --healthcheck`                      # IBKR reachable
5. Watch `logs/` for a full week before enabling real orders.

## Artifacts
- `~/.tradingagents/logs/ratings_YYYY-MM-DD.json` — morning ratings
- `~/.tradingagents/logs/executed_YYYY-MM-DD.json` — order log (idempotency guard)
- `~/.tradingagents/logs/pool_YYYY-WW.json` — weekly candidate pool
- `~/.tradingagents/memory/trading_memory.md` — framework decision memory
