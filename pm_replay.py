#!/usr/bin/env python3
"""pm_replay.py — replay a pre-binding day's real PM payloads through the
binding engine and diff against what the legacy engine actually executed.

The observe phase writes cards + events but leaves the engine legacy; this
tool answers "what WOULD binding have done that morning?" against the real
artifacts (structured PM payloads, ratings file, executed log), so the
binding flip lands with its behavior already measured on real model output.

Derivation rules (never fabricate): holdings-before = tickers the legacy
log SOLD that day; reference closes are derived from the legacy BUY stop
(stop = close x (1 - stop_loss_pct/100)) — exact to the cent for the day's
actual entries. Values from the CURRENT watchlist config (entry protection,
stop band, minimums) apply — they are what a live flip would use.

Usage:
    python pm_replay.py --logs-dir ~/.tradingagents/logs --date 2026-09-04
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backfill_cards import parse_pm_payload
from decisions import orders_from_execution
from pm_execution import EXECUTION_VALID, extract_execution

PM_AGENT = "Portfolio Manager"


def load_legacy_orders(logs_root: Path, date_str: str) -> dict[str, list[dict]]:
    """{ticker: [legacy Order dicts]} from executed_{date}.json."""
    path = logs_root / f"executed_{date_str}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list):
        return {}
    by_ticker: dict[str, list[dict]] = {}
    for o in orders:
        if isinstance(o, dict) and o.get("ticker"):
            by_ticker.setdefault(o["ticker"], []).append(o)
    return by_ticker


def derive_closes(logs_root: Path, date_str: str,
                  stop_loss_pct: float = 8.0) -> dict[str, float]:
    """Reference closes derived from legacy BUY stops (stop = close x (1-pct))."""
    closes: dict[str, float] = {}
    for ticker, orders in load_legacy_orders(logs_root, date_str).items():
        for o in orders:
            if o.get("action") == "BUY" and o.get("stop_price"):
                closes[ticker] = round(
                    float(o["stop_price"]) / (1 - stop_loss_pct / 100), 4)
                break
    return closes


def infer_holdings(logs_root: Path, date_str: str) -> dict[str, int]:
    """Holdings before the day's execution: tickers the legacy log SOLD."""
    holdings: dict[str, int] = {}
    for ticker, orders in load_legacy_orders(logs_root, date_str).items():
        for o in orders:
            if o.get("action") == "SELL":
                holdings[ticker] = int(o.get("shares", 0))
                break
    return holdings


def replay_day(logs_root: Path, date_str: str,
               counts: dict | None = None) -> list[dict]:
    """Replay every ticker with a structured PM payload that day."""
    rows: list[dict] = []
    structured_dir = logs_root / "structured" / date_str
    if not structured_dir.exists():
        return rows
    holdings = infer_holdings(logs_root, date_str)
    closes = derive_closes(logs_root, date_str)
    legacy = load_legacy_orders(logs_root, date_str)
    for path in sorted(structured_dir.glob("*.jsonl")):
        ticker = path.stem
        payload = parse_pm_payload(path)
        if payload is None:
            continue
        status, intent, reason = extract_execution(payload)
        row = {
            "ticker": ticker,
            "status": status,
            "reason": reason,
            "orders": [],
            "legacy": [(o["action"], int(o["shares"]))
                       for o in legacy.get(ticker, [])],
        }
        if counts is not None:
            counts[status] = counts.get(status, 0) + 1
        if status == EXECUTION_VALID:
            orders, clamps = orders_from_execution(
                intent, ticker=ticker, holdings=holdings,
                last_close={ticker: closes.get(ticker, 0.0)})
            if orders is not None:
                row["orders"] = [(o.action, o.shares) for o in orders]
            row["clamps"] = clamps
        rows.append(row)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, default=Path.home()
                        / ".tradingagents" / "logs")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--stop-loss-pct", type=float, default=8.0)
    args = parser.parse_args(argv)
    counts: dict = {}
    rows = replay_day(args.logs_dir, args.date, counts=counts)
    print(f"day {args.date}: {len(rows)} PM payload(s), "
          f"status counts: {counts}")
    for row in rows:
        print(f"- {row['ticker']}: {row['status']}"
              + (f" ({row['reason']})" if row.get("reason") else "")
              + f" | binding orders: {row['orders'] or 'legacy fallback'}"
              + f" | legacy actual: {row['legacy'] or 'none'}")
        for clamp in row.get("clamps", []):
            print(f"    clamp: {clamp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
