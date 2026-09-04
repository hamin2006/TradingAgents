"""tests/test_decisions.py"""
from decisions import compute_orders, orders_from_execution
from pm_execution import ExecutionIntent, PmOrder

RATINGS = {"AAPL": "Buy", "MSFT": "Hold", "NVDA": "Overweight", "TSLA": "Sell"}
HOLDINGS = {"TSLA": 40}
CLOSE = {"AAPL": 100.0, "MSFT": 200.0, "NVDA": 150.0, "TSLA": 250.0}


def test_sell_held_on_sell_rating():
    orders = compute_orders(RATINGS, HOLDINGS, CLOSE, capital=100_000, max_positions=10)
    sell = [o for o in orders if o.action == "SELL"]
    assert len(sell) == 1
    assert sell[0].ticker == "TSLA" and sell[0].shares == 40
    assert sell[0].protection_price is None


def test_buy_not_held_on_buy_rating_with_protection():
    orders = compute_orders(RATINGS, HOLDINGS, CLOSE, capital=100_000, max_positions=10)
    buys = {o.ticker: o for o in orders if o.action == "BUY"}
    assert set(buys) == {"AAPL", "NVDA"}
    assert buys["AAPL"].shares == 150   # 100_000 / 10 x 1.5 / 100.0 (conviction Buy)
    assert buys["AAPL"].protection_price == 102.0  # +2%
    assert buys["AAPL"].reason == "entry"


def test_hold_and_held_buy_produce_no_orders():
    ratings = {"MSFT": "Hold", "AAPL": "Buy"}
    holdings = {"AAPL": 50}
    orders = compute_orders(ratings, holdings, CLOSE, capital=100_000, max_positions=10)
    assert orders == []


def test_review_rating_is_noop():
    """Upstream v0.4.0 emits REVIEW when a model's output has no recognizable
    rating. It must never trade: not a buy, not a sell — a flag for re-run."""
    orders = compute_orders(
        {"AMZN": "REVIEW"}, {}, {"AMZN": 100.0}, 100_000, 10,
        entry_protection_pct=5.0)
    assert orders == []


def test_review_rating_on_held_position_keeps_position():
    """A REVIEW on a held position must not liquidate it (no fabricated Sell)."""
    orders = compute_orders(
        {"AMZN": "REVIEW"}, {"AMZN": 10}, {"AMZN": 100.0}, 100_000, 10)
    assert orders == []


def test_underweight_held_is_sell():
    ratings = {"NVDA": "Underweight"}
    holdings = {"NVDA": 10}
    orders = compute_orders(ratings, holdings, CLOSE, capital=100_000, max_positions=10)
    assert orders[0].action == "SELL"


def test_shares_lt_1_skips_buy():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 300_000.0},
                            capital=100_000, max_positions=10)
    assert orders == []  # slice = 10_000 -> 0 shares


def test_max_order_value_cap_drops_largest_buy():
    ratings = {"AAPL": "Buy", "NVDA": "Buy"}
    # slice 15_000 each (conviction x1.5) -> AAPL 150@100 = 15_000, NVDA 100@150 = 15_000
    orders = compute_orders(ratings, {}, CLOSE, capital=100_000, max_positions=10,
                            max_order_value_cap=31_000)
    assert len([o for o in orders if o.action == "BUY"]) == 2  # total under cap
    orders = compute_orders(ratings, {}, CLOSE, capital=100_000, max_positions=10,
                            max_order_value_cap=20_000)
    # total 30_000 > 20_000 -> drop the largest-ticket buy (AAPL)
    assert len([o for o in orders if o.action == "BUY"]) == 1
    assert orders[0].ticker == "NVDA"


def test_missing_rating_or_price_skipped():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 100.0, "MSFT": 200.0},
                            capital=100_000, max_positions=10)
    assert all(o.ticker == "AAPL" for o in orders)


def test_buy_includes_stop_loss():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 100.0},
                            capital=100_000, max_positions=10)
    assert orders[0].stop_price == 92.0  # last_close * (1 - 8%)


def test_stop_loss_pct_configurable():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 100.0},
                            capital=100_000, max_positions=10, stop_loss_pct=5.0)
    assert orders[0].stop_price == 95.0


def test_sell_has_no_stop():
    orders = compute_orders({"TSLA": "Sell"}, {"TSLA": 40}, {"TSLA": 250.0},
                            capital=100_000, max_positions=10)
    assert orders[0].action == "SELL"
    assert orders[0].stop_price is None


