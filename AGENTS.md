# AGENTS.md

Guidance for agent sessions working in this repository.

## What this project is

A fork of **TauricResearch/TradingAgents** (multi-agent LLM trading framework, LangGraph) extended with a daily paper-trading automation layer:

> **Framework base:** upstream **v0.4.0** (merged `2026-08-31`, commit `0e9de89`). Notable upstream surface we consume: memory log entries may carry a trailing `resolved:YYYY-MM-DD` tag (our analytics tolerate it), unparseable ratings surface as `REVIEW` (a safe no-op in `compute_orders`), and the cross-provider `max_tokens` config key is **active at 15000** to bound upstream #1204 (deepseek-v4-flash deployments emit unbounded reasoning/output on complex prompts and appear "hung" — verified live 2026-09-03).

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
| `ibkr.py` / `alpaca_broker.py` / `broker.py` | Broker backends, same interface (connect, positions+cash, orders, disconnect). **Alpaca is active** (paper-only, hard-guarded); IBKR kept for a later flip. `alpaca_broker.get_position_details()` (optional addition) exposes avg entry prices for the portfolio snapshot |
| `daily_run.py` | CLI orchestrator: `--analyze` (parallel per-ticker pipeline runs with retry + memory log + per-ticker structured logging), `--execute` (kill switch → ratings → holdings → orders → two-phase executed log), `--healthcheck`. Also watchlist assembly (holdings ∪ screened candidates, exclusion window, min-size gate), buy-quota expansion (`min_buy_quota`/`max_analyze`), and a large runtime-patch installer chain run at analyze start: memory-log locking, reddit pacing/oauth, reddit archive, stocktwits resilience, graph tool-callbacks (`get_graph_args`), news pre-fetch logging, **FRED alias map + tool-description disclosure** (`_ensure_fred_aliases`), **structured-fallback logging + header-only rating guard** (`_ensure_structured_fallback_logging`/`_header_rating`, F3), **analyst report recovery** (`_ensure_analyst_report_recovery`, F7), **portfolio-context stance/book-shape injection** (`_ensure_portfolio_context`, phantom fix), and **converter-level reasoning capture** (`_ensure_reasoning_capture` — patches `langchain_openai...base._convert_dict_to_message` so OpenRouter's `reasoning` extra survives into `additional_kwargs`). Every installer is idempotent with a `_reset_*` helper for tests |
| `reddit_auth.py` | OAuth Reddit fetcher — inactive until `REDDIT_CLIENT_ID`/`REDDIT_SECRET` are set; falls back to the paced anonymous RSS path. Resilience wrapper (retry + per-ticker cache) applies on both paths |
| `reddit_archive.py` | Keyless Arctic Shift archive pull (complete subreddit coverage, per-sub cache, local ticker filter); archive-first wrapper with RSS fallback |
| `news_dating.py` | Dated news rendering: mirrors the yfinance news feed but keeps each article's `published YYYY-MM-DD` (the upstream feed extracts pub_dates then drops them at render — the 2026-09-03 audit's stale-news root cause) and prepends a memoized **verified-snapshot anchor header** (last close + date) telling the News Analyst to treat conflicting price claims as stale. `daily_run._ensure_news_dating` swaps the shared news Tool `.func`s (install BEFORE `_ensure_news_logging` so the logging wrapper stays outermost) |
| `edgar.py` | SEC EDGAR client (keyless): CIK map, per-CIK companyfacts + submissions with disk cache, paced HTTP (≥1s, RLock), **as-of semantics** (`filed <= as-of`, amendment dedupe by latest filed), fiscal-quarter TTM math, tag-fallback chains, computed metrics. `Facts.quarters/ttm/latest_instant/shares_outstanding` are the primitives the fundamentals renderers use |
| `fundamentals_edgar.py` | EDGAR-backed fundamentals renderers for the four tools. **Composition rule (single source per quantity):** statements/metrics from companyfacts (as-filed); consensus-only from yfinance quote (forward EPS/targets/dividend/sector, date-labeled); market cap/PE computed from EDGAR shares × our snapshot close; quote-price fields (50/200d, 52-week) **absent**. Config-gated (`fundamentals_source: edgar`, default yfinance); `payload_for`/`statements_for` raise `EdgarError` so the installer falls back to the yfinance originals |
| `corp_events.py` | Form 4 watcher + 8-K listing from EDGAR submissions (10-day window, memoized per ticker+day): deterministic edgardoc.xml parse (real shape = Guarini/McCourt 09-03), cashless M→S collapse ("EXERCISED 400 options @ $719.37 then SOLD 400 @ $850.00 ($340,000)"), 8-K lines with accession. Failure-safe ("" on any error) |
| `earnings_metrics.py` | 8-K earnings-release metrics: locates the latest earnings 8-K, fetches the release (exhibit-99 preferred), **one cached LLM structured extraction per filing** (period/revenue/EPS/guidance — cached by accession so daily re-analysis is free), source-dated. This is the structural home for the non-GAAP/guidance numbers the audit found riding unstructured news |
| `market_tape.py` | Regime/tape context line: SPY close vs 200d SMA, VIX, sector-ETF change (GICS→XL* map). Per-clause failure-safe, memoized 600s. Injected with the events block via `_ensure_tape_and_events` (same `resolve_instrument_context` seam as the stance, chained after it — every agent sees the block) |
| `structured_log.py` | Per-ticker structured JSONL logging of the analyze run: every LLM turn (agent via `langgraph_node` metadata, model, provider, token usage, latency, `finish_reason`), **untruncated** prompts/responses, **structured-output payloads** (`tool_calls` field when response text is empty — Sentiment/Trader/RM/PM decisions), chain boundaries, tool calls (args), pre-fetch events (`fetch_end`), structured-fallback events, errors; `run_end` event + per-day `summary.json` |
| `power_schedule.py` | Self-managed RTC power: `--arm` (set next-weekday 04:00 ET alarm, stay on), `--shutdown` (arm + power off via `rtcwake -m off` via passwordless sudo); `@reboot` cron re-arms because this BIOS clears alarms on any boot |
| `analyze_results.py` | Outcome analytics over the decision memory log (hit rates by rating tier, per-ticker alpha, streaks) |
| `watchlist.yaml` | User-facing config: seed watchlist, models, provider pins, capital (10k), `max_tokens: 15000`, `fundamentals_source` (edgar/yfinance), sizing, screener + buy-quota (`min_buy_quota: 5`, `max_analyze: 16`) + broker settings, kill switch |
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
- **Dev/verification runs must be isolated** from production artifacts: launch with `TRADINGAGENTS_RESULTS_DIR`, `TRADINGAGENTS_MEMORY_LOG_PATH`, and `STRUCTURED_LOG_DIR` pointed at scratch dirs (else the run pollutes `structured/{date}/`, the memory log's exclusion window, and ratings). Background-launch through `pc_ssh.exp` needs `setsid bash -c '...' </dev/null >/dev/null 2>&1 &` — a plain `&` or `nohup` dies with the SSH session or hangs it; long remote pythons lose pipe output (write results to a file, read it back). `pgrep -f` self-matches inside `expect` wrapper commands (the pattern appears in the wrapper's own cmdline) — use `ps aux | grep` instead.
- **The PC is unreachable after 10:00 ET** (self-shutdown) — don't retry SSH; power on via `ensure_power.py --device "PC Plug"` and wait ~20s, then poll SSH.
- **A graceful shutdown anytime is safe for the next batch** — the armed RTC alarm (one-shot, absolute epoch) wakes the machine at the next 04:00 ET. A plug cut while running is NOT safe (ext4).

