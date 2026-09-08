#!/usr/bin/env python3
"""backfill_cards.py — reconstruct dated decision cards from pre-observe
artifacts (ratings files + per-ticker structured logs).

The decision-card store went live 2026-09-04; held tickers analyzed before
that have real PM decisions but no cards. A card is the flip baseline for
the next PM analysis (without one, a first-day rating change is invisible),
so backfill held tickers from the recent ratings files + the verbatim PM
structured-output payloads captured per day.

Honesty rules: the card rating comes from the RATINGS FILE (what actually
drove the engine, post-guard), prose is copied verbatim from the same date's
structured log when present, and ``execution`` is always None — pre-observe
decisions never carried an execution block, and one is never fabricated.

Usage:
    python backfill_cards.py --logs-dir ~/.tradingagents/logs \
        --tickers DASH,DXCM,HPE,MSFT,NOW,REGN --days 4 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from decision_cards import (
    CARD_SCHEMA_VERSION,
    append_card,
    append_outcome,
    latest_card,
    load_cards,
    load_outcomes,
)
from pm_execution import EXECUTION_VALID, extract_execution

PM_AGENT = "Portfolio Manager"


def parse_pm_payload(jsonl_path: Path) -> dict | None:
    """Args of the LAST Portfolio Manager structured-output tool call."""
    if not jsonl_path.exists():
        return None
    payload = None
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("agent") != PM_AGENT:
                    continue
                calls = event.get("tool_calls") or []
                for call in calls:
                    if isinstance(call, dict) and isinstance(
                            call.get("args"), dict):
                        payload = call["args"]
    except OSError:
        return None
    return payload


def load_ratings_for_date(logs_root: Path, date_str: str) -> dict:
    """{ticker: rating} from ratings_{date}.json (missing/invalid -> {})."""
    path = logs_root / f"ratings_{date_str}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    ratings = payload.get("ratings") if isinstance(payload, dict) else None
    return dict(ratings) if isinstance(ratings, dict) else {}


def load_executed_for_ticker(logs_root: Path, date_str: str,
                             ticker: str) -> list[dict]:
    """Orders the engine actually submitted that day (executed_{date}.json).

    These are the machine-recorded actuals for the pre-binding era: the PM's
    intent (card prose) and the engine's action (this) diverged by design,
    and the divergence must stay visible to future PMs — never be edited
    into the PM's own words.
    """
    path = logs_root / f"executed_{date_str}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list):
        return []
    return [o for o in orders
            if isinstance(o, dict) and o.get("ticker") == ticker]


def build_cards(tickers: list[str], logs_root: Path, days_back: int = 21,
                as_of: str | None = None) -> list[dict]:
    """Cards (oldest first) reconstructed per ticker over the window."""
    as_of = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    cards: list[dict] = []
    for ticker in tickers:
        for offset in range(days_back, -1, -1):
            date_str = (as_of - dt.timedelta(days=offset)).isoformat()
            rating = load_ratings_for_date(logs_root, date_str).get(ticker)
            if not rating:
                continue
            payload = parse_pm_payload(logs_root / "structured" / date_str
                                       / f"{ticker}.jsonl")
            executed = load_executed_for_ticker(logs_root, date_str, ticker)
            actual = None
            if executed:
                actual = {
                    "orders": executed,
                    "note": "pre-binding: executed by the legacy deterministic "
                            "engine; PM intent was not bound",
                }
            card = {
                "date": date_str,
                "ticker": ticker,
                "rating": rating,
                "ref_close": None,
                "schema_version": CARD_SCHEMA_VERSION,
                "executive_summary": (payload or {}).get("executive_summary"),
                "investment_thesis": (payload or {}).get("investment_thesis"),
                "execution": None,
                "actual": actual,
            }
            cards.append(card)
    return cards


def backfill(tickers: list[str], logs_root: Path, cards_root: Path,
             days_back: int = 21, as_of: str | None = None,
             dry_run: bool = False) -> tuple[int, int]:
    """Write missing (ticker, date) cards and upgrade stale ones (a card that
    predates the ``actual`` field gains its machine-recorded execution).
    Returns (written, skipped)."""
    written = skipped = 0
    for card in build_cards(tickers, logs_root, days_back, as_of):
        existing = latest_card(cards_root, card["ticker"])
        if existing and existing.get("date") == card["date"]:
            stale = existing.get("actual") is None and card.get("actual")
            if not stale:
                skipped += 1
                continue
            if dry_run:
                print("would upgrade:", card["date"], card["ticker"],
                      "with actuals")
                written += 1
                continue
            _replace_card(cards_root, card["ticker"], existing["date"], card)
            written += 1
            continue
        if dry_run:
            print("would write:", card["date"], card["ticker"],
                  card["rating"])
            written += 1
            continue
        append_card(cards_root, card)
        written += 1
    return written, skipped


def load_ratings_payload(logs_root: Path, date_str: str) -> dict:
    """Full ratings payload (ratings + v2 execution map) or {}."""
    path = logs_root / f"ratings_{date_str}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_gate_for_date(logs_root: Path, date_str: str) -> dict | None:
    """binding_gate_{date}.json (verdict/reasons) — absent before 2026-09-08."""
    path = logs_root / f"binding_gate_{date_str}.json"
    if not path.exists():
        return None
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return gate if isinstance(gate, dict) else None


def load_reports_for_ticker(logs_root: Path, date_str: str,
                            ticker: str) -> list[dict]:
    """Fill reports for a ticker from executed_{date}.json (orders and
    reports are index-aligned by construction)."""
    path = logs_root / f"executed_{date_str}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    orders = payload.get("orders") or []
    reports = payload.get("reports") or []
    out = []
    for o, r in zip(orders, reports, strict=False):
        if not (isinstance(o, dict) and o.get("ticker") == ticker):
            continue
        r = r if isinstance(r, dict) else {}
        out.append({"action": o.get("action"), "shares": o.get("shares"),
                    "filled": r.get("filled"), "avg_price": r.get("avg_price")})
    return out


def load_stop_for_ticker(logs_root: Path, date_str: str,
                         ticker: str) -> float | None:
    """First recorded stop level on the day's executed orders for a ticker
    (the protection the run set — sell anchor or buy attach)."""
    path = logs_root / f"executed_{date_str}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list):
        return None
    for o in orders:
        if isinstance(o, dict) and o.get("ticker") == ticker:
            stop = o.get("stop_price")
            if isinstance(stop, (int, float)):
                return float(stop)
    return None


def build_outcomes(tickers: list[str], logs_root: Path, cards_root: Path,
                   days_back: int = 21, as_of: str | None = None,
                   notes: dict | None = None) -> list[dict]:
    """execution_outcome events for (ticker, date) pairs that HAVE a card
    dated that day (post-observe days — pre-observe truth lives in the
    card's ``actual`` field and is never duplicated here). Derived only
    from artifacts: ratings payload (intent), executed log (fills), gate
    artifact (why binding was on/off). Idempotent per (ticker, date)."""
    as_of_d = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    outcomes: list[dict] = []
    for ticker in tickers:
        card_dates = {c.get("date") for c in load_cards(cards_root, ticker)}
        done_dates = {o.get("date") for o in load_outcomes(cards_root, ticker)}
        for offset in range(days_back, -1, -1):
            date_str = (as_of_d - dt.timedelta(days=offset)).isoformat()
            if date_str not in card_dates or date_str in done_dates:
                continue
            payload = load_ratings_payload(logs_root, date_str)
            if not payload.get("ratings", {}).get(ticker):
                continue
            gate = load_gate_for_date(logs_root, date_str)
            block = (payload.get("execution") or {}).get(ticker)
            pm_orders = None
            if block is not None:
                status, intent, _reason = extract_execution(
                    {"execution": block})
                pm_orders = ([[o.kind, o.shares] for o in intent.orders]
                             if status == EXECUTION_VALID and intent else None)
            outcomes.append({
                "type": "execution_outcome",
                "schema_version": 1,
                "date": date_str,
                "ticker": ticker,
                "binding_active": bool(gate)
                and gate.get("verdict") == "PASS",
                "gate_verdict": (gate or {}).get("verdict"),
                "gate_reasons": (gate or {}).get("reasons") or [],
                "pm_orders": pm_orders,
                "actual": load_reports_for_ticker(logs_root, date_str, ticker),
                "remaining": None,  # historical holdings not in artifacts
                "stop_anchored": load_stop_for_ticker(logs_root, date_str,
                                                      ticker),
                "note": (notes or {}).get(ticker),
            })
    return outcomes


def backfill_outcomes(tickers: list[str], logs_root: Path, cards_root: Path,
                      days_back: int = 21, as_of: str | None = None,
                      notes: dict | None = None,
                      dry_run: bool = False) -> int:
    """Append missing outcome events; idempotent. Returns written count."""
    written = 0
    for outcome in build_outcomes(tickers, logs_root, cards_root, days_back,
                                  as_of, notes):
        if dry_run:
            print("would write outcome:", outcome["date"], outcome["ticker"])
            written += 1
            continue
        if append_outcome(cards_root, outcome) is not None:
            written += 1
    return written


def _replace_card(cards_root: Path, ticker: str, date_str: str,
                  new_card: dict) -> None:
    """Rewrite one dated card in place (the append-only store is upgraded by
    this tool only: backfilled artifacts gain machine actuals once, in order,
    never PM-attributed content)."""
    from decision_cards import cards_file, load_cards

    path = cards_file(cards_root, ticker)
    kept = [c for c in load_cards(cards_root, ticker)
            if c.get("date") != date_str]
    kept.append(new_card)
    kept.sort(key=lambda c: c.get("date", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for card in kept:
            f.write(json.dumps(card, sort_keys=True) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, default=Path.home()
                        / ".tradingagents" / "logs")
    parser.add_argument("--cards-dir", type=Path, default=None,
                        help="decision_cards root (default: <logs-dir>)")
    parser.add_argument("--tickers", required=True,
                        help="comma-separated tickers to backfill")
    parser.add_argument("--days", type=int, default=21,
                        help="lookback window in days")
    parser.add_argument("--as-of", default=None,
                        help="anchor date YYYY-MM-DD (default: today)")
    parser.add_argument("--outcomes", action="store_true",
                        help="append execution_outcome events for days that "
                        "already have cards (post-observe days) instead of "
                        "backfilling cards")
    parser.add_argument("--note", action="append", default=[],
                        help="manual annotation for the outcome, formatted "
                        "'TICKER: text' (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    cards_root = args.cards_dir or args.logs_dir
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    notes = {}
    for raw in args.note:
        ticker, sep, text = raw.partition(":")
        if sep and ticker.strip() and text.strip():
            notes[ticker.strip().upper()] = " ".join(text.split())
    if args.outcomes:
        written = backfill_outcomes(tickers, args.logs_dir, cards_root,
                                    days_back=args.days, as_of=args.as_of,
                                    notes=notes, dry_run=args.dry_run)
        print(f"outcomes written: {written}")
        return 0
    written, skipped = backfill(tickers, args.logs_dir, cards_root,
                                days_back=args.days, as_of=args.as_of,
                                dry_run=args.dry_run)
    print(f"cards written: {written}, already present: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
