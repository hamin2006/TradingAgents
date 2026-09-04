"""scripts/edgar_diff_qa.py — throwaway QA: EDGAR companyfacts vs Yahoo quote
sanity diff for the fundamentals flip.

No agents, no LLM, no graph — just data: per ticker, compare our EDGAR-derived
metrics against yfinance's independent numbers and print field-by-field deltas.
Fields that disagree beyond tolerance get FLAGGED for investigation before
fundamentals_source: edgar is enabled for the production batch.

Usage:
    python scripts/edgar_diff_qa.py TICKER1 TICKER2 ...
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time

sys.path.insert(0, ".")


def load_watchlist():
    from config import load_watchlist_config
    from tradingagents.dataflows.config import set_config

    cfg = load_watchlist_config()
    set_config(cfg)
    return cfg


TOLERANCE = {
    "revenue_ttm": 0.05,
    "gross_profit_ttm": 0.10,
    "operating_income_ttm": 0.10,
    "net_income_ttm": 0.15,
    "equity": 0.03,
    "assets": 0.03,
    "shares": 0.02,
}


def edgar_metrics(ticker: str, as_of: str) -> dict:
    import edgar
    import fundamentals_edgar as fe

    facts = edgar.load_facts(ticker)
    ttm = lambda tags: facts.ttm(tags, as_of)  # noqa: E731
    out = {
        "revenue_ttm": ttm(fe._REVENUE_TAGS),
        "gross_profit_ttm": ttm(fe._GP_TAGS),
        "operating_income_ttm": ttm(fe._OPINC_TAGS),
        "net_income_ttm": ttm(fe._NI_TAGS),
        "equity": facts.latest_instant(fe._EQUITY_TAGS, as_of),
        "assets": facts.latest_instant(fe._ASSETS_TAGS, as_of),
        "shares": facts.shares_outstanding(as_of),
    }
    return {k: v for k, v in out.items() if v is not None}


def yahoo_metrics(ticker: str) -> dict:
    import yfinance as yf

    info = yf.Ticker(ticker).info or {}
    return {
        "revenue_ttm": info.get("totalRevenue"),
        "gross_profit_ttm": info.get("grossProfits"),
        "operating_income_ttm": info.get("operatingIncome"),
        "net_income_ttm": info.get("netIncomeToCommon"),
        "equity": info.get("totalStockholderEquity"),
        "assets": info.get("totalAssets"),
        "shares": info.get("sharesOutstanding"),
    }


def pct(a, b) -> float:
    return (a - b) / b * 100 if b else float("inf")


def main(tickers: list[str]) -> int:
    from datetime import datetime, timezone

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    load_watchlist()
    flagged = 0
    checked = 0
    for ticker in tickers:
        try:
            ed = edgar_metrics(ticker, as_of)
        except Exception as exc:  # noqa: BLE001
            print(f"{ticker}: EDGAR FAILED ({type(exc).__name__}: {exc})")
            flagged += 1
            continue
        try:
            yh = yahoo_metrics(ticker)
        except Exception as exc:  # noqa: BLE001
            print(f"{ticker}: YAHOO FAILED ({type(exc).__name__}: {exc})")
            flagged += 1
            continue
        row = [f"{ticker}"]
        for field, tol in TOLERANCE.items():
            a, b = ed.get(field), yh.get(field)
            if a is None and b is None:
                row.append(f"{field}: both n/a")
                continue
            if a is None or b is None:
                row.append(f"{field}: EDGAR {a} vs YAHOO {b} (missing side)")
                flagged += 1
                continue
            delta = pct(a, b)
            checked += 1
            mark = "OK " if abs(delta) <= tol * 100 else "FLAG"
            if mark == "FLAG":
                flagged += 1
            row.append(f"{field}: {delta:+.1f}% {mark}")
        print(" | ".join(row))
        time.sleep(0.2)
    print(f"\n{len(tickers)} tickers, {checked} comparable fields, "
          f"{flagged} flags")
    return 1 if flagged else 0


def default_pool() -> list[str]:
    """Most recent ratings pool (any day) plus the current holdings."""
    import glob
    import os


    tickers = set()
    files = sorted(glob.glob(os.path.expanduser(
        "~/.tradingagents/logs/ratings_*.json")))
    if files:
        try:
            d = json.load(open(files[-1]))
            tickers |= set(d.get("ratings", {}).keys())
        except (OSError, ValueError):
            pass
    tickers |= {"EL", "REGN"}
    return sorted(tickers)


if __name__ == "__main__":
    tickers = [t.upper() for t in sys.argv[1:]] or default_pool()
    raise SystemExit(main(tickers))
