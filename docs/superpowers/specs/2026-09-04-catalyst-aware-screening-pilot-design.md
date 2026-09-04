# Catalyst-Aware Screening Pilot (2026-09-04)

Status: **Design (approved 2026-09-04)** — spec written 2026-09-04. Next step: backtest
go/no-go gate, then implementation plan (build from this spec when pursued).

## 1. Problem

The rank-based momentum screener surfaces S&P 500 names on 21-day raw-momentum
composites at the 04:10 ET screen. It cannot surface an event-driven move until the move
itself dominates the rank. Live exhibit (HOOD, 2026-09): a public Morgan Stanley upgrade
(09-01) preceded a +16.6% day (09-03); the screen could not surface HOOD until 09-04 —
after the move — and the debate then (correctly, per its own style) rated it Hold.

The backlog idea was "catalyst-aware screening" (news/upgrade-aware boost). This spec
narrows that idea to a **pilot** after scoping discussion. Decided deliberately:

- **Not** a fast-trade sleeve. The system's exits are rating-flip-only (`compute_orders`
  reacts to ratings; take-profits/time-stops/brackets do not exist; the paper engine's
  OTO is verified broken for buys). A 1-day/week trade with its own exit rules is a
  second strategy leg with machinery we deliberately deferred (PM execution intent).
- **Pilot scope:** detect *price bursts* (the market's own event aggregation) at the
  existing screen, surface the names into the morning pool 1–2 days earlier than rank
  alone, and let the existing debate + rating machine decide entry/exit at its normal
  cadence. Strict rating gate: Buy/Overweight only; Hold = no trade (the debate that
  says "don't chase" after a pop may well produce zero conversions — a measured negative
  result is a valid, cheap answer).
- **Detection is price-first, label-only:** no new feeds, no new schedules. The 48h news
  probe labels context for the debate but never gates (feeds verified to lag 1–3 days —
  gating would reject the HOOD-class catalyst; a ≥4–6% unlabeled move can be noise, but
  the debate is the filter).

The pilot's success metric is *conversion*: do burst-surfaced names rate Buy/Overweight
more often than the rank baseline, and do they outperform it? Provenance tags through
the memory log + analytics make this answerable after ~2 weeks of mornings.

## 2. Goals

1. Surface event-driven S&P 500 names into the morning analyze pool 1–2 days earlier
   than rank momentum alone, without a second strategy leg or new data feeds.
2. Bounded experiment: ≤2 burst slots/day replacing the rank *tail* (never displacing
   holdings or the measured rank core), config-disablable.
3. Clean measurement: every analyzed ticker carries a surfacing class (`burst` /
   `rank` / held re-rate) through the structured log and memory log; `analyze_results`
   slices hit rate + alpha by surfacing class.
4. Evidence gate before any live change: a 5y in-sample backtest of burst continuation;
   if burst days mean-revert on the sample, the pilot is dead pre-ship and the measured
   answer is the deliverable.

Non-goals (explicit): fast-sleeve exits (profit targets, time-stops, brackets, day
expiry), intraday/pre-market detection beyond the existing 04:10 screen, paid or new
news feeds, any change to execution mechanics, any change to the framework.

## 3. Current State

- `screener.py`: S&P 500 momentum screen (Wikipedia universe, batched yfinance, liquidity
  filter, vol-adjusted z-score composite, ranked pool). Registry strategies:
  `raw_momentum` (production) / `vol_adjusted` / `rank_based`. Backtested via 5y
  crash-in-sample (§5bis of the screener spec); `backtest_prices_*.csv` on the PC.
- `daily_run.py` watchlist assembly: holdings ∪ screened candidates → analyze batch
  (`max_analyze` cap + `min_buy_quota`/`max_analyze` buy-quota expansion); churn-guard
  exclusion window disabled (`exclusion_days: 0`, 2026-09-04) — rank alone spaces
  re-looks.
- Analyze → memory log (`trading_memory.md`); `analyze_results.py` measures hit rates by
  rating tier + per-ticker alpha.
- News context during analyze comes from the shared news Tool (yfinance feed with dated
  rendering); reddit/stocktwits wrappers exist. Feed lag for analyst actions verified
  (HOOD upgrade absent 09-01→~09-03/04).

## 4. Design

### 4.1 Burst scan (inside the existing 04:10 screen)

Runs on the per-ticker daily close series the screener already fetches for ranking
(marginal network cost ≈ 0; no universe re-fetch). Pure function:

```
burst_candidates(closes, one_day_pct, two_day_pct) -> list[dict]
    dict: ticker, one_day_pct (T-1/T-2), two_day_pct (T-1/T-3), magnitude
```

Candidate fires when **either**:
- 1-day: `close(T-1)/close(T-2) − 1 ≥ one_day_pct/100`, or
- 2-day: `close(T-1)/close(T-3) − 1 ≥ two_day_pct/100`

(Monday's T-3 = Wednesday → the "2-day" window is the last two trading days, which is
the correct since-last-screen semantics. T-1 is always the last completed session.)

Exclusions (applied in order):
- holdings (re-rated every morning anyway — no injection needed),
- tickers already in the rank pool for the day,
- tickers burst-surfaced and analyzed within the last `reanalysis_days: 2` (burst-slot
  churn guard — the disabled rank churn guard is NOT re-enabled; this is burst-slot
  only).

Remaining candidates sorted by burst magnitude (largest first); top
`max_bursts_per_day: 2` kept. Defaults are provisional and re-derived from the backtest
gate (§4.4).

**News label (label-only, never gates):** for each kept candidate, fetch recent
headlines via the existing single-ticker yfinance news call (≤2 calls/day). Regex label
set: `upgrade`, `downgrade`, `target`, `FDA`, `approval`, `beat`, `miss`, `guidance`,
`acquire`, `takeover`, `merger`, `buyback`, `CEO`, `CFO`, `depart`, `resign`, `launch`,
`partner`. First match wins → `{label, date, source}`. Failure-safe: any fetch/parse
error yields `""` — the debate still sees the burst with "(no 48h label found —
investigate)".

