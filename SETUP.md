# Daily Paper-Trading Setup (Ubuntu 24.04)

## One-time install
1. `sudo apt install -y python3.12 python3.12-venv cron`
2. `git clone <repo> /opt/tradingagents && cd /opt/tradingagents`
3. `python3.12 -m venv .venv && .venv/bin/pip install . ib_async pyyaml`
4. Install IB Gateway (paper), enable API access, paper account login on port 7497.
5. Create `.env` with `OPENROUTER_API_KEY` and `FRED_API_KEY` (dotenv is loaded by the framework).
6. Copy `watchlist.yaml` into place; verify `trading_enabled: true`.

## Cron (CRON_TZ avoids DST bugs)
Run `crontab -e` and add:
```cron
CRON_TZ=America/New_York
50 6 * * 1-5  /opt/tradingagents/.venv/bin/python /opt/tradingagents/daily_run.py --healthcheck >> /opt/tradingagents/logs/health.log 2>&1
0 18 * * 0    /opt/tradingagents/.venv/bin/python /opt/tradingagents/screener.py --screen >> /opt/tradingagents/logs/screener.log 2>&1
0 7 * * 1-5   /opt/tradingagents/.venv/bin/python /opt/tradingagents/daily_run.py --analyze >> /opt/tradingagents/logs/cron.log 2>&1
0 9 * * 1-5   /opt/tradingagents/.venv/bin/python /opt/tradingagents/daily_run.py --execute >> /opt/tradingagents/logs/orders.log 2>&1
```

## Kill switch
`touch /opt/tradingagents/DISABLE_TRADING`   # analysis runs, no orders
`rm /opt/tradingagents/DISABLE_TRADING`      # re-enable

## Smoke test (before trusting cron)
1. `cd /opt/tradingagents && .venv/bin/python screener.py --screen`   # full weekly screen
2. `.venv/bin/python daily_run.py --analyze --tickers AAPL`           # one-ticker analysis
3. `.venv/bin/python daily_run.py --execute --dry-run`                # prints orders, places none
4. `.venv/bin/python daily_run.py --healthcheck`                      # IBKR reachable
5. Watch `logs/` for a full week before enabling real orders.

## Artifacts
- `~/.tradingagents/logs/ratings_YYYY-MM-DD.json` — morning ratings
- `~/.tradingagents/logs/executed_YYYY-MM-DD.json` — order log (idempotency guard)
- `~/.tradingagents/logs/pool_YYYY-WW.json` — weekly candidate pool
- `~/.tradingagents/memory/trading_memory.md` — framework decision memory
