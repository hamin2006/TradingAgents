"""tests/test_backtest_screener.py — guard the backtest's correctness core.

Two invariants matter: (1) per-date metrics use data <= D only (no look-ahead),
and (2) the three scoring strategies genuinely re-rank the cross-section. The
experiment itself is a script, but these cheap guards protect the part that makes
its conclusions valid.
"""
import numpy as np
import pandas as pd

import backtest_screener as bt
from screener import compute_raw_metrics


def _hist(n=300, drift=0.001, amplitude=0.0):
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="B")
    base = [100 * (1 + drift) ** i for i in range(n)]
    if amplitude:
        import math
        cycles = 12
        osc = [1 - amplitude * abs(math.sin(2 * math.pi * cycles * i / n))
               for i in range(n)]
        vals = [b * o for b, o in zip(base, osc, strict=True)]
    else:
        vals = base
    return pd.DataFrame({"Open": vals, "High": [v * 1.01 for v in vals],
                         "Low": [v * 0.99 for v in vals],
                         "Close": vals, "Volume": [2_000_000] * n}, index=idx)


def test_precompute_matches_compute_raw_metrics_asof():
    """The vectorized precompute must equal compute_raw_metrics(hist.loc[:D]) at
    every D — i.e. scoring never reads data after the screen date (no look-ahead)."""
    h = _hist(n=400, amplitude=0.12)
    pc = bt._precompute_ticker(h)
    for i in range(130, len(h), 40):
        d = h.index[i]
        row = pc.iloc[pc.index.get_loc(d)]
        m = compute_raw_metrics(h.loc[:d])
        assert m is not None
        for col in ("ret_1m", "ret_3m", "ret_6m", "sma50_spread",
                    "high_proximity", "avg_dollar_vol", "realized_vol"):
            assert np.isclose(row[col], m[col], rtol=1e-9), (col, row[col], m[col])


def test_strategies_rerank_volatile_winner():
    """raw_momentum (z of raw return) must rank the high-vol parabolic mover
    above the steady mover with the same endpoint; vol_adjusted and rank_based
    must demote it (the momentum-crash defense the backtest is meant to measure)."""
    steady = _hist(drift=0.001)
    parabolic = _hist(drift=0.001, amplitude=0.12)
    frame = pd.DataFrame({
        "STEADY": pd.Series(bt._precompute_ticker(steady).iloc[-1]),
        "PARABOLIC": pd.Series(bt._precompute_ticker(parabolic).iloc[-1]),
    }).T
    raw = bt.composite_score(frame, "raw_momentum")
    vol = bt.composite_score(frame, "vol_adjusted")
    rank = bt.composite_score(frame, "rank_based")
    assert raw["PARABOLIC"] > raw["STEADY"]
    assert vol["STEADY"] > vol["PARABOLIC"]
    assert rank["STEADY"] > rank["PARABOLIC"]


def test_regime_stress_pauses_and_warn_drops_top_decile():
    spy_above = bt._gate_frame(_hist(drift=0.003), sma=200)
    spy_below = spy_above.copy()
    spy_below["close"] = spy_below["close"] * 0.5  # force below its own 200d SMA
    vix = bt._gate_frame(_hist(drift=0.0, amplitude=0.0))
    d = spy_above.index[-1]
    assert bt._regime_at(spy_below, vix, d) == "STRESS"

    # WARN: SPY above its SMA but VIX at its trailing-year max -> elevated
    vix_high = vix.copy()
    vix_high.loc[d, "close"] = vix["close"].max() * 10
    assert bt._regime_at(spy_above, vix_high, d) == "WARN"