def test_conviction_scaling_buy_outranks_overweight():
    orders = compute_orders({"AAPL": "Buy", "NVDA": "Overweight"}, {},
                            {"AAPL": 100.0, "NVDA": 100.0},
                            capital=100_000, max_positions=10)
    by_ticker = {o.ticker: o for o in orders}
    assert by_ticker["AAPL"].shares == 150   # base slice 10k x 1.5 / 100
    assert by_ticker["NVDA"].shares == 100   # base slice x 1.0


def test_conviction_weights_configurable():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 100.0},
                            capital=100_000, max_positions=10,
                            conviction_weights={"Buy": 2.0})
    assert orders[0].shares == 200


def test_conviction_cap_still_enforced():
    orders = compute_orders({"AAPL": "Buy", "NVDA": "Overweight"}, {},
                            {"AAPL": 100.0, "NVDA": 100.0},
                            capital=100_000, max_positions=10,
                            max_order_value_cap=16_000)
    # AAPL 15_000 + NVDA 10_000 = 25_000 > 16_000 -> drop largest (AAPL)
    assert [o.ticker for o in orders] == ["NVDA"]


def test_position_cap_trims_buys():
    """max_positions caps total positions: 7 held + 8 Buy-rated candidates
    -> only 3 new buys."""
    ratings = {f"C{i}": "Buy" for i in range(8)}
    holdings = {f"H{i}": 10 for i in range(7)}
    close = {**{f"C{i}": 100.0 for i in range(8)}, **{f"H{i}": 50.0 for i in range(7)}}
    orders = compute_orders(ratings, holdings, close, capital=100_000, max_positions=10)
    buys = [o for o in orders if o.action == "BUY"]
    assert len(buys) == 3  # 10 - 7 slots


def test_position_cap_no_buys_when_full():
    ratings = {"C0": "Buy", "C1": "Buy"}
    holdings = {f"H{i}": 10 for i in range(10)}
    close = {"C0": 100.0, "C1": 100.0, **{f"H{i}": 50.0 for i in range(10)}}
    orders = compute_orders(ratings, holdings, close, capital=100_000, max_positions=10)
    assert [o for o in orders if o.action == "BUY"] == []


def test_position_cap_prioritizes_buy_over_overweight():
    ratings = {"A": "Overweight", "B": "Buy"}
    holdings = {f"H{i}": 10 for i in range(9)}  # 1 slot left
    close = {"A": 100.0, "B": 100.0, **{f"H{i}": 50.0 for i in range(9)}}
    orders = compute_orders(ratings, holdings, close, capital=100_000, max_positions=10)
    buys = [o.ticker for o in orders if o.action == "BUY"]
    assert buys == ["B"]  # conviction wins the last slot


def _intent(orders, **extra):
    return ExecutionIntent(orders=[
        PmOrder(**o) for o in orders], **extra)


class TestOrdersFromExecutionBuy:
    def test_value_usd_sizes_and_protects(self):
        intent = _intent([{"kind": "BUY", "value_usd": 200.0}])
        orders, clamps = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25},
            entry_protection_pct=2.0, stop_px_band_pct=(3, 25))
        assert len(orders) == 1
        o = orders[0]
        assert (o.ticker, o.action, o.shares) == ("HPE", "BUY", 3)
        assert o.protection_price == round(54.25 * 1.02, 2)  # ceiling
        assert o.stop_price == round(54.25 * 0.92, 2)        # default -8%
        assert o.reason == "pm-execution"
        assert clamps == []

    def test_pm_limit_only_tightens_the_ceiling(self):
        intent = _intent([{"kind": "BUY", "value_usd": 200.0,
                           "limit_px": 54.0}])
        orders, _ = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25},
            entry_protection_pct=2.0, stop_px_band_pct=(3, 25))
        assert orders[0].protection_price == 54.0

    def test_pm_limit_above_ceiling_clamped_to_ceiling(self):
        intent = _intent([{"kind": "BUY", "value_usd": 200.0,
                           "limit_px": 60.0}])
        orders, _ = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25},
            entry_protection_pct=2.0, stop_px_band_pct=(3, 25))
        assert orders[0].protection_price == round(54.25 * 1.02, 2)

    def test_stop_clamped_into_band(self):
        intent = _intent([{"kind": "BUY", "value_usd": 200.0,
                           "stop_px": 40.0}])   # -26%: below the band
        orders, clamps = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25},
            stop_px_band_pct=(3, 25))
        assert orders[0].stop_price == round(54.25 * 0.75, 2)  # 25% floor
        assert any("stop" in c for c in clamps)

    def test_stop_within_band_kept(self):
        intent = _intent([{"kind": "BUY", "value_usd": 200.0,
                           "stop_px": 45.7}])   # -16%
        orders, clamps = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25},
            stop_px_band_pct=(3, 25))
        assert orders[0].stop_price == 45.7
        assert clamps == []

    def test_cap_value_clamps_shares(self):
        intent = _intent([{"kind": "BUY", "value_usd": 500.0,
                           "cap_value_usd": 200.0}])
        orders, _ = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25})
        assert orders[0].shares == 3  # 200 / 54.25

    def test_order_below_minimum_skipped(self):
        intent = _intent([{"kind": "BUY", "value_usd": 10.0}])
        orders, clamps = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25},
            min_order_value_usd=50.0)
        assert orders == []
        assert any("minimum" in c for c in clamps)

    def test_shares_sized_buy(self):
        intent = _intent([{"kind": "BUY", "shares": 5}])
        orders, _ = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25})
        assert orders[0].shares == 5

    def test_held_add_allowed(self):
        intent = _intent([{"kind": "BUY", "value_usd": 200.0}])
        orders, _ = orders_from_execution(
            intent, ticker="HPE", holdings={"HPE": 10},
            last_close={"HPE": 54.25})
        assert len(orders) == 1 and orders[0].action == "BUY"

    def test_missing_close_falls_back_to_legacy(self):
        intent = _intent([{"kind": "BUY", "value_usd": 200.0}])
        orders, clamps = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={})
        assert orders is None  # legacy skips tickers without a close anyway
        assert any("close" in c for c in clamps)

    def test_buy_with_fraction_invalid_falls_back(self):
        intent = _intent([{"kind": "BUY", "fraction_held": 0.5}])
        orders, clamps = orders_from_execution(
            intent, ticker="HPE", holdings={}, last_close={"HPE": 54.25})
        assert orders is None
        assert any("fraction" in c for c in clamps)


