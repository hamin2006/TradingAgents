"""decisions.py — pure decision engine: ratings + holdings -> order list."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pm_execution import ExecutionIntent

SELL_RATINGS = {"Sell", "Underweight"}
BUY_RATINGS = {"Buy", "Overweight"}


@dataclass(frozen=True)
class Order:
    ticker: str
    action: str  # "BUY" | "SELL"
    shares: int
    reason: str
    protection_price: float | None = None  # BUY: cap limit; SELL: floor limit
    stop_price: float | None = None        # BUY: attach after fill; SELL
                                           # (partial): re-anchor the remainder


@dataclass(frozen=True)
class ProtectionIntent:
    """Desired broker-side protection for one held PM-bound position."""

    ticker: str
    target_px: float
    stop_px: float


DEFAULT_CONVICTIION_WEIGHTS = {"Buy": 1.5, "Overweight": 1.0}


def compute_orders(ratings, holdings, last_close, cash, max_positions,
                   max_order_value_cap=None, entry_protection_pct=2.0,
                   stop_loss_pct=8.0, conviction_weights=None,
                   risk_budget_pct=1.2):
    """Legacy fallback sizer (risk-budget, spec 2026-09-11).

    Base is portfolio equity (cash + holdings at the reference close), not a
    cash-derived slice. Per-name target weight = conviction x (1 /
    max_positions), trimmed to the risk ceiling = risk_budget_pct /
    stop_loss_pct (15% at defaults). A too-expensive name still buys one
    whole share when its weight fits the ceiling — the min-1 rule at the
    whole-share boundary.
    """
    orders = []
    weights = conviction_weights or DEFAULT_CONVICTIION_WEIGHTS
    equity = float(cash) + sum(
        int(shares) * float(last_close.get(ticker, 0.0) or 0.0)
        for ticker, shares in holdings.items())
    risk_ceiling = (risk_budget_pct / stop_loss_pct
                    if stop_loss_pct and stop_loss_pct > 0 else None)

    for ticker, shares in holdings.items():
        if ticker in ratings and ratings[ticker] in SELL_RATINGS:
            orders.append(Order(ticker=ticker, action="SELL", shares=int(shares),
                                reason="rating exit"))

    buys = []
    if equity > 0:
        for ticker, rating in ratings.items():
            if ticker in holdings or rating not in BUY_RATINGS:
                continue
            price = last_close.get(ticker)
            if not price or price <= 0:
                continue
            target_w = weights.get(rating, 1.0) / max_positions
            if risk_ceiling is None:
                max_w = min1_w = target_w
            else:
                max_w = min(target_w, risk_ceiling)
                min1_w = risk_ceiling
            shares = int(max_w * equity / price)
            if shares < 1 and price <= min1_w * equity:
                shares = 1
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


def apply_cash_budget(orders, cash, last_close, ratings):
    """Skip buys the account cash cannot cover; PM orders allocate first.

    Deterministic, no shaving (spec 2026-09-11): an order is either kept at
    its stated size or skipped entirely. Allocation order: PM execution
    intents, then legacy buys by conviction (Buy before Overweight, ties by
    ticker). Returns (kept_orders, skipped_buys); non-buy orders are never
    touched.
    """
    buy_positions = [i for i, o in enumerate(orders) if o.action == "BUY"]
    if not buy_positions:
        return list(orders), []

    def _rank(i):
        o = orders[i]
        return (0 if o.reason == "pm-execution" else 1,
                0 if ratings.get(o.ticker) == "Buy" else 1,
                o.ticker)

    remaining = float(cash)
    keep: set[int] = set()
    skipped = []
    for i in sorted(buy_positions, key=_rank):
        o = orders[i]
        price = last_close.get(o.ticker)
        if price and o.shares * price <= remaining:
            keep.add(i)
            remaining -= o.shares * price
        else:
            skipped.append(o)
    kept = [o for i, o in enumerate(orders)
            if o.action != "BUY" or i in keep]
    return kept, skipped


# --- PM execution intent -> binding orders (spec 2026-09-04) -----------------
# The PM's ExecutionIntent carries today's open-window orders. The engine
# converts them to broker Orders inside the guardrail envelope; anything the
# mechanics cannot honor returns None = fall back to the legacy tier path
# (the compliance stream logs the reason). An empty order list is binding:
# the PM explicitly ordered nothing today (overrides the legacy tier action).


def _clamp_stop(stop_px: float, price: float, band_pct: tuple[float, float],
                clamps: list[str], ticker: str) -> float:
    lo, hi = sorted(band_pct)  # e.g. (3, 25) = stops 3%..25% below close
    lo_px, hi_px = price * (1 - hi / 100), price * (1 - lo / 100)
    if lo_px <= stop_px <= hi_px:
        return round(stop_px, 2)
    clamped = min(max(stop_px, lo_px), hi_px)
    clamps.append(f"{ticker}: stop {stop_px:.2f} outside band "
                  f"{lo_px:.2f}..{hi_px:.2f}; clamped to {clamped:.2f}")
    return round(clamped, 2)


def protection_from_execution(
    execution: ExecutionIntent,
    *,
    ticker: str,
    holdings: dict[str, int],
    last_close: dict[str, float],
    stop_loss_pct: float,
    stop_px_band_pct: tuple[float, float],
    standing_stop_px: float | None = None,
) -> tuple[ProtectionIntent | None, list[str]]:
    """Derive and validate an OCO target from a PM execution block.

    A returned protection with non-empty reasons is a diagnostic candidate
    only: callers must use the reasons to fall back to legacy execution.
    Keeping it available lets the gate record the target/stop it rejected.
    """
    if execution.take_profit_px is None:
        return None, []

    target = round(float(execution.take_profit_px), 2)
    price = float(last_close.get(ticker, 0.0) or 0.0)
    reasons: list[str] = []
    if int(holdings.get(ticker, 0) or 0) < 1:
        reasons.append(f"{ticker}: take-profit target on unheld ticker")
    if price <= 0:
        reasons.append(f"{ticker}: no reference close for take-profit validation")
    elif target <= price:
        reasons.append(f"{ticker}: take-profit target must exceed reference close")

    sell_stop = next((o.stop_px for o in execution.orders
                      if o.kind.value == "SELL" and o.stop_px is not None),
                     None)
    if sell_stop is not None:
        if price > 0:
            stop = _clamp_stop(float(sell_stop), price, stop_px_band_pct,
                               [], ticker)
        else:
            stop = round(float(sell_stop), 2)
    elif standing_stop_px is not None and float(standing_stop_px) > 0:
        stop = round(float(standing_stop_px), 2)
    elif price > 0:
        stop = round(price * (1 - stop_loss_pct / 100), 2)
    else:
        return None, reasons

    protection = ProtectionIntent(ticker=ticker, target_px=target, stop_px=stop)
    if stop >= target - 0.01:
        reasons.append(f"{ticker}: OCO stop must be at least $0.01 below target")
    return protection, reasons


def orders_from_execution(
    execution: ExecutionIntent,
    *,
    ticker: str,
    holdings: dict[str, int],
    last_close: dict[str, float],
    entry_protection_pct: float = 2.0,
    stop_loss_pct: float = 8.0,
    stop_px_band_pct: tuple[float, float] = (3.0, 25.0),
    min_order_value_usd: float = 50.0,
) -> tuple[list[Order] | None, list[str]]:
    """Convert the PM's execution block into broker Orders (guardrailed).

    Returns (None, [reasons]) when the block cannot be honored as stated —
    the caller falls back to the legacy rating-tier path. Returns
    (orders, clamps) otherwise; orders may be empty (explicit no-order).
    """
    orders: list[Order] = []
    clamps: list[str] = []
    held = int(holdings.get(ticker, 0) or 0)
    price = last_close.get(ticker)
    has_price = price is not None and price > 0
    if not execution.orders:
        return [], clamps  # explicit empty = no order today (binding)
    if not has_price and any(o.kind.value == "BUY" for o in execution.orders):
        # BUY sizing/protection derives from the reference close; without it
        # the legacy path is the fallback (it also skips closeless buys).
        return None, [f"{ticker}: no reference close for buy sizing "
                      "(legacy fallback)"]

    for o in execution.orders:
        if o.kind.value == "BUY":
            if o.fraction_held is not None:
                return None, [f"{ticker}: BUY with fraction_held is not "
                              "executable (sells trim; buys size in $/shares)"]
            shares = (int(o.value_usd / price) if o.value_usd is not None
                      else int(o.shares))
            if o.cap_value_usd is not None:
                shares = min(shares, int(o.cap_value_usd / price))
            if shares < 1:
                if o.value_usd is not None and o.value_usd < min_order_value_usd:
                    clamps.append(f"{ticker}: order ${o.value_usd:.2f} below "
                                  f"minimum ${min_order_value_usd:.2f}")
                continue
            ceiling = round(price * (1 + entry_protection_pct / 100), 2)
            limit = ceiling if o.limit_px is None else round(
                min(o.limit_px, ceiling), 2)
            if o.limit_px is not None and o.limit_px > ceiling:
                clamps.append(f"{ticker}: buy limit {o.limit_px:.2f} above "
                              f"protection ceiling {ceiling:.2f}; tightened")
            stop = (round(price * (1 - stop_loss_pct / 100), 2)
                    if o.stop_px is None
                    else _clamp_stop(o.stop_px, price, stop_px_band_pct, clamps,
                                     ticker))
            orders.append(Order(ticker=ticker, action="BUY", shares=shares,
                                reason="pm-execution",
                                protection_price=limit, stop_price=stop))
        else:  # SELL — held only, never shorts
            if held < 1:
                return None, [f"{ticker}: SELL on a ticker not held is not "
                              "executable"]
            if o.fraction_held is not None:
                shares = max(1, int(round(held * o.fraction_held)))
                if shares > held:
                    shares = held
            elif o.shares is not None:
                shares = int(o.shares)
            else:
                return None, [f"{ticker}: SELL must size in shares or a "
                              "fraction of held"]
            if shares < 1 or shares > held:
                return None, [f"{ticker}: SELL {shares} of {held} held is "
                              "not executable"]
            floor = None if o.limit_px is None else round(o.limit_px, 2)
            remainder_stop = None
            if held - shares > 0 and o.stop_px is not None:
                if has_price:
                    remainder_stop = _clamp_stop(o.stop_px, price,
                                                 stop_px_band_pct, clamps,
                                                 ticker)
                else:
                    remainder_stop = round(o.stop_px, 2)
                    clamps.append(f"{ticker}: no reference close; remainder "
                                  f"stop {o.stop_px:.2f} left unclamped")
            orders.append(Order(ticker=ticker, action="SELL", shares=shares,
                                reason="pm-execution",
                                protection_price=floor,
                                stop_price=remainder_stop))
    return orders, clamps
