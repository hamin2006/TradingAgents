"""decision_cards store + injection sizing tests (hermetic)."""


import pytest

import decision_cards
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


class TestRenderFullSummaryAndExecution:
    LONG_SUMMARY = "s" * 300
    PM_SUMMARY = ("Initiate DASH with a measured starter probe of 1-2% of book "
                  "near $222; scale additional capital only on stabilization "
                  "near $205-$210, a reclaim of the 10-EMA with improving "
                  "momentum, or a breakout above $236.93; cap total exposure at "
                  "6%; hard stop at $205.50 on the probe; horizon 3-6 months.")

    def test_full_summary_not_truncated(self):
        card = _card("2026-09-04", "Overweight",
                     summary=self.PM_SUMMARY)
        block = render_prior_decisions("DASH", [card])
        assert "stabilization near $205-$210" in block
        assert "hard stop at $205.50" in block
        assert len(self.PM_SUMMARY) > 220  # the old cut would have dropped these

    def test_thesis_not_rendered_in_prompt(self):
        card = _card("2026-09-04", "Overweight", summary="short plan")
        card["investment_thesis"] = "a very long re-litigable essay " * 50
        block = render_prior_decisions("EL", [card])
        assert "essay" not in block

    def test_execution_orders_render_deterministically(self):
        card = _card("2026-09-04", "Overweight", summary="plan")
        card["execution"] = {"orders": [
            {"kind": "BUY", "value_usd": 200.0, "limit_px": 54.25,
             "stop_px": 45.7, "cap_value_usd": 500.0}],
            "future_notes": "add tranche 2 on MACD flip"}
        block = render_prior_decisions("HPE", [card])
        assert "BUY $200" in block
        assert "<= $54.25" in block or "≤ $54.25" in block
        assert "stop $45.70" in block
        assert "cap $500" in block
        assert "add tranche 2 on MACD flip" in block

    def test_partial_sell_orders_render(self):
        card = _card("2026-09-04", "Underweight", summary="trim")
        card["execution"] = {"orders": [
            {"kind": "SELL", "shares": 2, "limit_px": 100.5,
             "stop_px": 95.6}]}
        block = render_prior_decisions("EL", [card])
        assert "SELL 2 shares" in block
        assert ">= $100.50" in block or "≥ $100.50" in block

    def test_actual_engine_orders_render(self):
        card = _card("2026-09-04", "Overweight", summary="plan")
        card["actual"] = {"orders": [
            {"action": "BUY", "shares": 3, "stop_price": 202.0}],
            "note": "pre-binding: legacy engine"}
        block = render_prior_decisions("DASH", [card])
        assert "actual (legacy engine" in block or "actual" in block
        assert "BUY 3" in block


class TestOutcomes:
    """execution_outcome events: what the engine ACTUALLY did vs the intent."""

    @staticmethod
    def _outcome(date=DATE, ticker="EL", **kw):
        o = {"type": "execution_outcome", "schema_version": 1, "date": date,
             "ticker": ticker, "binding_active": False, "gate_verdict": None,
             "gate_reasons": [], "pm_orders": None, "actual": [],
             "remaining": 8, "stop_anchored": None, "note": None}
        o.update(kw)
        return o

    def test_append_then_load_roundtrips(self, store):
        o = self._outcome()
        decision_cards.append_outcome(store, o)
        loaded = decision_cards.load_outcomes(store, "EL")
        assert len(loaded) == 1
        assert loaded[0]["date"] == DATE
        assert loaded[0]["remaining"] == 8

    def test_outcomes_are_not_cards(self, store):
        """Flip logic and latest_card must never see outcome events."""
        decision_cards.append_outcome(store, self._outcome())
        assert load_cards(store, "EL") == []
        assert latest_card(store, "EL") is None
        decision_cards.append_card(store, _card(DATE, "Hold"))
        assert len(load_cards(store, "EL")) == 1

    def test_render_appends_outcome_under_matching_date(self, store):
        o = self._outcome(
            binding_active=False, gate_verdict="FAIL",
            gate_reasons=["DELL: empty execution orders"],
            pm_orders=[["SELL", 2], ["SELL", 2]],
            actual=[{"action": "SELL", "shares": 13, "filled": 1,
                     "avg_price": 52.75}],
            remaining=12)
        block = decision_cards.render_prior_decisions(
            "EL", [_card(DATE, "Underweight")], outcomes=[o])
        assert "outcome:" in block
        assert "binding OFF (gate FAIL)" in block
        assert "SELL 13 -> 1 filled @ $52.75" in block
        assert "12 remain" in block

    def test_render_without_outcomes_unchanged(self, store):
        """Regression: no outcome events -> identical rendering."""
        block = decision_cards.render_prior_decisions(
            "EL", [_card(DATE, "Hold")])
        assert "outcome:" not in block
        block2 = decision_cards.render_prior_decisions(
            "EL", [_card(DATE, "Hold")],
            outcomes=[self._outcome(date="2026-01-01")])
        assert block2 == block  # outcome for another date never leaks in

    def test_render_outcome_note_included(self, store):
        o = self._outcome(note="exit completed manually at 12:18 ET")
        block = decision_cards.render_prior_decisions(
            "EL", [_card(DATE, "Hold")], outcomes=[o])
        assert "exit completed manually" in block

    def test_render_outcome_includes_resting_oco_levels(self, store):
        o = self._outcome(protection={"kind": "oco", "qty": 3,
                                      "target_px": 120.0, "stop_px": 92.0})
        block = decision_cards.render_prior_decisions(
            "EL", [_card(DATE, "Hold")], outcomes=[o])
        assert "OCO target $120.00" in block
        assert "stop $92.00" in block

    def test_corrected_outcome_supersedes_in_render(self, store):
        """Reconciliation appends a corrected outcome for the same date; the
        renderer must use the LATEST event (ZBRA 2026-09-10: the card said
        '1 remain' while the broker-side stop had already sold it)."""
        original = self._outcome(remaining=1)
        corrected = self._outcome(
            remaining=0, reconciled=True,
            actual=[{"action": "SELL(STOP)", "shares": 1, "filled": 1,
                     "avg_price": 336.08, "source": "broker-stop"}],
            note="broker-side stop filled 1 EL @ $336.08")
        decision_cards.append_outcome(store, original)
        decision_cards.append_outcome(store, corrected)

        block = decision_cards.render_prior_decisions(
            "EL", [_card(DATE, "Hold")],
            outcomes=decision_cards.load_outcomes(store, "EL"))

        assert "0 remain" in block
        assert "1 remain" not in block
        assert "broker-side stop filled" in block

    def test_append_outcome_failure_safe(self, tmp_path):
        """An outcome write must never raise (execution already done)."""
        blocker = tmp_path / "afile"
        blocker.write_text("")
        root = blocker / "decision_cards"  # parent is a regular file
        assert decision_cards.append_outcome(str(root), self._outcome()) is None
