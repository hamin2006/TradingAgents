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
| `daily_run.py` | CLI orchestrator: `--analyze` (parallel per-ticker pipeline runs with retry + memory log + per-ticker structured logging), `--execute` (kill switch → ratings → holdings → orders → two-phase executed log), `--healthcheck`. Also watchlist assembly (holdings ∪ screened candidates, exclusion window, min-size gate), buy-quota expansion (`min_buy_quota`/`max_analyze`), and runtime patches (memory-log locking, reddit pacing/oauth, structured-log callbacks) |
| `reddit_auth.py` | OAuth Reddit fetcher — inactive until `REDDIT_CLIENT_ID`/`REDDIT_SECRET` are set; falls back to the paced anonymous RSS path. Resilience wrapper (retry + per-ticker cache) applies on both paths |
| `reddit_archive.py` | Keyless Arctic Shift archive pull (complete subreddit coverage, per-sub cache, local ticker filter); archive-first wrapper with RSS fallback |
| `structured_log.py` | Per-ticker structured JSONL logging of the analyze run: every LLM turn (agent via `langgraph_node` metadata, model, provider, token usage, latency), chain boundaries, errors; `run_end` event + per-day `summary.json` |
| `power_schedule.py` | Self-managed RTC power: `--arm` (set next-weekday 04:00 ET alarm, stay on), `--shutdown` (arm + power off via `rtcwake -m off` via passwordless sudo); `@reboot` cron re-arms because this BIOS clears alarms on any boot |
| `analyze_results.py` | Outcome analytics over the decision memory log (hit rates by rating tier, per-ticker alpha, streaks) |
| `watchlist.yaml` | User-facing config: seed watchlist, models, provider pins, capital (10k), sizing, screener + buy-quota + broker settings, kill switch |
| `SETUP.md` | Full production setup: keys, broker, cron + power schedule, smoke tests |

Daily pipeline data flow: `screener` (04:10 ET) → `daily_run --analyze` (04:30 ET, parallel threads, ratings JSON) → `daily_run --execute` (09:00 ET, waits for the 09:30 open, places orders) → artifacts + memory log. Full details in the spec (`docs/superpowers/specs/`) and `SETUP.md`.

## Operational facts

- **Production host:** the user's Ubuntu 24.04 PC (`pc` SSH alias over Tailscale; run commands via `expect ~/.config/opencode/skills/pc-dev/scripts/pc_ssh.exp '<cmd>'`, password auth via `PC_PASSWORD`; power via `ensure_power.py --device "PC Plug"`). Repo lives at `/home/harsh-amin/workplace/TradingAgents`.
- **PC timezone is America/Edmonton** — cron runs in host-local time (Ubuntu ignores `CRON_TZ`; ET = local + 2h year-round); every cron job `cd`s into the repo first (the framework loads `.env` from the working directory).
- **Cron schedule (Mon–Fri, local):** 02:05 power arm → 02:10 screen → 02:25 healthcheck → 02:30 analyze → 07:00 execute → 08:00 power off; `@reboot` re-arms. RTC alarm wakes the machine at 04:00 ET (= 02:00 local). The morning chain is early so a full `max_analyze` batch (~3h, 4 workers) lands before the 09:00 ET execute checkpoint.
- **Power:** self-managed via `power_schedule.py` + RTC alarm; the PC shuts itself down at 10:00 ET and wakes at 04:00 ET. Manual wake anytime via the Kasa plug (`ensure_power.py --device "PC Plug"`).
- **Artifacts:** `~/.tradingagents/logs/` (ratings/executed/pool JSONs, analysis_report.md, structured/{date}/{ticker}.jsonl + summary.json), `~/.tradingagents/memory/trading_memory.md` (decision log).
- **Kill switch:** a `DISABLE_TRADING` file at the repo root forces analysis-only mode.
- **Safety chain on execute:** kill switch → ratings-file check → once-per-day idempotency (mark-before-submit log) → capital capped by real account cash → per-day order-value cap → entry protection cap (+5%, cancel if gapped up) → two-step GTC stop-loss (−8%) → gap-down undo (fill at/below stop = sell back) → fill timeout + cancel.
- **Keys in `.env`:** `OPENROUTER_API_KEY`, `FRED_API_KEY`, `ALPACA_API_KEY`/`ALPACA_SECRET` (required/active); `REDDIT_CLIENT_ID`/`SECRET` optional (activates OAuth Reddit).

