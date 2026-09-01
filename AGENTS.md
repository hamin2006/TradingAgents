# AGENTS.md

Guidance for agent sessions working in this repository.

## What this project is

A fork of **TauricResearch/TradingAgents** (multi-agent LLM trading framework, LangGraph) extended with a daily paper-trading automation layer:

> **Framework base:** upstream **v0.4.0** (merged `2026-08-31`, commit `0e9de89`). Notable upstream surface we consume: memory log entries may carry a trailing `resolved:YYYY-MM-DD` tag (our analytics tolerate it), unparseable ratings surface as `REVIEW` (a safe no-op in `compute_orders`), and a cross-provider `max_tokens` config key exists (tracked as `null` in `watchlist.yaml` for merge cleanliness).

1. A deterministic **S&P 500 momentum screener** generates buy candidates every trading morning (raw-momentum composite + liquidity filter + **regime gate**: SPY-vs-200d-SMA × VIX → CALM/WARN/STRESS — WARN drops the 1m-momentum tail, STRESS pauses new buys; measured via a 5y crash-in-sample backtest, see spec §5bis).
2. A **parallel multi-agent analysis pass** (4 analysts → bull/bear debate → trader → 3-way risk debate → portfolio manager) rates each watchlist ticker Buy/Overweight/Hold/Underweight/Sell.
3. Ratings execute **automatically on an Alpaca paper account** at the 09:30 ET market open, with broker-side entry protection caps and stop-losses.
4. A decision **memory log** records every call with realized returns and LLM reflections; an **analytics script** measures hit rates by rating tier over time.

The user observes the paper portfolio and can pause trading via a kill switch. No live money is ever involved.

## Hard constraints

- **NEVER modify anything under `tradingagents/`** — the framework is consumed as a library. Behavior extensions are runtime patches from our own modules (see `daily_run.py`: RLock for re-entrancy, `_wrapped_original` tagging, lazy `_ensure_*` installers).
- Tests are **hermetic**: no network, no real LLM calls, no real broker (`tests/conftest.py` installs placeholder API keys). Gate: `pytest -q` fully green + `uvx ruff check <files>` (line-length 100, rules E/W/F/I/B/UP/C4/SIM).
- Conventional commits (`feat:` / `fix:` / `docs:`), push to `origin/main`, then deploy = `git pull` on the production PC.
- Review process is deliberately **light** (user preference): spec-based TDD + mechanical gates (tests, ruff, no framework changes). No reviewer subagents.
- Never commit secrets; `.env` is gitignored.
- All schedule/date logic is pinned to `America/New_York` (`ZoneInfo`), never server-local or UTC.

## Current state (modules)

| Module | Role |
|---|---|
| `config.py` | Loads `watchlist.yaml` (user config) merged over framework `DEFAULT_CONFIG` over `APP_DEFAULTS`; unknown keys raise |
| `decisions.py` | Pure decision engine: ratings + holdings → orders. Protection-capped buys, broker-side stop-loss, conviction-scaled sizing (Buy 1.5×, Overweight 1.0×), order-value caps |
| `screener.py` | S&P 500 momentum screen: Wikipedia universe (User-Agent header required), batched yfinance, liquidity filter, vol-adjusted z-score composite, ranked pool |
| `ibkr.py` / `alpaca_broker.py` / `broker.py` | Broker backends, same interface (connect, positions+cash, orders, disconnect). **Alpaca is active** (paper-only, hard-guarded); IBKR kept for a later flip |
| `daily_run.py` | CLI orchestrator: `--analyze` (parallel per-ticker pipeline runs with retry + memory log), `--execute` (kill switch → ratings → holdings → orders → two-phase executed log), `--healthcheck`. Also watchlist assembly (holdings ∪ screened candidates, exclusion window, min-size gate) and runtime patches (memory-log locking, reddit pacing/oauth) |
| `reddit_auth.py` | OAuth Reddit fetcher — inactive until `REDDIT_CLIENT_ID`/`REDDIT_SECRET` are set; falls back to the paced anonymous RSS path. Resilience wrapper (retry + per-ticker cache) applies on both paths |
| `analyze_results.py` | Outcome analytics over the decision memory log (hit rates by rating tier, per-ticker alpha, streaks) |
| `watchlist.yaml` | User-facing config: seed watchlist, models, capital, sizing, screener + broker settings, kill switch |
| `SETUP.md` | Full production setup: keys, broker, cron, smoke tests |

