# PM Execution Intent & Dated Decision Cards (2026-09-04)

Status: **Design (approved 2026-09-04; AMENDED same day)** — spec written 2026-09-04,
backlogged (no implementation plan written; build only from this spec when pursued).
Amendment (2026-09-04, follow-up brainstorm): §8 replaced — held-only structured-only
thesis cards are out. Every analyzed ticker gets a **dated decision card** (full PM
decision + execution block, prose included) stored per-ticker as append-only JSONL and
injected deterministically into future PM prompts through the `past_context` seam
(flip between the two latest fresh cards → last ≤3; stable → latest card; age-gated;
no broker dependency). Prose IS fed back — dated and framed as overridable, so an
overturn must cite what changed (`rating_flip` events measure it). No PM tool loop
(the PM is single-shot structured by design). See §8.

**BUILT 2026-09-04 + BINDING ENABLED for the 2026-09-05 batch** — all code landed
(observe installers, card store/injection, binding engine `orders_from_execution`,
broker SELL-floor limits + partial-sell remainder re-anchor, `cancel_stops_for`
returning cancelled stops, ratings v2, replay + probe tools), 1122 hermetic tests.
Pre-flip evidence: `pm_schema_probe` replayed the real 09-04 HPE/EL prompts against
the live model with the extended schema bound — **both VALID** (HPE: BUY 4 @ $54.25
refining the injected prior card; EL: SELL 2 @ ≥$100.50 — the exact 09-04 trim the
old schema couldn't express). Known field-discipline gap: protective stops still
land in `future_notes` prose rather than `order.stop_px` (default −8% applies; EL
remainder covered by the original-stop fallback) — measure via compliance events.
Morning checkpoint: post-analyze binding dry-run preview (execute re-reads config at
09:00; a bad preview flips `pm_execution` off in the window; kill switch as backstop).

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
- Give every analyzed ticker a dated decision card — the full PM decision incl. its
  long-term intent — so a future PM must confirm-or-refute the standing thesis with
  dates visible instead of re-deriving from scratch or overturning silently (reducing
  noise-based flips without an anchoring vector: dated prose is overridable by design).
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
5. **Dated decision cards, deterministic PM-only injection.** Card = the full PM
   decision (rating, executive summary, investment thesis, execution block incl.
   `future_notes`), stored per ticker at analyze time. Any analyzed ticker with a
   fresh card (≤ `card_max_age_days`) gets it injected into the next PM prompt via
   the memory-log `get_past_context` seam (PM-only by construction). Rating flip
   between the two latest fresh cards → inject the last ≤3 fresh cards so the arc is
   visible; stable → latest card only. Prior prose IS fed back — dated, framed as
   overridable ("decided 09-03, may be stale; current evidence governs") — because
   dated attribution is the anti-anchor: an overturn must cite what changed. No tool
   loop (the PM is single-shot structured output by design — `NO_EXTERNAL_TOOLS`);
   no broker dependency (store-driven).
6. **Card store = per-ticker append-only JSONL** (`logs/decision_cards/{TICKER}.jsonl`),
   one card per analysis day; latest card = last line; full history retained for flip
   analytics; every card carries `schema_version`. Cards + injection gate on
   `execution_intent` (§11) — with it off, no cards are written and nothing injects
   (byte-identical today behavior).
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
    future_notes: str | None = None       # long-term intent: tranche 2/3 plans, triggers,
                                          # pauses, watch levels. NOT executable today —
                                          # recorded on the ticker's dated decision card,
                                          # which a future PM reads before re-deciding.

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
5. **Card write** (§8): extractor result (present-valid or absent) + the PM's rating,
   summaries, and reference close → append one card to
   `decision_cards/{TICKER}.jsonl` (gated on `pm_execution: true`).

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

## 8. Dated decision cards (store + deterministic PM-only injection)

The framework's `past_context` feeds the PM only *resolved* lessons (outcome known);
a held position's buy thesis is invisible to the next morning's PM (EL exhibit: OW
9/3 → UW full-exit 9/4 at ~the same price, nothing required the 9/4 PM to argue
against its own 9/3 thesis). The PM is also single-shot structured output by design
(`NO_EXTERNAL_TOOLS`, `with_structured_output`) — it cannot pull history itself
without replacing that pattern with a tool loop. Fix: the pipeline stores the full
decision per ticker and injects it deterministically.

**Store** — per-ticker append-only JSONL at
`~/.tradingagents/logs/decision_cards/{TICKER}.jsonl`, one card per analysis day,
written at analyze time (gated on `execution_intent`, §11 — so phase-1 observation
accumulates cards while execution binding is still off). Card content:

```json
{"date": "2026-09-03", "ticker": "EL", "rating": "Overweight",
 "ref_close": 101.15, "schema_version": 1,
 "executive_summary": "...", "investment_thesis": "...",
 "execution": {"orders": [...], "invalidation_px": 95.60,
               "future_notes": "redeploy only on Q1 catalyst or RSI<50 pullback"}}
```

Latest card = last line; full history retained for flip analytics. Malformed trailing
lines are tolerated (skip + warn) — a corrupt card never blocks analysis.

**Injection rule** (deterministic, no model discretion):
- Any ticker entering today's analyze pool whose most recent card is fresher than
  `card_max_age_days` (default 21) gets a card block in its PM prompt.
- **Stable** (latest two fresh cards share a rating) → inject the latest card only.
- **Flip** (latest two fresh cards differ) → inject the last ≤ `card_flip_inject_max`
  (default 3) fresh cards so the PM sees the arc and must justify the latest rating
  against what it overturned.
- No card / expired → nothing (no prompt change).

**Seam**: patch the memory-log `get_past_context` wrapper (its output feeds ONLY the
PM prompt — `tradingagents/agents/managers/portfolio_manager.py:36`) to append the
card block after the lessons section. Installer idempotent with `_reset_*` helper
(same discipline as the other memory-log wrappers). No broker snapshot needed —
cards are store-driven, so they appear even when the book fetch fails (the stance/
shape block keeps its own no-book rule).

**Prompt framing** (the anti-anchor is the date + explicit overridability, not
prose suppression):

```
Prior PM decisions on EL (may be stale — current evidence governs; if you overturn
a prior rating, say what changed since its date):
  [2026-09-04] Underweight — exit thesis: ...
  [2026-09-03] Overweight — buy thesis: ...
```

**Measurement**:
- `rating_flip` structured event when today's rating differs from the injected
  card's rating (ticker, card date, old/new) — flip-without-new-info becomes
  measurable over time.
- `decision_card` event per ticker: `injected` (n cards) / `absent` / `expired`.

**Card vs memory log**: the memory log stays the outcome archive (pending → resolved
with realized returns + reflection, fed back as lessons via the existing path). Cards
are the intent store. Both reach the PM: lessons via `past_context`, cards via the
wrapper append.

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
| All tranche-2+/add-on triggers, FOMC pauses, vol-backed completions | NOT EXECUTABLE → `future_notes`; day-expiry; next-morning re-decision. **Carried forward**: `future_notes` lands on the dated decision card the next PM reads |
| Close/band invalidation lines (weekly close < $440.61, close < $85.50) | NOT EXECUTABLE as stated (GTC is touch-based) → invalidation_px advisory only, recorded on the card |
| Sector-overlap caps (DXCM healthcare 8% via REGN) | NOT ENFORCEABLE (no sector logic) → cap_value_usd only |

## 10. Logging & measurement

- `execution_intent` structured event per ticker: present_valid / present_invalid(reason)
  / absent; clamped fields with clamp reasons.
- `decision_card` event per ticker: injected (n cards) / absent / expired.
- `rating_flip` event (card vs new rating; ticker, card date, old/new) — flip-without-
  new-info is measurable because injection is deterministic (every flip saw its prior
  card by construction).
- Per-day `summary.json` counts: valid orders, fallbacks, clamps, cards injected, flips
  → PM-compliance miss-rate trend (feeds prompt-quality iteration, same culture as F4
  measurement).

## 11. Config (watchlist.yaml)

```yaml
pm_execution: true            # execution binding; off = legacy engine exactly
execution_intent: true        # schema swap + extractor + cards + injection + events
                              # (phase-1 observe switch; pm_execution false + this true =
                              # measure compliance with zero execution impact)
stop_px_band_pct: [3, 25]     # protective stop clamp band from reference close
min_order_value_usd: 50
card_max_age_days: 21         # decision-card freshness gate (only fresher cards inject)
card_flip_inject_max: 3       # fresh cards injected when the latest two ratings differ
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
- **Decision cards**: store append/read-latest per ticker; malformed-line tolerance;
  stable → 1 card injected; flip → ≤ `card_flip_inject_max`; expired/absent → nothing;
  framing render carries date + overridability language; `rating_flip`/`decision_card`
  events fire; golden EL flip arc (OW → UW → re-analysis sees both cards); config off →
  no cards written, no injection.
- Config toggle off → byte-identical legacy behavior.

## 13. Rollout

1. **Phase 1 — observe (no execution change).** Merge + deploy with
   `pm_execution: false` (engine stays legacy) but `execution_intent: true`: the
   schema swap, extractor, card store, deterministic injection, and events all run —
   compliance rate, schema-rejection rate, card-injection counts, and flip-vs-evidence
   accumulate over several days with zero execution impact. (Off = off: with
   `execution_intent: false` no cards are written and nothing is injected.)
2. **Phase 2 — bind execution.** Flip `pm_execution: true` once compliance is high
   enough and rejections are understood; dev-isolated A/B runs before the
   production flip.
3. **Watch flip-vs-card events**; revisit if flip-without-new-info does not decrease.

## 14. Out of scope (explicit)

- Intraday/resting same-day orders beyond the auction window (no monitoring loop).
- Multi-day standing build plans (persistent intents store).
- Sector-level exposure logic.
- Close-based (non-touch) stop execution.
- Any framework change under `tradingagents/`.
