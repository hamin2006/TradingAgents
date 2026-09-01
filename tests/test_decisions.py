"""tests/test_decisions.py"""
from decisions import compute_orders

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
