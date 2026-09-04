# PM Execution Intent & Open-Position Thesis Cards (2026-09-04)

Status: **Design (approved 2026-09-04)** — spec written 2026-09-04, backlogged (no
implementation plan written; build only from this spec when pursued).

## 1. Problem

The portfolio manager is the last decision-maker in the per-ticker pipeline, but its
structured `PortfolioDecision` contract (`tradingagents/agents/schemas.py:188`) carries
only a rating letter plus free-text narratives. All execution math is done afterwards by
a deterministic engine (`decisions.py`) keyed on the rating tier alone. Verified live on
the 2026-09-04 batch — the PM's risk engineering is advisory prose that the engine ignores:

| Ticker | PM said | Engine did/would do |
|---|---|---|
| HPE (OW) | 2% starter ≈ $200 near $54.25; stop $45.70; cap 5%; 2nd tranche on MACD flip | 1.0× ≈ $1,000 slice at open; fixed −8% stop; no gating |
| MSFT (OW) | ~2% starter; limit zone $495–500; pause adds until post-FOMC; stop $480 | $1,000 slice at open; −8% stop |
| NOW (OW) | 5% starter ≈ $500; build to 10%, hard cap 12% | flat ≈ $1,000 (~10%) in one shot |
| DASH (OW) | 1–2% probe ≈ $100–200; adds on confirmation; cap ~6%; stop $205.50 | $1,000 slice at open |
| DXCM (OW) | 3-tranche ladder; 3% starter; GTCs into $86.60–87.70; vol-backed completion | $1,000 slice at open; no ladder |
| EL (UW, held 8) | **Trim 2 of 8** @ ≥$100.50 limit; keep 6 with stop $95.60 | Full exit of all 8 at open |

Secondary problem: open-position decisions are never fed forward. The framework's
`past_context` lessons feed only *resolved* decisions (outcomes known; `pending` entries
excluded), so a held stock's buy thesis is absent from the next morning's PM prompt. Live
exhibit: EL Overweight bought 9/3 → Underweight full-exit 9/4 at ~the same price with no
new information — nothing required the 9/4 PM to argue against its own 9/3 thesis.

## 2. Goals

- Give the PM an executable, schema-validated order vocabulary so its explicit intent
  (sizes, price limits, stops, partial trims) binds execution — within guardrails.
- Give held positions a dated, structured "thesis card" so the next run's PM must
  confirm-or-refute instead of re-deriving from scratch (reducing noise-based flips).
- Never modify anything under `tradingagents/`; behavior ships via runtime installers
  (existing pattern), config-gated, with hermetic tests.

## 3. Decisions (approved 2026-09-04)

1. **Pilot with guardrails.** PM orders are binding within the safety envelope; asks that
   are mechanically impossible are logged and fall back to legacy behavior.
2. **Open-window only, day-expiry.** Orders express today's open-window intent (auction
   fill semantics). Future-conditioned intents (MACD flips, FOMC pauses, tranche ladders,
   volume-backed triggers) are NOT executable by this stack — they go in `future_notes`,
   no order today, and the next morning's re-analysis re-evaluates with fresh data.
3. **Legacy fallback on invalid/absent execution block.** `execution=None` (absent) →
   today's exact deterministic tier behavior. `execution.orders=[]` (explicit) → no order
   today (overrides legacy, e.g. an Underweight with an explicit hold intent).
4. **Explicit adds on held positions allowed** (guarded); legacy tier-buys on held
   tickers remain suppressed.
5. **Thesis card = structured fields only, held tickers only, age-gated.** Never feed
   prior prose back to the model (anchoring vector). Prose stays in the memory log.
6. **Fallback measurement:** every absent/invalid/clamped block logs an
   `execution_intent` structured event so the PM-compliance rate is visible daily.

## 4. Schema (new module `pm_execution.py` — our code)

```python
class PmOrderKind(str, Enum):
    BUY    # non-held buy, or explicit add to a held position
    SELL   # held only; shares <= held; never shorts

class PmOrder(BaseModel):
    kind: PmOrderKind
    # exactly one of the three sizing fields (validated)
    value_usd: float | None      # "$200"  — buy intent value
    shares: int | None           # "2 of 8" — sell/buy share count
    fraction_held: float | None  # "trim 25%" — sell fraction of held (0 < f <= 1)
    limit_px: float | None       # SELL: minimum acceptable price (floor).
                                 # BUY: max payable; fills only if auction print <= limit
    stop_px: float | None        # protective GTC stop override (replaces the -8% default);
                                 # after a partial sell it re-anchors the remainder
    cap_value_usd: float | None  # day-clamp on this order ("cap total at 5%")
    notes: str | None = None

class ExecutionIntent(BaseModel):
    orders: list[PmOrder] = []   # today's open-window orders
    invalidation_px: float | None = None  # advisory only — NEVER executed
                                          # (close/band semantics differ from GTC touch stops)
    future_notes: str | None = None       # tranches, triggers, pauses, catalysts — card+log only

class ExecutionPortfolioDecision(PortfolioDecision):  # subclass of the framework schema
    execution: ExecutionIntent | None = None
```

