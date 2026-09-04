"""tests/test_market_tape.py — hermetic tape/regime line-builder tests."""

from __future__ import annotations

import pandas as pd
import pytest

import market_tape


def _series(*values) -> pd.Series:
    idx = pd.date_range("2026-08-28", periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


@pytest.fixture(autouse=True)
def _no_memo_leak():
    market_tape.reset_tape_cache()
    yield
    market_tape.reset_tape_cache()


def test_spy_above_200d_with_vix(monkeypatch):
    monkeypatch.setattr(market_tape, "_spy_tape",
                        lambda: {"close": 620.0, "sma200": 600.0})
    monkeypatch.setattr(market_tape, "_vix_tape", lambda: {"close": 14.2})
    monkeypatch.setattr(market_tape, "_etf_change", lambda etf: 1.5)
    line = market_tape.tape_line(sector="Healthcare")
    assert "SPY 620.00 above its 200d SMA (600.00)" in line
    assert "VIX 14.2" in line
    assert "XLV (Healthcare) +1.5%" in line


def test_spy_below_200d(monkeypatch):
    monkeypatch.setattr(market_tape, "_spy_tape",
                        lambda: {"close": 590.0, "sma200": 600.0})
    monkeypatch.setattr(market_tape, "_vix_tape", lambda: {"close": 21.0})
    line = market_tape.tape_line()
    assert "below its 200d SMA" in line


def test_partial_failure_drops_only_that_clause(monkeypatch):
    """A broken sector fetch must not kill the SPY/VIX clauses."""
    monkeypatch.setattr(market_tape, "_spy_tape",
                        lambda: {"close": 620.0, "sma200": 600.0})
    monkeypatch.setattr(market_tape, "_vix_tape", lambda: {"close": 14.2})

    def boom(_etf):
        raise OSError("no net")

    monkeypatch.setattr(market_tape, "_etf_change", boom)
    line = market_tape.tape_line(sector="Healthcare")
    assert "SPY 620.00" in line and "VIX 14.2" in line
    assert "XLV" not in line


def test_total_failure_returns_empty(monkeypatch):
    def boom():
        raise OSError("no net")

    monkeypatch.setattr(market_tape, "_spy_tape", boom)
    monkeypatch.setattr(market_tape, "_vix_tape", boom)
    assert market_tape.tape_line() == ""


def test_memoized_within_ttl(monkeypatch):
    calls = {"n": 0}

    def spy():
        calls["n"] += 1
        return {"close": 620.0, "sma200": 600.0}

    monkeypatch.setattr(market_tape, "_spy_tape", spy)
    monkeypatch.setattr(market_tape, "_vix_tape", lambda: {"close": 14.2})
    monkeypatch.setattr(market_tape, "_etf_change", lambda etf: None)
    market_tape.reset_tape_cache()
    market_tape.tape_line("Healthcare")
    market_tape.tape_line("Healthcare")
    market_tape.tape_line("Technology")
    assert calls["n"] == 2  # per-sector cache keys


def test_unknown_sector_omits_sector_clause(monkeypatch):
    monkeypatch.setattr(market_tape, "_spy_tape",
                        lambda: {"close": 620.0, "sma200": 600.0})
    monkeypatch.setattr(market_tape, "_vix_tape", lambda: {"close": 14.2})
    line = market_tape.tape_line(sector="Weird Sector")
    assert "SPY 620.00" in line
    assert "Weird Sector" not in line
