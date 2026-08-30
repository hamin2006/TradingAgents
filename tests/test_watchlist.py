"""tests/test_watchlist.py"""
from datetime import date, timedelta

import pytest

from daily_run import TODAY_ET, WatchlistShortError, assemble_watchlist, extract_rating

POOL = [{"ticker": "NVDA", "score": 3.0}, {"ticker": "AAPL", "score": 2.5},
        {"ticker": "AMD", "score": 2.0}, {"ticker": "MSFT", "score": 1.5},
        {"ticker": "GOOGL", "score": 1.0}, {"ticker": "META", "score": 0.5}]
TODAY = date(2026, 8, 31)  # Monday


def _entry(ticker, days_ago, rating="Hold"):
    return {"ticker": ticker, "rating": rating,
            "date": (TODAY - timedelta(days=days_ago)).isoformat()}


def test_extract_rating():
    assert extract_rating("**Rating**: Buy\n\nExecutive Summary: ...") == "Buy"
    assert extract_rating("no rating word here") == "Hold"


def test_holdings_always_included():
    got = assemble_watchlist({"TSLA"}, POOL, [], {}, TODAY)
    assert "TSLA" in got


def test_top_candidates_taken():
    got = assemble_watchlist(set(), POOL, [], {"candidate_slots": 2,
                                               "min_watchlist_size": 2}, TODAY)
    assert got[:2] == sorted(["NVDA", "AAPL"])


def test_recently_analyzed_excluded():
    entries = [_entry("NVDA", 1)]
    got = assemble_watchlist(set(), POOL, entries, {"candidate_slots": 3}, TODAY)
    assert "NVDA" not in got
    assert "AAPL" in got


def test_recent_sell_rating_excluded():
    entries = [_entry("NVDA", 2, rating="Sell")]
    got = assemble_watchlist(set(), POOL, entries, {"candidate_slots": 3}, TODAY)
    assert "NVDA" not in got


def test_old_entries_do_not_exclude():
    entries = [_entry("NVDA", 10)]
    got = assemble_watchlist(set(), POOL, entries, {"candidate_slots": 3,
                                                    "exclusion_days": 7}, TODAY)
    assert "NVDA" in got


def test_min_size_topup():
    got = assemble_watchlist(set(), POOL, [],
                             {"candidate_slots": 1, "min_watchlist_size": 5}, TODAY)
    assert len(got) >= 5


def test_min_size_gate_fails_loudly():
    with pytest.raises(WatchlistShortError):
        assemble_watchlist(set(), [{"ticker": "NVDA", "score": 1.0}], [],
                           {"candidate_slots": 1, "min_watchlist_size": 5}, TODAY)


def test_empty_pool_uses_seed():
    got = assemble_watchlist(set(), [], [],
                             {"seed_watchlist": ["AAPL", "MSFT"],
                              "min_watchlist_size": 2}, TODAY)
    assert got == ["AAPL", "MSFT"]


def test_today_et_is_date():
    assert isinstance(TODAY_ET(), date)