## Known gotchas

- Reddit's anonymous RSS path is rate-limited (~10 req/min): fetches are paced + retried + cached. Reddit's own 429 retry **re-invokes the module attribute**, so wrappers around it must use `RLock` (plain `Lock` deadlocks).
- Wikipedia 403s requests without a `User-Agent` header; `pd.read_html` needs `io.StringIO` for HTML strings.
- Module names must not collide with pip packages (`alpaca.py` would shadow `alpaca-py` — hence `alpaca_broker.py`).
- Alpaca quirks: `BRACKET` order class requires both legs (use `OTO` for stop-only); entry orders before 09:30 with `extended_hours=False` queue for the open. **Do NOT use OTO for buys**: the paper engine inverts the leg creation at the open and entries never fill (verified live 2/2 — IT 8/31, CRWD 9/1). Buys are two-step: plain capped limit entry → poll fill → attach GTC stop. If the fill is at/below the stop level (gapped through the stop), the entry is sold back immediately (gap-down guard). **Fill polling must count `partially_filled` as a fill and re-query once after the deadline** — verified live 2026-09-03 (EL 8/9 partial + REGN fill at +59s were both misread as "no fill": orders cancelled, positions left WITHOUT stops, log at filled 0). Stops and undos size to the FILLED qty, never the intended qty.
- **This Gigabyte B550 BIOS clears the armed RTC alarm on ANY boot** (plug cut or graceful reboot) — the `@reboot` cron re-arms (`power_schedule.py --arm`); without it a plug-flip between runs kills the next morning's wake.
- **Hard plug cuts corrupt recent filesystem writes** (ext4 loses dirty pages — seen live: `.git` object store destroyed, a freshly cloned file truncated to 0 bytes). Only flip the plug when the machine is fully off; never trust a repo state across a cut without `sync` + `git fsck`.
- **Ubuntu cron ignores `CRON_TZ`** (verified twice) — all crontab times are host-local America/Edmonton (ET + 2h year-round, both zones share DST dates). The crontab header comment carries the mapping; keep power entries in local time too.
- `daily_run.py` had **no logging config** — INFO was silently dropped; only WARNING+ surfaced via Python's last-resort handler. Per-ticker structured logs now exist at `~/.tradingagents/logs/structured/{date}/{ticker}.jsonl` (agent attribution via LangGraph's `metadata['langgraph_node']`, NOT chain events — `on_chain_start` never fires in this graph). Pre-fetched data (reddit/stocktwits/news) IS now logged via `fetch_end` events emitted by the resilience wrappers + the news-func wrapper.
- **deepseek-v4-flash-0731 "stalls" are NOT provider outages — they're unbounded runaway output** (upstream #1204): uncapped, the model emits 100K+ token responses on complex analytical prompts (~88 tok/s → 20+ min single calls that look hung). Reproduces via raw API on any host; `max_tokens` is the cure (measured 2026-09-03: the pathological 36K-char prompt completed in 85s at cap=8000 with full content). `max_tokens: 15000` in watchlist bounds it. Cap probes also proved completion tokens INCLUDE reasoning (~70/30 split), so large "output token" counts are mostly thinking.
- **Portfolio-context injection (phantom fix) needs a broker snapshot** — `_portfolio_snapshot(cfg)` fetches the real account (memoized, 600s TTL); on broker failure NO stance/shape is injected (never assert a wrong book). The empty-cache sentinel is `None`, never `0.0` (monotonic starts near zero on fresh boots).
- **The decision-tail wrappers (book shape) and analyst-recovery wrappers re-enter `tradingagents.graph.setup` factories** — tests must `_reset_*` before swapping in fakes (reset order matters: reset first, then patch, then install).
- ib_async's `StopOrder` stores the stop price in `auxPrice`; `GetOrdersRequest`+`QueryOrderStatus` for open-order queries.
- The PC's smart plug is controlled via a `tplinkcloud` shim (user site-packages) backed by python-kasa local LAN discovery — TP-Link deprecated the cloud login. Works on the home network only.
- Mac framework Python has broken SSL certs: prefix scripts with `SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")`. The Ubuntu PC is unaffected.
- Paper account cash is **$10,000** (not the $100k config) — execute-time capital is capped by real cash with a warning.

