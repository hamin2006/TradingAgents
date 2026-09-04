"""fundamentals_edgar.py — EDGAR-backed fundamentals renderers.

Replaces the .func of the framework's four fundamentals tools
(get_fundamentals / get_balance_sheet / get_cashflow / get_income_statement)
when ``fundamentals_source: edgar`` is configured.

Composition rule (single source per quantity — the 2026-09-03 audit showed
yfinance quote-info contradicting its own statements: INCY 55.7%-implied GP
vs ~93%-GM fields, PFE's two 200-day SMAs):
- statements/metrics  -> EDGAR companyfacts (as-filed, point-in-time)
- consensus           -> yfinance quote-info ONLY (forward EPS / targets /
                         dividend), labeled "as-of"
- price-derived       -> computed from our market snapshot × EDGAR shares
- quote-price fields  -> absent (market domain's job)

Renderers are pure over injected inputs so hermetic tests exercise the real
composition; ``payload_for`` / ``statements_for`` orchestrate the fetches and
raise EdgarError so the installer can fall back to the recorded yfinance
originals.
"""

from __future__ import annotations

from datetime import datetime, timezone

import yfinance as yf

import edgar

_USD_M = "USD M"

_REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax",
                 "Revenues", "SalesRevenueNet"]
_GP_TAGS = ["GrossProfit"]
_OPINC_TAGS = ["OperatingIncomeLoss"]
_NI_TAGS = ["NetIncomeLoss"]
_CFO_TAGS = ["NetCashProvidedByUsedInOperatingActivities",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
_CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment"]
_BUYBACK_TAGS = ["PaymentsForRepurchaseOfCommonStock",
                 "PaymentsForRepurchaseOfCommonStock1"]
_DIV_TAGS = ["PaymentsOfDividends"]
_DEPR_TAGS = ["DepreciationDepletionAndAmortization",
              "DepreciationAmortizationAndAccretionNet"]
_ASSETS_TAGS = ["Assets"]
_LIAB_TAGS = ["Liabilities"]
_EQUITY_TAGS = ["StockholdersEquity"]
_CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue",
              "CashCashEquivalentsRestrictedAndRestrictedCashAndEquivalents"]
_DEBT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebt"]


def _usd(raw):
    """Companyfacts serves raw dollars; metrics render in USD M."""
    return None if raw is None else raw / 1e6


def _m(val) -> str:
    return "n/a" if val is None else f"{val:,.1f}"


def _m0(val) -> str:
    return "n/a" if val is None else f"{val:,.0f}"


def _pct(numer, denom) -> float | None:
    if numer is None or denom is None or denom == 0:
        return None
    return numer / denom * 100


def _ttm(facts: edgar.Facts, tags, as_of: str) -> float | None:
    return facts.ttm(tags, as_of)


def _memo_share(facts: edgar.Facts, as_of: str) -> int | None:
    return facts.shares_outstanding(as_of)


def revenue_ttm(facts, as_of):
    return _ttm(facts, _REVENUE_TAGS, as_of)


def _ttm_table_rows(facts, as_of, tags, label):
    rows = facts.quarters(tags, as_of)
    return rows, label


# --------------------------------------------------------------------------
# quote-info seam (consensus only)
# --------------------------------------------------------------------------

