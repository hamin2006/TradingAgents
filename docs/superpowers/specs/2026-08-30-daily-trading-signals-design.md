# Daily Paper-Trading Signals System — Design

Date: 2026-08-30 (rev. 2: automated IBKR paper-trading execution; rev. 3: autonomous watchlist curation; rev. 4: ranked pool queue + protection-capped entry orders)
Status: Draft
Framework: TradingAgents v0.3.1 (cloned repo in this directory, used as a library, NOT modified)

## 1. Purpose

A daily, automated system that runs the TradingAgents multi-agent analysis pipeline once per
trading day for a fixed watchlist of US equities, then **executes the resulting signals
automatically on an Interactive Brokers (IBKR) paper-trading account**. The system decides
both entries (Buy/Overweight ratings) and exits (Sell/Underweight ratings on held positions).

The user observes the paper portfolio in TWS/IBKR and can pause the system at any time via a
kill switch. No live money is ever involved.

## 2. Requirements

- Runs once per trading day, Mon–Fri. Analysis runs 07:00 ET; **orders are placed at the
  09:30 ET market open**.
- **Autonomous watchlist curation**: each morning's watchlist is assembled automatically =
  all IBKR holdings (so sells are always evaluated) ∪ 2–3 top-ranked candidates from a
  weekly S&P 500 momentum screen (see §5bis). The watchlist must contain **at least 5
  tickers before any production run** (test mode: 1); a run that cannot meet the minimum
  fails loudly. A static seed list in `watchlist.yaml` covers the first run before any
  screen exists.
- **Fully automated execution** on the IBKR paper account via `ib_async` (maintained
  successor to `ib_insync`) talking to IB Gateway running on the VM.
- **Position tracking is owned by IBKR** — holdings are read from the account at run start;
  the system keeps no separate portfolio bookkeeping.
- **Entry rule**: a ticker not currently held gets a buy order at the market open when
  today's rating is Buy or Overweight. Buys are **MKT orders with a protection cap**
  (aux price = previous close × 1.02): filled at the open unless the stock gaps up more
  than 2%, in which case the order is cancelled rather than overpaid. **Sell orders are
  plain MKT at the open** (clean exit, no cap).
- **Exit rule**: a held position is sold at the open when today's rating is Sell or
  Underweight. Rating-based only — no intraday monitoring, no stop-loss/target/time rules
  (deferred, see §3).
- **Sizing**: equal-weight. Config paper capital (default $100,000) split evenly across
  `max_positions` (default 10, independent of watchlist size); whole shares only (floor
  division at the last close price).
- **Safety**: a kill-switch file disables execution (analysis still runs); a max order value
  cap; execution refused if ratings cannot be computed.
- Cheap LLM inference via OpenRouter (2-tier model split; see §7).
- Data vendors are the framework's free defaults (yfinance / FRED / Polymarket) — $0 data
  cost; the only recurring cost is LLM inference.
- One ticker's failure must not kill the run or the day's decisions.
- Every order and fill is logged locally for audit (replaces the email brief).

## 3. Out of scope (explicitly deferred)

- Email briefs and push notifications (removed from v1).
- LLM news-vetting of screening candidates (v1 uses pure deterministic momentum screening;
  an LLM overlay can be added later if the pure screen underperforms).
- NASDAQ-100 / larger candidate universes (v1 screens the S&P 500 only).
- Daily-frequency screening (v1 screens weekly with cached universe + scores).
- Stop-loss / take-profit / time-stop exits, rebalancing, averaging into positions,
  fractional shares, options, shorting.
- Live (real-money) execution.
- Cross-ticker ranking beyond the entry rule's ordering.
- Alpha Vantage / paid data vendors.
- Modifying the `tradingagents/` framework package itself.

## 4. Architecture

Six new files at the repo root; the framework is used exactly as its README shows
(`TradingAgentsGraph(config).propagate(ticker, date)`). No framework changes.

```
daily_run.py          # orchestrator: connect IBKR → fetch holdings → assemble watchlist → analyze → decide → execute → log
ibkr.py               # IBKR wrapper: connect to Gateway, positions/cash, place MKT orders, risk checks
screener.py           # S&P 500 universe fetch + momentum scoring → weekly candidate pool
watchlist.yaml        # user-facing config: seed list, models, capital, sizing, screener, execution settings
tests/test_daily_run.py   # decision-logic + watchlist-assembly unit tests (no broker)
tests/test_ibkr.py        # broker-layer tests with a mocked ib_async client
tests/test_screener.py    # screener scoring/ranking/exclusion tests
```

