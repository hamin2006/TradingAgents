"""pm_execution schema + extractor tests (hermetic)."""

import pytest
from pydantic import ValidationError

from pm_execution import (
    EXECUTION_ABSENT,
    EXECUTION_INVALID,
    EXECUTION_VALID,
    ExecutionIntent,
    ExecutionPortfolioDecision,
    PmOrder,
    PmOrderKind,
    extract_execution,
)


class TestPmOrderSizing:
    def test_value_usd_only_valid(self):
        order = PmOrder(kind=PmOrderKind.BUY, value_usd=200.0)
        assert order.value_usd == 200.0
        assert order.shares is None and order.fraction_held is None

    def test_shares_only_valid(self):
        order = PmOrder(kind=PmOrderKind.SELL, shares=2)
        assert order.shares == 2

    def test_fraction_held_only_valid(self):
        order = PmOrder(kind=PmOrderKind.SELL, fraction_held=0.25)
        assert order.fraction_held == 0.25

    def test_two_sizing_fields_invalid(self):
        with pytest.raises(ValidationError, match="exactly one"):
            PmOrder(kind=PmOrderKind.BUY, value_usd=200.0, shares=3)

    def test_no_sizing_field_invalid(self):
        with pytest.raises(ValidationError, match="exactly one"):
            PmOrder(kind=PmOrderKind.BUY)

    def test_zero_or_negative_amounts_invalid(self):
        for kwargs in ({"value_usd": 0.0}, {"value_usd": -5},
                       {"shares": 0}, {"shares": -2}):
            with pytest.raises(ValidationError):
                PmOrder(kind=PmOrderKind.BUY, **kwargs)

    def test_fraction_bounds(self):
        with pytest.raises(ValidationError):
            PmOrder(kind=PmOrderKind.SELL, fraction_held=0.0)
        with pytest.raises(ValidationError):
            PmOrder(kind=PmOrderKind.SELL, fraction_held=1.5)
        assert PmOrder(kind=PmOrderKind.SELL, fraction_held=1.0).fraction_held == 1.0

    def test_optional_knobs_parse(self):
        order = PmOrder(kind=PmOrderKind.BUY, value_usd=500.0,
                        limit_px=500.0, stop_px=480.0, cap_value_usd=1000.0,
                        notes="starter")
        assert order.limit_px == 500.0 and order.cap_value_usd == 1000.0


class TestExecutionIntent:
    def test_empty_orders_is_explicit_no_order(self):
        intent = ExecutionIntent(orders=[])
        assert intent.orders == []

    def test_default_orders_empty(self):
        assert ExecutionIntent().orders == []

    def test_conflicting_buy_and_sell_invalid(self):
        with pytest.raises(ValidationError, match="conflict"):
            ExecutionIntent(orders=[
                PmOrder(kind=PmOrderKind.BUY, value_usd=200.0),
                PmOrder(kind=PmOrderKind.SELL, shares=2),
            ])

    def test_future_notes_and_invalidation_carry(self):
        intent = ExecutionIntent(orders=[], invalidation_px=95.6,
                                 future_notes="redeploy on Q1 catalyst")
        assert intent.invalidation_px == 95.6
        assert "catalyst" in intent.future_notes


class TestExecutionPortfolioDecision:
    def test_subclasses_framework_decision(self):
        from tradingagents.agents.schemas import PortfolioDecision
        assert issubclass(ExecutionPortfolioDecision, PortfolioDecision)

    def test_inherited_fields_validate(self):
        d = ExecutionPortfolioDecision(
            rating="Overweight",
            executive_summary="2% starter near $54.25",
            investment_thesis="cheap re-rating",
        )
        assert d.rating.value == "Overweight"
        assert d.execution is None

    def test_execution_block_parses(self):
        d = ExecutionPortfolioDecision(
            rating="Overweight",
            executive_summary="2% starter",
            investment_thesis="thesis",
            execution={"orders": [
                {"kind": "BUY", "value_usd": 200.0, "stop_px": 45.7}]},
        )
        assert d.execution.orders[0].kind == PmOrderKind.BUY
        assert d.execution.orders[0].stop_px == 45.7


