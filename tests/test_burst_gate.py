"""Hermetic tests for the burst-continuation gate (burst_gate.py)."""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest

from burst_gate import detect, forward_alphas, summarize


def _wide_csv(closes: dict[str, list[float]], dates: list[str]) -> str:
    """Serialize a yf wide-CSV layout: Ticker/Price/Date header rows."""
    rows = ["Ticker," + ",".join(f"{t},{t},{t},{t},{t}" for t in closes) + ",",
            "Price," + ",".join("Open,High,Low,Close,Volume" for t in closes) + ",",
            "Date," + ",".join([""] * (5 * len(closes))) + ","]
    for i, d in enumerate(dates):
        cells = []
        for t in closes:
            c = closes[t][i]
            cells += [c, c, c, c, 1e6]
        rows.append(f"{d}," + ",".join(f"{v:g}" for v in cells) + ",")
    return "\n".join(rows) + "\n"


def _load(csv) -> pd.DataFrame:
    from burst_gate import load_close_panel
    return load_close_panel(io.StringIO(csv), min_rows=10)


def _synthetic_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=60)
    rng = np.random.default_rng(7)
    tickers = {t: 100 * np.cumprod(1 + rng.normal(0, 0.008, len(dates)))
               for t in ("AAA", "BBB", "CCC")}
    csv = _wide_csv(tickers, [d.strftime("%Y-%m-%d") for d in dates])
    return _load(csv)


def test_load_close_panel_parses_yf_layout():
    panel = _synthetic_panel()
    assert list(panel.columns) == ["AAA", "BBB", "CCC"]
    assert len(panel) == 60
    assert isinstance(panel.index, pd.DatetimeIndex)


def test_detect_fires_on_engineered_bursts():
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2024-01-01", periods=40)
    closes = {}
    for t in ("AAA", "BBB", "CCC"):
        closes[t] = list(100 * np.cumprod(1 + rng.normal(0, 0.005, len(dates))))
    closes["AAA"][20] = closes["AAA"][19] * 1.05  # +5% 1-day burst at S=20
    # Pure 2-day burst: +3.0% then +3.2% (r2 = +6.3% >= 6%, but each single
    # day < 4% — must fire the 2d rule only, not the 1d rule).
    closes["BBB"][24] = closes["BBB"][23] * 1.03
    closes["BBB"][25] = closes["BBB"][24] * 1.032
    csv = _wide_csv(closes, [d.strftime("%Y-%m-%d") for d in dates])
    panel = _load(csv)
    ev = detect(panel, one_day_pct=4.0, two_day_pct=None)
    assert (ev.ticker == "AAA").any()
    assert not (ev.ticker == "BBB").any()          # sub-4% days must not fire
    ev2 = detect(panel, one_day_pct=None, two_day_pct=6.0)
    assert (ev2.ticker == "BBB").any()
    # AAA's +5% day sits on a flat prior day: r2 = 4.9% < 6% — correctly out.
    assert not (ev2.ticker == "AAA").any()


def test_forward_alpha_uses_same_session_baseline():
    """A market-wide +5% day must produce ~zero alpha for its movers: alpha
    is measured against the same-session universe mean, not raw return."""
    rng = np.random.default_rng(2)
    dates = pd.bdate_range("2024-01-01", periods=40)
    closes = {t: list(100 * np.cumprod(1 + rng.normal(0, 0.004, len(dates))))
              for t in ("AAA", "BBB", "CCC")}
    for t in closes:
        closes[t][20] = closes[t][19] * 1.05        # market-wide burst day
        closes[t][21] = closes[t][20] * 1.05        # market-wide follow-through
    csv = _wide_csv(closes, [d.strftime("%Y-%m-%d") for d in dates])
    panel = _load(csv)
    ev = detect(panel, one_day_pct=4.0, two_day_pct=None)
    fa = forward_alphas(panel, ev)
    s = dates[20]                                   # the engineered session only
    engineered = fa[fa["session"] == s]
    assert len(engineered) == 3
    # Same constant follow-through for every ticker -> alpha ~0 at fwd1; fp
    # noise from the 1.05 multiplication runs ~1e-5, so use a 1e-4 band.
    assert (engineered["fwd1"].abs() < 1e-4).all()


def test_summarize_reports_stats_for_engineered_events():
    """Hand-computed alpha: AAA +50% burst then reversion to 100; BBB/CCC
    flat. Universe of 3, baseline = same-session mean forward return:
    fwd1 alpha = (105/150-1) - mean(105/150-1, 0, 0) = 2/3 * -0.30 = -20%.
    fwd5 alpha = 2/3 * (100/150-1) = -22.22%."""
    dates = pd.bdate_range("2024-01-01", periods=40)
    closes = {"AAA": [100.0] * 40, "BBB": [100.0] * 40, "CCC": [100.0] * 40}
    closes["AAA"][20] = 150.0   # +50% 1-day burst
    closes["AAA"][21] = 105.0   # mean-reversion starts next session
    csv = _wide_csv(closes, [d.strftime("%Y-%m-%d") for d in dates])
    panel = _load(csv)
    ev = detect(panel, one_day_pct=4.0, two_day_pct=None)
    assert list(ev.ticker) == ["AAA"]
    row = summarize(forward_alphas(panel, ev), "union 5/6")
    assert row["n"] == 1
    assert row["alpha1d"] == pytest.approx(-20.0)
    assert row["alpha5d"] == pytest.approx(-22.222, abs=0.002)  # 3dp rounding
    assert row["pos5d"] == 0.0
    assert row["t5d"] is not None


def test_verdict_thresholds():
    from burst_gate import verdict
    adopt = {"rule": "union 4/6", "n": 500, "alpha5d": 0.4, "alpha10d": 0.9}
    dead = {"rule": "union 4/6", "n": 500, "alpha5d": -0.3, "alpha10d": -0.8}
    inc = {"rule": "union 4/6", "n": 12, "alpha5d": 0.4, "alpha10d": 0.9}
    assert verdict(4.0, 6.0, [adopt]).startswith("ADOPT")
    assert verdict(4.0, 6.0, [dead]).startswith("DEAD")
    assert verdict(4.0, 6.0, [inc]).startswith("INCONCLUSIVE")
    assert verdict(4.0, 6.0, []).startswith("INCONCLUSIVE")


def test_no_fire_when_quiet_market():
    closes = {"AAA": [100.0 + i * 0.1 for i in range(40)]}
    dates = pd.bdate_range("2024-01-01", periods=40)
    csv = _wide_csv(closes, [d.strftime("%Y-%m-%d") for d in dates])
    panel = _load(csv)
    assert detect(panel, one_day_pct=4.0, two_day_pct=None).empty
    assert detect(panel, one_day_pct=None, two_day_pct=6.0).empty


def test_run_gate_split_rows_report_both_halves(tmp_path):
    dates = pd.bdate_range("2024-01-01", periods=120)
    closes = {"AAA": [100.0] * 120, "BBB": [100.0] * 120}
    closes["AAA"][40] = 105.0    # burst before the cut (r1 = +5%)
    closes["AAA"][100] = 105.0   # burst after the cut
    csv = _wide_csv(closes, [d.strftime("%Y-%m-%d") for d in dates])
    f = tmp_path / "panel.csv"
    f.write_text(csv)
    from burst_gate import run_gate
    _, table, v = run_gate(f, split_date=dates[60].strftime("%Y-%m-%d"))
    pre = next(r for r in table if "(pre" in r["rule"])
    post = next(r for r in table if "(post" in r["rule"])
    assert pre["n"] == 1 and post["n"] == 1
    assert "ADOPT" in v or "INCONCLUSIVE" in v