Daily pipeline data flow: `screener` (06:00) → `daily_run --analyze` (07:00, parallel threads, ratings JSON) → `daily_run --execute` (09:00, waits for the 09:30 open, places orders) → artifacts + memory log. Full details in the spec (`docs/superpowers/specs/`) and `SETUP.md`.

## Operational facts

- **Production host:** the user's Ubuntu 24.04 PC (`pc` SSH alias over Tailscale; run commands via `expect ~/.config/opencode/skills/pc-dev/scripts/pc_ssh.exp '<cmd>'`, password auth via `PC_PASSWORD`; power via `ensure_power.py --device "PC Plug"`). Repo lives at `/home/harsh-amin/workplace/TradingAgents`.
- **PC timezone is America/Edmonton** — cron uses `CRON_TZ=America/New_York`; every cron job `cd`s into the repo first (the framework loads `.env` from the working directory).
- **Cron schedule (Mon–Fri):** 06:00 screen → 06:50 healthcheck → 07:00 analyze → 09:00 execute.
- **Artifacts:** `~/.tradingagents/logs/` (ratings/executed/pool JSONs, analysis_report.md), `~/.tradingagents/memory/trading_memory.md` (decision log).
- **Kill switch:** a `DISABLE_TRADING` file at the repo root forces analysis-only mode.
- **Safety chain on execute:** kill switch → ratings-file check → once-per-day idempotency (mark-before-submit log) → capital capped by real account cash → per-day order-value cap → entry protection cap (+2%, cancel if gapped) → GTC stop-loss (−8%) → fill timeout + cancel.
- **Keys in `.env`:** `OPENROUTER_API_KEY`, `FRED_API_KEY`, `ALPACA_API_KEY`/`ALPACA_SECRET` (required/active); `REDDIT_CLIENT_ID`/`SECRET` optional (activates OAuth Reddit).

## Known gotchas

- Reddit's anonymous RSS path is rate-limited (~10 req/min): fetches are paced + retried + cached. Reddit's own 429 retry **re-invokes the module attribute**, so wrappers around it must use `RLock` (plain `Lock` deadlocks).
- Wikipedia 403s requests without a `User-Agent` header; `pd.read_html` needs `io.StringIO` for HTML strings.
- Module names must not collide with pip packages (`alpaca.py` would shadow `alpaca-py` — hence `alpaca_broker.py`).
- Alpaca quirks: `BRACKET` order class requires both legs (use `OTO` for stop-only); entry orders before 09:30 with `extended_hours=False` queue for the open.
- ib_async's `StopOrder` stores the stop price in `auxPrice`; `GetOrdersRequest`+`QueryOrderStatus` for open-order queries.
- The PC's smart plug is controlled via a `tplinkcloud` shim (user site-packages) backed by python-kasa local LAN discovery — TP-Link deprecated the cloud login. Works on the home network only.
- Mac framework Python has broken SSL certs: prefix scripts with `SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")`. The Ubuntu PC is unaffected.
- Paper account cash is **$10,000** (not the $100k config) — execute-time capital is capped by real cash with a warning.

## Pending (known, deferred — do not build unprompted)

- Reddit OAuth credentials: app creation is captcha-blocked (network flag); retry later — the code path activates automatically once creds land in `.env`.
- Drawdown circuit breaker: deferred until ~2 weeks of resolved decisions exist in the memory log (threshold from evidence, not guesswork).
- Screening robustness roadmap (spec §5bis): the 5y crash-in-sample backtest settled the defaults — production = **raw_momentum + regime gate** (vol_adjusted/rank_based remain selectable registry strategies); dual momentum and anti-lottery overlay are deferred pending outcome data.
