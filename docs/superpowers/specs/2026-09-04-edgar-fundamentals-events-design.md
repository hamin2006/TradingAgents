# EDGAR Fundamentals, Corporate Events & Market Tape (2026-09-04)

Status: **Design (approved for implementation)** — spec written 2026-09-04.

## Problem

The 2026-09-03 batch audit surfaced four data-quality gaps:

1. **Fundamentals payloads contradict themselves.** yfinance serves the same
   quantity from multiple accounting scopes in one payload (INCY: GP $3.24B on
   $5.82B revenue = 55.7% alongside ~93%-GM fields; PFE: two 200-day SMAs;
   TTM EPS never summing to quarterly EPS; RVTY: revenue mismatch). Models
   reproduced the contradictions faithfully into bull/bear/RM speeches.
2. **Non-GAAP metrics and forward guidance ride unstructured news.** EL's
   entire valuation debate fought over guidance ($3.10–3.35 "above analyst
   estimates" vs $3.91 consensus) with no attributable source. PFE's
   "normalized earnings" had the same provenance problem.
3. **The framework's insider tool is dead on arrival.** `get_insider_transactions`
   was bound to the News Analyst but invoked 0 times across all 16 tickers,
   and its yfinance vendor lags filings by days (verified live 2026-09-03:
   Guarini's Form 4 filed that morning was still absent from yfinance hours
   later; McCourt's Sep-3 sale was never going to appear same-day).
4. **Analysts argue market/regime blind.** SPY-vs-200d, VIX, and sector tape
   never reach per-ticker context (REGN's bull cited "PFE at 52-week highs,
   healthcare defensive" from news articles rather than data).

## Design decisions (agreed with the user)

- **EDGAR companyfacts** replaces yfinance for statement fundamentals — the
  change-1 "consistency guard" was rejected as brittle rule-list code; the
  generalizable principle is **one source of truth per quantity**. EDGAR is
  primary-source, point-in-time (`filed` dates), free, keyless.
- **yfinance stays for exactly three things**: consensus estimates (no free
  alternative — proprietary sell-side aggregation), dividend declared
  rate/yield, and GICS sector taxonomy (EDGAR only has SIC). All date-labeled.
- **8-K earnings-release extraction IS in v1**: one cached LLM structured
  extraction per filing (revenue, EPS, forward-guidance sentence), because
  it kills the observed EL-class failure and amortizes to ~nothing at
  quarterly frequency per ticker.
- **Corporate events (Form 4 + 8-K)** land in v1, injected into the context
  every agent receives — replacing the dead insider-tool path.
- **Market tape** (SPY/VIX/sector ETF) injects with it. Cheap, failure-safe.
- **Explicitly out** (considered, rejected — revisit only with new evidence):
  EDGAR full-text search, proxy statements (DEF 14A), institutional ownership
  (13F). No audit finding maps to them; parsing surface not justified.

## Architecture

All code lives in our modules; `tradingagents/` is never modified. Every
behavior change is a runtime installer in `daily_run.py` (idempotent, with
`_reset_*` helpers — existing pattern).

### `edgar.py` — shared EDGAR client

- CIK resolution: `data.sec.gov/files/company_tickers.json` (ticker→CIK),
  in-memory + disk cache (`~/.tradingagents/cache/edgar/`), refresh ≤1/day.
- companyfacts: `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` —
  fetch once per ticker per day, cache to disk, parse lazily.
- submissions: `data.sec.gov/submissions/CIK##########.json` — recent
  filings list (Form 4 / 8-K detection).
- SEC etiquette: UA header, paced requests (≥1s apart, shared `RLock` —
  4 parallel analyze workers), `HTTP` seam injectable for hermetic tests.
- **As-of semantics**: keep facts with `filed <= curr_date`; dedupe
  amendments (latest `filed` wins per period+form); income-statement TTM =
  sum of the 4 latest quarterly duration facts (filed-ordered).
- **Tag taxonomy**: fallback chains per quantity, e.g. revenue
  `RevenueFromContractWithCustomerExcludingAssessedTax` → `Revenues`;
  operating income `OperatingIncomeLoss` → …; unit handling (USD vs shares).
- Computed metrics: EBITDA ≈ op income + D&A (tagged); FCF = CFO − capex;
  shares (instant `dei:EntityCommonStockSharesOutstanding`);
  dividends paid + buybacks from cash-flow facts.

### `fundamentals_edgar.py` — the four tool renderers

Replaces the `.func` of `get_fundamentals`, `get_balance_sheet`,
`get_cashflow`, `get_income_statement` (shared Tool objects in
`tradingagents.agents.utils.fundamental_data_tools` — one patch point).
Markdown shapes mirror today's outputs. Composition rule:

| Quantity | Source |
|---|---|
| Revenue, GP, margins, EPS, shares, BS/CF/IS rows, FCF, EBITDA, buybacks, dividends paid | EDGAR companyfacts (as-filed) |
| Forward EPS, target price, dividend rate/yield, sector/industry (GICS) | yfinance quote-info — consensus-only, date-labeled |
| Market cap, PE TTM, P/B, forward PE | Computed: EDGAR shares × our memoized market-snapshot close |
| Quote-price fields (50/200-day, 52-week, raw price) | **Dropped from this payload** (market domain's job — kills the contradiction class) |

Config gate: `fundamentals_source: edgar|yfinance` (watchlist.yaml, default
`yfinance`). EDGAR ingest failure → call the recorded original tool func
(yfinance) and log an event — fundamentals never go dark mid-batch.

### `corp_events.py` — Form 4 watcher + 8-K detection

From the ticker's submissions JSON (last 10 calendar days):

- **Form 4**: fetch `edgardoc.xml` (dashless accession — S3 key lesson from
  2026-09-03), parse deterministically: owner, position, code, shares,
  price, date. Render one line per trade:
  `2026-09-02 Form 4: Guarini (Director) SOLD 400 @ $850.00 ($340,000)`.
  Collapse exercise→sale pairs. (Real shape validated against the
  Guarini/McCourt filings.)
- **8-K**: list (date, accession); if the most recent one is an earnings
  release (item 2.02 / exhibit 99.1 present in the index), surface it for
  `earnings_metrics`.
- Output ≤6 items, cached per ticker per day.

### `earnings_metrics.py` — 8-K exhibit extraction (v1)

- Locate the earnings-release 8-K's exhibit 99.1 (index-based).
- **One cached LLM structured extraction per filing** (structured output;
  small `max_tokens`): reported revenue, reported EPS, and the explicit
  forward-guidance sentence if present. Result cached on disk keyed by
  accession — a ticker analyzed daily reuses the extraction all quarter.
- Source-labeled output: `From Q2 2026 earnings release, filed 2026-08-06`.
- Model: the configured flash pin via OpenRouter (same env key); extraction
  failure → the events block shows the filing date with a
  "metrics unavailable" note; never fails the run.

### `market_tape.py` — regime/tape context

Memoized per day (600s TTL, failure-safe per line):

- SPY last close vs 200d SMA (above/below, %).
- VIX level vs 20.
- Sector ETF 5-day % change (small GICS→XL* map; sector from the cached
  instrument identity). Unknown sector → skip the sector line.

### Installers (`daily_run.py`)

- `_ensure_edgar_fundamentals()` — swaps the four fundamentals Tool `.func`s
  only when `cfg["fundamentals_source"] == "edgar"`; preserves originals for
  fallback and reset.
- `_ensure_tape_and_events()` — wraps `TradingAgentsGraph.resolve_instrument_context`
  (same seam as the phantom-fix stance, chained after it) appending two
  compact blocks: market tape + corporate events (Form 4 / 8-K / earnings
  metrics). Every analyst and debater receives them; both blocks failure-safe.

### Config

- `watchlist.yaml` + `config.py` `APP_DEFAULTS`: `fundamentals_source:
  "yfinance"` (flip to `edgar` post-validation).

## Rollout

1. Implement + hermetic tests → ship. Fundamentals renderer ships gated OFF
   (`fundamentals_source: yfinance`); events + tape ship ON (additive,
   same class as the news-dating change).
2. `scripts/edgar_diff_qa.py` (throwaway QA, not in the suite): field-by-field
   EDGAR-vs-yfinance disagreement report against the live pool.
3. Flip `fundamentals_source: edgar` on parity; the yfinance fallback stays as
   the safety net.

## Testing (hermetic)

- `edgar.py`: CIK map, as-of filtering, amendment dedupe, TTM math, tag
  fallbacks, unit handling — fixture companyfacts JSON via injected HTTP seam.
- `corp_events.py`: real-shape Form-4 XML fixtures (Guarini/McCourt),
  line formatting, M→S collapse, 8-K listing.
- `fundamentals_edgar.py`: renderer composition — EDGAR fields present,
  consensus fields present + labeled, price-derived computed, quote-price
  fields absent.
- `earnings_metrics.py`: extraction via fake LLM seam; cache-by-accession;
  failure note path.
- `market_tape.py`: line builders with stub market fetches; skip-on-error.
- Installers: idempotency, `.func` swap + fallback-on-EDGAR-failure, resets,
  context injection shows stance + tape + events; failure paths skip.
- Full existing suite stays green; ruff clean. No full-batch live runs for
  testing (user constraint); live smoke = single-ticker EDGAR fetch +
  Form-4 parse probe on the PC.

## Out of scope (recorded)

- 8-K beyond v1 metrics (segment detail, full non-GAAP bridge) — revisit with
  evidence.
- EDGAR full-text search, DEF 14A proxies, 13F institutional ownership.
- Consensus estimates remain yfinance (no free alternative) — labeled.