Component responsibilities:

- **daily_run.py**
  - Loads `watchlist.yaml` and merges it over the framework's `DEFAULT_CONFIG`
    (framework `TRADINGAGENTS_*` env overrides still win).
  - Determines the analysis date as "today" in `America/New_York` (server TZ irrelevant).
  - Assembles the day's watchlist: IBKR holdings ∪ top candidates from the screener pool
    (see §5bis), with the min-5 gate applied.
  - For each ticker in order: create `TradingAgentsGraph`, call
    `propagate(ticker, today)`; wrap in try/except; on failure log the reason and retry
    once, then continue.
  - Extracts the rating from each returned decision.
  - Calls the decision engine (see §5) with today's ratings + current IBKR holdings to
    produce the order list.
  - Waits until 09:30 ET, places the orders via `ibkr.py`, logs fills.
  - Writes `daily_order_log_YYYY-MM-DD.md` and appends to `results_dir/trading_history.md`.
- **ibkr.py**
  - `connect()` — ib_async `IB()` connected to local IB Gateway (paper port 7497,
    configurable); retries with backoff.
  - `get_positions_and_cash()` — account summary + positions (the single source of truth
    for holdings).
  - `place_market_orders(orders)` — maps internal orders to `Stock` contracts + MKT orders,
    submits, waits for fills (or cancels on timeout), returns fill report.
  - Risk guards: max order value cap (config), refuse if account connection lost, refuse
    if more than `max_positions` buys would be placed in one day.
  - All actions are idempotent-safe: the order log is consulted before placing, so a
    crashed-then-restarted run cannot double-buy.
- **watchlist.yaml**
  - `seed_watchlist` (static fallback list for the first run before any screen exists),
    `llm_provider`, `quick_think_llm`, `deep_think_llm`, `output_language`.
  - `capital`, `max_positions` (default 10), `max_order_value_cap`.
  - `screener`: `universe` ("sp500"), `pool_size` (default 50 — a ranked queue, see
    §5bis), `candidate_slots` (default 3), `min_watchlist_size` (default 5, test mode 1),
    `exclusion_days` (default 7), `entry_protection_pct` (default 2.0).
  - `ibkr`: `host`, `port` (default 7497 paper), `client_id`.
  - Kill switch: `trading_enabled: true` + the file `DISABLE_TRADING` in the repo root
    overrides it to false at runtime.
- **screener.py**
  - `fetch_universe()` — S&P 500 constituents (Wikipedia table, verified reachable),
    cached to `results_dir/universe_sp500.json`, refreshed weekly (cache TTL config).
  - `score_universe()` — liquidity filter (avg dollar volume ≥ $10M) then momentum
    z-score composite (1m/3m/6m trailing returns + price-vs-50d-SMA + proximity to 52w
    high), ranking the filtered universe.
  - `build_pool()` — persists the **ranked queue**: every scored ticker (not just a top-10
    cut) plus their scores, ordered by score, to `results_dir/pool_YYYY-WW.json`
    (week-keyed). The daily run draws from the top of this queue (§5bis), so a 3/day draw
    with a 7-day exclusion can never exhaust the week's candidates.
  - Failure mode: if the weekly screen dies, the daily run falls back to the last cached
    pool with a warning — never blocks Monday's analysis.

## 5bis. Watchlist curation

**Daily screen (06:00 ET Mon–Fri, `screener.py --screen`):**
1. Fetch S&P 500 constituents (Wikipedia table; cached weekly in
   `results_dir/universe_sp500.json`; fail-open to last cached universe).
2. Download ~6 months of daily OHLCV for the universe in a single batch
   (`yf.download`), cache to disk.
3. Liquidity filter first: average dollar volume ≥ $10M (tradability guard).
4. Momentum composite (deterministic, $0): z-scores of 1m / 3m / 6m trailing returns,
   price vs 50-day SMA spread, and proximity to the 52-week high, summed per ticker.
