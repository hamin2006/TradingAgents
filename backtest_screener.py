"""backtest_screener.py — historical validation of the screening-method matrix.

Experiment (NOT a production feature, per docs/handoffs/backtest-screener-experiment.md):
replay the S&P 500 momentum screen on historical daily data with STRICT no-look-ahead
scoring (metrics for date D use data <= D only), record the top-N candidates each
trading day, and measure their forward 5d/20d alpha vs SPY, plus a deterministic
portfolio simulation with proxy exits. The result decides the real rollout order of
the spec §5bis screening upgrades with evidence instead of literature priors.

The LLM pipeline is deliberately excluded (non-deterministic + expensive; it sits
after the screen). Candidate quality only.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from config import load_watchlist_config
from screener import (
    MIN_ROWS,
    SCORE_STRATEGIES,
    composite_score,
    fetch_universe,
)

logger = logging.getLogger(__name__)

BENCH = "SPY"
VIX = "^VIX"
IRX = "^IRX"
GATE_TICKERS = (BENCH, VIX, IRX)
GATES = ("none", "regime_gate", "dual_momentum")
DEFAULT_YEARS = 3
DEFAULT_TOP_N = 10
DEFAULT_MIN_DOLLAR_VOL = 10_000_000
# One extra year of lookback downloaded before the replay window: it funds the
# warm-up the gates need (SPY 200d SMA, VIX trailing-year percentile, 12m returns).
WARMUP_YEARS = 1
LOOKBACK_DAYS = 252


def _align_to_cal(df: pd.DataFrame, cal: pd.DatetimeIndex) -> pd.DataFrame:
    """Align a per-ticker frame onto the master trading-day calendar, forward-filling
    within its own history (NaNs before the first available date stay NaN)."""
    return df.reindex(cal).ffill()


def _cache_path(years: int) -> Path:
    window = years + WARMUP_YEARS
    return Path.home() / ".tradingagents" / "logs" / f"backtest_prices_y{window}.csv"


def fetch_history(universe: list[str], years: int = DEFAULT_YEARS,
                  force: bool = False) -> dict[str, pd.DataFrame]:
    """Download `years` years of daily OHLCV for the universe plus SPY/^VIX/^IRX,
    cached to ~/.tradingagents/logs/backtest_prices_y{window}.csv (window = years + 1;
    the extra year funds the warm-up). Returns dict[ticker -> frame]."""
    window = years + WARMUP_YEARS
    tickers = list(universe) + list(GATE_TICKERS)
    cache_path = _cache_path(years)
    frame = None
    if cache_path.exists() and not force:
        try:
            frame = pd.read_csv(cache_path, index_col=0, header=[0, 1], parse_dates=[0])
            frame.index = pd.to_datetime(frame.index)
            logger.info("loaded %d cached rows from %s", len(frame), cache_path)
        except Exception:  # noqa: BLE001 - corrupt cache re-downloads
            frame = None
    if frame is None:
        logger.info("downloading %d tickers over %dy...", len(tickers), window)
        frame = yf.download(" ".join(tickers), period=f"{window}y",
                            group_by="ticker", auto_adjust=True, threads=False,
                            progress=False)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(cache_path)
        logger.info("cached panel to %s", cache_path)
    prices: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            col = frame[t]
            if isinstance(col.columns, pd.MultiIndex):
                col.columns = col.columns.get_level_values(0)
            col = col.dropna(how="all")
            if len(col) >= MIN_ROWS:
                prices[t] = col
        except KeyError:
            continue
    return prices


def _precompute_ticker(hist: pd.DataFrame) -> pd.DataFrame:
    """Vectorized per-ticker metrics aligned to its own index — the as-of equivalent
    of compute_raw_metrics(hist.loc[:D]) for every D at once (exact match, but O(1)
    per date instead of a full recompute)."""
    close = hist["Close"].dropna()
    if len(close) < MIN_ROWS:
        return pd.DataFrame()
    daily = close.pct_change()
    vol = daily.expanding().std() * (252 ** 0.5)  # production uses full-history std
    sma50 = close.rolling(50).mean()
    high = close.expanding().max()
    dollar_vol = (hist["Close"] * hist["Volume"]).rolling(20).mean()
    return pd.DataFrame({
        "close": close,
        "ret_1m": close / close.shift(21) - 1,
        "ret_3m": close / close.shift(63) - 1,
        "ret_6m": close / close.shift(126) - 1,
        "ret_12m": close / close.shift(LOOKBACK_DAYS) - 1,
        "sma50_spread": close / sma50 - 1,
        "high_proximity": close / high,
        "avg_dollar_vol": dollar_vol,
        "realized_vol": vol,
    })


def _gate_frame(hist: pd.DataFrame, sma: int | None = None) -> pd.DataFrame:
    close = hist["Close"].dropna()
    out = {"close": close}
    if sma:
        out[f"sma{sma}"] = close.rolling(sma).mean()
    return pd.DataFrame(out)


def _regime_at(spy: pd.DataFrame, vix: pd.DataFrame, d) -> str:
    """SPY vs 200-day SMA x VIX trailing-year percentile -> CALM/WARN/STRESS
    (research doc §3.2; STRESS = index below its 200d SMA, WARN = elevated VIX)."""
    above = spy.at[d, "close"] > spy.at[d, "sma200"]
    vix_hist = vix["close"].loc[:d].dropna()
    vix_pct = 0.0 if len(vix_hist) == 0 else float((vix_hist <= vix.at[d, "close"]).mean())
    if not above:
        return "STRESS"
    if vix_pct >= 0.80:
        return "WARN"
    return "CALM"


def _gate_none(ranked, frame, spy, vix, irx, d, **kw):
    return list(ranked)


def _gate_regime(ranked, frame, spy, vix, irx, d, **kw):
    regime = _regime_at(spy, vix, d)
    if regime == "STRESS":
        return []  # pause new buys
    if regime == "WARN":
        # drop the top-decile 1m-momentum tail (the post-squeeze set)
        thresh = frame["ret_1m"].quantile(0.90)
        return [t for t in ranked if not frame.at[t, "ret_1m"] > thresh]
    return list(ranked)


def _gate_dual(ranked, frame, spy, vix, irx, d, **kw):
    # Antonacci absolute (dual) momentum: ticker beats the T-bill proxy over 12m
    # AND is positive over 6m (research doc §3.4). IRX is an annualized % yield.
    cash = irx.at[d, "close"] / 100.0
    return [t for t in ranked
            if frame.at[t, "ret_12m"] >= cash and frame.at[t, "ret_6m"] > 0]


GATE_FNS = {"none": _gate_none, "regime_gate": _gate_regime, "dual_momentum": _gate_dual}


def _screen_at_date(aligned, cal, i, strategy, gate, gate_data,
                    min_dollar_vol, top_n):
    """Top-N candidates for replay date index i (data <= cal[i] only)."""
    d = cal[i]
    rows = {}
    for t, df in aligned.items():
        row = df.iloc[i]  # value at cal[i], forward-filled within real history
        if pd.isna(row["ret_6m"]):
            continue  # <126d history -> insufficient (warm-up / late-listing)
        if not row["avg_dollar_vol"] >= min_dollar_vol:
            continue
        rows[t] = row
    if not rows:
        return []
    frame = pd.DataFrame(rows).T
    score = composite_score(frame, strategy)
    ranked = list(score.sort_values(ascending=False).index)
    spy, vix, irx = gate_data
    filtered = GATE_FNS[gate](ranked, frame, spy, vix, irx, d)
    return filtered[:top_n]


def simulate(prices: dict[str, pd.DataFrame], strategy: str = "vol_adjusted",
             gate: str = "none", top_n: int = DEFAULT_TOP_N,
             min_dollar_vol: float = DEFAULT_MIN_DOLLAR_VOL, step: int = 1,
             horizons=(5, 20), stop_loss_pct: float = 8.0,
             entry_protection_pct: float = 2.0, capital: float = 100_000.0,
             max_positions: int = 10, time_stop: int | None = None,
             years: int = DEFAULT_YEARS) -> dict:
    """Replay the screen every trading day over the last `years` years. Returns a
    dict of per-horizon forward-alpha stats + portfolio equity stats."""
    cal = prices[BENCH].index if BENCH in prices else pd.DatetimeIndex([])
    if len(cal) < LOOKBACK_DAYS + 10:
        raise ValueError("benchmark history too short for the replay window")
    # replay window = the most recent `years` years of the calendar
    start = cal[-1] - pd.DateOffset(years=years)
    replay = cal[cal >= start]
    cal_idx = {dt: i for i, dt in enumerate(cal)}

    # align every series onto the master calendar once (fast as-of lookup later)
    aligned = {}
    for t, h in prices.items():
        if t in GATE_TICKERS:
            continue
        pc = _precompute_ticker(h)
        if not pc.empty:
            aligned[t] = _align_to_cal(pc, cal)
    aprice = {t: _align_to_cal(h[["Open", "Close", "Low"]], cal)
              for t, h in prices.items() if t not in GATE_TICKERS}
    bprice = _align_to_cal(prices[BENCH][["Open", "Close"]], cal)
    spy = _align_to_cal(_gate_frame(prices[BENCH], sma=200), cal)
    vix = _align_to_cal(_gate_frame(prices[VIX]), cal)
    irx = _align_to_cal(_gate_frame(prices[IRX]), cal)
    gate_data = (spy, vix, irx)

    obs: list[tuple] = []          # (date, ticker, horizon, alpha)
    positions: dict[str, dict] = {}
    scheduled: list[str] = []      # candidates picked yesterday, enter at today's open
    equity: list[tuple] = []       # (date, equity)
    closed: list[dict] = []
    cash = float(capital)
    slice_size = capital / max_positions

    for i in range(len(replay)):
        d = replay[i]
        # 1. execute yesterday's scheduled entries at today's open (gap filter)
        if scheduled:
            prev_d = replay[i - 1]
            for t in list(scheduled):
                if len(positions) >= max_positions or t in positions:
                    continue
                o = aprice[t].at[d, "Open"]
                pc = aprice[t].at[prev_d, "Close"]
                if not np.isfinite(o) or not np.isfinite(pc):
                    continue
                if o > pc * (1 + entry_protection_pct / 100.0):
                    continue  # gaps past the cap -> no fill (never overpay)
                shares = int(slice_size // o)
                if shares < 1:
                    continue
                positions[t] = {"shares": shares, "entry": float(o), "entry_date": d}
                cash -= shares * o
            scheduled = []
        # 2. today's screen (data <= d): used for pool-exit + next entries + alphas
        pool = _screen_at_date(aligned, cal, cal_idx[d], strategy, gate, gate_data,
                               min_dollar_vol, top_n)
        pool_set = set(pool)
        # 3. forward alphas for today's picks, measured from the NEXT open
        if i + 1 < len(replay):
            next_d = replay[i + 1]
            for t in pool:
                o = aprice[t].at[next_d, "Open"]
                so = bprice.at[next_d, "Open"]
                if not np.isfinite(o) or not np.isfinite(so):
                    continue
                for k in horizons:
                    j = cal_idx.get(next_d)
                    if j is None or j + k >= len(cal):
                        continue
                    end_d = cal[j + k]
                    tc = aprice[t].at[end_d, "Close"]
                    sc = bprice.at[end_d, "Close"]
                    if not (np.isfinite(tc) and np.isfinite(sc)):
                        continue
                    alpha = (tc / o - 1) - (sc / so - 1)
                    obs.append((d, t, k, float(alpha)))
        # 4. exits: broker stop-loss (intraday) then pool-drop (at today's close)
        for t in list(positions):
            p = positions[t]
            stop = p["entry"] * (1 - stop_loss_pct / 100.0)
            low = aprice[t].at[d, "Low"]
            op = aprice[t].at[d, "Open"]
            if time_stop and (cal_idx[d] - cal_idx.get(p["entry_date"], cal_idx[d])) >= time_stop:
                exit_price = aprice[t].at[d, "Close"]
            elif np.isfinite(low) and low <= stop:
                fill = min(op, stop)
                exit_price = float(fill) if np.isfinite(fill) else stop
            elif t not in pool_set:
                exit_price = aprice[t].at[d, "Close"]
            else:
                continue
            if np.isfinite(exit_price):
                cash += p["shares"] * exit_price
                closed.append({"ticker": t, "entry": p["entry"], "exit": exit_price,
                               "entry_date": p["entry_date"], "exit_date": d})
            del positions[t]
        # 5. schedule tomorrow's entries from today's candidates
        scheduled = pool
        # 6. equity today
        eq = cash + sum(p["shares"] * aprice[t].at[d, "Close"]
                        for t, p in positions.items())
        equity.append((d, float(eq)))

    return {"observations": obs, "equity": equity, "closed": closed,
            "capital": capital, "positions": len(positions)}


def _half_split(obs, equity, capital, horizons):
    dates = sorted({d for d, _t, _k, _a in obs})
    if not dates:
        return {}
    mid = dates[len(dates) // 2]
    out = {}
    for name, lo, hi in (("first", dates[0], mid), ("second", mid, dates[-1])):
        sel = [(d, t, k, a) for d, t, k, a in obs if lo <= d <= hi]
        eq = [(d, e) for d, e in equity if lo <= d <= hi]
        out[name] = _metrics(sel, eq, capital, horizons)
    return out


def _metrics(obs, equity, capital, horizons):
    out = {}
    for k in horizons:
        vals = [a for _d, _t, kk, a in obs if kk == k]
        if not vals:
            continue
        arr = np.array(vals)
        out[f"avg_{k}d"] = float(arr.mean())
        out[f"hit_{k}d"] = float((arr > 0).mean())
        out[f"p5_{k}d"] = float(np.percentile(arr, 5))
        out[f"worst_{k}d"] = float(arr.min())
        out[f"n_{k}d"] = int(len(arr))
    if equity:
        eq = pd.Series(dict(equity)).sort_index()
        out["total_return"] = float(eq.iloc[-1] / capital - 1)
        out["max_drawdown"] = float((eq / eq.cummax() - 1).min())
        out["n_equity_days"] = int(len(eq))
    return out


def run_combo(prices, strategy, gate, **sim_kw) -> dict:
    """Run one combo and aggregate its metrics."""
    res = simulate(prices, strategy=strategy, gate=gate, **sim_kw)
    m = _metrics(res["observations"], res["equity"], res["capital"],
                 sim_kw.get("horizons", (5, 20)))
    win = sum(1 for c in res["closed"] if c["exit"] > c["entry"])
    m["trade_win_rate"] = win / len(res["closed"]) if res["closed"] else 0.0
    m["n_trades"] = len(res["closed"])
    m["n_obs"] = len(res["observations"])
    m["splits"] = _half_split(res["observations"], res["equity"], res["capital"],
                              sim_kw.get("horizons", (5, 20)))
    return m


def _fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v * 100:.2f}%"


def _single_report(strategy, gate, m) -> list[dict]:
    return [{
        "label": f"{strategy}+{gate}", "avg_5d": _fmt_pct(m.get("avg_5d")),
        "hit_5d": _fmt_pct(m.get("hit_5d")), "p5_5d": _fmt_pct(m.get("p5_5d")),
        "avg_20d": _fmt_pct(m.get("avg_20d")), "hit_20d": _fmt_pct(m.get("hit_20d")),
        "p5_20d": _fmt_pct(m.get("p5_20d")),
        "total_return": _fmt_pct(m.get("total_return")),
        "max_drawdown": _fmt_pct(m.get("max_drawdown")),
        "trade_win_rate": _fmt_pct(m.get("trade_win_rate")),
        "n_trades": m.get("n_trades", 0),
    }]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Historical validation of the screening-method matrix")
    parser.add_argument("--run", action="store_true",
                        help="run the full 3x3 matrix and write docs/research/backtest-results.md")
    parser.add_argument("--tickers", default="",
                        help="comma-separated ticker list (default: S&P 500 universe)")
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS,
                        help=f"replay-window length in years (default {DEFAULT_YEARS})")
    parser.add_argument("--strategy", choices=list(SCORE_STRATEGIES),
                        help="single-combo scoring strategy")
    parser.add_argument("--gate", choices=list(GATES), help="single-combo gate")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"candidates per date (default {DEFAULT_TOP_N})")
    parser.add_argument("--force", action="store_true",
                        help="re-download the price cache")
    parser.add_argument("--report", action="store_true",
                        help="regenerate docs/research/backtest-results.md from the "
                             "saved results JSON (no re-run)")
    args = parser.parse_args(argv)

    cfg = load_watchlist_config()
    capital = float(cfg["capital"])
    max_positions = int(cfg["max_positions"])
    stop_loss_pct = float(cfg["stop_loss_pct"])
    entry_protection_pct = float(cfg["screener"]["entry_protection_pct"])

    if args.tickers:
        universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        universe = fetch_universe(cfg)
    logger.info("universe size: %d", len(universe))
    prices = fetch_history(universe, years=args.years, force=args.force)
    missing = [t for t in (BENCH, VIX, IRX) if t not in prices]
    if missing:
        logger.error("gate data missing: %s", missing)
        return 1

    sim_kw = {"top_n": args.top_n, "stop_loss_pct": stop_loss_pct,
              "entry_protection_pct": entry_protection_pct, "capital": capital,
              "max_positions": max_positions, "years": args.years}
    results_json = Path("docs") / "research" / "backtest-results.json"

    if args.report:
        if not results_json.exists():
            logger.error("no saved results at %s; run --run first", results_json)
            return 1
        _write_report(_load_results_from_json(results_json), universe, args.years, sim_kw)
        print("regenerated docs/research/backtest-results.md from cache")
        return 0

    if args.run:
        matrix = [(s, g) for s in SCORE_STRATEGIES for g in GATES]
        results = {}
        for s, g in matrix:
            logger.info("running %s + %s ...", s, g)
            results[(s, g)] = run_combo(prices, s, g, **sim_kw)
        results_json.parent.mkdir(parents=True, exist_ok=True)
        results_json.write_text(
            json.dumps(_results_serializable(results), indent=2, default=str),
            encoding="utf-8")
        logger.info("persisted results to %s", results_json)
        _write_report(results, universe, args.years, sim_kw)
        print("wrote docs/research/backtest-results.md")
    else:
        strategy = args.strategy or "vol_adjusted"
        gate = args.gate or "none"
        m = run_combo(prices, strategy, gate, **sim_kw)
        print(f"=== {strategy} + {gate} ===")
        for k, v in m.items():
            if k == "splits":
                continue
            print(f"  {k}: {v}")
        print("  splits.first:", m.get("splits", {}).get("first"))
        print("  splits.second:", m.get("splits", {}).get("second"))
    return 0


def _results_serializable(results: dict) -> dict:
    """JSON dict keys must be strings; the matrix keys are (strategy, gate) tuples."""
    return {f"{s}+{g}": m for (s, g), m in results.items()}


def _load_results_from_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {(k.split("+")[0], k.split("+", 1)[1]): v for k, v in payload.items()}


def _write_report(results, universe, years, sim_kw):
    lines = []
    ap = lines.append
    ap("# Backtest: Screening-Method Matrix (candidate quality, not final P&L)")
    ap("")
    ap(f"**Date:** {datetime.now().date()} · **Universe:** {len(universe)} S&P 500 names · "
       f"**Replay window:** {years}y daily, screen replayed every trading day · "
       f"**Top-N/day:** {sim_kw.get('top_n')} · **Horizons:** 5d/20d forward alpha vs SPY")
    ap("")
    ap("Candidate **quality only** — the LLM multi-agent layer (non-deterministic, "
       "expensive) is deliberately excluded; it sits after the screen. Results decide "
       "the rollout order of the §5bis upgrades.")
    ap("")
    ap("## 1. Comparison table")
    ap("")
    ap("| combo | avg 5d | hit 5d | p5 5d | avg 20d | hit 20d | p5 20d | tot ret | max DD | win% | trades |")
    ap("|---|---|---|---|---|---|---|---|---|---|---|")
    for (s, g), m in results.items():
        r = _single_report(s, g, m)[0]
        ap(f"| {r['label']} | {r['avg_5d']} | {r['hit_5d']} | {r['p5_5d']} | "
           f"{r['avg_20d']} | {r['hit_20d']} | {r['p5_20d']} | {r['total_return']} | "
           f"{r['max_drawdown']} | {r['trade_win_rate']} | {r['n_trades']} |")
    ap("")
    ap("## 2. Half-period splits (robustness)")
    ap("")
    for (s, g), m in results.items():
        ap(f"### {s} + {g}")
        ap("")
        ap("| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |")
        ap("|---|---|---|---|---|---|---|---|")
        for half in ("first", "second"):
            h = m.get("splits", {}).get(half, {})
            ap(f"| {half} | {_fmt_pct(h.get('avg_5d'))} | {_fmt_pct(h.get('hit_5d'))} | "
               f"{_fmt_pct(h.get('p5_5d'))} | {_fmt_pct(h.get('avg_20d'))} | "
               f"{_fmt_pct(h.get('hit_20d'))} | {_fmt_pct(h.get('p5_20d'))} | "
               f"{_fmt_pct(h.get('total_return'))} | {_fmt_pct(h.get('max_drawdown'))} |")
        ap("")
    ap("## 3. Method verdicts (rollout order)")
    ap("")
    ap("_Filled from the measured table above._")
    ap("")
    ap("## 4. Caveats")
    ap("")
    ap("1. Candidate quality ≠ final P&L (the LLM layer filters further — excluded for determinism).")
    ap("2. Survivorship bias (today's S&P 500 used for all past dates) inflates absolute numbers "
       "but affects all methods equally → comparisons valid.")
    ap("3. Overlapping forward windows inflate sample correlation → half-period splits are the robustness check.")
    ap("4. Literature params only, no tuning — validation of pre-registered methods, not a parameter search.")
    ap("5. Portfolio sim uses equal-weight sizing at config capital ($100k), deterministic proxy exits "
       "(pool-drop, stop-loss, optional time stop) — absolute P&L won't match live (no LLM, no costs); "
       "the method *ranking* is the deliverable.")
    ap("6. Entry fills at the next open with a 2% gap cap (skipped if gapped past prev_close × 1.02).")
    ap("7. The screen replays with top-10/day (deliberate divergence from production's 3/day + 3-day "
       "exclusion) for statistical mass.")
    ap("8. Regime/dual gates need a 200d SMA and 12m returns → a ~1y warm-up precedes the replay window.")
    ap("")
    ap("_Machine-readable results embedded below._")
    ap("")
    ap("```json")
    ap(json.dumps(_results_serializable(results), indent=2, default=str))
    ap("```")
    ap("")
    path = Path("docs") / "research" / "backtest-results.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