class TestExtractor:
    def test_absent_when_no_execution_key(self):
        status, intent, reason = extract_execution(
            {"rating": "Overweight", "executive_summary": "s"})
        assert status == EXECUTION_ABSENT
        assert intent is None and reason is None

    def test_absent_when_execution_null(self):
        status, intent, reason = extract_execution(
            {"rating": "Overweight", "executive_summary": "s",
             "execution": None})
        assert status == EXECUTION_ABSENT

    def test_valid_block(self):
        payload = {"rating": "Sell", "executive_summary": "trim 2 of 8",
                   "investment_thesis": "t",
                   "execution": {"orders": [
                       {"kind": "SELL", "shares": 2, "limit_px": 100.5,
                        "stop_px": 95.6}],
                       "future_notes": "keep 6 with stop"}}
        status, intent, reason = extract_execution(payload)
        assert status == EXECUTION_VALID
        assert intent.orders[0].shares == 2
        assert intent.orders[0].limit_px == 100.5
        assert "keep 6" in intent.future_notes
        assert reason is None

    def test_empty_object_is_valid_no_order(self):
        status, intent, reason = extract_execution(
            {"rating": "Hold", "executive_summary": "s",
             "execution": {}})
        assert status == EXECUTION_VALID
        assert intent.orders == []

    def test_invalid_sizing_flagged_with_reason(self):
        status, intent, reason = extract_execution(
            {"rating": "Buy", "executive_summary": "s",
             "execution": {"orders": [
                 {"kind": "BUY", "value_usd": 100.0, "shares": 5}]}})
        assert status == EXECUTION_INVALID
        assert intent is None
        assert "exactly one" in reason

    def test_invalid_conflict_flagged(self):
        status, intent, reason = extract_execution(
            {"rating": "Buy", "executive_summary": "s",
             "execution": {"orders": [
                 {"kind": "BUY", "value_usd": 100.0},
                 {"kind": "SELL", "shares": 1}]}})
        assert status == EXECUTION_INVALID
        assert "conflict" in reason

    def test_invalid_order_kind_flagged(self):
        status, intent, reason = extract_execution(
            {"rating": "Buy", "executive_summary": "s",
             "execution": {"orders": [
                 {"kind": "HODL", "value_usd": 100.0}]}})
        assert status == EXECUTION_INVALID
        assert "orders.0.kind" in reason


class TestRealisticPayloads:
    """Capability-matrix-shaped payloads parse to the intended orders."""

    def test_hpe_starter_with_cap(self):
        payload = {"executive_summary": "2% starter near $54.25",
                   "execution": {"orders": [
                       {"kind": "BUY", "value_usd": 200.0,
                        "stop_px": 45.7, "cap_value_usd": 500.0}]}}
        status, intent, _ = extract_execution(payload)
        assert status == EXECUTION_VALID
        order = intent.orders[0]
        assert order.value_usd == 200.0 and order.stop_px == 45.7
        assert order.cap_value_usd == 500.0

    def test_el_partial_sell_with_remainder_anchor(self):
        payload = {"execution": {"orders": [
            {"kind": "SELL", "shares": 2, "limit_px": 100.5,
             "stop_px": 95.6}]}}
        status, intent, _ = extract_execution(payload)
        assert status == EXECUTION_VALID
        order = intent.orders[0]
        assert order.limit_px == 100.5 and order.stop_px == 95.6

    def test_now_buy_limit_zone(self):
        payload = {"execution": {"orders": [
            {"kind": "BUY", "value_usd": 500.0, "limit_px": 500.0,
             "cap_value_usd": 1000.0}]}}
        status, intent, _ = extract_execution(payload)
        assert status == EXECUTION_VALID
        assert intent.orders[0].limit_px == 500.0

    def test_ladder_lives_in_future_notes_not_orders(self):
        payload = {"execution": {"orders": [
            {"kind": "BUY", "value_usd": 270.0}],
            "future_notes": ("3-tranche ladder; add tranche 2 into "
                             "$86.60-87.70 on volume confirmation")}}
        status, intent, _ = extract_execution(payload)
        assert status == EXECUTION_VALID
        assert len(intent.orders) == 1
        assert "$86.60-87.70" in intent.future_notes
