"""corp_events.py — corporate-events block (Form 4 + 8-K) for agent context.

The framework's own insider tool (get_insider_transactions) is bound but never
invoked and its yfinance vendor lags filings by days (verified live
2026-09-03: Guarini's $340K sale Form 4 was filed that morning and absent from
yfinance hours later). This module watches EDGAR submissions directly and
renders dated, attributable events the analysts can weigh:

    2026-09-03 Form 4: Guarini Kathryn (Director) EXERCISED 400 options
    @ $719.37 then SOLD 400 @ $850.00 ($340,000).
    2026-09-02 8-K filed (earnings release).

Form-4 XML parsing is deterministic (issuer/owner/relationship/transaction
tags); cashless exercise→sale pairs collapse into one line. Failure-safe:
any fetch/parse error degrades to fewer lines or an empty block — context
decoration must never break an analysis.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import timedelta

import edgar

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 10
_MAX_FORM4 = 3
_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/edgardoc.xml"

_block_cache: dict[tuple[str, str], str] = {}
_lock = threading.Lock()


def reset_cache() -> None:
    with _lock:
        _block_cache.clear()


def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


def _fmt_price(value: float) -> str:
    return f"${value:,.2f}"


def parse_form4(xml: str) -> list[dict]:
    """Deterministic parse of a Form 4 (edgardoc.xml) into trade dicts."""
    if "<ownershipDocument>" not in xml:
        raise edgar.EdgarError("not a Form 4 document")
    owner = _tag(xml, "rptOwnerName")
    is_director = bool(re.search(r"<isDirector>\s*1\s*</isDirector>", xml))
    title = _tag(xml, "rptOwnerTitle")
    trades: list[dict] = []
    blocks = re.split(r"<nonDerivativeTransaction>|<derivativeTransaction>", xml)
    for block in blocks[1:]:
        code = _tag(block, "transactionCode")
        if not code:
            continue
        shares = _int_tag(block, "transactionShares")
        price = _float_tag(block, "transactionPricePerShare")
        date = _tag(block, "transactionDate")
        if not shares or not date:
            continue
        trades.append({
            "owner": owner or "Unknown",
            "role": "Director" if is_director else (title or "Insider"),
            "code": code,
            "shares": shares,
            "price": price,
            "date": date,
        })
    if not trades:
        raise edgar.EdgarError("Form 4 contained no transactions")
    return trades


def _tag(xml: str, name: str) -> str:
    m = re.search(rf"<{name}>\s*<value>(.*?)</value>\s*</{name}>", xml, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _int_tag(xml: str, name: str) -> int | None:
    val = _tag(xml, name)
    try:
        return int(float(val)) if val else None
    except ValueError:
        return None


def _float_tag(xml: str, name: str) -> float | None:
    val = _tag(xml, name)
    try:
        return float(val) if val else None
    except ValueError:
        return None


def _describe(trade: dict) -> str:
    shares, price = trade["shares"], trade["price"]
    value = f" ({_fmt_money(shares * price)})" if price else ""
    if trade["code"] == "S":
        return f"SOLD {shares} @ {_fmt_price(price)}{value}"
    if trade["code"] == "M":
        return f"EXERCISED {shares} options @ {_fmt_price(price)}"
    if trade["code"] == "P":
        return f"BOUGHT {shares} @ {_fmt_price(price)}{value}"
    return f"{trade['code']} {shares} @ {_fmt_price(price)}"


def _render_trades(trades: list[dict]) -> str:
    """Render trades grouped by (owner, date); collapse cashless exercises."""
    lines = []
    by_key: dict[tuple, list[dict]] = {}
    for trade in trades:
        by_key.setdefault((trade["owner"], trade["date"]), []).append(trade)
    for (owner, date), group in by_key.items():
        role = group[0]["role"]
        sales = [t for t in group if t["code"] == "S"]
        exercises = [t for t in group if t["code"] == "M"]
        combined = ""
        if sales and exercises and sales[0]["shares"] == exercises[0]["shares"]:
            combined = (f"{_describe(exercises[0])} then {_describe(sales[0])}")
        else:
            combined = "; ".join(_describe(t) for t in group)
        lines.append(f"Form 4: {owner} ({role}) {combined} on {date}.")
    return "\n".join(lines)


def _form4_url(cik_num: str, accn: str) -> str:
    return _ARCHIVES.format(cik=cik_num, accn=edgar.dashless(accn))


def events_block(ticker: str, since: str | None = None) -> str:
    """Dated corporate-events block for ``ticker`` ("" when none/on error).

    Memoized per (ticker, since) — the context seam calls it once per agent
    (12 agents per ticker), and the underlying data is daily.
    """
    if since is None:
        import datetime as _dt
        since = (_dt.datetime.now(_dt.timezone.utc) - timedelta(
            days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    key = (ticker.strip().upper(), since)
    with _lock:
        if key in _block_cache:
            return _block_cache[key]
    try:
        cik = edgar.resolve_cik(ticker)
        cik_num = str(int(cik))  # Archives paths use unpadded CIK
        subs = edgar.load_submissions(ticker)
        recent = [f for f in subs.recent(since)
                  if f["form"] in ("4", "8-K")][:5]
        if not recent:
            result = ""
        else:
            lines: list[str] = []
            form4_count = 0
            for filing in recent:
                if filing["form"] == "4" and form4_count < _MAX_FORM4:
                    form4_count += 1
                    try:
                        xml = edgar._http_get(_form4_url(
                            cik_num, filing["accession_number"]))
                        rendered = _render_trades(parse_form4(xml.decode()))
                        for line in rendered.splitlines():
                            lines.append(f"{filing['filing_date']} {line}")
                    except Exception:  # noqa: BLE001 - skip one bad filing
                        continue
                elif filing["form"] == "8-K":
                    lines.append(f"8-K filed {filing['filing_date']} "
                                 f"(accession {filing['accession_number']}).")
            result = (f"Corporate events ({ticker}):\n- " + "\n- ".join(lines)
                      ) if lines else ""
        with _lock:
            _block_cache[key] = result
        return result
    except Exception as exc:  # noqa: BLE001 - context decoration never breaks runs
        logger.warning("corporate events failed for %s: %s", ticker, exc)
        with _lock:
            _block_cache[key] = ""
        return ""
