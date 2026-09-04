"""decision_cards store + injection sizing tests (hermetic)."""


import pytest

from decision_cards import (
    append_card,
    cards_file,
    fresh_cards,
    latest_card,
    load_cards,
    render_prior_decisions,
    select_cards_for_injection,
)

DATE = "2026-09-04"


def _card(date, rating, ticker="EL", summary="thesis text"):
    return {
        "date": date, "ticker": ticker, "rating": rating,
        "ref_close": 100.0, "schema_version": 1,
        "executive_summary": summary, "investment_thesis": "long thesis",
        "execution": {"orders": [], "future_notes": "build on pullback"},
    }


@pytest.fixture
def store(tmp_path):
    (tmp_path / "decision_cards").mkdir()
    return tmp_path


class TestStore:
    def test_append_then_latest_reads_back_last_card(self, store):
        append_card(store, _card("2026-09-03", "Overweight"))
        append_card(store, _card("2026-09-04", "Underweight"))
        latest = latest_card(store, "EL")
        assert latest["date"] == "2026-09-04"
        assert latest["rating"] == "Underweight"

    def test_history_is_retained(self, store):
        append_card(store, _card("2026-09-03", "Overweight"))
        append_card(store, _card("2026-09-04", "Underweight"))
        assert [c["rating"] for c in load_cards(store, "EL")] == [
            "Overweight", "Underweight"]

    def test_per_ticker_isolation(self, store):
        append_card(store, _card("2026-09-03", "Overweight"))
        append_card(store, _card("2026-09-03", "Buy", ticker="MSFT"))
        assert latest_card(store, "EL")["ticker"] == "EL"
        assert len(load_cards(store, "MSFT")) == 1

    def test_missing_ticker_yields_nothing(self, store):
        assert latest_card(store, "AAPL") is None
        assert load_cards(store, "AAPL") == []

    def test_malformed_trailing_line_is_tolerated(self, store):
        append_card(store, _card("2026-09-03", "Overweight"))
        with open(cards_file(store, "EL"), "a", encoding="utf-8") as f:
            f.write("{not json\n")
        assert latest_card(store, "EL")["rating"] == "Overweight"
        assert len(load_cards(store, "EL")) == 1

    def test_malformed_middle_line_keeps_surrounding_cards(self, store):
        append_card(store, _card("2026-09-02", "Buy"))
        with open(cards_file(store, "EL"), "a", encoding="utf-8") as f:
            f.write("{broken\n")
        append_card(store, _card("2026-09-03", "Overweight"))
        cards = load_cards(store, "EL")
        assert [c["rating"] for c in cards] == ["Buy", "Overweight"]

    def test_ticker_path_is_sanitized(self, store):
        assert cards_file(store, "EL").name == "EL.jsonl"
        evil = cards_file(store, "../evil")
        assert evil.parent == store / "decision_cards"
        assert evil.name == "EVIL.jsonl"


class TestFreshCards:
    def test_age_gate_excludes_old_cards(self, store):
        append_card(store, _card("2026-08-10", "Buy"))       # 25 days old
        append_card(store, _card("2026-09-01", "Overweight"))  # fresh
        fresh = fresh_cards(store, "EL", max_age_days=21, as_of=DATE)
        assert [c["rating"] for c in fresh] == ["Overweight"]

    def test_age_gate_boundary_inclusive(self, store):
        append_card(store, _card("2026-08-14", "Overweight"))  # exactly 21 days
        fresh = fresh_cards(store, "EL", max_age_days=21, as_of=DATE)
        assert len(fresh) == 1

    def test_returns_oldest_first(self, store):
        append_card(store, _card("2026-09-01", "Overweight"))
        append_card(store, _card("2026-09-03", "Buy"))
        append_card(store, _card("2026-09-04", "Overweight"))
        fresh = fresh_cards(store, "EL", max_age_days=21, as_of=DATE)
        assert [c["date"] for c in fresh] == ["2026-09-01", "2026-09-03",
                                              "2026-09-04"]


class TestInjectionSizing:
    def test_no_cards_injects_nothing(self):
        assert select_cards_for_injection([], flip_max=3) == []

    def test_single_fresh_card_injects_latest(self):
        cards = [_card("2026-09-03", "Overweight")]
        assert select_cards_for_injection(cards, flip_max=3) == cards

    def test_stable_ratings_inject_latest_only(self):
        cards = [_card("2026-09-01", "Overweight"),
                 _card("2026-09-03", "Overweight")]
        assert select_cards_for_injection(cards, flip_max=3) == [cards[-1]]

    def test_flip_injects_tail_up_to_flip_max(self):
        cards = [_card("2026-09-01", "Buy"),
                 _card("2026-09-02", "Overweight"),
                 _card("2026-09-03", "Underweight"),
                 _card("2026-09-04", "Sell")]
        picked = select_cards_for_injection(cards, flip_max=3)
        assert picked == cards[1:]

    def test_flip_with_short_history_injects_what_exists(self):
        cards = [_card("2026-09-03", "Overweight"),
                 _card("2026-09-04", "Underweight")]
        assert select_cards_for_injection(cards, flip_max=3) == cards

    def test_flip_beyond_age_gate_is_stable(self):
        # Old flip happened at 30 days; the fresh set holds one rating only,
        # so the fresh latest-two share a rating -> latest card only.
        fresh = [_card("2026-09-02", "Overweight"),
                 _card("2026-09-04", "Overweight")]
        assert select_cards_for_injection(fresh, flip_max=3) == [fresh[-1]]

    def test_flip_max_respected_on_long_flip_arc(self):
        cards = [_card(f"2026-08-{d:02d}", "Overweight" if d % 2 else "Buy")
                 for d in range(25, 29)]
        cards.append(_card("2026-09-01", "Sell"))
        picked = select_cards_for_injection(cards, flip_max=2)
        assert picked == cards[-2:]


class TestRender:
    def test_empty_cards_render_empty(self):
        assert render_prior_decisions("EL", []) == ""

    def test_render_carries_date_and_overridability_language(self):
        block = render_prior_decisions("EL", [_card("2026-09-03", "Overweight")])
        assert "EL" in block
        assert "2026-09-03" in block
        assert "Overweight" in block
        assert "current evidence governs" in block
        assert "overturn" in block

    def test_render_flip_arc_shows_both_cards(self):
        cards = [_card("2026-09-03", "Overweight"),
                 _card("2026-09-04", "Underweight")]
        block = render_prior_decisions("EL", cards)
        assert block.index("2026-09-04") < block.index("2026-09-03")
        assert block.index("Underweight") < block.index("Overweight")

    def test_render_survives_missing_summary(self):
        card = _card("2026-09-03", "Overweight")
        del card["executive_summary"]
        assert "2026-09-03" in render_prior_decisions("EL", [card])
