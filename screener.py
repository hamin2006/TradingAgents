"""screener.py — weekly S&P 500 momentum screen producing the candidate pool."""

import argparse
import io
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

from config import load_watchlist_config
from tradingagents.dataflows.config import set_config

logger = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
UNIVERSE_CACHE_TTL_DAYS = 7
MIN_ROWS = 60
ET = ZoneInfo("America/New_York")


def today_et() -> date:
    """Screener dates are pinned to America/New_York, never server-local
    (Sunday 18:00 ET may already be Monday on a non-ET host)."""
    return datetime.now(ET).date()


def week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-{iso[1]:02d}"


def _results_dir(cfg: dict) -> Path:
    return Path(cfg["results_dir"])


def _universe_cache_path(cfg: dict) -> Path:
    return _results_dir(cfg) / "universe_sp500.json"


def fetch_universe(cfg: dict) -> list[str]:
    path = _universe_cache_path(cfg)
    if path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        if age < timedelta(days=UNIVERSE_CACHE_TTL_DAYS):
            return json.loads(path.read_text(encoding="utf-8"))
    try:
        # Wikipedia 403s requests without a User-Agent; pd.read_html alone
        # sends none, so fetch explicitly with a UA header first (#weekly-screen).
        resp = requests.get(
            WIKI_URL,
            headers={"User-Agent": "Mozilla/5.0 (daily-paper-trading; +research)"},
            timeout=30,
        )
        resp.raise_for_status()
        tables = pd.read_html(io.StringIO(resp.text))
        symbols = sorted(tables[0]["Symbol"].astype(str).str.strip().tolist())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(symbols), encoding="utf-8")
        return symbols
    except Exception as exc:  # noqa: BLE001 - fail open to cache
        logger.warning("universe fetch failed (%s); using cached list", exc)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return []


