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
    assert buys["AAPL"].shares == 100  # 100_000 / 10 / 100.0
    assert buys["AAPL"].protection_price == 102.0  # +2%
    assert buys["AAPL"].reason == "entry"


def test_hold_and_held_buy_produce_no_orders():
    ratings = {"MSFT": "Hold", "AAPL": "Buy"}
    holdings = {"AAPL": 50}
    orders = compute_orders(ratings, holdings, CLOSE, capital=100_000, max_positions=10)
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
    # slice = 10_000 each -> AAPL 100@100 = 10_000, NVDA 66@150 = 9_900; total 19_900
    orders = compute_orders(ratings, {}, CLOSE, capital=100_000, max_positions=10,
                            max_order_value_cap=20_000)
    assert len([o for o in orders if o.action == "BUY"]) == 2  # total under cap
    orders = compute_orders(ratings, {}, CLOSE, capital=100_000, max_positions=10,
                            max_order_value_cap=12_000)
    # total 19_900 > 12_000 -> drop the largest-ticket buy (AAPL)
    assert len([o for o in orders if o.action == "BUY"]) == 1
    assert orders[0].ticker == "NVDA"


def test_missing_rating_or_price_skipped():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 100.0, "MSFT": 200.0},
                            capital=100_000, max_positions=10)
    assert all(o.ticker == "AAPL" for o in orders)