def _yf_info_min(ticker: str) -> dict:
    """Consensus/identity subset from yfinance quote info (consensus has no
    free alternative — EDGAR only carries reported facts)."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:  # noqa: BLE001 - consensus is enrichment
        return {}
    keep = {}
    for key, target in (("longName", "company_name"),
                        ("sector", "sector"),
                        ("industry", "industry"),
                        ("forwardEps", "forward_eps"),
                        ("targetMeanPrice", "target_mean_price"),
                        ("dividendRate", "dividend_rate"),
                        ("dividendYield", "dividend_yield")):
        val = info.get(key)
        if val not in (None, "", "None"):
            keep[target] = val
    return keep


def _last_close(ticker: str) -> float | None:
    """Last close at/under today (market-snapshot stand-in)."""
    try:
        hist = yf.Ticker(ticker).history(period="7d", interval="1d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# renderers + structural quality gate
# --------------------------------------------------------------------------

_STALE_STATEMENT_DAYS = 120


def _quarter_gaps(rows: list[dict]) -> list[str]:
    """Consecutive-row continuity check (start of one vs end of the prior).

    Calendar-end comparison would false-positive on 13-week filers (PFE's
    quarters end 2026-06-28, not 06-30); adjacency never does. A gap > ~3
    weeks means the filer's rows skip a quarter (BDX 2025-09-30, MRNA
    2025-12-31 — live QA finds) and any TTM over it silently undercounts.
    """
    gaps: list[str] = []
    for i in range(1, len(rows)):
        prev_end = rows[i - 1]["end"]
        cur_start = rows[i]["start"]
        cur_end = rows[i]["end"]
        try:
            gap_days = (datetime.strptime(cur_start, "%Y-%m-%d")
                        - datetime.strptime(prev_end, "%Y-%m-%d")).days
        except (ValueError, TypeError):
            continue
        if gap_days > 21:
            n_missing = max(1, round(gap_days / 90))
            gaps.append(f"{n_missing} missing quarter(s) between "
                        f"{prev_end} and {cur_end}")
    return gaps


def structural_quality(facts: edgar.Facts, curr_date: str) -> list[str]:
    """Structural red flags that make an EDGAR payload untrustworthy.

    The PFE/BDX/MRNA/EL classes (2026-09-04 QA) were all *silent*: stale tag
    tails, quarter gaps, missing shares produced plausible-looking numbers.
    Any reason returned here makes the caller fall back to the yfinance
    originals — wrong-but-plausible must never reach a debate.
    """
    reasons: list[str] = []
    rows = facts.quarters(_REVENUE_TAGS, curr_date)
    if len(rows) < 4:
        reasons.append(f"only {len(rows)} revenue quarters on file")
    gaps = _quarter_gaps(rows)
    reasons.extend(gaps)
    if rows:
        latest = rows[-1]["end"]
        try:
            age = (datetime.strptime(curr_date, "%Y-%m-%d")
                   - datetime.strptime(latest, "%Y-%m-%d")).days
        except ValueError:
            age = 0
        if age > _STALE_STATEMENT_DAYS:
            reasons.append(f"statements end {latest} ({age}d old)")
    if facts.shares_outstanding(curr_date) is None:
        reasons.append("no shares count")
    return reasons


def render_fundamentals(facts: edgar.Facts, ticker: str, curr_date: str,
                        price: float | None, identity: dict,
                        consensus: dict, today: str | None = None) -> str:
    """The comprehensive fundamentals payload (mirrors get_fundamentals)."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows: list[tuple[str, str]] = []
    name = identity.get("company_name") or ticker
    rows.append(("Name", name))
    rows.append(("Sector / Industry", f"{identity.get('sector') or 'n/a'} / "
                                        f"{identity.get('industry') or 'n/a'}"))
    rows.append(("Data source", "EDGAR companyfacts (as-filed, point-in-time "
                                 f"as of {curr_date}); consensus via Yahoo "
                                 f"quote as of {today}"))

    q_rows = facts.quarters(_REVENUE_TAGS, curr_date)
    if q_rows:
        # Window-lag visibility (2026-09-04 QA): when the latest quarter is
        # only in the press release (10-Q unfiled), EDGAR statements end one
        # quarter earlier — say so instead of letting analysts read a stale
        # quarter as current.
        rows.append(("Latest filed quarter-end (statements)",
                     q_rows[-1]["end"]))
        gaps = _quarter_gaps(q_rows)
        if gaps:
            rows.append(("TTM coverage warning", "; ".join(gaps)
                         + " — trailing sums may undercount"))

    rev = _usd(revenue_ttm(facts, curr_date))
    gp = _usd(_ttm(facts, _GP_TAGS, curr_date))
    oi = _usd(_ttm(facts, _OPINC_TAGS, curr_date))
    ni = _usd(_ttm(facts, _NI_TAGS, curr_date))
    depr = _usd(_ttm(facts, _DEPR_TAGS, curr_date))
    cfo = _usd(_ttm(facts, _CFO_TAGS, curr_date))
    capex = _usd(_ttm(facts, _CAPEX_TAGS, curr_date))
    buybacks = _usd(_ttm(facts, _BUYBACK_TAGS, curr_date))
    dividends = _usd(_ttm(facts, _DIV_TAGS, curr_date))
    n_q = len(facts.quarters(_REVENUE_TAGS, curr_date))

    rows.append(("Revenue (TTM)", f"{_m(rev)} {_USD_M}"
                                   f" ({n_q} quarters on file)"))
    rows.append(("Gross Profit (TTM)", f"{_m(gp)} {_USD_M}"))
    rows.append(("Operating Income (TTM)", f"{_m(oi)} {_USD_M}"))
    rows.append(("Net Income (TTM)", f"{_m(ni)} {_USD_M}"))
    rows.append(("Gross Margin (TTM)", f"{_m(_pct(gp, rev))}%"))
    rows.append(("Operating Margin (TTM)", f"{_m(_pct(oi, rev))}%"))
    rows.append(("Net Margin (TTM)", f"{_m(_pct(ni, rev))}%"))

    ebitda = None
    if oi is not None and depr is not None:
        ebitda = oi + depr
    rows.append(("EBITDA (TTM, approx)", f"{_m(ebitda)} {_USD_M}"))
    fcf = None
    if cfo is not None and capex is not None:
        fcf = cfo - capex
    rows.append(("Free Cash Flow (TTM)", f"{_m(fcf)} {_USD_M}"))
    rows.append(("Dividends paid (TTM)", f"{_m(dividends)} {_USD_M}"))
    rows.append(("Buybacks (TTM)", f"{_m(buybacks)} {_USD_M}"))

    shares = _memo_share(facts, curr_date)
    rows.append(("Shares outstanding", "n/a" if shares is None
                                        else f"{shares:,}"))
    if price is not None and shares:
        mcap = price * shares / 1e6
        rows.append(("Market Cap", f"{_m(mcap)} {_USD_M}"))
        # ni is USD M; EPS needs raw dollars against raw share count
        eps_ttm = ni * 1e6 / shares if ni else None
        if eps_ttm:
            rows.append(("PE (TTM)", f"{_m(price / eps_ttm)}"))
    if consensus.get("forward_eps"):
        rows.append(("Forward EPS consensus (Yahoo)", str(consensus["forward_eps"])))
    if consensus.get("target_mean_price"):
        rows.append(("Target mean price (Yahoo)", str(consensus["target_mean_price"])))
    if consensus.get("dividend_rate"):
        rows.append(("Dividend rate (Yahoo)", str(consensus["dividend_rate"])))

    equity = _usd(facts.latest_instant(_EQUITY_TAGS, curr_date))
    assets = _usd(facts.latest_instant(_ASSETS_TAGS, curr_date))
    cash = _usd(facts.latest_instant(_CASH_TAGS, curr_date))
    debt = _usd(facts.latest_instant(_DEBT_TAGS, curr_date))
    if equity:
        rows.append(("Stockholders Equity (latest)", f"{_m0(equity)} {_USD_M}"))
    if assets:
        rows.append(("Total Assets (latest)", f"{_m0(assets)} {_USD_M}"))
    if cash:
        rows.append(("Cash & equivalents (latest)", f"{_m0(cash)} {_USD_M}"))
    if debt:
        rows.append(("Long-term debt (latest)", f"{_m0(debt)} {_USD_M}"))
    if ni and equity:
        rows.append(("ROE (TTM)", f"{_m(_pct(ni, equity))}%"))

    body = "\n".join(f"- {label}: {value}" for label, value in rows)
    return (f"# Company Fundamentals for {ticker} (EDGAR as-filed)\n"
            f"# Data retrieved on: {today}\n{body}")


