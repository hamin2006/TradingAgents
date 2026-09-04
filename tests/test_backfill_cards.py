"""backfill_cards tests (hermetic): reconstruct dated decision cards from
pre-observe artifacts (ratings files + per-ticker structured logs)."""

import json

import pytest

from backfill_cards import (
    backfill,
    build_cards,
    load_ratings_for_date,
    parse_pm_payload,
)


def _pm_event(args):
    return {"type": "llm_end", "agent": "Portfolio Manager",
            "tool_calls": [{"name": "PortfolioDecision", "args": args}]}


def _ratings_file(dirpath, date_str, ratings, failures=None):
    p = dirpath / f"ratings_{date_str}.json"
    p.write_text(json.dumps({"date": date_str, "ratings": ratings,
                             "failures": failures or []}))
    return p


def _structured_log(dirpath, date_str, ticker, events):
    p = dirpath / "structured" / date_str / f"{ticker}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in events))
    return p


@pytest.fixture
def logs(tmp_path):
    ratings = tmp_path / "logs"
    ratings.mkdir()
    return ratings


class TestParsePmPayload:
    def test_missing_file_returns_none(self, logs):
        assert parse_pm_payload(logs / "structured" / "x" / "A.jsonl") is None

    def test_returns_last_pm_tool_call_args(self, logs):
        path = _structured_log(logs, "2026-09-04", "HPE", [
            _pm_event({"rating": "Overweight", "executive_summary": "first"}),
            {"type": "llm_end", "agent": "Trader", "tool_calls": [{}]},
            _pm_event({"rating": "Buy", "executive_summary": "second"}),
        ])
        payload = parse_pm_payload(path)
        assert payload["rating"] == "Buy"
        assert payload["executive_summary"] == "second"

    def test_ignores_non_pm_events(self, logs):
        path = _structured_log(logs, "2026-09-04", "HPE", [
            {"type": "llm_end", "agent": "Bull Analyst",
             "tool_calls": [{"name": "Sentiment", "args": {}}]},
        ])
        assert parse_pm_payload(path) is None

    def test_ignores_events_without_tool_calls(self, logs):
        path = _structured_log(logs, "2026-09-04", "HPE", [
            {"type": "llm_end", "agent": "Portfolio Manager", "text": "prose"},
        ])
        assert parse_pm_payload(path) is None

    def test_tolerates_malformed_lines(self, logs):
        p = _structured_log(logs, "2026-09-04", "HPE",
                            [_pm_event({"rating": "Hold"})])
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n{broken\n")
        assert parse_pm_payload(p)["rating"] == "Hold"


class TestLoadRatings:
    def test_returns_ticker_rating_map(self, logs):
        _ratings_file(logs, "2026-09-04", {"HPE": "Overweight", "EL": "Sell"})
        ratings = load_ratings_for_date(logs, "2026-09-04")
        assert ratings == {"HPE": "Overweight", "EL": "Sell"}

    def test_missing_date_yields_empty(self, logs):
        assert load_ratings_for_date(logs, "2026-01-01") == {}


class TestBuildCards:
    def test_card_from_ratings_and_prose(self, logs):
        _ratings_file(logs, "2026-09-04", {"HPE": "Overweight"})
        _structured_log(logs, "2026-09-04", "HPE", [
            _pm_event({"rating": "Overweight",
                       "executive_summary": "2% starter near 54.25",
                       "investment_thesis": "beat-and-raise"})])
        cards = build_cards(["HPE"], logs, days_back=3, as_of="2026-09-04")
        assert len(cards) == 1
        card = cards[0]
        assert card["date"] == "2026-09-04"
        assert card["ticker"] == "HPE"
        assert card["rating"] == "Overweight"          # ratings file wins
        assert card["executive_summary"] == "2% starter near 54.25"
        assert card["investment_thesis"] == "beat-and-raise"
        assert card["execution"] is None               # never fabricated
        assert card["schema_version"] == 1

    def test_rating_only_card_when_no_prose(self, logs):
        _ratings_file(logs, "2026-09-04", {"HPE": "Overweight"})
        cards = build_cards(["HPE"], logs, days_back=3, as_of="2026-09-04")
        card = cards[0]
        assert card["executive_summary"] is None
        assert card["rating"] == "Overweight"

    def test_multi_day_arc_oldest_first(self, logs):
        _ratings_file(logs, "2026-09-03", {"HPE": "Overweight"})
        _structured_log(logs, "2026-09-03", "HPE",
                        [_pm_event({"rating": "Overweight",
                                    "executive_summary": "d1"})])
        _ratings_file(logs, "2026-09-04", {"HPE": "Overweight"})
        cards = build_cards(["HPE"], logs, days_back=3, as_of="2026-09-04")
        assert [c["date"] for c in cards] == ["2026-09-03", "2026-09-04"]

    def test_unrated_day_skipped(self, logs):
        _ratings_file(logs, "2026-09-04", {"AAPL": "Buy"})
        _structured_log(logs, "2026-09-04", "HPE",
                        [_pm_event({"rating": "Buy"})])
        assert build_cards(["HPE"], logs, days_back=3, as_of="2026-09-04") == []

    def test_days_outside_window_skipped(self, logs):
        _ratings_file(logs, "2026-07-01", {"HPE": "Overweight"})
        assert build_cards(["HPE"], logs, days_back=3, as_of="2026-09-04") == []


class TestBackfill:
    def test_dry_run_writes_nothing(self, logs, tmp_path):
        _ratings_file(logs, "2026-09-04", {"HPE": "Overweight"})
        out = tmp_path / "cards"
        written, skipped = backfill(["HPE"], logs, out, days_back=3,
                                    as_of="2026-09-04", dry_run=True)
        assert written == 1 and skipped == 0
        assert not (out / "decision_cards" / "HPE.jsonl").exists()

    def test_write_and_idempotent_rerun(self, logs, tmp_path):
        _ratings_file(logs, "2026-09-04", {"HPE": "Overweight"})
        out = tmp_path / "cards"
        written, skipped = backfill(["HPE"], logs, out, days_back=3,
                                    as_of="2026-09-04")
        assert written == 1
        written2, skipped2 = backfill(["HPE"], logs, out, days_back=3,
                                      as_of="2026-09-04")
        assert written2 == 0 and skipped2 == 1

    def test_card_lands_in_store(self, logs, tmp_path):
        _ratings_file(logs, "2026-09-04", {"HPE": "Overweight"})
        out = tmp_path / "cards"
        backfill(["HPE"], logs, out, days_back=3, as_of="2026-09-04")
        import decision_cards
        latest = decision_cards.latest_card(out, "HPE")
        assert latest["rating"] == "Overweight"