**Screen output** JSON (`pool_{date}.json`) gains a `"bursts"` array:
`[{ticker, one_day_pct, two_day_pct, news_label}]` alongside the ranked pool.

### 4.2 Pool merge (`daily_run.py` watchlist assembly)

Merge rule (single priority, no new code paths in execution):

1. Assemble as today: holdings ∪ ranked pool (quota expansion as today).
2. For each burst candidate not already in the assembled set (dedup; holdings never
   displaced):
   - if the assembled set is at/over `max_analyze`: the burst **replaces the lowest-
     ranked non-held name** (drop that many rank-tail names — the measured rank core is
     preserved, only the tail yields),
   - if the assembled set is under `max_analyze` (thin day): append.
3. Cap: total never exceeds `max_analyze` + quota-expansion semantics as today.

**Provenance tag:** burst tickers carry `surfacing: burst` into:
- the structured log events (per-ticker JSONL `run_start` metadata),
- the analyze context line rendered into the ticker's analysis: `Surfacing:
  price-burst overlay (+5.9% 2-day; news label: "Morgan Stanley upgrade, 09-01")` or
  `(no 48h label found — investigate)`,
- the memory log entry written at decision time (missing tag = `rank` — backward
  compatible).

### 4.3 Measurement

`analyze_results.py` gains a `--by-surfacing` split: hit rate / mean alpha / decision
count per surfacing class (`burst` vs `rank` vs held re-rate). The pilot verdict after
~2 weeks of mornings: do burst-surfaced names (a) convert to Buy/Overweight at or above
the rank baseline, and (b) show ≥ rank-baseline forward alpha? Note: `Hold`-rated bursts
produce no trade by design — conversion rate is the primary metric, alpha secondary.

### 4.4 Backtest gate (go/no-go, before any live pool change)

Reuse the 5y crash-in-sample price data the screener §5bis backtest already uses
(`backtest_prices_*.csv`). For thresholds ∈ {3, 4, 5, 6}% × {1d, 2d} windows:
- define burst days per threshold on the sample,
- measure forward returns +1/+3/+5/+10 sessions vs the same-day S&P universe baseline,
- continuation = burst-day names outperform baseline forward; mean-reversion = they
  underperform.

Verdict rules:
- **Positive continuation evidence** → adopt threshold with the best forward profile
  (defaulting provisionally to 1d=4.0 / 2d=6.0), proceed to implementation plan.
- **No evidence / mean-reversion** → pilot dead pre-ship; record the measured answer in
  this spec's status + AGENTS.md Pending, no pool change.

The gate is a script run against local CSVs — no network, no LLM, no broker; it can run
in dev days before any other implementation.

### 4.5 Config

`watchlist.yaml` (unknown-keys strictness applies — `config.py` key list extended):

```yaml
burst_overlay:
  enabled: true
  one_day_pct: 4.0     # provisional; backtest-derived after the gate
  two_day_pct: 6.0
  max_bursts_per_day: 2
  reanalysis_days: 2
```

`enabled: false` = instant revert to today's behavior. Screener strategy registry is
untouched (bursts are a pool overlay, not a ranking strategy).

## 5. Testing (hermetic — no network, no LLM, no broker)

- Burst math on synthetic close series: 1d fire, 2d fire, both, neither, Monday
  (T-3) window semantics.
- Exclusions: holding skipped; already-ranked skipped; recently-burst-analyzed skipped
  (memory-log read); cap at `max_bursts_per_day`.
- Merge priority: burst displaces rank tail not holdings; thin-day append; total cap
  respected; quota-expansion interplay.
- News probe: regex unit tests per label class; empty-label path; fetch-error → `""`
  failure-safety.
- Provenance: `surfacing: burst` reaches the structured log event and memory entry;
  legacy entries parse as `rank`.
- Config: unknown key raises; defaults merge; `enabled: false` no-op path.
- Backtest gate function on the crash-sample CSVs (deterministic in-sample).

Gates: `pytest -q` fully green + `uvx ruff check` (line-length 100, rules
E/W/F/I/B/UP/C4/SIM). Conventional commits; no framework changes.

## 6. Dispositions

| # | Decision | Disposition |
|---|---|---|
| 1 | Fast-trade sleeve (own exits) | Out of scope — depends on PM execution intent (backlogged spec `2026-09-04-pm-execution-and-thesis-cards-design.md`); revisit if the pilot shows conversion |
| 2 | Paid/structured news API for same-day analyst actions | Phase 2 candidate — only if the pilot shows early surfacing converts |
| 3 | Burst entry without Buy/Overweight (relaxed gate) | Rejected — would need fast exits to be safe; contradicts pilot scope |
| 4 | Unlabeled-burst gate / two-tier burst size | Rejected — feed lag makes labels unreliable day-of; debate is the filter |
| 5 | New schedule (afternoon/evening scan) | Rejected — 04:10 cadence stands; 1-day floor accepted |
| 6 | Burst churn guard | Burst-slot only (2d); rank churn guard stays off (`exclusion_days: 0`) |

## 7. Deliverables / sequencing

1. This spec (committed).
2. Backtest gate script + verdict (evidence; runs on local CSVs in dev).
3. Burst scan + pool merge + provenance tags (TDD).
4. `analyze_results --by-surfacing` slice.
5. Config + deploy (watchlist.yaml, config.py keys, docs).
