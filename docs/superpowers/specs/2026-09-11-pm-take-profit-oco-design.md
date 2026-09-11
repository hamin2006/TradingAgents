# PM Take-Profit via Broker-Side OCO Orders (2026-09-11)

Status: **Design approved 2026-09-11** (brainstormed same day; Alpaca support verified
live on the paper account, including the probe classes below). No implementation plan
yet — build from this spec when pursued.

## 1. Problem

The PM can express open-window exits (`SELL` orders, floor limits) and the engine
maintains a broker-side GTC stop (−8% default or `stop_px`), but there is **no way to
express an upside target**. A held position's only exits between daily runs are the
stop or tomorrow's open. Verified mechanics that make this a real gap:

- The only sell price field (`PmOrder.limit_px`) is a *floor*: the order is placed at
  the open and, if unfilled, is cancelled after the fill window (~120s + grace, one
  retry round) — there is no resting day order, let alone a GTC target.
- `invalidation_px` is advisory-only; `future_notes` are carried to the next day's card
  but never executed.
- Alpaca will not accept two independent conditional closes for the same shares
  (position-qty check), and even if it did, the survivor after one leg fills would be a
  naked short while the engine is offline (the ZBRA-class invisible-fill risk).

The user's framing: "PMs should be able to sell at a desired high since we won't be
re-evaluating stocks until the next day." The correct primitive is a broker-side **OCO**
(take-profit limit + stop-loss), which is exactly what Alpaca provides.

## 2. Goals

- A PM execution block can carry `take_profit_px` for a **held** ticker; the engine
  maintains a resting GTC OCO at (stop, target) for the post-orders remainder, so the
  upside exit can fill any session without re-analysis.
- The OCO is **re-affirmed daily** by the PM: an OCO exists only while today's valid
  block re-emits the target; otherwise it is downgraded to a plain stop (never removed).
- All existing safety invariants hold: no naked positions, no double-sell at the open,
  no blind broker mutations, per-ticker legacy fallback on invalid blocks.

## 3. Non-goals

- Fresh-entry OCO brackets (post-fill OCO for brand-new positions) — the paper engine's
  OTO/bracket entry class is known-broken; separate future feature.
- Position-level stop overrides on maintains (per-order `stop_px` semantics stay as-is).
- Trailing stops (broker supports; not now).
- TP on legacy-path tickers — PM execution blocks only.
- Multi-day persistence without daily re-affirmation (explicitly rejected).

## 4. Verified Alpaca behavior (live paper probes, 2026-09-11)

Both probes ran on the paper account; production state was restored after each.

1. **OCO placement/query/cancel probe (MSFT 1 share)** — `order_class=oco`, GTC,
   `extended_hours=false`, sell side, `take_profit.limit_price` + `stop_loss.stop_price`
   was accepted with **no paper-engine rejection**. Nested query
   (`GetOrdersRequest(nested=True)`) returns the parent (LIMIT = TP) with the stop child
   under `legs` (child status `HELD` while resting). **Cancelling the parent cancelled
   both legs** (`CANCELED` each).
   Critical integration finding: **the flat (non-nested) open-orders list shows only the
   parent LIMIT — the stop child is invisible.** All existing seams that scan for
   `type == "stop"` in the flat list (stop sweep, pre-open disarm) miss OCO protection
   as written and must switch to nested-aware detection.
2. **Fill-cascade probe (fresh symbol F, 1 share, position closed by the probe)** — a
   marketable TP filled @ $13.99 (limit 13.86) in ~4s and the stop child was
   **auto-CANCELED** by the broker. A filled TP appears as
   `type=limit, order_class=oco, status=filled`, `filled_at` present, child visible under
   nested closed orders (child `canceled`). No residue; cleanup was a no-op.

Docs confirm: OCO requires both legs; TIF `day`/`gtc`; TP > stop by ≥$0.01 (base price
for the stop threshold is the TP limit); 90-day GTC aged-order auto-cancel (irrelevant
under daily re-affirmation).

## 5. Requirements (approved decisions)

1. **Daily re-affirmation.** Each morning's execute reconciles the resting OCO to that
   day's PM intent: same target → leave; changed → replace; not re-emitted (valid block
   without target, legacy/invalid/absent block) → cancel the OCO, keep a plain stop at
   the OCO's stop level. Removal is safe between analyze and open because fills need
   regular hours.
