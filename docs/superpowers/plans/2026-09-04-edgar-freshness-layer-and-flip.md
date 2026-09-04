# EDGAR Freshness Layer + Fundamentals Flip — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a company has announced its latest quarter via 8-K press release but not yet filed the 10-Q, serve those announced headline numbers (from our own extraction, never Yahoo) alongside the as-filed EDGAR statements — then flip `fundamentals_source` to `edgar` in production.

**Architecture:** `payload_for` (fundamentals_edgar.py) currently *raises* on any structural-gate reason, forcing a whole-payload Yahoo fallback. This plan splits gate reasons into **fatal** (still raise → Yahoo) vs **staleness-only** (statements old but a newer reported quarter exists in our cached 8-K extraction): staleness-only now renders the EDGAR payload with an injected "latest reported quarter (8-K …)" row. A cache-only accessor on `earnings_metrics` exposes the 8-K headline without ever triggering a fresh LLM extraction. The flip is a one-line config change gated behind the re-run of the QA diff harness and an isolated single-ticker live run.

**Tech Stack:** Python 3.12, pytest, ruff (line-length 100, E/W/F/I/B/UP/C4/SIM), SEC EDGAR APIs, yfinance.

## Global Constraints

- **NEVER modify anything under `tradingagents/`** — framework is a library; all behavior changes are runtime patches/our own modules.
- Tests **hermetic**: no network, no LLM, no broker (`tests/conftest.py` installs placeholder keys). Gate: full `pytest -q` green + `uvx ruff check <files>`.
- Conventional commits (`feat:` / `fix:`); deploy = `git pull` on the production PC + full suite there.
- All date logic pinned to `America/New_York` (`ZoneInfo`) or UTC for cache keys; never server-local.
- `EDGAR_CACHE_DIR` env var isolates the disk cache in tests; module caches get reset between tests.
- No secrets in commits; `.env` is gitignored.
- Quality gate semantics (existing, from 2026-09-04): wrong-but-plausible EDGAR data must never reach a debate — fatal reasons fall back to the recorded yfinance originals with a logged warning.

---

### Task 1: Cache-only 8-K headline accessor in `earnings_metrics`

**Files:**
- Modify: `earnings_metrics.py` (add `reported_headline` next to `earnings_line`, ~line 160)
- Test: `tests/test_earnings_metrics.py`

**Interfaces:**
- Consumes: existing `find_latest_earnings_8k(ticker) -> dict | None`, `_disk_load(ticker, accn) -> dict | None`, `_cache` (module-level, reset via `reset_cache()`), `edgar` module.
- Produces: `reported_headline(ticker: str) -> dict | None` — the cached 8-K metrics `{"period", "revenue", "eps", "guidance", "filed"}` or `None`. **Cache-only by design**: never calls `_call_extract_llm` — a missing cache returns `None` so the fundamentals render path can never trigger an extraction.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_earnings_metrics.py`:

```python
class TestReportedHeadline:
    def test_returns_cached_metrics_without_extracting(self, http, tmp_path,
                                                       monkeypatch):
        """The fundamentals freshness layer must read the 8-K headline from
        cache only — it must never trigger a fresh LLM extraction."""
        calls = {"n": 0}

        def fake_extract(text, filing_date):
            calls["n"] += 1
            return {"period": "Q2 2026", "revenue": "$4.29B", "eps": "$15.50",
                    "guidance": "FY26 GAAP EPS $60.61-$62.00"}

        monkeypatch.setattr(em, "_call_extract_llm", fake_extract)
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        em.reset_cache()
        assert em.earnings_line("REGN") != ""   # warms the disk cache
        assert calls["n"] == 1
        em.reset_cache()                        # fresh process simulation
        head = em.reported_headline("REGN")
        assert head is not None
        assert head["period"] == "Q2 2026"
        assert head["revenue"] == "$4.29B"
        assert head["filed"] == "2026-09-03"
        assert calls["n"] == 1                  # no new extraction

    def test_returns_none_when_cache_missing(self, http, monkeypatch):
        """Without a warm cache (and no LLM allowed), headline is None —
        the caller falls back rather than blocking on an extraction."""
        def boom(_t, _d):
            raise AssertionError("must not extract")
        monkeypatch.setattr(em, "_call_extract_llm", boom)
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        em.reset_cache()
        assert em.reported_headline("REGN") is None

    def test_no_8k_returns_none(self, http):
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["10-Q"]))
        assert em.reported_headline("REGN") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_earnings_metrics.py::TestReportedHeadline -v`
Expected: FAIL with `AttributeError: module 'earnings_metrics' has no attribute 'reported_headline'`

- [ ] **Step 3: Write minimal implementation**

In `earnings_metrics.py`, after `earnings_line`:

```python
def reported_headline(ticker: str) -> dict | None:
    """Cached 8-K earnings headline (period/revenue/eps/guidance/filed).

    Cache-only on purpose: the fundamentals freshness layer calls this on
    every render, so it must never trigger an LLM extraction. A cold cache
    (first morning after an earnings 8-K) returns None and the caller
    falls back to the yfinance payload that day.
    """
    try:
        filing = find_latest_earnings_8k(ticker)
        if filing is None:
            return None
        accn = filing["accession_number"]
        with _lock:
            cached = _cache.get((ticker, accn), "missing")
        if cached == "missing":
            cached = _disk_load(ticker, accn)
        if not cached:
            return None
        return {k: cached.get(k, "") for k in
                ("period", "revenue", "eps", "guidance", "filed")}
    except Exception:  # noqa: BLE001 - routine cold-cache misses are silent
        return None