Notes:
- `ExecutionPortfolioDecision` lives in our module; base fields unchanged so
  `render_pm_decision`, memory log, CLI, report writers, and the structured-log seams
  keep working unchanged.
- Field descriptions are the output instructions (framework philosophy at
  `schemas.py:191-194`) — the vocabulary, the day-expiry rule, and the
  advisory-vs-executable split must be written into the descriptions.
- Validation rule: an order with zero or multiple sizing fields is invalid; a ticker
  with both BUY and SELL orders is an invalid block (conflict) → legacy fallback.

## 5. Runtime mechanism: subclass swap installer

`daily_run._ensure_pm_execution_schema()`:
- Repoint the `PortfolioDecision` global to `ExecutionPortfolioDecision` in
  `tradingagents.agents.schemas` AND in `tradingagents.agents.managers.portfolio_manager`
  (that module imports the class at import time; both pointers must move) before the
  single graph build at analyze start.
- Restore after the build so tests and later processes see the original class; idempotent
  with a `_reset_pm_execution_schema()` helper (same discipline as the other installers).
- The bound tool schema then carries `execution`; the PM's raw tool-call args (captured
  by the existing structured-log seam) include the block.

## 6. Data flow

**Analyze** (`daily_run --analyze`):
1. Installer chain gains `_ensure_pm_execution_schema` (runs before graph build).
2. Per-ticker pipeline runs; the PM tool-call args are captured as today.
3. New extractor validates the captured args against `ExecutionIntent`:
   - `present_valid` → keep the parsed block
   - `present_invalid` → log reason, treat as absent
   - `absent` → legacy
4. Per-ticker result gains `execution`; ratings file `ratings_{date}.json` gains a
   top-level `"execution": {ticker: block}` map + `"schema_version": 2`. Reader stays
   backward compatible (v1 files / missing blocks → legacy).

**Execute** (`daily_run --execute`):
1. Read ratings + execution blocks (existing idempotency, kill switch, cash caps).
2. For each ticker: block present+valid → `decisions.orders_from_execution(...)` →
   guardrail clamps → order list (may be empty = no order today).
   Block absent/invalid → legacy `compute_orders` tier path (unchanged behavior).
3. Broker sequence (existing two-phase machinery, extended):
   - Any SELL order on a held symbol → pre-open `cancel_stops_for(symbol)` (existing),
     now *returning* the cancelled stops (price/qty) so a partial-sell remainder can be
     re-anchored.
   - Orders at the 09:30 auction: market fills at print; BUY limit fills only if
     print ≤ limit; SELL limit fills only if print ≥ limit.
   - Poll fills (partial fills count — existing rule), attach stops:
     - new buys: PM `stop_px` if given (band-clamped) else default −8%
     - partial-sell remainder: PM `stop_px` if given else re-attach the cancelled
       original stop
   - Deadline cancel for unfilled orders (existing) — day-expiry.

## 7. Guardrails (config-backed, all in the decision engine)

| Rule | Behavior |
|---|---|
| No shorts | SELL shares ≤ held; fraction_held ≤ 1; remainder ≥ 0 |
| Buy protection ceiling | pay no more than ref × (1 + entry_protection_pct); PM buy limit can only tighten |
| Stop band | stop_px clamped to 3–25% from reference close; clamp + loud log outside |
| Order minimum | value_usd ≥ $50 after rounding; whole shares only |
| Position-count cap | distinct held+bought tickers ≤ max_positions (unchanged) |
| Cash & per-day caps | unchanged top-level machinery; cap_value_usd further clamps this order |
| Held-add guard | explicit PM BUY on held allowed; post-add total ≤ cap_value_usd or count/cash guards |
| Sell of non-held | invalid → legacy (which itself never sells non-held) |
| Conflicting orders | BUY+SELL same ticker in one block → invalid block → legacy |
| Invalidation_px | never executed; card + log only |

## 8. Open-position thesis cards

Sources (all structured artifacts, no prose): previous day's `ratings_{date}.json`
(rating, execution block: stop_px, invalidation_px, future_notes, cap) +
`executed_{date}.json` (reference close).