2. **Held positions only.** A target on an unheld ticker is invalid (fresh entries are
   out of scope). Held adds are fine — the target covers the post-order remainder.
3. **TP ≤ reference price is invalid.** A marketable target is confused intent; exits
   belong in `SELL` orders. The whole block falls back to legacy for that ticker (the
   existing per-ticker gate mechanism), with a logged reason. No clamping, no silent
   immediate fills.
4. **Position-level field.** `take_profit_px` lives on `ExecutionIntent`, not on
   `PmOrder` — maintain blocks (`orders: []`) must be able to carry it.
5. **Stop continuity.** The OCO's stop leg reuses, in priority order: the block's
   sell-order `stop_px` (clamped by the existing `_clamp_stop` band) → the standing
   protection's current stop level → the `stop_loss_pct` default (−8% of reference).
   The position is never left unprotected during a transition.

## 6. Data model & validation

- `pm_execution.ExecutionIntent.take_profit_px: float | None = None` (structural check:
  > 0 when present). `PmOrder` is unchanged.
- `decisions.protection_from_execution(intent, *, ticker, holdings, last_close,
  stop_loss_pct, stop_px_band_pct) -> (ProtectionIntent | None, reasons)` — pure, new:
  - `ProtectionIntent = {ticker, target_px, stop_px}`.
  - Reasons (any one ⇒ caller treats the whole block as legacy per ticker):
    `target on unheld ticker`; `target ≤ reference close`; `stop ≥ target − 0.01`
    (Alpaca's OCO threshold).
  - Stop derivation per requirement 5; the "standing stop level" input is supplied by
    the caller when available (execute) and omitted at gate time (no broker state
    there — the level continuity is an execute-time concern).
- **Gate-time validation** (`binding_gate.evaluate`, which already fetches holdings +
  `last_close` via `daily_run._last_close`): an invalid target marks the ticker's
  per-ticker status `legacy` with the reason and records the would-be
  `take_profit_px` / `oco_stop_px` in the artifact for observability.
- **Execute-time re-derivation** for bound tickers (same pure function, same reference
  close), so a guard failure at execute also falls back per ticker — defense in depth,
  no second source of truth.
- **PM contract disclosure** (`daily_run._PM_CONTRACT_DISCLOSURE`) gains: the field, the
  daily re-affirmation rule, and the "must be above the current price and above the
  stop" rule.
- The ratings-file schema is unchanged (`schema_version: 2`); the field rides inside
  each ticker's existing execution block.

## 7. Execute mechanics

New step `daily_run._reconcile_protection(cfg, broker, intents)` runs in `--execute`
**after `place_market_orders` resolves** (all fills/re-anchors settled) and before the
executed log finalizes:

1. Fresh `broker.get_positions_and_cash()` — post-fill sizes.
2. `broker.get_resting_protection()` — one nested open-orders query; per symbol:
   standalone stop (`kind=stop`) or OCO (`kind=oco`, parent id, TP from parent limit,
   stop from the leg, qty).
3. Decision table per held ticker:

| Today's intent | Resting state | Action |
|---|---|---|
| valid target | matching OCO (target/stop at cent precision, same qty) | **leave** (no churn) |
| valid target | other / none / multiple | cancel old protection → `place_oco(...)` |
| no valid target | OCO | **downgrade**: cancel parent → plain stop at the OCO's stop-leg level |
| no valid target | plain stop / none | leave (sweep's job) |
| unheld symbol | any protection | cancel |

4. Actions are logged as `protection` rows in the executed log:
   `oco_set` / `oco_keep` / `oco_replace` / `downgrade` / `cancel_unheld` /
   `stop_fallback`.

For a ticker whose block binds with a `SELL` trim: the existing batch finalize
re-anchors a plain stop for the remainder first (unchanged code path), then this step
upgrades it to an OCO if a target intent exists. If the process dies in between, the
remainder keeps its plain stop — safe by construction.

## 8. Broker primitives & failure handling

`alpaca_broker.AlpacaBroker` (interface additions; `ibkr.IBKRBroker` stubs raise
`NotImplementedError` since it is inactive):

- `get_resting_protection() -> dict[str, list[dict]]` — nested open-orders walk;
  handles standalone stops, OCO parents, and anomalies (multiple entries).
- `cancel_protection(order_id)` — parent/standalone cancel; the parent-cancel cascade
  is probe-verified.
- `place_oco(symbol, qty, stop_px, target_px) -> order_id` — `LimitOrderRequest` with
  `order_class=OCO`, `time_in_force=GTC`, `extended_hours=False`; per-attempt position
  query + bounded retries (the just-cancelled `held_for_orders` lag class already
  handled by `_submit_remainder_stop`).

Failure rules: every cancel→place transition falls back to a plain stop via the
existing retry helper (`stop_fallback` row) — a failed OCO submit never leaves a symbol
naked; a failed `get_resting_protection()` query aborts the reconcile (never
blind-cancel); reconcile failures never fail the execute pass (logged, next morning's
sweep/reconcile recovers).

## 9. Seam updates

- **Stop sweep** (`daily_run._ensure_stop_sweep`): detect protection via
  `broker.get_resting_protection()` instead of the flat `type == "stop"` scan — an
  OCO-protected ticker must not get a second stop.
- **Pre-open disarm** (`alpaca_broker._cancel_open_stops` → `_cancel_open_protection`):
  also match OCO parents, cancel the parent (cascade kills both legs), and return the
  stop-leg level + qty so `_anchor_sell_remainders` re-anchors partial-sell remainders
  at the OCO's stop level. Without this, an exit at the open plus a resting OCO is the
  old double-sell class.
- **Outcome reconciliation** (`get_filled_stop_orders` → `get_filled_exit_orders`):
  classify `kind=STOP` (filled stop, standalone or OCO leg) vs `kind=TP` (filled OCO
  parent, `type=limit`, `order_class=oco`); nested-aware so a filled stop leg is caught.
  `_reconcile_broker_outcomes` renders `SELL(TP)` rows; cards are corrected via the
  existing latest-event-wins mechanism (idempotent `reconciled` events).
- **Cards/disclosure**: execution-outcome lines summarize resting protection (OCO
  target/stop vs plain stop); the PM prompt already injects fresh cards, so tomorrow's
  PM sees the standing target.

## 10. Testing & verification

Hermetic (TDD, fakes; `pytest -q` + `uvx ruff check` gates):

- `pm_execution`: schema field + `> 0`.
- `decisions`: `protection_from_execution` table — unheld target, `≤` reference,
  `stop ≥ target − 0.01`, stop fallback order, stop band clamp, trim remainder sizing.
- `binding_gate`: invalid target marks per-ticker `legacy` with reason; artifact records
  target/stop; execute re-derivation consistent.
- `alpaca_broker`: nested parsing (parent+leg / standalone / multiple), parent-cancel
  cascade, `place_oco` request shape + `held_for_orders` retry, exit-fill
  classification incl. the OCO stop-leg-filled case.
- `daily_run`: `_reconcile_protection` decision table (leave/replace/downgrade/
  cancel-unheld/stop_fallback), sweep no-double-stop, disarm level handoff, `SELL(TP)`
  outcome rows.

Live probes during implementation (paper, PC; state restored after each):

1. **Overnight rest** — OCO on a fresh cheap symbol during RTH; next morning verify it
   still rests + nested-visible, run the sweep path and assert no second stop, then
   cancel/flatten.
2. **Disarm cascade** — invoke the real disarm path for an OCO symbol; verify both legs
   die and the returned stop level matches the leg; re-place.
3. **Engine-path TP fill** — run `_reconcile_protection` + `get_filled_exit_orders`
   against a real filled TP (fresh cheap symbol).
4. **OCO stop-leg fill classification** — the one classification the probes have not
   covered (stop leg fires instead of the TP); force it on a fresh symbol.

## 11. Rollout & operations

- Rides the existing `pm_execution` switch; **no new config keys**. Kill switch and the
  full safety chain are unchanged.
- First live day watched like the 2026-09-08 binding rollout: gate artifact shows TP
  validations, executed log shows protection rows, card outcome shows the resting OCO.
- Docs: AGENTS.md rows updated (new primitives + OCO gotchas: nested-child invisibility
  in flat queries, parent-cancel cascade, TP>stop threshold, GTC 90-day aging) and this
  spec committed.

## 12. Open risks

- The OCO stop-leg-filled classification path is the only unprobed broker behavior;
  probe 4 in §10 covers it before production reliance.
- Paper fills for resting non-marketable limits follow NBBO marketability; live behavior
  may differ around halts/gaps (the broker is the safety envelope either way).
- A PM target can be arbitrarily ambitious; the existing daily re-analysis is the
  corrective mechanism (the next morning's PM sees the resting target on the card and
  must re-affirm it).
