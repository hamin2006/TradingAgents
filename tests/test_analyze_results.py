"""tests/test_analyze_results.py — outcome analytics tests."""

import pytest

import analyze_results as ar


def test_parse_pct():
    assert ar.parse_pct("+2.3%") == 2.3
    assert ar.parse_pct("-1.0%") == -1.0
    assert ar.parse_pct("0.0%") == 0.0
    assert ar.parse_pct(None) is None
    assert ar.parse_pct("garbage") is None


def _e(ticker, rating, raw, alpha, date, pending=False):
    return {"ticker": ticker, "rating": rating, "raw": raw, "alpha": alpha,
            "date": date, "pending": pending}


def test_compute_stats_skips_pending_and_unparseable():
    entries = [
        _e("AAPL", "Buy", "+2.0%", "+1.0%", "2026-08-25"),
        _e("MSFT", "Buy", None, None, "2026-08-25", pending=True),   # unresolved
        _e("NVDA", "Hold", "garbage", None, "2026-08-25"),            # unparseable
    ]
    stats = ar.compute_stats(entries)
    assert stats["total_resolved"] == 1


def test_compute_stats_tier_aggregates():
    entries = [
        _e("AAPL", "Buy", "+2.0%", "+1.0%", "2026-08-20"),
        _e("NVDA", "Buy", "-1.0%", "-0.5%", "2026-08-21"),
        _e("MSFT", "Overweight", "+3.0%", "+2.0%", "2026-08-22"),
        _e("TSLA", "Sell", "-4.0%", "-2.0%", "2026-08-23"),   # correct: alpha < 0
        _e("GOOGL", "Underweight", "+1.0%", "+0.5%", "2026-08-24"),  # wrong: alpha > 0
        _e("AMZN", "Hold", "+0.5%", "+0.1%", "2026-08-25"),
    ]
    stats = ar.compute_stats(entries)
    buy = stats["tiers"]["Buy/Overweight"]
    assert buy["n"] == 3
    assert buy["hit_rate"] == pytest.approx(2 / 3)
    assert buy["avg_alpha"] == pytest.approx((1.0 - 0.5 + 2.0) / 3)

    sell = stats["tiers"]["Sell/Underweight"]
    assert sell["n"] == 2
    assert sell["hit_rate"] == pytest.approx(0.5)  # TSLA correct, GOOGL wrong

    hold = stats["tiers"]["Hold"]
    assert hold["n"] == 1
    assert hold["hit_rate"] is None  # no directional claim


def test_compute_stats_per_ticker():
    entries = [
        _e("AAPL", "Buy", "+2.0%", "+1.0%", "2026-08-20"),
        _e("AAPL", "Hold", "+0.5%", "+0.3%", "2026-08-25"),
        _e("NVDA", "Buy", "-1.0%", "-0.5%", "2026-08-21"),
    ]
    stats = ar.compute_stats(entries)
    assert stats["tickers"]["AAPL"]["n"] == 2
    assert stats["tickers"]["AAPL"]["avg_alpha"] == pytest.approx(0.65)
    assert stats["tickers"]["NVDA"]["avg_alpha"] == pytest.approx(-0.5)


def test_compute_stats_streaks():
    entries = [
        _e("A", "Buy", "+1%", "-1%", "2026-08-20"),   # loss
        _e("B", "Buy", "+1%", "-2%", "2026-08-21"),   # loss
        _e("C", "Buy", "+1%", "+3%", "2026-08-22"),   # win
        _e("D", "Buy", "+1%", "+1%", "2026-08-25"),   # win
    ]
    stats = ar.compute_stats(entries)
    assert stats["streaks"]["longest_loss"] == 2
    assert stats["streaks"]["longest_win"] == 2
    assert stats["streaks"]["current"] == ("win", 2)


def test_compute_stats_empty():
    stats = ar.compute_stats([])
    assert stats["total_resolved"] == 0
    assert stats["streaks"]["current"] == (None, 0)


def test_render_report_contains_key_numbers():
    entries = [
        _e("AAPL", "Buy", "+2.0%", "+1.0%", "2026-08-20"),
        _e("NVDA", "Buy", "-1.0%", "-0.5%", "2026-08-21"),
        _e("TSLA", "Sell", "-4.0%", "-2.0%", "2026-08-22"),
    ]
    report = ar.render_report(ar.compute_stats(entries), as_of="2026-08-30")
    assert "Outcome Analytics" in report
    assert "Buy/Overweight" in report
    assert "Sell/Underweight" in report
    assert "1 of 2" in report or "50%" in report  # directional hit rate visible