def fetch_prices(universe: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    if not universe:
        return {}
    frame = yf.download(" ".join(universe), period=period, group_by="ticker",
                        auto_adjust=True, threads=False, progress=False)
    prices: dict[str, pd.DataFrame] = {}
    for ticker in universe:
        try:
            col = frame[ticker]
            if isinstance(col.columns, pd.MultiIndex):
                col.columns = col.columns.get_level_values(0)
            if len(col.dropna(how="all")) >= MIN_ROWS:
                prices[ticker] = col.dropna(how="all")
        except KeyError:
            continue
    return prices


def compute_raw_metrics(hist: pd.DataFrame) -> dict | None:
    close = hist["Close"].dropna()
    if len(close) < MIN_ROWS:
        return None
    last = close.iloc[-1]
    n = len(close)
    ret = lambda days: last / close.iloc[-1 - days] - 1 if n > days else None  # noqa: E731
    sma50 = close.rolling(50).mean().iloc[-1]
    high = close.max()
    avg_dollar_vol = float((hist["Close"] * hist["Volume"]).tail(20).mean())
    daily_rets = close.pct_change().dropna()
    realized_vol = float(daily_rets.std() * (252 ** 0.5))  # annualized
    return {
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
        "sma50_spread": last / sma50 - 1,
        "high_proximity": float(last / high),
        "avg_dollar_vol": avg_dollar_vol,
        "realized_vol": realized_vol,
    }


def _zscore(values: pd.Series) -> pd.Series:
    s = pd.Series(values)
    return (s - s.mean()) / s.std()


def _winsorize_rank(values: pd.Series) -> pd.Series:
    """Winsorize the 5–95% tails, then convert to a 0–1 percentile rank. Rank
    transforms are robust to fat tails (squeeze names don't blow up the score);
    z-scores are not (research doc §3.3)."""
    s = pd.Series(values)
    lo, hi = s.quantile([0.05, 0.95])
    return s.clip(lo, hi).rank(pct=True)


# Vol-adjusted momentum (Barroso–Santa-Clara 2015): rank by return relative to
# its own realized volatility, with a floor so low-vol names can't divide to
# infinity. Demotes lottery-like parabolic movers — the names that mean-revert
# hardest in momentum crashes.
VOL_FLOOR = 0.10  # 10% annualized

# Screening-method registry (spec §5bis roadmap). Each strategy reshapes *who*
# ranks high; the factors and their equal weighting are identical, so the only
# variable is the cross-sectional transform — a clean A/B.
SCORE_STRATEGIES = ("raw_momentum", "vol_adjusted", "rank_based")


def composite_score(frame: pd.DataFrame, strategy: str = "raw_momentum") -> pd.Series:
    """Cross-sectional composite score for one screen date. `frame` is indexed by
    ticker with columns as produced by compute_raw_metrics (ret_1m/3m/6m,
    sma50_spread, high_proximity, realized_vol, ...). Returns a Series indexed by
    ticker, higher = better. `raw_momentum` z-scores raw returns (production
    default, paired with the regime gate); `vol_adjusted` z-scores return ÷
    realized vol; `rank_based` winsorizes and percentile-ranks the vol-adjusted
    terms."""
    if strategy not in SCORE_STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; choose from {SCORE_STRATEGIES}")
    if strategy == "raw_momentum":
        ret_cols = ("ret_1m", "ret_3m", "ret_6m")
        f = frame
    else:  # vol_adjusted, rank_based — both scale momentum by realized vol
        f = frame.copy()
        vol = f["realized_vol"].clip(lower=VOL_FLOOR)
        for c in ("ret_1m", "ret_3m", "ret_6m"):
            f[f"{c}_adj"] = f[c] / vol
        ret_cols = ("ret_1m_adj", "ret_3m_adj", "ret_6m_adj")
    score_cols = ret_cols + ("sma50_spread", "high_proximity")
    if strategy == "rank_based":
        parts = [_winsorize_rank(f[c]).fillna(0.0) for c in score_cols]
    else:
        parts = [_zscore(f[c]).fillna(0.0) for c in score_cols]
    return sum(parts)


def score_universe(prices: dict[str, pd.DataFrame],
                   min_dollar_vol: float = 10_000_000,
                   strategy: str = "raw_momentum") -> list[dict]:
    metrics = {t: m for t, m in ((t, compute_raw_metrics(h)) for t, h in prices.items())
               if m is not None}
    rows = {t: m for t, m in metrics.items() if m["avg_dollar_vol"] >= min_dollar_vol}
    if not rows:
        return []
    frame = pd.DataFrame(rows).T
    score = composite_score(frame, strategy)
    ranked = score.sort_values(ascending=False)
    return [{"ticker": t, "score": round(float(s), 4)} for t, s in ranked.items()]


# --- regime gate (5y backtest: the only defense that survived crash-in-sample) ---

SMA_WINDOW = 200
VIX_WARN_PERCENTILE = 0.80


def regime_gate_enabled(cfg: dict) -> bool:
    return bool(cfg.get("screener", {}).get("regime_gate", True))


def fetch_gate_data(cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """SPY + ^VIX daily history for the regime gate (1y covers SMA200 + the
    trailing-year VIX percentile)."""
    frames = fetch_prices(["SPY", "^VIX"], period="1y")
    if "SPY" not in frames or "^VIX" not in frames:
        raise RuntimeError("gate data download incomplete")
    return frames["SPY"], frames["^VIX"]


def regime_at(spy: pd.DataFrame, vix: pd.DataFrame, d=None) -> str:
    """SPY vs 200-day SMA x VIX trailing-year percentile -> CALM/WARN/STRESS.

    STRESS = index below its 200d SMA (pauses new buys); WARN = elevated VIX
    (drops the top-decile 1m-momentum tail). Insufficient data fails open to
    CALM — the gate degrades, the screen never blocks on a data blip.
    """
    spy_close = spy["Close"].dropna()
    if len(spy_close) < SMA_WINDOW:
        return "CALM"
    d = spy_close.index[-1] if d is None else d
    sma200 = spy_close.rolling(SMA_WINDOW).mean().asof(d)
    close = spy_close.asof(d)
    vix_close = vix["Close"].dropna()
    vix_now = vix_close.asof(d)
    vix_hist = vix_close[vix_close.index <= d]
    vix_pct = float((vix_hist <= vix_now).mean()) if len(vix_hist) else 0.0
    if close < sma200:
        return "STRESS"
    if vix_pct >= VIX_WARN_PERCENTILE:
        return "WARN"
    return "CALM"


def _drop_1m_tail(ranked: list[dict], prices: dict[str, pd.DataFrame],
                  decile: float = 0.90) -> list[dict]:
    """WARN regime: drop the top-decile 1m-momentum names (the post-squeeze
    set that mean-reverts hardest)."""
    rets = {}
    for r in ranked:
        t = r["ticker"]
        if t in prices:
            m = compute_raw_metrics(prices[t])
            if m is not None and m["ret_1m"] is not None:
                rets[t] = m["ret_1m"]
    if not rets:
        return ranked
    thresh = pd.Series(rets).quantile(decile)
    kept = [r for r in ranked if rets.get(r["ticker"], -1e9) <= thresh]
    dropped = [r["ticker"] for r in ranked
               if r["ticker"] not in {k["ticker"] for k in kept}]
    if dropped:
        logger.warning("regime WARN: dropped 1m-momentum tail %s", dropped)
    return kept


def build_pool(cfg: dict, limit: int | None = None) -> Path:
    universe = fetch_universe(cfg)
    if limit:
        universe = universe[:limit]
    prices = fetch_prices(universe)
    ranked = score_universe(prices)

    regime = "CALM"
    if regime_gate_enabled(cfg):
        try:
            spy, vix = fetch_gate_data(cfg)
            regime = regime_at(spy, vix)
        except Exception as exc:  # noqa: BLE001 - fail open to CALM
            logger.warning("regime gate data unavailable (%s); assuming CALM", exc)
        if regime == "WARN":
            ranked = _drop_1m_tail(ranked, prices)
        elif regime == "STRESS":
            ranked = []
            logger.warning("regime STRESS: candidate pool paused (no new buys)")

    path = _results_dir(cfg) / f"pool_{week_key(today_et())}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"year_week": week_key(today_et()),
                                "built_at": datetime.now().isoformat(),
                                "regime": regime,
                                "pool": ranked}, indent=2), encoding="utf-8")
    logger.info("pool written to %s with %d tickers (regime %s)",
                path, len(ranked), regime)
    return path


def load_regime(cfg: dict) -> str:
    """Market regime recorded by the latest pool build (default CALM)."""
    pool_files = sorted(_results_dir(cfg).glob("pool_*.json"))
    if not pool_files:
        return "CALM"
    try:
        return json.loads(pool_files[-1].read_text(encoding="utf-8")).get("regime", "CALM")
    except (OSError, ValueError):
        return "CALM"


def load_pool(cfg: dict) -> list[dict]:
    pool_files = sorted(_results_dir(cfg).glob("pool_*.json"))
    if not pool_files:
        return []
    payload = json.loads(pool_files[-1].read_text(encoding="utf-8"))
    return payload.get("pool", [])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Weekly S&P 500 momentum screen")
    parser.add_argument("--screen", action="store_true", help="run the screen and write the pool")
    parser.add_argument("--universe-size", type=int, default=0,
                        help="limit universe for smoke tests (0 = full)")
    args = parser.parse_args(argv)
    if not args.screen:
        parser.error("nothing to do; pass --screen")
    cfg = load_watchlist_config()
    set_config(cfg)
    build_pool(cfg, limit=args.universe_size or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
