"""decision_cards.py — dated PM decision cards: store + deterministic injection.

One card per ticker per analysis day, appended to
``<results_dir>/decision_cards/{TICKER}.jsonl``. The latest card is the last
line; the full history is retained for flip analytics. Cards carry the PM's
full decision (rating, summaries, execution block incl. future intents) and
are injected into future PM prompts (PM-only by construction) so a future
PM must confirm-or-refute the standing thesis with dates visible.

The card store is intent, not outcome: the memory log stays the outcome
archive (pending -> resolved with realized returns). Both reach the PM:
lessons via the framework's past_context, cards via this module's render
appended by the daily_run installer.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

CARD_SCHEMA_VERSION = 1

_SAFE_TICKER = re.compile(r"[^A-Za-z0-9.\-]")
_DATE_FMT = "%Y-%m-%d"


def _sanitize_ticker(ticker: str) -> str:
    """Path-safe ticker component: keep letters/digits/single interior dots."""
    safe = _SAFE_TICKER.sub("", str(ticker)).upper()
    while ".." in safe:
        safe = safe.replace("..", ".")
    return safe.lstrip(".").lstrip("-") or "UNKNOWN"


def cards_file(root: str | Path, ticker: str) -> Path:
    """Per-ticker JSONL path under ``root/decision_cards`` (path-safe)."""
    return Path(root) / "decision_cards" / f"{_sanitize_ticker(ticker)}.jsonl"


def append_card(root: str | Path, card: dict) -> Path:
    """Append one card (JSON line). Creates the directory on first write."""
    path = cards_file(root, card.get("ticker", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(card, sort_keys=True) + "\n")
    return path


def load_cards(root: str | Path, ticker: str) -> list[dict]:
    """All cards for a ticker, oldest first. Malformed lines are skipped —
    a corrupt card never blocks analysis (skip + count)."""
    path = cards_file(root, ticker)
    if not path.exists():
        return []
    cards = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(card, dict) and card.get("date") and card.get("ticker"):
                    cards.append(card)
    except OSError:
        return []
    return cards


def latest_card(root: str | Path, ticker: str) -> dict | None:
    """The most recent card for a ticker, or None."""
    cards = load_cards(root, ticker)
    return cards[-1] if cards else None


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], _DATE_FMT).date()


def fresh_cards(root: str | Path, ticker: str, max_age_days: int,
                as_of: str | date | None = None) -> list[dict]:
    """Cards within ``max_age_days`` of ``as_of`` (default: today), oldest first."""
    as_of = _parse_date(as_of or date.today())
    cutoff = as_of - timedelta(days=max_age_days)
    return [c for c in load_cards(root, ticker)
            if cutoff <= _parse_date(c["date"]) <= as_of]


def select_cards_for_injection(fresh: list[dict], flip_max: int) -> list[dict]:
    """Injection sizing over a date-ascending fresh-card list.

    Stable (the two latest cards share a rating) -> latest card only.
    Flip (they differ) -> the last ``flip_max`` cards so the PM sees the
    arc and must justify the latest rating against what it overturned.
    """
    if not fresh:
        return []
    if len(fresh) == 1:
        return fresh
    if fresh[-1]["rating"] == fresh[-2]["rating"]:
        return [fresh[-1]]
    return fresh[-max(1, int(flip_max)):]


def _summary_text(card: dict) -> str:
    """The PM's executive summary, verbatim (bounded by schema design)."""
    summary = card.get("executive_summary")
    return " ".join(str(summary).split()) if summary else ""


def _order_line(order: dict) -> str:
    """Deterministic one-line projection of a PmOrder for the prompt."""
    kind = order.get("kind", "?")
    value = order.get("value_usd")
    shares = order.get("shares")
    fraction = order.get("fraction_held")
    if value is not None:
        size = f"${value:g}"
    elif shares is not None:
        size = f"{shares} shares"
    elif fraction is not None:
        size = f"{fraction * 100:g}% of held"
    else:
        size = "?"
    parts = [f"{kind} {size}"]
    limit = order.get("limit_px")
    if limit is not None:
        cmp_ = "<=" if kind == "BUY" else ">="
        parts.append(f"@{cmp_} ${limit:.2f}")
    stop = order.get("stop_px")
    if stop is not None:
        parts.append(f"stop ${stop:.2f}")
    cap = order.get("cap_value_usd")
    if cap is not None:
        parts.append(f"cap ${cap:g}")
    return ", ".join(parts)


def _execution_lines(card: dict) -> list[str]:
    """Prompt lines from the execution block (orders + advisory fields)."""
    lines = []
    execution = card.get("execution")
    if not isinstance(execution, dict):
        return lines
    orders = execution.get("orders") or []
    for order in orders:
        if isinstance(order, dict) and order.get("kind"):
            lines.append(f"orders: {_order_line(order)}")
    invalidation = execution.get("invalidation_px")
    if invalidation is not None:
        lines.append(f"invalid: ${invalidation:g} (advisory, never executed)")
    future = execution.get("future_notes")
    if future:
        lines.append(f"future: {' '.join(str(future).split())}")
    return lines


def _actual_lines(card: dict) -> list[str]:
    """Prompt lines from the machine-recorded actual execution (engine)."""
    lines = []
    actual = card.get("actual")
    if not isinstance(actual, dict):
        return lines
    orders = actual.get("orders") or []
    if not orders:
        return lines
    parts = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        text = f"{o.get('action', '?')} {o.get('shares', '?')} shares"
        stop = o.get("stop_price")
        if isinstance(stop, (int, float)):
            text += f", stop ${stop:.2f}"
        parts.append(text)
    if not parts:
        return lines
    note = actual.get("note")
    label = f"actual (engine, {note})" if note else "actual (engine)"
    lines.append(f"{label}: {'; '.join(parts)}")
    return lines


def render_prior_decisions(ticker: str, cards: list[dict]) -> str:
    """Framed, dated card block for the PM prompt (empty when no cards).

    Renders the full executive summary (bounded by schema design), the
    deterministic execution-block projection when present, and the
    machine-recorded actual execution when present. Prior prose is dated +
    framed as overridable: the anti-anchor is attribution, not truncation.
    """
    if not cards:
        return ""
    lines = [
        f"Prior PM decisions on {ticker} (decided at these dates; they may be "
        "stale — current evidence governs, but if you overturn a prior rating, "
        "say what changed since its date):",
    ]
    for card in reversed(cards):
        rating = card.get("rating", "?")
        summary = _summary_text(card)
        entry = f"- [{card.get('date')}] {rating}"
        if summary:
            entry += f" — {summary}"
        lines.append(entry)
        lines.extend("    " + line for line in _execution_lines(card))
        lines.extend("    " + line for line in _actual_lines(card))
    return "\n".join(lines)
