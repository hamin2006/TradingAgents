"""earnings_metrics.py — 8-K earnings-release metrics for agent context.

The 2026-09-03 audit showed guidance/non-GAAP numbers (EL's "$3.10-3.35
above estimates", PFE's "normalized earnings") riding unstructured news with
no attributable source. Earnings releases are filed as 8-K exhibit 99.1 on
EDGAR — the structural, dated home for those numbers. This module locates
the latest earnings 8-K, pulls the release text, and runs ONE cached LLM
extraction per filing (revenue, EPS, forward-guidance sentence) so a ticker
analyzed daily reuses the same result all quarter.

Failure-safe end to end: any fetch/parse/extraction error degrades to an
empty line — context decoration never breaks an analysis.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading

import edgar

logger = logging.getLogger(__name__)

_EARNINGS_WINDOW_DAYS = 180
_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{name}"

_cache: dict[str, dict | None] = {}
_lock = threading.Lock()


def reset_cache() -> None:
    """Drop the in-memory cache (tests; the disk cache survives)."""
    with _lock:
        _cache.clear()


def _disk_key(ticker: str, accn: str) -> str:
    return f"{ticker.strip().upper()}-{edgar.dashless(accn)}"


def _disk_load(ticker: str, accn: str) -> dict | None:
    """Per-filing extraction from disk (immutable once filed — no TTL)."""
    try:
        body = edgar._cache_read("earnings-metrics", _disk_key(ticker, accn),
                                 ttl=None)
        return json.loads(body) if body is not None else None
    except (OSError, ValueError):
        return None


def _disk_store(ticker: str, accn: str, payload: dict) -> None:
    import contextlib

    with contextlib.suppress(OSError):  # cache is best-effort
        edgar._cache_write("earnings-metrics", _disk_key(ticker, accn),
                           json.dumps(payload).encode())


def find_latest_earnings_8k(ticker: str, window_days: int = _EARNINGS_WINDOW_DAYS
                            ) -> dict | None:
    """Newest 8-K filing within the window (best-effort earnings proxy)."""
    import datetime as _dt
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
        days=window_days)).strftime("%Y-%m-%d")
    for filing in edgar.load_submissions(ticker).recent(since):
        if filing["form"] == "8-K":
            return filing  # recent() is newest-first
    return None


_EX99_MARKERS = ("ex991", "ex_991", "ex-991", "exh_991", "exh-991",
                 "exhibit99", "exhibit_99", "exhibit-99", "ex99", "99.1")


def _pick_exhibit(index: dict) -> str | None:
    """Prefer an exhibit-99-ish file; fall back to the biggest .htm."""
    items = index.get("directory", {}).get("item", [])
    names = [i.get("name", "") for i in items]
    for name in names:
        lowered = name.lower()
        if any(marker in lowered for marker in _EX99_MARKERS):
            return name
    htm = [n for n in names if n.lower().endswith((".htm", ".html"))
           and not n.lower().endswith("-index.htm")]
    if htm:
        return max(htm, key=lambda n: _size_of(n, items))
    return None


def _size_of(name: str, items: list[dict]) -> int:
    for item in items:
        if item.get("name") == name:
            try:
                return int(item.get("size", 0) or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _strip_html(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _filing_url(cik_num: str, accn: str, name: str) -> str:
    return _ARCHIVES.format(cik=cik_num, accn=accn, name=name)


def _fetch_release_text(ticker: str, filing: dict) -> str:
    cik = edgar.resolve_cik(ticker)
    cik_num = str(int(cik))
    accn = filing["accession_number"]
    accn_dl = edgar.dashless(accn)
    index = json.loads(edgar._http_get(
        f"https://www.sec.gov/Archives/edgar/data/{cik_num}/{accn_dl}/index.json"))
    primary = filing.get("primary_document") or ""
    name = _pick_exhibit(index) or primary
    if not name:
        raise edgar.EdgarError(f"no document found for {ticker} 8-K {accn}")
    body = edgar._http_get(_filing_url(cik_num, accn_dl, name))
    return _strip_html(body.decode(errors="replace"))[:60000]


def _call_extract_llm(text: str, filing_date: str) -> dict:
    """Structured LLM extraction of revenue/EPS/guidance from release text."""
    from langchain_openai import ChatOpenAI
    from pydantic import BaseModel, Field

    class Metrics(BaseModel):
        period: str = Field(description="reporting period, e.g. Q2 2026")
        revenue: str = Field(description="reported quarterly revenue, e.g. $4.29 billion")
        eps: str = Field(description="reported quarterly EPS, e.g. $15.50")
        guidance: str = Field(description="forward guidance sentence, or empty if absent")

    llm = ChatOpenAI(
        model=os.environ.get("EDGAR_EXTRACT_MODEL",
                             "deepseek/deepseek-v4-flash-0731"),
        api_key=os.environ.get("OPENROUTER_API_KEY", "missing"),
        base_url=os.environ.get("OPENROUTER_BASE_URL",
                                "https://openrouter.ai/api/v1"),
        max_tokens=int(os.environ.get("EDGAR_EXTRACT_MAX_TOKENS", "4000")),
        temperature=0.0, timeout=int(os.environ.get("EDGAR_EXTRACT_TIMEOUT_S", "300")))
    prompt = (
        f"This is the earnings release of a company filed on {filing_date}.\n"
        "Extract from the RELEASE's own words only: the reporting period, "
        "reported quarterly revenue, reported quarterly EPS, and any explicit "
        "forward guidance. For guidance, quote the SPECIFIC figures if present "
        "(e.g. \"FY26 revenue growth ~10%, GAAP EPS $45-$47\") in one short "
        "sentence; if the release only restates boilerplate without figures, "
        "leave guidance empty. Do not invent numbers.\n\nRelease text:\n"
        + text[:45000])
    out = llm.with_structured_output(Metrics).invoke(prompt)
    return {"period": out.period or "", "revenue": out.revenue or "",
            "eps": out.eps or "", "guidance": out.guidance or ""}


def earnings_line(ticker: str) -> str:
    """One dated earnings-release line; "" when none/failure."""
    try:
        filing = find_latest_earnings_8k(ticker)
        if filing is None:
            return ""
        accn = filing["accession_number"]
        with _lock:
            cached = _cache.get((ticker, accn), "missing")
        if cached == "missing":
            cached = _disk_load(ticker, accn)
        if cached is None:
            text = _fetch_release_text(ticker, filing)
            metrics = _call_extract_llm(text, filing["filing_date"])
            cached = {**metrics, "filed": filing["filing_date"]}
            _disk_store(ticker, accn, cached)
            with _lock:
                _cache[(ticker, accn)] = cached
        if not cached:
            return ""
        parts = [f"8-K earnings release filed {cached['filed']}"]
        if cached.get("period"):
            bits = [f"period {cached['period']}"]
            if cached.get("revenue"):
                bits.append(f"revenue {cached['revenue']}")
            if cached.get("eps"):
                bits.append(f"EPS {cached['eps']}")
            parts.append(": ".join(bits))
        if cached.get("guidance"):
            parts.append(f"Guidance: {cached['guidance']}")
        return "Latest " + "; ".join(parts) + "."
    except Exception as exc:  # noqa: BLE001 - context decoration never breaks runs
        logger.warning("earnings_line failed for %s: %s", ticker, exc)
        return ""


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