## Known gotchas

- Reddit's anonymous RSS path is rate-limited (~10 req/min): fetches are paced + retried + cached. Reddit's own 429 retry **re-invokes the module attribute**, so wrappers around it must use `RLock` (plain `Lock` deadlocks).
- Wikipedia 403s requests without a `User-Agent` header; `pd.read_html` needs `io.StringIO` for HTML strings.
- Module names must not collide with pip packages (`alpaca.py` would shadow `alpaca-py` — hence `alpaca_broker.py`).
- Alpaca quirks: `BRACKET` order class requires both legs (use `OTO` for stop-only); entry orders before 09:30 with `extended_hours=False` queue for the open. **Do NOT use OTO for buys**: the paper engine inverts the leg creation at the open and entries never fill (verified live 2/2 — IT 8/31, CRWD 9/1). Buys are two-step: plain capped limit entry → poll fill → attach GTC stop. If the fill is at/below the stop level (gapped through the stop), the entry is sold back immediately (gap-down guard).
- **This Gigabyte B550 BIOS clears the armed RTC alarm on ANY boot** (plug cut or graceful reboot) — the `@reboot` cron re-arms (`power_schedule.py --arm`); without it a plug-flip between runs kills the next morning's wake.
- **Hard plug cuts corrupt recent filesystem writes** (ext4 loses dirty pages — seen live: `.git` object store destroyed, a freshly cloned file truncated to 0 bytes). Only flip the plug when the machine is fully off; never trust a repo state across a cut without `sync` + `git fsck`.
- **Ubuntu cron ignores `CRON_TZ`** (verified twice) — all crontab times are host-local America/Edmonton (ET + 2h year-round, both zones share DST dates). The crontab header comment carries the mapping; keep power entries in local time too.
- `daily_run.py` had **no logging config** — INFO was silently dropped; only WARNING+ surfaced via Python's last-resort handler. Per-ticker structured logs now exist at `~/.tradingagents/logs/structured/{date}/{ticker}.jsonl` (agent attribution via LangGraph's `metadata['langgraph_node']`, NOT chain events — `on_chain_start` never fires in this graph). Tool-style events don't fire for pre-fetched data (reddit/stocktwits are plain function calls, not LangChain tools).
- ib_async's `StopOrder` stores the stop price in `auxPrice`; `GetOrdersRequest`+`QueryOrderStatus` for open-order queries.
- The PC's smart plug is controlled via a `tplinkcloud` shim (user site-packages) backed by python-kasa local LAN discovery — TP-Link deprecated the cloud login. Works on the home network only.
- Mac framework Python has broken SSL certs: prefix scripts with `SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")`. The Ubuntu PC is unaffected.
- Paper account cash is **$10,000** (not the $100k config) — execute-time capital is capped by real cash with a warning.

## Pending (known, deferred — do not build unprompted)

- Reddit OAuth credentials: app creation is captcha-blocked (network flag); retry later — the code path activates automatically once creds land in `.env`.
- Drawdown circuit breaker: deferred until ~2 weeks of resolved decisions exist in the memory log (threshold from evidence, not guesswork).
- Screening robustness roadmap (spec §5bis): the 5y crash-in-sample backtest settled the defaults — production = **raw_momentum + regime gate** (vol_adjusted/rank_based remain selectable registry strategies); dual momentum and anti-lottery overlay are deferred pending outcome data.
- Structured log shows `provider_used: "openai"` (OpenRouter's platform name in `model_provider`), not the actual host (Relace vs fallback) — needs OpenRouter response-header parsing; and pre-fetched data sources (reddit/stocktwits/news) aren't logged as distinct events (plain function calls, not LangChain tools) — could emit structured events from the resilience wrappers directly.