class TestOrdersFromExecutionSell:
    def test_shares_partial_sell(self):
        intent = _intent([{"kind": "SELL", "shares": 2, "limit_px": 100.5,
                           "stop_px": 95.6}])
        orders, _ = orders_from_execution(
            intent, ticker="EL", holdings={"EL": 8},
            last_close={"EL": 101.15}, stop_px_band_pct=(3, 25))
        assert len(orders) == 1
        o = orders[0]
        assert (o.action, o.shares) == ("SELL", 2)
        assert o.protection_price == 100.5   # floor limit
        assert o.stop_price == 95.6          # remainder re-anchor
        assert o.reason == "pm-execution"

    def test_fraction_trim_rounds_down_to_whole(self):
        intent = _intent([{"kind": "SELL", "fraction_held": 0.25}])
        orders, _ = orders_from_execution(
            intent, ticker="EL", holdings={"EL": 8},
            last_close={"EL": 101.15})
        assert orders[0].shares == 2

    def test_fraction_one_sells_all(self):
        intent = _intent([{"kind": "SELL", "fraction_held": 1.0}])
        orders, _ = orders_from_execution(
            intent, ticker="EL", holdings={"EL": 8},
            last_close={"EL": 101.15})
        assert orders[0].shares == 8
        assert orders[0].stop_price is None  # nothing left to re-anchor

    def test_sell_of_non_held_invalid_falls_back(self):
        intent = _intent([{"kind": "SELL", "shares": 2}])
        orders, clamps = orders_from_execution(
            intent, ticker="EL", holdings={}, last_close={"EL": 101.15})
        assert orders is None
        assert any("not held" in c for c in clamps)

    def test_sell_more_than_held_invalid(self):
        intent = _intent([{"kind": "SELL", "shares": 9}])
        orders, clamps = orders_from_execution(
            intent, ticker="EL", holdings={"EL": 8},
            last_close={"EL": 101.15})
        assert orders is None
        assert any("held" in c for c in clamps)

    def test_empty_orders_is_explicit_no_order(self):
        intent = _intent([])
        orders, _ = orders_from_execution(
            intent, ticker="EL", holdings={"EL": 8},
            last_close={"EL": 101.15})
        assert orders == []

    def test_partial_sell_without_close_still_executes(self):
        """Replay-found bug: share-based SELLs carry explicit floor/stop and
        never needed the reference close, but a block-level close gate killed
        them. The engine must honor them (stop left unclamped with a note)."""
        intent = _intent([{"kind": "SELL", "shares": 2, "limit_px": 100.5,
                           "stop_px": 95.6}])
        orders, clamps = orders_from_execution(
            intent, ticker="EL", holdings={"EL": 8}, last_close={},
            stop_px_band_pct=(3, 25))
        assert len(orders) == 1
        assert (orders[0].action, orders[0].shares, orders[0].stop_price) == (
            "SELL", 2, 95.6)
        assert any("unclamped" in c for c in clamps)
