"""tests/test_screener.py"""
import json
from datetime import date

import pandas as pd
import pytest

from screener import (
    build_pool,
    compute_raw_metrics,
    fetch_prices,
    load_pool,
    score_universe,
    week_key,
)


def _hist(n=130, start_price=100.0, drift=0.0):
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="B")
    vals = [start_price * (1 + drift) ** i for i in range(n)]
    df = pd.DataFrame({"Open": vals, "High": [v * 1.01 for v in vals],
                       "Low": [v * 0.99 for v in vals],
                       "Close": vals, "Volume": [2_000_000] * n},
                      index=idx)
    return df


def test_week_key():
    assert week_key(date(2026, 8, 30)) == "2026-35"


def test_compute_raw_metrics_uptrend():
    m = compute_raw_metrics(_hist(drift=0.001))
    assert m is not None
    assert m["ret_1m"] > 0 and m["sma50_spread"] > 0
    assert m["avg_dollar_vol"] == pytest.approx(200 * 2_000_000, rel=0.5)


def test_compute_raw_metrics_too_short():
    assert compute_raw_metrics(_hist(n=10)) is None


def test_score_universe_ranks_momentum_first():
    prices = {
        "WINNER": _hist(drift=0.003),
        "LOSER": _hist(drift=-0.003),
    }
    ranked = score_universe(prices)
    assert ranked[0]["ticker"] == "WINNER"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_score_universe_liquidity_filter():
    prices = {"LIQUID": _hist(), "THIN": _hist(n=130)}
    prices["THIN"]["Volume"] = [1_000] * 130  # ~$100k/day
    ranked = score_universe(prices, min_dollar_vol=10_000_000)
    assert all(r["ticker"] != "THIN" for r in ranked)


def test_build_and_load_pool_roundtrip(tmp_path, monkeypatch):
    import config as config_mod
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    monkeypatch.setattr(config_mod, "load_watchlist_config", lambda *a, **k: cfg)
    monkeypatch.setattr("screener.fetch_universe", lambda cfg: ["AAA", "BBB"])
    monkeypatch.setattr("screener.fetch_prices", lambda u, period="6mo": {
        "AAA": _hist(drift=0.002), "BBB": _hist(drift=-0.002)})

    path = build_pool(cfg)
    assert path.exists()
    pool = load_pool(cfg)
    assert pool[0]["ticker"] == "AAA"
    assert len(pool) == 2
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    assert "year_week" in payload and "pool" in payload


def test_load_pool_missing_returns_empty(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    assert load_pool(cfg) == []


def test_fetch_prices_batched(monkeypatch):
    import screener
    captured = {}

    class FakeFrame(pd.DataFrame):
        pass

    def fake_download(tickers, **kwargs):
        captured["tickers"] = tickers
        return FakeFrame()
    monkeypatch.setattr(screener.yf, "download", fake_download)
    prices = fetch_prices(["AAA", "BBB"])
    assert captured["tickers"] == "AAA BBB"
    assert isinstance(prices, dict)
