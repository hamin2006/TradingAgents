"""tests/test_screener.py"""
import json
from datetime import date

import pandas as pd
import pytest

import screener
from screener import (
    build_pool,
    compute_raw_metrics,
    fetch_prices,
    fetch_universe,
    load_pool,
    load_regime,
    regime_at,
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


def test_fetch_universe_sends_ua_and_caches(tmp_path, monkeypatch):
    """Wikipedia 403s requests without a User-Agent; the fetch must send one
    and cache the parsed universe (regression for the weekly screen)."""
    html = """<html><body><table>
    <tr><th>Symbol</th><th>Security</th></tr>
    <tr><td>AAPL</td><td>Apple Inc.</td></tr>
    <tr><td>MSFT</td><td>Microsoft Corp.</td></tr>
    </table></body></html>"""

    captured = {}

    class FakeResp:
        text = html

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["kwargs"] = kwargs
        return FakeResp()

    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)

    monkeypatch.setattr("screener.requests.get", fake_get)
    symbols = fetch_universe(cfg)
    assert symbols == ["AAPL", "MSFT"]
    assert "User-Agent" in captured["kwargs"]["headers"]
    assert (tmp_path / "universe_sp500.json").exists()  # cached


def test_fetch_universe_falls_back_to_cache_on_error(tmp_path, monkeypatch):
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    (tmp_path / "universe_sp500.json").write_text(
        json.dumps(["CACHED"]), encoding="utf-8")

    def fake_get(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("screener.requests.get", fake_get)
    assert fetch_universe(cfg) == ["CACHED"]


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


def test_build_pool_uses_et_week_key(tmp_path, monkeypatch):
    """Pool files must be keyed by the ET week, not the server-local date
    (a non-ET host at Sunday 18:00 ET may already be Monday locally)."""
    from datetime import date

    import config as config_mod
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    monkeypatch.setattr(config_mod, "load_watchlist_config", lambda *a, **k: cfg)
    monkeypatch.setattr("screener.fetch_universe", lambda cfg: ["AAA"])
    monkeypatch.setattr("screener.fetch_prices", lambda u, period="6mo": {
        "AAA": _hist(drift=0.002)})
    monkeypatch.setattr("screener.today_et", lambda: date(2026, 8, 30))  # Sunday
    path = build_pool(cfg)
    assert path.name == "pool_2026-35.json"


def _oscillating_hist(n=130, drift=0.001, amplitude=0.15):
    """Same endpoint and same 52w-high signature as the steady path, but with
    much higher realized vol (dips only, so max == final value for both)."""
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="B")
    base = [100 * (1 + drift) ** i for i in range(n)]
    cycles = 12
    osc = [1 - amplitude * abs(__import__("math").sin(2 * 3.14159 * cycles * i / n))
           for i in range(n)]
    vals = [b * o for b, o in zip(base, osc, strict=True)]
    return pd.DataFrame({"Open": vals, "High": [v * 1.01 for v in vals],
                         "Low": [v * 0.99 for v in vals],
                         "Close": vals, "Volume": [2_000_000] * n}, index=idx)


def test_compute_raw_metrics_includes_realized_vol():
    steady = compute_raw_metrics(_hist(drift=0.001))
    wild = compute_raw_metrics(_oscillating_hist())
    assert steady["realized_vol"] > 0
    assert wild["realized_vol"] > steady["realized_vol"]


def test_score_universe_prefers_steady_over_parabolic():
    """Vol-adjusted momentum: a high-vol parabolic path with the same endpoint
    returns must rank BELOW a steadier mover (momentum-crash defense)."""
    prices = {"STEADY": _hist(drift=0.001), "PARABOLIC": _oscillating_hist()}
    ranked = score_universe(prices)
    by_ticker = {r["ticker"]: r["score"] for r in ranked}
    assert by_ticker["STEADY"] > by_ticker["PARABOLIC"]


def test_vol_floor_bounds_tiny_vol_names():
    """A near-zero-vol name must not produce an infinite score via division."""
    hist = _hist(drift=0.001)
    hist["Close"] = hist["Close"] * 1.0  # already smooth; vol is small but nonzero
    # realized vol of a smooth ramp is small; the floor applies in scoring
    ranked = score_universe({"SMOOTH": hist})
    import math
    assert math.isfinite(ranked[0]["score"])


def test_score_universe_default_strategy_is_raw():
    """Production default switched to raw_momentum + regime gate (5y backtest)."""
    prices = {"STEADY": _hist(drift=0.001), "PARABOLIC": _oscillating_hist()}
    assert score_universe(prices) == score_universe(prices, strategy="raw_momentum")


