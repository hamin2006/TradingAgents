"""screener.py — weekly S&P 500 momentum screen producing the candidate pool."""

import argparse
import io
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from config import load_watchlist_config
from tradingagents.dataflows.config import set_config

logger = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
UNIVERSE_CACHE_TTL_DAYS = 7
MIN_ROWS = 60


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
    return {
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
        "sma50_spread": last / sma50 - 1,
        "high_proximity": float(last / high),
        "avg_dollar_vol": avg_dollar_vol,
    }


def _zscore(values: pd.Series) -> pd.Series:
    s = pd.Series(values)
    return (s - s.mean()) / s.std()


def score_universe(prices: dict[str, pd.DataFrame],
                   min_dollar_vol: float = 10_000_000) -> list[dict]:
    metrics = {t: m for t, m in ((t, compute_raw_metrics(h)) for t, h in prices.items())
               if m is not None}
    rows = {t: m for t, m in metrics.items() if m["avg_dollar_vol"] >= min_dollar_vol}
    if not rows:
        return []
    frame = pd.DataFrame(rows).T
    score = sum(_zscore(frame[col]).fillna(0.0) for col in
                ("ret_1m", "ret_3m", "ret_6m", "sma50_spread", "high_proximity"))
    ranked = score.sort_values(ascending=False)
    return [{"ticker": t, "score": round(float(s), 4)} for t, s in ranked.items()]


def build_pool(cfg: dict, limit: int | None = None) -> Path:
    universe = fetch_universe(cfg)
    if limit:
        universe = universe[:limit]
    prices = fetch_prices(universe)
    ranked = score_universe(prices)
    path = _results_dir(cfg) / f"pool_{week_key(date.today())}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"year_week": week_key(date.today()),
                                "built_at": datetime.now().isoformat(),
                                "pool": ranked}, indent=2), encoding="utf-8")
    logger.info("pool written to %s with %d tickers", path, len(ranked))
    return path


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