Card shape:
```
Prior thesis card (2026-09-03, ref close $101.15):
  Rating: Overweight | stop: $93.06 | invalid: $95.60 (advisory)
  Future notes: "redeploy only on Q1 catalyst or RSI<50 pullback"
Confirm the thesis with current evidence, or cite what changed to overturn it.
```

Injection: extend `_ensure_portfolio_context` (already fetches the real broker snapshot,
memoized, 600s TTL) — cards for currently HELD tickers only, age-gated
(`card_max_age_days`, default 21; older cards dropped so stale theses never anchor).
Never assert a book on broker failure (existing rule: no snapshot → no stance/shape/cards).

Flip measurement: when a ticker's new rating differs from its card rating, emit a
`rating_flip` structured event (ticker, card date, old/new rating) so flip-vs-evidence
can be measured over time.

## 9. Capability matrix (acceptance reference — today's real PM asks)

| Request (2026-09-04) | Capability |
|---|---|
| HPE starter 2% ~$200; stop $45.70 (−16%); cap 5% | EXECUTE (band: 3–25% ok) |
| MSFT starter; widened stop $480 (−6%) | EXECUTE |
| MSFT limit zone $495–500 vs ref $510.12 | EXECUTE as auction-window BUY limit — fills only on a print ≤ $500; else day-expiry no-fill |
| NOW starter 5% ~$500; build cap 10% | EXECUTE starter; cap clamps today's order |
| DASH probe 1–2%; stop $205.50 | EXECUTE |
| DXCM starter 3% ~$270 at ≥$89.70-or-better | EXECUTE (limit ≈ market) |
| EL sell 2 of 8 @ ≥ $100.50; remainder stop $95.60 | EXECUTE (partial SELL limit; stop disarm → partial fill → re-anchor remainder) |
| All tranche-2+/add-on triggers, FOMC pauses, vol-backed completions | NOT EXECUTABLE → future_notes; day-expiry; next-morning re-decision |
| Close/band invalidation lines (weekly close < $440.61, close < $85.50) | NOT EXECUTABLE as stated (GTC is touch-based) → invalidation_px advisory only |
| Sector-overlap caps (DXCM healthcare 8% via REGN) | NOT ENFORCEABLE (no sector logic) → cap_value_usd only |

## 10. Logging & measurement

- `execution_intent` structured event per ticker: present_valid / present_invalid(reason)
  / absent; clamped fields with clamp reasons.
- `rating_flip` event (card vs new rating).
- Per-day `summary.json` counts: valid orders, fallbacks, clamps, flips → PM-compliance
  miss rate trend (feeds prompt-quality iteration, same culture as F4 measurement).

## 11. Config (watchlist.yaml)

```yaml
pm_execution: true            # master switch; off = today's behavior exactly
stop_px_band_pct: [3, 25]     # protective stop clamp band from reference close
min_order_value_usd: 50
card_max_age_days: 21         # thesis-card freshness gate
```

## 12. Testing (hermetic)

- **Golden fixtures:** today's real PM payloads (HPE, MSFT, NOW, DASH, DXCM, EL, REGN)
  as recorded in `~/.tradingagents/logs/structured/2026-09-04/*.jsonl` → assert the
  capability matrix (schema parse + resulting orders).
- Schema: sizing-field exclusivity, invalid-block rules, defaults.
- Guardrail clamp table: size/stop/limit clamps incl. band edges.
- Engine: orders_from_execution × rating-fallback matrix (absent/invalid × each rating);
  held-add allowance; legacy tier-buys on held still suppressed.
- Broker sequence on fake broker: pre-open stop cancel (returns stops) → partial SELL
  limit fill → remainder stop re-anchor (PM px and original-stop cases); market SELL of
  all; BUY limit below market (no fill → deadline cancel); partial-fill counting.
- Ratings file v1/v2 roundtrip; absent-block legacy path end-to-end.
- Installer: idempotency, `_reset_*`, class restored after build, parallel-run safety.
- Thesis card: content/shape, held-only, age gating, broker-failure → no cards.
- Config toggle off → byte-identical legacy behavior.

## 13. Rollout

1. Merge + deploy (git pull on PC) with `pm_execution: false` first — measure the
   `execution_intent` event stream on live analyze runs (compliance rate, schema
   rejection rate) for several days.
2. Flip `pm_execution: true` once compliance is high enough and rejections are
   understood; dev-isolated A/B runs before the production flip.
3. Watch flip-vs-card events; revisit if flip-without-new-info does not decrease.

## 14. Out of scope (explicit)

- Intraday/resting same-day orders beyond the auction window (no monitoring loop).
- Multi-day standing build plans (persistent intents store).
- Sector-level exposure logic.
- Close-based (non-touch) stop execution.
- Any framework change under `tradingagents/`.