def test_vol_adjusted_still_demotes_parabolic():
    """The vol-adjusted property remains available as a registry strategy."""
    prices = {"STEADY": _hist(drift=0.001), "PARABOLIC": _oscillating_hist()}
    ranked = score_universe(prices, strategy="vol_adjusted")
    by_ticker = {r["ticker"]: r["score"] for r in ranked}
    assert by_ticker["STEADY"] > by_ticker["PARABOLIC"]


def _frame(closes, volume=2_000_000):
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=len(closes),
                        freq="B")
    return pd.DataFrame({"Close": closes, "Volume": [volume] * len(closes)},
                        index=idx)


def test_regime_at_stress_when_spy_below_sma200():
    closes = [100 + i * 0.1 for i in range(200)] + [100 - i * 1.0 for i in range(1, 21)]
    spy = _frame(closes)
    vix = _frame([15.0] * len(closes))
    assert regime_at(spy, vix) == "STRESS"


def test_regime_at_warn_on_vix_spike():
    closes = [100 + i * 0.05 for i in range(250)]
    spy = _frame(closes)
    vix = _frame([15.0] * 245 + [40.0] * 5)  # spike into the top percentile
    assert regime_at(spy, vix) == "WARN"


def test_regime_at_calm_by_default():
    spy = _frame([100 + i * 0.05 for i in range(250)])
    vix = _frame([20.0 - i * 0.01 for i in range(250)])  # declining: last value = lowest pct
    assert regime_at(spy, vix) == "CALM"


def test_regime_at_insufficient_data_fails_open():
    spy = _frame([100.0] * 50)  # < 200 days
    vix = _frame([15.0] * 50)
    assert regime_at(spy, vix) == "CALM"


def _gate_cfg(tmp_path, monkeypatch, spy, vix, n=12):
    import config as config_mod
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    monkeypatch.setattr(config_mod, "load_watchlist_config", lambda *a, **k: cfg)
    monkeypatch.setattr("screener.fetch_universe", lambda cfg: [f"T{i:02d}" for i in range(n)])
    monkeypatch.setattr("screener.fetch_prices",
                        lambda u, period="6mo": {t: _hist(drift=0.001) for t in u})
    monkeypatch.setattr("screener.fetch_gate_data", lambda cfg: (spy, vix))
    return cfg


def test_build_pool_stress_pauses_buys(tmp_path, monkeypatch):
    closes = [100 + i * 0.1 for i in range(200)] + [100 - i * 1.0 for i in range(1, 21)]
    cfg = _gate_cfg(tmp_path, monkeypatch, _frame(closes), _frame([15.0] * 220))
    path = build_pool(cfg)
    payload = json.loads(path.read_text())
    assert payload["regime"] == "STRESS"
    assert payload["pool"] == []


def test_build_pool_warn_drops_top_decile_1m_tail(tmp_path, monkeypatch):
    closes = [100 + i * 0.05 for i in range(250)]
    spy, vix = _frame(closes), _frame([15.0] * 245 + [40.0] * 5)  # WARN
    cfg = _gate_cfg(tmp_path, monkeypatch, spy, vix)
    # one ticker with a parabolic 1m spike -> clearly the top-decile 1m name
    spiked = _hist(drift=0.001)
    spiked["Close"] = spiked["Close"].astype(float)
    spiked.loc[spiked.index[-21]:] *= 2.0
    prices = {t: _hist(drift=0.001) for t in [f"T{i:02d}" for i in range(12)]}
    prices["T05"] = spiked
    monkeypatch.setattr("screener.fetch_prices",
                        lambda u, period="6mo": prices)
    path = build_pool(cfg)
    payload = json.loads(path.read_text())
    assert payload["regime"] == "WARN"
    assert "T05" not in [r["ticker"] for r in payload["pool"]]
    assert len(payload["pool"]) == 11


def test_build_pool_calm_keeps_all(tmp_path, monkeypatch):
    spy = _frame([100 + i * 0.05 for i in range(250)])
    vix = _frame([20.0 - i * 0.01 for i in range(250)])  # declining VIX -> CALM
    cfg = _gate_cfg(tmp_path, monkeypatch, spy, vix)
    payload = json.loads(build_pool(cfg).read_text())
    assert payload["regime"] == "CALM"
    assert len(payload["pool"]) == 12


def test_load_regime_defaults_calm(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    assert load_regime(cfg) == "CALM"