## Pending (known, deferred — do not build unprompted)

- Reddit OAuth credentials: app creation is captcha-blocked (network flag); retry later — the code path activates automatically once creds land in `.env`.
- **Catalyst-aware screening** (idea backlogged 2026-09-04 from the HOOD case: +16.6% on 9/3 after a public 9/1 Morgan Stanley upgrade; the rank-based price screen couldn't surface it until 9/4): a news/upgrade-aware boost in the screener (e.g., analyst-upgrade promotion, news-flow momentum) would catch upgrade-driven moves 1–2 days sooner. Spec separately if pursued — do not build unprompted.
- Drawdown circuit breaker: deferred until ~2 weeks of resolved decisions exist in the memory log (threshold from evidence, not guesswork).
- Screening robustness roadmap (spec §5bis): the 5y crash-in-sample backtest settled the defaults — production = **raw_momentum + regime gate** (vol_adjusted/rank_based remain selectable registry strategies); dual momentum and anti-lottery overlay are deferred pending outcome data.
- **Reasoning capture resolved 2026-09-03** — root cause: langchain_openai 1.6 reaches the SDK via `chat.completions.with_raw_response.parse` (never `Completions.create`, so the old SDK-patch seam could not fire) and `_convert_dict_to_message` drops the extra `reasoning` key. The installer now patches the converter (reasoning lands in `additional_kwargs["reasoning_content"]`, read by `_reasoning_of`). Verified live end-to-end. **Note: reasoning presence is host/provider-variant** — some responses carry no `reasoning` extra at all, so a 0-reasoning event can be legitimate.
- Structured log shows `provider_used: "openai"` (OpenRouter's platform name in `model_provider`), not the actual host (Relace vs fallback) — needs OpenRouter response-header parsing.
- F4 researcher output bound (prompt-discipline proxy on bull/bear): size from batch data (finish_reason + reasoning split now logged) before implementing.
- F6 config experiment: `max_debate_rounds`/`max_risk_discuss_rounds` (currently 1 = 2 speeches + 3 risk takes) — A/B on dev before changing production.
- Docs `docs/superpowers/specs/` carry the dispositions: `2026-09-02-phantom-portfolio-positions-fix.md` (implemented, live-verified on SJM/CRWD 2026-09-03) and `2026-09-02-framework-architecture-review.md` (F1 implemented, F2 closed as deliberate upstream design, F3/F7 resolved, F4/F6 levers, F5/F8 accepted).