5. Persist the **full ranked queue** (all scored tickers, ordered by score) to
   `results_dir/pool_YYYY-WW.json`. `pool_size` (default 50) caps how deep the daily
   run may draw from; with 3 candidates/day and a 7-day exclusion the queue cannot be
   exhausted mid-week.

**Daily assembly (`daily_run.py --analyze`, before analysis):**
1. `candidates = top candidate_slots (default 3) pool members`, drawn from the ranked
   queue in score order, excluding:
   - tickers currently held in IBKR,
   - tickers analyzed within the last `exclusion_days` (default 7) — prevents churn,
   - tickers the memory log shows rated Sell/Underweight in the last 7 days.
2. `watchlist_today = holdings ∪ candidates`.
3. Minimum gate: if `watchlist_today` size < `min_watchlist_size` (default 5 in
   production, 1 in test mode), top up from the next pool members; if the queue is
   exhausted and the gate still fails, abort the run loudly (no partial execution).
4. First run before any pool exists: use `seed_watchlist` from `watchlist.yaml`.

**Why momentum:** the Jegadeesh–Titman (1993) result — buying recent winners persists —
is the canonical, well-documented basis for candidate generation; liquidity filtering
first is the standard safeguard against ranking into untradeable names.

**Screening robustness roadmap** (research report:
`.superpowers/sdd/research-screening-methods.md`; evidence: Daniel–Moskowitz 2016
"Momentum Crashes", Barroso–Santa-Clara 2015 volatility-managed momentum, Antonacci
dual momentum, Bali et al. MAX effect). Momentum ranking is regime-fragile — in
crashes the highest-momentum names mean-revert hardest. Upgrades, in rollout order
(each validated against `analyze_results.py` outcomes before the next lands):

1. ✅ **Volatility-adjusted momentum** (implemented, measured: `docs/research/backtest-results.md`): the three return z-scores are
   computed on `return ÷ annualized realized vol` (126d, 10% floor) instead of raw
   returns — Barroso–Santa-Clara's volatility-managed momentum ("~2× Sharpe,
   virtually eliminates crashes"). **Measured 5y (2021–26, incl. the 2022 crash):
   the crash protection does NOT materialize in the portfolio sim — vol_adjusted
   had a worse max DD than raw in the crash half (−26.0% vs −25.7%) at ~⅔ less return.
   It improves candidate downside tail only (p5_20d). → DEMOTE; do not default to it.**
2. ⏳ **Rank-based composite + winsorization**: percentile ranks replace z-scores
   (bounds score blow-ups like the z≈+30 outlier observed in testing), optional
   5-day rank-stability check. **Measured: worst return on every gate, no drawdown
   edge over vol → drop.**
3. ⏳ **Index-level regime gate**: SPY vs 200-day SMA × VIX percentile →
   CALM/WARN/STRESS; WARN drops the top-decile 1m-momentum tail, STRESS pauses buys.
   **Measured (crash-in-sample): the ONLY change that cut max DD on every strategy
   (raw −25.7→−21.0, vol −26.0→−18.6, rank −18.9→−12.0) — the system's drawdown hedge.**
4. ⏳ **Absolute (dual) momentum gate**: ticker must beat T-bills over 12m and be
   positive over 6m (Antonacci); suppress buys if SPY fails its own trend test.
   **Measured: no drawdown benefit over the regime gate, clear return cost → defer.**
5. ⏳ **Anti-lottery overlay**: penalize MAX (largest 1-day gain) or exclude the
   blow-off signature `z(1m)>+3 & z(6m)<+1`. **Not in the backtest matrix — untested.**

**Measured default decision (5y): switch the screener to `raw_momentum + regime_gate`**
— best return-per-drawdown in the matrix (3.93%/20d alpha, max DD −21% vs −25.7% raw-alone).
Production defaults change only with user approval.

## 5. Decision engine

Pure function — no I/O — so it is fully unit-testable:

