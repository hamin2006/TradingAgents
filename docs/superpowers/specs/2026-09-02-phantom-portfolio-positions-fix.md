# Phantom Portfolio Positions — Bug Fix Design

Date: 2026-09-02
Status: Draft
Framework: TradingAgents v0.4.0 (upstream `0e9de89`; used as a library, NOT modified)

## 1. Symptom

2026-09-02 morning run (16-ticker batch): **10 of 15 PM decisions referenced
"existing positions"** — e.g. *"Maintain existing IQV positions at full size;
tighten the stop-loss"*, *"Sell 10–15% of current PANW holdings at market"*,
*"Trim 25–50% of existing TGT holdings"* — while the Alpaca paper account
held **zero positions** (cash $9,999.31; IT 8/31 and CRWD 9/1 both
`filled: 0`, and 9/2 never executed because no ratings file was written).

The "reduce / trim / maintain existing position" advice has no executable
meaning for a flat book, and it plausibly biases the rating distribution
toward non-buys (7 Underweight / 4 Hold / 4 Overweight on a momentum-long
list).

## 2. Evidence (per-hop trace, `full_states_log_2026-09-02.json`)

**No prompt or data seeds holdings:**
- Instrument context is neutral ("The instrument to analyze is `IQV`");
  portfolio state is never stated anywhere in the pipeline.
- `get_verified_market_snapshot` returns only OHLCV + technical indicators —
  no analyst recommendations, no position data.
- The only holder-scaffolding is the PM rating scale (upstream):
  *Buy: enter or **add** / Hold: **maintain current position** / Underweight:
  **reduce exposure**, take partial profits / Sell: exit*. The PM is the last
  agent, so this cannot seed earlier stages.

**IQV trace — phantom originates in the FIRST agent:**
the Market Analyst (neutral prompt, neutral data) framed its own report as
*"Stance: HOLD for **existing positions** with a buy-the-dip bias; new longs
should wait…"*. Pure model style — no instruction or data said we hold IQV.

**Escalation through the chain:**

| Stage | IQV text | Confidence |
|---|---|---|
| Bull Researcher | "If you're already long, hold your position…" | hedged conditional |
| Research Manager | "Maintain existing positions. Do not sell or trim." | assertive directive |
| Conservative debator | "**We already hold the stock.** We haven't sold." | fabricated fact |
| Portfolio Manager | "Maintain existing IQV positions at full size…" | stated as established |

**Control case:** COP — identical prompts, same day, same model — shows
**zero** phantom-position language in its entire chain. Difference: COP's
setup read as "extended, initiate carefully"; IQV's read as "consolidation,
wait". For "wait/hold" narratives the model adopts a holder persona.

**Conclusion:** a prompt-design gap (portfolio truth is never established for
any agent) compounded by normal chain-of-context escalation. Both the flash
model (Market Analyst) and the pro model (PM, deepseek-v4-pro) exhibit it —
not a model-specific spiral.

## 3. Requirements

1. Every agent is anchored to the real stance: flat book → "deciding whether
   to initiate"; held ticker → add/trim language grounded in the real position.
2. The decision tail (Research Manager, 3 risk debators, Portfolio Manager)
   additionally sees the shape of the book (concentration / sizing judgment);
   no evidence-gathering stage does.
3. Portfolio facts are **precomputed by us** and injected as finished
   statements — the model never computes weights or totals from raw lists.
4. Never assert a book when broker data is unavailable (fall back to today's
   behavior rather than risk asserting a wrong book).
5. Runtime patches only — nothing under `tradingagents/` is modified.
6. No trades may ever be proposed outside the ticker under analysis.

## 4. Architecture

### 4.1 Portfolio snapshot (`daily_run`)

`_portfolio_snapshot(cfg)` — memoized (10-min TTL, thread-safe) broker
holdings + cash fetch via the existing broker interface, enriched with
last-close prices and sector weights (via the cached
`resolve_instrument_identity`). `run_analyze`'s auto mode already fetches
holdings for watchlist assembly — that result is stashed and reused instead
of a double fetch. Returns `None` on broker failure.

### 4.2 Tier 1 — stance line, all agents

`propagate()` resolves `instrument_context` once at run start
(`trading_graph.py:519`) and the string flows to every node via state.
Wrap `TradingAgentsGraph.resolve_instrument_context` (idempotent installer,
`_wrapped_original` pattern) to append the stance line:

- Flat: `Portfolio context (ground truth): no current position in {TICKER}.
  You are deciding whether to initiate. References to an existing position
  are incorrect.`
- Held: `Portfolio context (ground truth): holding {qty} shares of {TICKER}
  at avg cost {cost}; weight {pct}% of the book. Trim/add language must match
  this position.`

### 4.3 Tier 2 — book shape, decision tail only (5 nodes)

Wrap the factory functions where `tradingagents/graph/setup.py` resolves
them: `create_research_manager`, `create_aggressive_debator`,
`create_neutral_debator`, `create_conservative_debator`,
`create_portfolio_manager`. Each wrapped node appends the shape block to
`state["instrument_context"]` in a copy before running the original — all
five render that key at prompt time (verified: research_manager.py:28,
conservative_debator.py:15, portfolio_manager.py:29).

