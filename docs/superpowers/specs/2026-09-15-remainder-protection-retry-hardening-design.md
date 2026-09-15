# Remainder Protection Retry Hardening (2026-09-15)

Status: **Design proposed 2026-09-15** (short spec — two-module change; implement via
TDD from here). Triggered by a live recurrence: HPQ ended the execute run naked on
both 2026-09-14 and 2026-09-15 (three separate unprotected windows on 09-15 alone),
each closed only by manual intervention rather than by the system itself.

## 1. Problem

`AlpacaBroker._submit_remainder_stop` (plain stop) and `AlpacaBroker.place_oco`
(PM take-profit OCO) each retry a `held_for_orders` rejection **3 times, 2 seconds
apart (6s total)**, then give up:

```python
for attempt in range(3):
    remain = self._position_qty(symbol)
    ...
    except Exception as exc:
        logger.warning(...)
        time.sleep(2)
logger.error("... NOT placed after retries — position unprotected")
return False
```

The race itself is well understood and documented (`alpaca_broker.py:614-620`): a
just-cancelled or just-filled sell leaves shares reserved in Alpaca's
`held_for_orders` accounting for some seconds after the order resolves, so an
immediate re-anchor attempt sees `available < existing_qty` and is rejected. The
retry exists exactly to outlast this lag — but 6 seconds is a fixed guess, not a
measured bound, and it is now demonstrably too short under real conditions:

- **2026-09-09** (original case): DXCM cleared within the existing budget.
- **2026-09-14**: HPQ exhausted all 3 attempts and stayed naked (22 sh) until a
  manual stop was placed hours later; the next morning's `_ensure_stop_sweep` would
  have caught it ~14h after the fact.
- **2026-09-15**: HPQ hit the *same* race **three separate times** in one execute
  run (two remainder-stop exhaustions across two sell tranches, both ending
  "NOT placed after retries"), and the run's new OCO reconciliation path (`place_oco`)
  hit the identical `held_for_orders` signature on AMD/DELL/VLO/ZBRA — those four
  happened to clear (confirmed live), but only because `_reconcile_protection`'s
  caller retries the reconciliation pass across the whole batch, not because
  `place_oco`'s own inner retry is reliable.

Root cause: **the retry budget is a fixed guess (3 × 2s) with no visibility into
what it's actually waiting for.** The rejection payload already tells us the exact
number we're waiting on (`available`, `existing_qty`, `held_for_orders`,
`related_orders`) — the current code discards that and just sleeps blindly.

Consequence when the budget is exhausted: for `_submit_remainder_stop`, the position
is left with **zero protection** for the rest of the day, recoverable only by next
morning's `_ensure_stop_sweep` (a 14+ hour gap) or manual action. For `place_oco`,
an exhausted retry raises, and the caller (`_reconcile_protection`) falls back to a
plain stop attempt — which hits the same race and can *also* exhaust, compounding.

## 2. Decisions

1. **Retry on the specific blocking condition, not a fixed count.** Parse
   `held_for_orders` from the rejection payload (already shaped as JSON in the
   exception message — same shape used for logging today). If present and > 0,
   keep retrying with backoff **as long as `held_for_orders` is trending toward
   zero or the deadline hasn't elapsed** — not a blind fixed-count loop. If the
   payload doesn't parse (unexpected shape), fall back to the existing fixed-count
   behavior unchanged (never regress on an unrecognized error).
2. **Time-bounded, not count-bounded.** Replace the 3-attempt cap with a wall-clock
   deadline (default `remainder_protection_retry_s`, config, default **30s** —
   5x the current budget, still well inside the execute pass's existing per-order
   timeout budgets) polled every 2s. This directly targets "wait for the lag to
   clear" instead of guessing an attempt count; a shorter total (today's 6s) was
   simply never enough headroom based on live evidence.
3. **Same-run last-resort resweep, not just a longer wait.** If the deadline elapses
   and the position is still unprotected, do **one final check-and-place pass after
   the rest of the current order batch has been handled** (mirroring
   `_ensure_stop_sweep`'s logic, but invoked inline at the end of
   `place_market_orders` rather than deferred to next day). Rationale: other orders'
   cancels/fills in the same batch are plausible contributors to `held_for_orders`
   contention (2026-09-15 hit the race on HPQ/DXCM/AMD/DELL/VLO/ZBRA all in the same
   run); giving the whole batch time to settle before the final attempt is more
   likely to succeed than retrying a single ticker in isolation.
4. **`place_oco` inherits the same treatment.** Both call sites share one hardened
   retry helper (extract the loop into `_retry_until_available(symbol, submit_fn,
   deadline_s)`) so a fix to the wait strategy isn't duplicated or drifts between the
   plain-stop and OCO paths.
5. **Failure remains loud and safe.** If the final same-run resweep still fails
   (broker outage, genuinely stuck reservation), behavior is unchanged from today:
   log ERROR, return `False`/raise, leave the position for tomorrow's
   `_ensure_stop_sweep`. This spec closes the common-case gap (lag clears within
   normal seconds-to-tens-of-seconds); it does not claim to eliminate every
   pathological case, and does not add same-day alerting (out of scope, §5).
6. **No change to the sweep's own logic or its idempotency.** `_ensure_stop_sweep`
   is unchanged; it remains the correct backstop for whatever this hardening still
   misses.

## 3. Non-goals

- Alerting/notification on a same-day naked position (kill-switch style escalation)
  — the user currently checks logs manually; a follow-up if this keeps recurring
  after the retry fix ships.
- Changing `_reconcile_protection`'s batch-level retry structure — it already
  tolerates individual failures across a full pass; only the inner per-symbol
  retry primitive changes.
- Any change to stop-loss levels, OCO target levels, or sizing — this is purely an
  execution-reliability fix.

## 4. Tests (hermetic TDD)

- `_retry_until_available`: succeeds once `held_for_orders` reports 0 (or the
  broker call itself succeeds) within the deadline; exhausts loudly past the
  deadline; falls back to fixed-count behavior on an unparseable error shape;
  never sleeps past the deadline (bounded wall-clock, mockable clock/sleep).
- `_submit_remainder_stop` / `place_oco`: both routed through the shared helper;
  existing DXCM-style race tests (`test_remainder_stop_retries_through_held_for_orders_race`,
  `test_remainder_stop_gives_up_after_bounded_retries`) still pass unchanged in
  intent (retry-until-success, bounded-give-up) with the new time-based bound.
- `place_market_orders`: same-run resweep fires only after all orders in the batch
  are resolved, only for still-naked held tickers, is idempotent (already-protected
  tickers skipped), and is failure-safe (broker error during resweep logs and does
  not raise).
- Regression: existing OCO reconciliation and stop-sweep tests unaffected.

## 5. Rollout

Ship behind no new flag (pure reliability hardening, same call sites); verify via
one live paper-account observation day before considering this closed, watching for
`held_for_orders` log lines dropping to zero same-day resolutions instead of
next-morning sweeps.
