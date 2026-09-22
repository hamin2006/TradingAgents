# Remainder-Stop Price Re-Anchoring on Market-Moved Rejection (2026-09-22)

Status: **Design proposed 2026-09-22** (short spec — one-module change; implement
via TDD from here). Triggered by a live recurrence: MPC's remainder-stop
re-anchor exhausted BOTH the in-batch attempt and the same-run resweep, left
the position naked for hours, and would have stayed naked indefinitely had
the market not happened to recover above the stale stop level before a
manual check.

## 1. Problem

`_submit_remainder_stop` (and, by extension, `_retry_until_available`) retries
a stop-order submission at a **fixed `stop_price` captured once, outside the
retry loop**. The 2026-09-15 hardening made this retry deadline-based and
`held_for_orders`-aware, correctly handling the case where the broker's
share-reservation accounting needs a few seconds to settle — but it never
addresses a different, permanent-not-transient failure: Alpaca rejects a
SELL stop order outright if `stop_price >= current market price`
(`"stop price must be less than current price"`, code `42210000`, live
2026-09-22 on MPC), because such a stop would fire immediately at a
guaranteed loss and Alpaca's API refuses to accept it.

This happens when:
1. A held ticker's original stop was disarmed pre-open (the existing
   "cancel resting stops on SELL-bound symbols" safety step, before
   attempting the sell).
2. The intended sell does not fill (limit floor missed, same class of
   miss documented repeatedly — HPQ, VLO, RVTY).
3. The remainder-stop re-anchor tries to restore protection at the
   **original stop level** (the PM's decision-time value, e.g. $396 for
   MPC) — but the stock has since traded below that level (e.g. $390),
   making the requested stop price invalid relative to the live market.

The current code classifies this rejection as "unrecognized" (it does not
match the `held_for_orders` JSON shape), so it burns the small fixed
fallback budget (`_UNRECOGNIZED_ERROR_ATTEMPTS = 3`) retrying the *exact
same invalid price* every time, then gives up. The same-run resweep pass
(`_resweep_naked_sell_remainders`) also reuses the identical stale
`stop_price` from the order's own record, so it fails identically. There is
no path in the current code that ever tries a different, valid price — MPC
would have stayed naked until the stock happened to trade back above $396,
which is luck, not a mechanism.

## 2. Decisions

1. **Parse the rejection's own `market_price` field.** The payload already
   carries the exact number needed (`{"code":42210000,"market_price":
   "390.58","message":"stop price must be less than current price",
   "stop_price":"396"}`) — add `_market_price_too_high(exc)` (mirrors
   `_held_for_orders`'s parse-or-None shape) returning the parsed
   `market_price` float, or `None` if the payload doesn't match this
   specific rejection code/message.
2. **Re-anchor once, using the existing stop_loss_pct convention.** On a
   recognized market-price rejection, compute a fresh stop = `market_price
   * (1 - stop_loss_pct / 100)` (the same formula `_ensure_stop_sweep` and
   the two-step BUY entry path already use elsewhere — no new sizing
   concept, just reusing the established default). Retry ONCE more at
   that corrected price before giving up (not folded into the deadline
   loop — a single corrective retry, since re-parsing a fresh rejection
   and re-computing again in a tight loop risks chasing a falling price
   indefinitely, which is a different, worse failure mode than giving up
   loudly). If the corrected price is itself rejected (e.g. the market
   moved again in the few hundred ms it took to resubmit), give up exactly
   as before — loud log, tracked in `_naked_remainder_tickers` for the
   resweep, eventually caught by tomorrow's `_ensure_stop_sweep` if all
   else fails.
3. **`_retry_until_available` stays generic.** Rather than special-casing
   this inside the shared helper (which also serves `place_oco` and the
   BUY entry-stop path, where the identical rejection shape can occur —
   an OCO's stop leg or a fresh entry's stop can just as easily be rejected
   for the same reason), the market-price classification and one-shot
   re-anchor live in a new thin wrapper that all three call sites route
   through: `_submit_stop_with_reanchor(symbol, qty, stop_price,
   stop_loss_pct, submit_fn)`. `submit_fn` takes the (possibly corrected)
   price as an argument this time — the callers no longer close over a
   single fixed price.
4. **Scope: SELL-side stops only.** A BUY-side rejection would have the
   opposite sense (a buy-stop rejected for being on the wrong side of
   price means something different) — this spec covers exactly the
   observed SELL-remainder and OCO-stop-leg shapes; the BUY entry-stop
   path shares the retry helper but is not known to hit this specific
   rejection message and is not being changed in scope beyond flowing
   through the same generic plumbing (it should still work exactly as
   today for `held_for_orders`).
5. **No change to stop_loss_pct semantics, sizing, or the resweep's own
   scan logic** — only the price used in the retry submission when this
   specific rejection is recognized.

## 3. Tests (hermetic TDD)

- `_market_price_too_high`: parses the exact live payload shape; returns
  `None` on `held_for_orders`, generic exceptions, and any other code.
- `_submit_remainder_stop`: a market-price rejection on attempt 1 triggers
  exactly one corrective resubmission at `market_price * (1 -
  stop_loss_pct/100)`; if that second attempt also succeeds, returns True
  and the corrected price is what's on the actual submitted order; if the
  second attempt also fails (any reason), gives up loudly (no infinite
  loop chasing a falling price) and reports False.
- `place_oco`: same rejection shape on the stop leg gets the same one-shot
  re-anchor treatment (the OCO's take-profit leg is unaffected — only the
  stop leg's price is corrected).
- Regression: existing `held_for_orders` retry-through and
  bounded-give-up tests for both `_submit_remainder_stop` and `place_oco`
  unchanged in behavior (the new classification is additive, checked
  before falling back to the unrecognized-error path, never instead of
  it).
- `_resweep_naked_sell_remainders`: unaffected in scope (still resweeps
  via `_submit_remainder_stop`, which now carries the re-anchor
  internally — no resweep-level change needed).

## 4. Non-goals

- No change to how the ORIGINAL stop price is chosen at decision/order
  time (PM-set levels, default `stop_loss_pct` computation elsewhere) —
  this is purely a same-day execution-recovery fix for when that level
  goes stale before the remainder-stop attach fires.
- No repeated/looping re-anchor (chasing the market down) — exactly one
  correction attempt, then the existing loud-failure + resweep + tomorrow's
  sweep chain takes over unchanged.
