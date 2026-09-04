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


def _short_summary(card: dict, limit: int = 220) -> str:
    """One-line projection of the executive summary for prompt injection."""
    summary = card.get("executive_summary") or card.get("investment_thesis") or ""
    text = " ".join(str(summary).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render_prior_decisions(ticker: str, cards: list[dict]) -> str:
    """Framed, dated card block for the PM prompt (empty when no cards)."""
    if not cards:
        return ""
    lines = [
        f"Prior PM decisions on {ticker} (decided at these dates; they may be "
        "stale — current evidence governs, but if you overturn a prior rating, "
        "say what changed since its date):",
    ]
    for card in reversed(cards):
        rating = card.get("rating", "?")
        summary = _short_summary(card)
        lines.append(f"- [{card.get('date')}] {rating}"
                     + (f" — {summary}" if summary else ""))
    return "\n".join(lines)