- `Current book: {n}/{max_positions} positions, ${invested} invested
  ({pct}%), ${cash} cash. Sector mix by value: {…}.`
- Usage rule: `Rule: never propose trades outside {TICKER}; other holdings
  are concentration/sizing context only.`

No raw per-name qty/cost listings; no arithmetic left to the model.

### 4.4 Why the decision tail (not analysts/researchers/trader)

Analysts and researchers produce single-name evidence — book context is
noise and a hallucination seed there. The trader's proposal sizing is
mechanical downstream (`decisions.py` conviction weights), so the trader
stays out of the tail tier. RM is included because its plan is the blueprint
the trader converts and it was itself a demonstrated phantom producer.

## 5. Out of scope (deferred)

- Full raw portfolio (name + qty + cost lists) in LLM prompts — rejected:
  unverifiable cross-name claims and model arithmetic are the phantom
  mechanism one level up.
- Execution-layer changes (`decisions.py` already enforces real-holdings
  sells, `max_positions`, order caps deterministically).

## 6. Testing (hermetic, `tests/test_daily_run.py`)

1. Flat-book stance appended to resolved context.
2. Held-ticker stance includes qty / avg cost / weight.
3. Shape block reaches all 5 tail nodes; market-analyst path is stance-only.
4. Broker failure → context unchanged (graceful degradation).
5. Existing suite + ruff gates stay green.

## 7. Rollout & verification

1. Full suite + ruff → commit → PC pull + suite.
2. Next morning: spot-check memory-log entries for zero fabricated-position
   language across a flat book.
3. When the book fills: verify held-name trims/adds reference the real
   position and that no cross-ticker trades are proposed.

## 8. Related findings tracked from the same morning's logs

Separate flaws found while auditing the 2026-09-02 run; listed for tracking
(own fixes, not part of the phantom-position patch).

### 8.1 Hermetic-test pollution of the production structured-log dir (fix: conftest)

`_analyze_one` constructs `StructuredRunLogger` unconditionally with no env
gate, so any process running analyze writes to `~/.tradingagents/logs/` when
`STRUCTURED_LOG_DIR` is unset. Tests that exercise `run_analyze` without
setting it polluted the real logs dir:

- PC: instant 0-LLM stub entries (`A`–`E`, `AAPL`, `MSFT`, `NVDA`, `TSLA`,
  rating Hold, 04:39 UTC) from the 22:39 MDT test-suite run — interleaved
  with real production files under `structured/2026-09-02/`. Cleaned up
  2026-09-02 (9 stub files deleted; junk `A`–`D` keys removed from
  `summary.json`; the dev machine's live-verification AAPL run was real and
  kept).

Conftest has autouse key/config isolation but **no autouse
`STRUCTURED_LOG_DIR`** isolation. Fix: autouse fixture pointing it at a tmp
dir; optionally gate real logging in `_analyze_one` behind an explicit flag.

### 8.2 FRED alias-coverage gap: `crude_oil_wti` (resolved 2026-09-02, runtime patch)

One `get_macro_indicators` call failed with "series does not exist": the
News Analyst requested `crude_oil_wti`, which is absent from the framework's
`MACRO_SERIES` alias map (fred.py) and was passed verbatim as a raw FRED ID.
FRED's WTI spot series is `DCOILWTICO`. Framework dicts are unmodifiable on
disk but patchable at runtime (established pattern); add oil aliases
(`crude_oil_wti`, `wti`, `crude`, `oil`) in the daily_run installer chain.

**Root cause closed at runtime (`daily_run._ensure_fred_aliases`):** the real
gap was not the missing entry but that the model cannot see the alias map —
the tool description discloses only ~8 examples, so the model invents
snake_case aliases, and any unmapped string is sent to FRED verbatim as a
raw series ID. Fix has two idempotent parts: (1) extend `fred.MACRO_SERIES`
with the oil aliases → `DCOILWTICO`; (2) append the full alias map plus a
"unlisted strings go to FRED verbatim — do not invent aliases" warning to the
live `get_macro_indicators` tool description before the graph builds. The
same class of failure is now structurally prevented, not patched per alias.

### 8.3 Decision-stage structured-output fallbacks (watch)

3 of ~30 deep-tier calls fell back to free text (MRK + PSX Research
Manager, VLO Portfolio Manager) — all still produced parseable ratings.
~10% fallback rate on the deep tier; now that responses are logged
untruncated, future occurrences are diagnosable (truncation vs schema
refusal). Track before deciding on action.

### 8.4 Slow-throughput provider windows (watch)

Individual flash-tier completions ran at 5–19 tok/s vs 40–68 tok/s in the
same batch (Relace serving deepseek-v4-flash-0731), turning 5–17K-token
report calls into 8–23 min jobs. TGT/VLO drew the worst cluster. No action
yet; the 04:00 ET schedule absorbs the resulting makespan. Revisit if slow
windows become chronic (provider-routing lever).

### 8.5 (Fixed separately) Batch overran the execute checkpoint

Root cause of no-orders today; addressed by the max_analyze 16 + 04:00 ET
schedule change (commit `b7f7b6a`). Recorded here for completeness.