```
Inputs:  today's ratings {ticker: Rating}, current holdings {ticker: shares},
         last close prices, capital, max_positions, entry_protection_pct
Outputs: order list [ {ticker, action: BUY|SELL, shares, reason, protection_price?} ]

Rules (in order):
1. HOLDING + rating Sell/Underweight  → SELL whole position, plain MKT (reason: rating exit)
2. NOT holding + rating Buy/Overweight → BUY equal-weight slice as MKT-with-protection
   (aux limit = last_close × (1 + entry_protection_pct), default 2%); if the open gaps
   beyond the cap the order is cancelled, never overpaid (reason: entry)
3. Everything else → no order
Sizing: slice = capital / max_positions (default 10, independent of watchlist size);
        shares = floor(slice / last_close);
        skip the buy if shares < 1.
Cap:   total buy value must not exceed max_order_value_cap; otherwise skip the
       largest-ticket buy and log it.
```

- No re-entry while a position is held, regardless of rating.
- No averaging in / no rebalancing.
- Downgrade information (rating vs the memory log's previous rating) is still computed and
  logged for context, but sells are driven purely by today's rating + holding state.

## 6. Memory log interplay

The framework's `TradingMemoryLog` keeps working unchanged: every run appends the day's
decision per ticker and resolves prior same-ticker outcomes (realized returns + reflection)
automatically. That learning feedback feeds the Portfolio Manager prompt as before. The
paper portfolio is separate state, owned by IBKR.

## 7. LLM configuration

Provider: `openrouter` (`OPENROUTER_API_KEY`), cheap 2-tier split:

- `quick_think_llm` (analysts, researchers, trader, risk debators, reflections):
  `deepseek/deepseek-v4-flash` or `z-ai/glm-4.5-air`.
- `deep_think_llm` (Research Manager, Portfolio Manager judges):
  `deepseek/deepseek-v4-pro` or `z-ai/glm-4.7`.

Expected cost: ~$0.10–0.50 per ticker per day. With a watchlist of up to ~13 tickers
(10 holdings + 3 candidates) at one run/day: roughly **$10–100/month**. Models are
configurable in `watchlist.yaml`; any OpenRouter slug works.

## 8. Data layer (verified against the code)

| Category            | Default vendor | Key | Cost |
|---------------------|----------------|-----|------|
| OHLCV prices        | yfinance       | none | $0 |
| Technical indicators| yfinance       | none | $0 |
| Fundamentals/statements | yfinance   | none | $0 |
| Ticker news, insider trades | yfinance | none | $0 |
| Sentiment (StockTwits/Reddit) | stocktwits.py / reddit.py | none (Reddit degrades to RSS) | $0 |
| Macro indicators    | FRED           | `FRED_API_KEY` (free registration) | $0 |
| Prediction markets  | Polymarket     | none | $0 |

- Data cost is $0/month with framework defaults. The only recurring expense is LLM
  inference (§7).
- yfinance is free scraping with no SLA: fine for 7 tickers/day; the framework's vendor
  router (`dataflows/interface.py`) already handles no-data degradation with explicit
  sentinels rather than fabricated values.
- FRED key is the only setup step with a registration.

## 9. IBKR paper-trading setup (prerequisite)

- The user creates an IBKR account and enables **paper trading** (TWS API docs:
  paper accounts use port 7497 by default).
- IB Gateway is installed and left running on the VM (headless, auto-login with the
  paper account; API access enabled).
- **Data note**: IBKR paper accounts mirror the real account's market-data subscriptions.
  Delayed data is free; for market-open orders the daily decision loop needs no live data
  (prices come from yfinance), so no paid subscription is required to run. If fills at
  live prices are desired later, a real-time data subscription is needed.
- `ib_async` is added as a dependency (pip; maintained fork of `ib_insync`).

## 10. Scheduling

Three cron entries, timezone-aware to dodge DST:

```cron
CRON_TZ=America/New_York
0 18 * * 0    cd /opt/tradingagents && .venv/bin/python screener.py --screen >> logs/screener.log 2>&1
0 7 * * 1-5   cd /opt/tradingagents && .venv/bin/python daily_run.py --analyze >> logs/cron.log 2>&1
0 9 * * 1-5   cd /opt/tradingagents && .venv/bin/python daily_run.py --execute  >> logs/orders.log 2>&1
```

The 06:00 ET daily screen refreshes the candidate pool each trading morning
before the 07:00 analysis — scores are deterministic but prices move daily, and
a stale weekly snapshot would rank last week's momentum. A screen failure never
blocks the run: `daily_run.py` falls back to the last cached pool with a
warning.

Alternative: a single 07:00 entry that analyzes, sleeps until 09:30, then executes. Two
entries are preferred: a crash during analysis never prevents the execution step from
running its own decision pass, and logs stay separate. The analyze pass persists its
ratings to `results_dir/ratings_YYYY-MM-DD.json`; the execute pass reads that file (and
fails safe with no orders if it is missing).

- 07:00 ET analysis start; watchlist ≈ holdings (0–10) + 3 candidates ≈ 3–13 tickers ×
  ~3–6 min sequential → ratings saved to `results_dir/ratings_YYYY-MM-DD.json` by
  ~08:00–08:30. The 09:00 execution pass reads that file, re-fetches holdings from IBKR,
  and places orders at 09:30 (waits for the open if it starts early).
- The analysis date is pinned to "today in America/New_York", never UTC or server-local.

## 11. VM setup (one-time, scripted in the implementation plan)

1. Ubuntu 24.04 (or preferred distro) with Python 3.12.
2. `git clone` the repo to `/opt/tradingagents`; `python3.12 -m venv .venv`;
   `pip install .` plus `ib_async`.
3. IB Gateway (paper) installed, configured for auto-login + API on 7497.
4. `.env`: `OPENROUTER_API_KEY`, `FRED_API_KEY`, optional `TRADINGAGENTS_*` overrides.
5. `watchlist.yaml` with paper capital settings; `DISABLE_TRADING` file absent.
6. cron entries (§10); `logs/` with simple rotation (keep ~30 days).
7. Smoke test before trusting cron: `screener.py --screen --universe-size 50` once,
   then `daily_run.py --analyze --tickers AAPL` manually, then a dry-run execution pass
   (`--dry-run` flag prints the order list without submitting).

## 12. Error handling and safety

- Per-ticker try/except + one immediate retry; failure recorded with reason, run continues.
- **Watchlist gate**: a run that cannot assemble `min_watchlist_size` tickers (pool
  exhausted after top-up) aborts loudly before any analysis or execution.
- **Screener failure**: falls back to the last cached pool with a warning; a first-run
  screen failure falls back to `seed_watchlist`.
- **Execution safety**:
  - Kill switch: presence of `DISABLE_TRADING` file → analysis-only mode.
  - Connection to IBKR lost at execution time → no orders placed, run marked failed.
  - Order log consulted before placement → no double orders after a restart.
  - Max order value cap enforced in the decision engine.
  - Entry protection cap: a buy that gaps past `entry_protection_pct` above the previous
    close is cancelled (never overpaid); sells are never capped.
  - Fill timeout (e.g. 60 s) → cancel and log; never leave a stray open order.
- Framework-level failures (e.g. a vendor outage across all tickers) leave those tickers
  without ratings; the decision engine skips tickers with no rating (no orders).
- Checkpoint resume is off by default (short runs, fresh state per day is simpler).

## 13. Testing

- Unit: decision engine (holding+Sell → SELL; not-holding+Buy → BUY sized correctly;
  not-holding+Hold → nothing; holding+Buy → nothing; shares < 1 skip; cap enforcement;
  no-rating → no order); rating extraction from real decision strings; config merge
  precedence; kill-switch behavior.
- Screener unit tests: momentum score computation, liquidity filter, pool ranking and
  persistence, weekly cache TTL, stale-pool fallback, no-churn exclusion
  (held / recently-analyzed / recently-Sell), min-5 top-up, gate-abort on exhausted pool.
- Broker layer (mocked `ib_async`): connect/retry, position fetch, order placement, fill
  timeout → cancel, double-order guard.
- Integration: single-ticker end-to-end analysis run against the real framework with the
  cheap model pair; a dry-run execution pass against the real paper account (no orders).

## 14. Deliverables

- `daily_run.py`, `ibkr.py`, `screener.py`, `watchlist.yaml` (seed list + capital +
  screener settings), `tests/test_daily_run.py`, `tests/test_ibkr.py`,
  `tests/test_screener.py`.
- A short `docs/superpowers/plans/` implementation plan (written next).
- No changes to `tradingagents/`.

## 15. Open questions

- Whether IBKR requires the user to enable "API" access + paper trading on the account
  (expected yes; confirmed during setup).
- Whether to add an optional end-of-day summary email later (explicitly out of v1).
