# Handoff: Backtest Validation of Screening Methods

**Date:** 2026-08-31 · **Status:** planned, not started · **Est. effort:** ~2–3h build + ~15 min runtime · **Cost:** $0

## 1. Mission

Validate the 5 screening-method upgrades (spec §5bis roadmap) **on historical data** before rolling them out sequentially. For each method, replay the screener at past dates (strict no-look-ahead), record the top-N candidates it picks, and measure their **forward 5d/20d alpha vs SPY**. The result decides the real rollout order with evidence instead of literature priors.

**Scope boundary (important):** this validates **candidate quality only** — the LLM pipeline is deliberately excluded (non-deterministic + expensive; it sits after the screen). Do not simulate agent decisions. This is an **experiment script, not a production feature** — no tests, no test framework involvement; the deliverable is the script + the results report.

## 2. Where things are (the experiment runs ON THE PC)

- **Production PC** (this experiment runs here): Ubuntu 24.04, timezone **America/Edmonton**, repo at **`/home/harsh-amin/workplace/TradingAgents`**, venv at `.venv/bin/python` (deps installed: framework + ib_async + alpaca-py + pyyaml).
- **Access:** SSH alias `pc` over Tailscale; run commands via
  `expect ~/.config/opencode/skills/pc-dev/scripts/pc_ssh.exp '<cmd>'` (password auth via `PC_PASSWORD` env).
  If the PC is off, power it first: `python3 ~/.config/opencode/skills/pc-dev/scripts/ensure_power.py --device "PC Plug"`, then poll `pc_ssh.exp true` every 5s up to ~3 min.