def _quarter_table(facts: edgar.Facts, as_of: str, metric_tags: list[tuple]):
    """Columns = latest quarter ends (newest first); rows = metrics."""
    ends: list[str] = []
    for tags, _label in metric_tags:
        for row in facts.quarters(tags, as_of):
            if row["end"] not in ends:
                ends.append(row["end"])
    ends = sorted(ends, reverse=True)[:5]
    lines = ["| Metric | " + " | ".join(ends) + " |"]
    lines.append("| --- |" + " --- |" * len(ends))
    for tags, label in metric_tags:
        by_end = {r["end"]: _usd(r["val"]) for r in facts.quarters(tags, as_of)}
        cells = " | ".join(_m0(by_end.get(e)) for e in ends)
        lines.append(f"| {label} | {cells} |")
    return "\n".join(lines)


def _instant_table(facts: edgar.Facts, as_of: str,
                   metric_tags: list[tuple]) -> str:
    ends = []
    for tags, _label in metric_tags:
        rows = facts._rows(tags) or []
        for row in rows:
            if (row.get("start") is None and row.get("end")
                    and row.get("filed", "") <= as_of
                    and row["end"] not in ends):
                ends.append(row["end"])
    ends = sorted(ends, reverse=True)[:5]
    lines = ["| Metric | " + " | ".join(ends) + " |"]
    lines.append("| --- |" + " --- |" * len(ends))
    for tags, label in metric_tags:
        by_end = {}
        for row in facts._rows(tags) or []:
            if row.get("start") is None and row.get("end") and \
                    row.get("filed", "") <= as_of:
                by_end[row["end"]] = _usd(row["val"])
        cells = " | ".join(_m0(by_end.get(e)) for e in ends)
        lines.append(f"| {label} | {cells} |")
    return "\n".join(lines)