```

`except` is intentionally silent — a cold cache or transient EDGAR blip is
routine, not a warning.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_earnings_metrics.py -q`
Expected: PASS (all earnings tests incl. the 3 new)

- [ ] **Step 5: Commit**

```bash
git add earnings_metrics.py tests/test_earnings_metrics.py
git commit -m "feat: cache-only reported_headline accessor for the 8-K metrics"
```

---

### Task 2: Staleness classification + headline injection in `payload_for`

**Files:**
- Modify: `fundamentals_edgar.py` — `payload_for` (raise → classify), new `_headline_row(headline, latest_end)` helper, `render_fundamentals` gains optional `headline` param
- Test: `tests/test_fundamentals_edgar.py`

**Interfaces:**
- Consumes: `structural_quality(facts, curr_date) -> list[str]` (existing), `earnings_metrics.reported_headline(ticker) -> dict | None` (Task 1), `edgar.EdgarError`.
- Produces:
  - `render_fundamentals(facts, ticker, curr_date, price, identity, consensus, today=None, headline=None) -> str` — when `headline` is a non-empty dict, appends a row:
    `"Latest reported quarter ({period}, 8-K filed {filed}, official filing pending): revenue {revenue}; EPS {eps}"`
  - `payload_for(ticker, curr_date) -> str` semantics change:
    - **fatal** reasons (anything except the staleness message) → raise `EdgarError` (unchanged → Yahoo fallback)
    - **staleness-only** (`"statements end ..."` is the sole reason) **and** `earnings_metrics.reported_headline(ticker)` returns a dict → render EDGAR payload with the headline injected (NO raise)
    - **staleness-only and no headline** → raise `EdgarError` (no fresh data exists anywhere; Yahoo fallback is correct)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fundamentals_edgar.py` (module already imports `edgar as edgar_mod`, `fe`, `companyfacts`):

```python
class TestFreshnessLayer:
    def _http_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EDGAR_CACHE_DIR", str(tmp_path / "cache"))
        routes = {
            "company_tickers.json": (
                b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"}]'),
            "companyfacts/CIK0000872589.json":
                edgar_mod._jb(companyfacts()),
        }

        def fake_get(url: str) -> bytes:
            for key, payload in routes.items():
                if key in url:
                    return payload
            raise edgar_mod.EdgarError(f"no route for {url}")

        monkeypatch.setattr(edgar_mod, "_http_get", fake_get)
        edgar_mod.clear_cache()
        return routes

    def test_stale_with_headline_renders_instead_of_raising(
            self, monkeypatch, tmp_path):
        """INCY class: statements 120d+ old but the 8-K headline is cached —
        serve EDGAR statements + the announced quarter instead of raising."""
        self._http_env(monkeypatch, tmp_path)
        monkeypatch.setattr(fe, "_yf_info_min", lambda t: {})
        monkeypatch.setattr(fe, "_last_close", lambda t: 100.0)
        monkeypatch.setattr(fe, "earnings_metrics",
                            _FakeEarnings({"period": "Q2 2026",
                                           "revenue": "$4,291M",
                                           "eps": "$12.23",
                                           "filed": "2026-07-30",
                                           "guidance": ""}))
        out = fe.payload_for("REGN", "2026-11-15")  # statements 138d old
        assert "Latest reported quarter (Q2 2026, 8-K filed 2026-07-30" in out
        assert "Revenue (TTM)" in out  # EDGAR statements still served

    def test_stale_without_headline_raises(self, monkeypatch, tmp_path):
        self._http_env(monkeypatch, tmp_path)
        monkeypatch.setattr(fe, "_yf_info_min", lambda t: {})
        monkeypatch.setattr(fe, "_last_close", lambda t: 100.0)
        monkeypatch.setattr(fe, "earnings_metrics",
                            _FakeEarnings(None))
        with pytest.raises(edgar_mod.EdgarError, match="quality gate"):
            fe.payload_for("REGN", "2026-11-15")

    def test_fatal_gap_still_raises_even_with_headline(
            self, monkeypatch, tmp_path):
        """Fatal structural problems (gaps after derivation, no shares) are
        never papered over by a headline — Yahoo fallback stays correct."""
        raw = companyfacts()
        # drop Q1'25..Q3'25 rows so the FY row cannot derive Q4 cleanly and
        # remove the shares tag: two fatal reasons
        rows = raw["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        raw["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [
            r for r in rows if r["end"] not in ("2025-09-30", "2025-12-31",
                                                "2026-03-31", "2026-06-30")]
        del raw["facts"]["dei"]["EntityCommonStockSharesOutstanding"]
        self._http_env(monkeypatch, tmp_path)
        monkeypatch.setattr(edgar_mod, "_http_get", _route(
            {"company_tickers.json": edgar_mod._jb(
                [{"cik_str": 872589, "ticker": "REGN"}]),
             "companyfacts/CIK0000872589.json": edgar_mod._jb(raw)}))
        monkeypatch.setattr(fe, "earnings_metrics",
                            _FakeEarnings({"period": "Q2 2026",
                                           "filed": "2026-07-30"}))
        with pytest.raises(edgar_mod.EdgarError):
            fe.payload_for("REGN", "2026-08-01")
```

Add a module-scoped fake near the top of `tests/test_fundamentals_edgar.py`:

```python
class _FakeEarnings:
    def __init__(self, headline):
        self._headline = headline

    def reported_headline(self, ticker):
        return self._headline
```

`_route` is a tiny helper returning a `fake_get(url)` closure over the given
routes (mirror `_http_env`'s `fake_get`). Replace the duplicated fixture body
in `test_fatal_gap_still_raises_even_with_headline` with a single call to a
shared helper `_fake_edgar(monkeypatch, tmp_path, raw=None)` extracted in the
same test file if preferred — keep tests green and hermetic.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fundamentals_edgar.py::TestFreshnessLayer -v`
Expected: FAIL — `payload_for` still raises on the staleness case (test 1),
and `render_fundamentals` has no `headline` parameter.

- [ ] **Step 3: Implement the classification + headline row**

In `fundamentals_edgar.py`:

```python
import earnings_metrics  # module import at top of fundamentals_edgar.py


def _headline_row(headline: dict | None) -> str | None:
    """One source-dated row for the announced-but-unfiled quarter."""
    if not headline or not headline.get("period"):
        return None
    parts = [f"Latest reported quarter ({headline['period']}, 8-K filed "
             f"{headline.get('filed') or '?'}, official filing pending)"]
    bits = []
    if headline.get("revenue"):
        bits.append(f"revenue {headline['revenue']}")
    if headline.get("eps"):
        bits.append(f"EPS {headline['eps']}")
    if bits:
        parts.append(": " + "; ".join(bits))
    if headline.get("guidance"):
        parts.append(f"; Guidance: {headline['guidance']}")
    return "".join(parts)
```

`render_fundamentals(..., headline=None)`: after the existing
`("Latest filed quarter-end (statements)", ...)` row, add:

```python
    if headline:
        row = _headline_row(headline)
        if row:
            rows.append(("Announced quarter (8-K, 10-Q pending)", row))
```

`payload_for` becomes:

```python
def payload_for(ticker: str, curr_date: str) -> str:
    facts = edgar.load_facts(ticker)
    reasons = structural_quality(facts, curr_date)
    headline = None
    if reasons:
        fatal = [r for r in reasons
                 if not r.startswith("statements end ")]
        if fatal or len(reasons) > 1:
            raise edgar.EdgarError(
                "EDGAR payload failed the structural quality gate: "
                + "; ".join(reasons))
        # staleness-only: serve as-filed statements + the announced quarter
        headline = earnings_metrics.reported_headline(ticker)
        if not headline:
            raise edgar.EdgarError(
                "EDGAR statements stale and no 8-K headline cached: "
                + "; ".join(reasons))
    identity = _yf_info_min(ticker)
    consensus = {k: v for k, v in _yf_info_min(ticker).items()
                 if k in ("forward_eps", "target_mean_price",
                          "dividend_rate", "dividend_yield")}
    return render_fundamentals(facts, ticker, curr_date,
                               price=_last_close(ticker),
                               identity=identity, consensus=consensus,
                               headline=headline)
```

Note the existing renderer already appends `Latest filed quarter-end` and the
`TTM coverage warning` — do not duplicate; the headline row is the only
addition. Existing calls to `render_fundamentals` in tests pass `headline`
defaults to `None` — no breakage.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fundamentals_edgar.py tests/test_earnings_metrics.py -q`
Then the full suite: `pytest -q`
Expected: PASS — `test_payload_for_raises_on_quality_gate` (existing, stale-as-of 2026-11-15) must be updated: with no fake earnings wired, `earnings_metrics.reported_headline` returns None → still raises → test unchanged and green. Ruff: `uvx ruff check fundamentals_edgar.py earnings_metrics.py tests/test_fundamentals_edgar.py tests/test_earnings_metrics.py`.

- [ ] **Step 5: Commit**

```bash
git add fundamentals_edgar.py tests/test_fundamentals_edgar.py
git commit -m "feat: serve as-filed statements + 8-K announced quarter when the 10-Q lags"
```

---

### Task 3: Live validation of the freshness layer (single tickers, no batch)

**Files:**
- Create: `/tmp/freshness_probe.py` on the PC (throwaway, not committed)
- Test: none — live probe against real EDGAR data

**Interfaces:**
- Consumes: `fe.payload_for`, `earnings_metrics.earnings_line` (warm the cache first), `edgar.load_facts`, `fe.structural_quality` — all existing.

- [ ] **Step 1: Warm INCY's 8-K cache and render its payload**

On the PC (repo at latest main):

```bash
cd /home/harsh-amin/workplace/TradingAgents
PYTHONPATH=. .venv/bin/python - <<'EOF'
from dotenv import load_dotenv; load_dotenv(".env")
import earnings_metrics as em
print(em.earnings_line("INCY")[:200] or "(no 8-K cached)")
EOF
```

Expected: INCY's earnings line renders (first call extracts + caches; if it
prints `(no 8-K cached)` after a ~2 min wait, re-run once — the extraction is
one-time per filing).

- [ ] **Step 2: Render the payload and confirm the headline row**

```bash
PYTHONPATH=. .venv/bin/python - <<'EOF'
import fundamentals_edgar as fe
print(fe.payload_for("INCY", "2026-09-04"))
EOF
```

Expected: payload includes `Announced quarter (8-K, 10-Q pending)` with
INCY's reported revenue/EPS AND the as-filed EDGAR statements (revenue TTM
through Q1'26) with the `Latest filed quarter-end (statements) 2026-03-31`
row. Confirm no `quality gate` exception (previously this raised).

- [ ] **Step 3: Confirm REGN (fully filed) renders WITHOUT a headline row**

```bash
PYTHONPATH=. .venv/bin/python - <<'EOF'
import fundamentals_edgar as fe
out = fe.payload_for("REGN", "2026-09-04")
assert "Announced quarter" not in out, "REGN has a filed 10-Q; no headline expected"
print("REGN clean, no headline row")
EOF
```

Expected: `REGN clean, no headline row`.

- [ ] **Step 4: Run the QA diff harness on the full pool**

```bash
PYTHONPATH=. timeout 400 .venv/bin/python scripts/edgar_diff_qa.py
```

Expected: revenue_ttm within ±2.2% on 15/16 tickers; INCY revenue flagged
~−7.9% **is expected and correct** (statements lag one quarter; the headline
row now supplies the announced quarter to the debate). Note the result in the
commit message of Task 4.

---

### Task 4: Flip `fundamentals_source` to `edgar` + deploy + docs

**Files:**
- Modify: `watchlist.yaml` (line ~25: `fundamentals_source: yfinance` → `edgar`)
- Modify: `AGENTS.md` (module table row for `fundamentals_edgar.py`: note the flip is live; `watchlist.yaml` row: `fundamentals_source` default note)
- Modify: `docs/superpowers/specs/2026-09-04-edgar-fundamentals-events-design.md` (status: fundamentals hybrid live)

**Interfaces:**
- Consumes: everything from Tasks 1–3 + the deployed QA harness.
- Produces: production state where the 4 fundamentals tools render EDGAR
  payloads with the freshness layer; yfinance remains the per-ticker
  automatic fallback and the consensus/dividend/sector source.

- [ ] **Step 1: Flip the config**

`watchlist.yaml`:

```yaml
fundamentals_source: edgar  # "edgar" = SEC companyfacts as-filed statements
                           # + 8-K announced-quarter layer; yfinance fallback
                           # stays automatic per ticker (quality gate)
```

- [ ] **Step 2: Update docs**

`AGENTS.md` `fundamentals_edgar.py` row: append `(LIVE 2026-09-04: config
flipped to edgar; staleness-only gate reasons render as-filed statements plus
the 8-K announced-quarter headline from earnings_metrics.reported_headline;
fatal reasons still fall back to yfinance per ticker).`

`AGENTS.md` `watchlist.yaml` row: change `fundamentals_source (edgar/yfinance)`
to `fundamentals_source (edgar — live)`.

Spec doc: add a status line under the title: `Status: fundamentals hybrid
LIVE (2026-09-04); corporate events + tape live; see freshness-layer plan.`

- [ ] **Step 3: Full gates + commit + push**

```bash
pytest -q          # full suite green (expect ~948+)
uvx ruff check . --exclude tradingagents
git add watchlist.yaml AGENTS.md docs/superpowers/specs/2026-09-04-edgar-fundamentals-events-design.md
git commit -m "feat: flip fundamentals_source to edgar (freshness layer + QA green)"
git push origin main
```

- [ ] **Step 4: Deploy to the PC and verify the config**

```bash
ssh pc 'cd /home/harsh-amin/workplace/TradingAgents && git pull -q origin main && grep -n fundamentals_source watchlist.yaml && pytest -q --timeout=30 -p no:cacheprovider | tail -1'
```

Expected: `fundamentals_source: edgar` in the file; full suite green on the PC.

- [ ] **Step 5: Post-run verification checklist (next morning's batch)**

After the next `daily_run --analyze` batch completes, run:

```bash
# 1. No fallback warning storm (stale/fatal should be rare, INCY-class only)
grep -c "falling back to yfinance" ~/.tradingagents/logs/structured/$(date +%F)/*.jsonl 2>/dev/null || true
# 2. EDGAR provenance present in fundamentals payloads
grep -l "EDGAR companyfacts (as-filed" ~/.tradingagents/logs/structured/$(date +%F)/*.jsonl | wc -l
# 3. Headline rows appear only for lagging filers
grep -c "Announced quarter (8-K" ~/.tradingagents/logs/structured/$(date +%F)/*.jsonl 2>/dev/null | grep -v ":0" || echo "no lagging filers today"
```

Expected: ≥15/16 tickers carry EDGAR provenance; ≤1–2 yfinance fallbacks
(only genuinely stale/broken names); headline rows only on lagging filers.
Record the numbers in the day's notes. If a ticker shows an unexpected
fallback, run `scripts/edgar_diff_qa.py <TICKER>` and inspect the gate
reason before the next batch.
