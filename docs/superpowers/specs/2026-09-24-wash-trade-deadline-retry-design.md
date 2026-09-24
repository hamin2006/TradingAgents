# Wash-Trade Rejection Joins the Deadline-Based Retry (2026-09-24)

Status: **Design proposed 2026-09-24** (short spec — one-module change; implement
via TDD from here). Triggered by a live recurrence: ILMN's fresh BUY entry-stop
attach exhausted the short 3-attempt/6s unrecognized-error fallback and stayed
naked, even though the 2026-09-17 fix already routes this exact call site
through `_retry_until_available`.

## 1. Problem

`_retry_until_available` classifies every submit failure into exactly two
buckets:

1. **Recognized (`held_for_orders`)** — retries against the 30s wall-clock
   deadline (`remainder_protection_retry_s`).
2. **Unrecognized (everything else)** — retries a small fixed number of times
   (`_UNRECOGNIZED_ERROR_ATTEMPTS = 3`, ~6s total), then gives up.

The wash-trade rejection Alpaca returns when a resting sibling order exists
on the same symbol ("potential wash trade detected... opposite side limit
order exists", code `40310000`) falls into bucket 2 today — it has no
`held_for_orders` field, so `_held_for_orders(exc)` returns `None` and it
gets only the 6-second budget. This is the SAME call site the 2026-09-17
fix (RVTY) already wired through the retry helper specifically to handle
this rejection class — the fix correctly retries, it just doesn't retry
*long enough*, because the rejection isn't recognized as "wait, this
resolves on its own" the way `held_for_orders` is.

Live evidence it needs the longer budget: ILMN 2026-09-24 hit the identical
`existing_order_id` across all 3 attempts (the sibling BUY tranche stayed
open longer than 6 seconds), exhausted, and the position stayed naked until
a manual fix. RVTY 2026-09-17 hit the same class and happened to clear
within the shorter budget available at the time — this is not consistently
survivable on the current bound.

## 2. Decisions

1. **Recognize the wash-trade shape as a second retriable race,
   alongside `held_for_orders`.** Add `_is_wash_trade_race(exc)` (mirrors
   `_held_for_orders`'s parse-or-bool shape) — returns `True` when the
   payload matches `code: 40310000` AND the message contains "wash trade"
   (the two markers together are Alpaca's specific signature for this
   rejection; `code: 40310000` alone is reused for `held_for_orders` too,
   so the message text disambiguates).
2. **Same deadline, same budget — this is not a new mechanism, it's
   widening what counts as "the mechanism we already built."**
   `_retry_until_available`'s existing branch becomes: recognized (either
   `held_for_orders` OR wash-trade) → wall-clock deadline retry;
   unrecognized (neither) → the short fixed fallback, unchanged. No new
   config, no new deadline constant — `remainder_protection_retry_s`
   already governs this.
3. **No qty/count to log for the wash-trade case** (unlike
   `held_for_orders`, which logs the exact reservation count) — log the
   `existing_order_id` instead, since that's the diagnostic signal this
   shape actually carries (confirms whether it's the same blocking order
   across attempts or a new one each time).
4. **Scope: this only changes classification, not behavior at any other
   layer.** The market-price re-anchor (2026-09-22 spec) is unrelated and
   untouched — wash-trade and market-price-too-high are two distinct,
   already-distinguishable rejection shapes handled by two independent
   mechanisms (widen-the-deadline vs. correct-the-price).

## 3. Tests (hermetic TDD)

- `_is_wash_trade_race`: recognizes the exact live payload shape; returns
  `False` for `held_for_orders`, `market_price_too_high`, and generic
  unrelated errors (never double-classifies the same exception two ways).
- `_retry_until_available`: a wash-trade rejection keeps retrying past the
  old 3-attempt point, up to the configured deadline (mirrors the existing
  `held_for_orders` outlasts-old-budget test); still bounded — gives up
  loudly once the deadline elapses, same as today.
- Regression: existing `held_for_orders` and unrecognized-error tests for
  `_retry_until_available`, `_submit_remainder_stop`, `place_oco`, and the
  BUY entry-stop path all unchanged in behavior.

## 4. Non-goals

- No change to the market-price re-anchor mechanism (2026-09-22, separate
  and independent).
- No change to `_UNRECOGNIZED_ERROR_ATTEMPTS` or the deadline value itself
  — this widens which errors qualify for the existing deadline, not the
  deadline's length.