- **Repo remote:** origin = `hamin2006/TradingAgents` (user's fork). Deploy flow: commit → `git push -q origin main` → `pc_ssh.exp 'cd /home/harsh-amin/workplace/TradingAgents && git pull -q'`.
- **Data/artifacts on the PC:** `~/.tradingagents/logs/` (ratings_YYYY-MM-DD.json, pool_YYYY-WW.json, universe_sp500.json, analysis_report.md), `~/.tradingagents/memory/trading_memory.md`.
- **Spec (the contract):** `docs/superpowers/specs/2026-08-30-daily-trading-signals-design.md` — esp. §5bis (screener + robustness roadmap) and `docs/research/screening-robustness-methods.md` (evidence: Daniel–Moskowitz 2016 "Momentum Crashes", Barroso–Santa-Clara 2015 volatility-managed momentum, Antonacci dual momentum, Bali et al. MAX effect).

## 3. Current state

- HEAD `c76f565` on origin/main, PC clone in sync. The experiment plan below is approved (design decisions in §5 are settled).
- **Daily pipeline on the PC (cron, `CRON_TZ=America/New_York`):** 06:00 screener → 06:50 healthcheck → 07:00 analyze (parallel, `analyze_max_workers: 4`) → 09:00 execute (waits for the 09:30 open). Logs in `~/workplace/TradingAgents/logs/`.
- **Shipped screening:** vol-adjusted momentum (Barroso–Santa-Clara) — `compute_raw_metrics` includes `realized_vol` (annualized daily-return std); `score_universe` z-scores `ret_{1m,3m,6m} ÷ max(realized_vol, 10% floor)` + `sma50_spread` + `high_proximity`, after a `avg_dollar_vol >= $10M` liquidity filter.
- **Reddit resilience:** rate limiter (8s min interval, RLock) + resilient retry/backoff + per-ticker cache; OAuth fetcher (`reddit_auth.py`) deployed but inactive — the user's Reddit app creation is captcha-blocked (retry later; it activates automatically once `REDDIT_CLIENT_ID`/`REDDIT_SECRET` appear in `.env`).

## 4. Key code APIs the backtest will use

All in `/home/harsh-amin/workplace/TradingAgents`:

- `screener.py`: `fetch_universe(cfg) -> list[str]` (Wikipedia + UA header fix; cached `universe_sp500.json`) · `fetch_prices(universe, period="6mo") -> dict[str, DataFrame]` (backtest needs a `years=3` variant — extend, don't break) · `compute_raw_metrics(hist) -> dict|None` (ret_1m/3m/6m, sma50_spread, high_proximity, avg_dollar_vol, realized_vol) · `score_universe(prices, min_dollar_vol=10M) -> [{"ticker","score"}]` (**this gets refactored into a strategy registry**) · `build_pool(cfg, limit)` / `load_pool(cfg)` · `today_et()`, `week_key(d)` · `VOL_FLOOR = 0.10`.
- `config.py`: `load_watchlist_config()` merges `watchlist.yaml` over `DEFAULT_CONFIG` over `APP_DEFAULTS`; unknown yaml keys raise. Relevant keys: `results_dir`, `capital` 100000, `max_positions` 10, `stop_loss_pct` 8.0, `conviction_weights` {Buy 1.5, Overweight 1.0}, `screener` block (universe sp500, pool_size 50, candidate_slots 3, min_watchlist_size 5, exclusion_days 3, entry_protection_pct 2.0).
- **Constraint:** NEVER modify anything under `tradingagents/`. All experiment code goes in new files + `screener.py`.
- **Style:** ruff (line-length 100, E/W/F/I/B/UP/C4/SIM) — run `uvx ruff check <files>` before committing.

## 5. The experiment (agreed design)

- **Backtest window:** 3 years of daily OHLCV, one batched download (`yf.download`, includes SPY + ^VIX for gates), cached to `~/.tradingagents/logs/backtest_prices.csv` (data cost $0, ~5 min).
- **Universe note:** today's S&P 500 list used for all past dates → survivorship bias inflates absolute numbers but affects all methods equally → **comparisons valid**; document in the report.
- **Simulated cadence:** step every 5 trading days (~250 dates over 3y), horizons **5d and 20d** forward alpha vs SPY, top **N=10** picks per date (overlapping windows acknowledged).
- **Combo matrix (~9 rows):** scoring ∈ {`raw_momentum` (pre-vol-adjust baseline), `vol_adjusted` (current default), `rank_based` (percentile ranks + winsorization)} × gate ∈ {none, `regime_gate` (SPY vs 200-day SMA × realized-vol state → CALM/WARN/STRESS: WARN drops the top-decile 1m-momentum tail, STRESS pauses buys), `dual_momentum` (ticker beats a T-bill proxy over 12m AND positive 6m)}.
- **Per-combo metrics:** avg 5d/20d alpha, hit rate (alpha>0), 5th-percentile alpha (tail), worst window, turnover; **split by half-periods** for robustness (no parameter tuning — validation, not overfitting).
- **Output:** `docs/research/backtest-results.md` — comparison table + per-combo notes + caveats section.

## 6. Implementation tasks (follow in order)

1. **Data + harness:** new file `backtest_screener.py`:
   - `fetch_history(universe, years=3)` — batched download incl. SPY + ^VIX, cached to `~/.tradingagents/logs/backtest_prices.csv`.
   - `simulate(prices, strategy, gate, step=5, horizons=[5,20])` — per-step top-N picks + forward alphas. **Strict date-slicing: metrics for date D use data ≤ D only.** This is the correctness core of the experiment.
   - CLI: `python backtest_screener.py --run` executes the full matrix and writes the report; `--tickers`/`--years` knobs for quick reruns.
2. **Strategy registry:** refactor `screener.py` — a `SCORE_STRATEGIES` registry {`raw_momentum`, `vol_adjusted` (current default), `rank_based` (percentile ranks + winsorization)}; `score_universe(prices, strategy="vol_adjusted")`. Default behavior must remain byte-identical (the existing repo test suite still passes — run `pytest -q` once after the refactor; it is the repo's mechanical gate even for experiments).
3. **Run the matrix on the PC:** `cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python backtest_screener.py --run` (~15 min incl. download).
4. **Report + decision:** generate `docs/research/backtest-results.md` (comparison table, sub-period splits, caveats §7); present results to the user; update the spec §5bis roadmap table with the measured order; production screener defaults change only with user approval.

## 7. Caveats (state in the report)

1. Candidate quality ≠ final P&L (the LLM layer filters further — deliberately excluded for determinism).
2. Survivorship bias (equal across methods → comparisons valid, absolute numbers optimistic).
3. Overlapping forward windows inflate sample correlation → half-period splits as the robustness check.
4. Literature params only, no tuning — this is validation of pre-registered methods, not a parameter search.

## 8. Gotchas (do not re-learn these)

- `pgrep -f` inside the expect helper matches its own wrapper shell — use distinctive patterns like `\.venv/bin/python screener`.
- Wikipedia 403s requests without a User-Agent (`screener.fetch_universe` handles this — don't regress).
- `pd.read_html` needs `io.StringIO(html_text)`.
- Screener dates are pinned to `America/New_York` (`today_et()`), never server-local.
- Module-name collisions: never name a module after a pip package.
- `tradingagents/` is read-only; framework behavior is patched at runtime from our modules only (see `daily_run.py` memory/reddit wrappers for the pattern — RLock + `_wrapped_original` tagging).
- Full-suite check after any cross-module change: `pytest -q` (the repo's mechanical gate — expected all green).

## 9. Definition of done

- `backtest_screener.py` committed; the matrix run completes on the PC; `docs/research/backtest-results.md` committed with the comparison table, sub-period splits, and caveats; spec §5bis roadmap table updated with measured verdicts; user has decided the production rollout order.

## 10. Still open elsewhere (context, not this experiment's scope)

- Reddit OAuth creds: user's app creation is captcha-blocked (account/IP flag; retry in a few days from a different network). `reddit_auth.py` activates automatically once creds land in `.env`.
- Drawdown circuit breaker: deferred until ~2 weeks of resolved decisions exist in the memory log.
- Kiro Crew ops skill: user's mental note, deferred.
- Paper account cash = **$10,000** (not the $100k config) — the capital cap in `run_execute` handles it; expect small slices.
