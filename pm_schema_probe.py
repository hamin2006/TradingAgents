#!/usr/bin/env python3
"""pm_schema_probe.py — replay a captured PM prompt against the live model
with the execution-bearing schema bound, and report whether the model emits
a well-formed execution block.

This is the ONLY unmeasured variable before the binding flip: the 09-04
payloads predate the extended schema, so nobody has watched the model
produce an `execution` block yet. The probe rebuilds the exact prompt the
PM saw (from the structured-log llm_start dump, role labels stripped) plus
the decision-card block tomorrow's injection would add, binds the
ExecutionPortfolioDecision schema exactly as the framework would, invokes
the SAME model/config as the morning pipeline, and reports parse status +
the resulting orders. Read-only: one (or a few) model calls, no broker.

Usage (run on the PC, prod config):
    python pm_schema_probe.py --ticker HPE --date 2026-09-04
    python pm_schema_probe.py --ticker HPE,EL --date 2026-09-04
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from backfill_cards import parse_pm_payload  # noqa: F401  (shared seams doc)

_ROLE_PREFIX = re.compile(r"^\[[a-zA-Z_]+\] ")


def extract_pm_prompt(jsonl_path: Path) -> str | None:
    """Last Portfolio Manager llm_start prompt, role labels stripped."""
    if not jsonl_path.exists():
        return None
    dump = None
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (event.get("agent") == "Portfolio Manager"
                        and event.get("type") == "llm_start"
                        and event.get("prompt")):
                    dump = event["prompt"]
    except OSError:
        return None
    if dump is None:
        return None
    lines = []
    for raw in str(dump).splitlines():
        stripped = _ROLE_PREFIX.sub("", raw)
        if stripped != raw:
            lines.append(stripped)
        elif not lines:
            lines.append(raw)  # preamble without a role label
        else:
            lines.append(raw)
    return "\n".join(lines).strip()


def append_cards_block(prompt: str, ticker: str, cards_root: Path,
                       as_of: str | None = None) -> str:
    """Append the dated-card block exactly as tomorrow's injection would."""
    import decision_cards

    try:
        fresh = decision_cards.fresh_cards(cards_root, ticker,
                                           max_age_days=21, as_of=as_of)
        picked = decision_cards.select_cards_for_injection(fresh, flip_max=3)
        block = decision_cards.render_prior_decisions(ticker, picked)
    except Exception:  # noqa: BLE001 — a card problem must never block a probe
        return prompt
    if not block:
        return prompt
    return f"{prompt}\n\n{block}"


def probe_ticker(ticker: str, date_str: str, logs_dir: Path,
                 cards_root: Path | None, llm: object) -> dict:
    """Replay one PM call with the execution schema bound; report compliance."""
    from pm_execution import ExecutionPortfolioDecision, extract_execution
    from tradingagents.agents.utils.structured import bind_structured

    path = logs_dir / "structured" / date_str / f"{ticker}.jsonl"
    prompt = extract_pm_prompt(path)
    if prompt is None:
        return {"ticker": ticker, "error": "no captured PM prompt in logs"}
    if cards_root is not None:
        prompt = append_cards_block(prompt, ticker, cards_root)
    structured_llm = bind_structured(llm, ExecutionPortfolioDecision,
                                     "Portfolio Manager")
    result = None
    failure = None
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
        except Exception as exc:  # noqa: BLE001 — report, don't crash
            failure = f"structured invocation failed: {exc}"
    if result is None and failure is None:
        failure = "structured invocation returned no parsed result"
    if failure is not None:
        return {"ticker": ticker, "error": failure,
                "verdict": "free-text fallback (schema rejection!)"}
    dumped = result.model_dump(mode="json")
    status, intent, reason = extract_execution(dumped)
    from pm_execution import EXECUTION_VALID
    out = {
        "ticker": ticker,
        "rating": getattr(result.rating, "value", result.rating),
        "execution_status": status,
        "reason": reason,
        "verdict": "VALID" if status == EXECUTION_VALID else status,
    }
    if intent is not None:
        out["orders"] = [o.model_dump(exclude_none=True) for o in intent.orders]
        out["future_notes"] = intent.future_notes
        out["invalidation_px"] = intent.invalidation_px
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True, help="comma-separated")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--logs-dir", type=Path, default=Path.home()
                        / ".tradingagents" / "logs")
    parser.add_argument("--no-cards", action="store_true",
                        help="skip the decision-card injection block")
    args = parser.parse_args(argv)

    from config import load_watchlist_config
    from tradingagents.dataflows.config import set_config

    cfg = load_watchlist_config()
    set_config(cfg)

    import daily_run
    daily_run._ensure_openrouter_pins(cfg.get("openrouter_provider_pins"))

    import tradingagents.graph.trading_graph as tg
    graph = tg.TradingAgentsGraph(config=cfg)
    llm = graph.deep_thinking_llm
    cards_root = None if args.no_cards else args.logs_dir
    for ticker in [t.strip().upper() for t in args.ticker.split(",")
                   if t.strip()]:
        print(json.dumps(probe_ticker(ticker, args.date, args.logs_dir,
                                      cards_root, llm), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