def render_income_statement(facts: edgar.Facts, ticker: str, curr_date: str,
                            freq: str) -> str:
    assert freq in ("quarterly", "annual")
    table = _quarter_table(facts, curr_date, [
        (_REVENUE_TAGS, "Revenue"),
        (_GP_TAGS, "Gross Profit"),
        (_OPINC_TAGS, "Operating Income"),
        (_NI_TAGS, "Net Income"),
    ])
    return (f"# Income Statement for {ticker} (EDGAR as-filed, {freq}, "
            f"{_USD_M})\n{table}")


def render_balance_sheet(facts: edgar.Facts, ticker: str, curr_date: str,
                         freq: str) -> str:
    assert freq in ("quarterly", "annual")
    table = _instant_table(facts, curr_date, [
        (_ASSETS_TAGS, "Total Assets"),
        (_CASH_TAGS, "Cash & Equivalents"),
        (_DEBT_TAGS, "Long-term Debt"),
        (_LIAB_TAGS, "Total Liabilities"),
        (_EQUITY_TAGS, "Stockholders Equity"),
    ])
    return (f"# Balance Sheet for {ticker} (EDGAR as-filed, {freq}, "
            f"{_USD_M})\n{table}")


def render_cashflow(facts: edgar.Facts, ticker: str, curr_date: str,
                    freq: str) -> str:
    assert freq in ("quarterly", "annual")
    table = _quarter_table(facts, curr_date, [
        (_CFO_TAGS, "Operating Cash Flow"),
        (_CAPEX_TAGS, "Capex"),
        (_BUYBACK_TAGS, "Buybacks"),
        (_DIV_TAGS, "Dividends Paid"),
    ])
    return (f"# Cash Flow for {ticker} (EDGAR as-filed, {freq}, "
            f"{_USD_M})\n{table}")


# --------------------------------------------------------------------------
# orchestration (raises EdgarError so the installer falls back)
# --------------------------------------------------------------------------

def payload_for(ticker: str, curr_date: str) -> str:
    facts = edgar.load_facts(ticker)
    reasons = structural_quality(facts, curr_date)
    if reasons:
        # Wrong-but-plausible must never reach a debate: structural red flags
        # (stale tag tail, quarter gaps, missing shares) raise so the
        # installer falls back to the recorded yfinance originals.
        raise edgar.EdgarError(
            "EDGAR payload failed the structural quality gate: "
            + "; ".join(reasons))
    identity = _yf_info_min(ticker)
    consensus = {k: v for k, v in _yf_info_min(ticker).items()
                 if k in ("forward_eps", "target_mean_price",
                          "dividend_rate", "dividend_yield")}
    return render_fundamentals(facts, ticker, curr_date,
                               price=_last_close(ticker),
                               identity=identity, consensus=consensus)


def statements_for(method: str, ticker: str, freq: str,
                   curr_date: str) -> str:
    facts = edgar.load_facts(ticker)
    if method == "get_balance_sheet":
        return render_balance_sheet(facts, ticker, curr_date, freq)
    if method == "get_cashflow":
        return render_cashflow(facts, ticker, curr_date, freq)
    return render_income_statement(facts, ticker, curr_date, freq)
