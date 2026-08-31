"""decisions.py — pure decision engine: ratings + holdings -> order list."""

from dataclasses import dataclass

SELL_RATINGS = {"Sell", "Underweight"}
BUY_RATINGS = {"Buy", "Overweight"}


@dataclass(frozen=True)
class Order:
    ticker: str
    action: str  # "BUY" | "SELL"
    shares: int
    reason: str
    protection_price: float | None = None  # set on BUY orders
    stop_price: float | None = None        # set on BUY orders (broker-side stop)


DEFAULT_CONVICTIION_WEIGHTS = {"Buy": 1.5, "Overweight": 1.0}


def compute_orders(ratings, holdings, last_close, capital, max_positions,
                   max_order_value_cap=None, entry_protection_pct=2.0,
                   stop_loss_pct=8.0, conviction_weights=None):
    orders = []
    weights = conviction_weights or DEFAULT_CONVICTIION_WEIGHTS
    base_slice = capital / max_positions

    for ticker, shares in holdings.items():
        if ticker in ratings and ratings[ticker] in SELL_RATINGS:
            orders.append(Order(ticker=ticker, action="SELL", shares=int(shares),
                                reason="rating exit"))

    buys = []
    for ticker, rating in ratings.items():
        if ticker in holdings or rating not in BUY_RATINGS:
            continue
        price = last_close.get(ticker)
        if not price:
            continue
        slice_value = base_slice * weights.get(rating, 1.0)
        shares = int(slice_value / price)
        if shares < 1:
            continue
        protection = round(price * (1 + entry_protection_pct / 100), 2)
        stop = round(price * (1 - stop_loss_pct / 100), 2)
        buys.append(Order(ticker=ticker, action="BUY", shares=shares,
                          reason="entry", protection_price=protection,
                          stop_price=stop))

    if max_order_value_cap is not None:
        while True:
            total = sum(o.shares * last_close[o.ticker] for o in buys)
            if total <= max_order_value_cap or not buys:
                break
            buys.remove(max(buys, key=lambda o: o.shares * last_close[o.ticker]))

    # Position-count cap: never hold more than max_positions. Slots are won by
    # conviction first (Buy before Overweight), then deterministically by ticker.
    slots = max_positions - len(holdings)
    if len(buys) > slots:
        buys.sort(key=lambda o: (0 if o.reason == "entry" and o.ticker in
                                 {t for t, r in ratings.items()
                                  if r == "Buy"} else 1, o.ticker))
        buys = buys[:max(0, slots)]

    return orders + buys
