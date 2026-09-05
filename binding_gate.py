#!/usr/bin/env python3
"""binding_gate.py — automated morning gate for PM execution binding.

No human reviews the morning batch before the 09:00 execute; binding must
be fail-closed: it runs ONLY when this gate passes on the morning's real
artifacts. Any doubt — missing/invalid/empty blocks, engine fallbacks,
v1 ratings — fails the day to the known-good legacy path, and the gate
artifact records what happened (and what binding WOULD have done) for the
next human review.

run_execute consults <results_dir>/binding_gate_{date}.json: binding is
effective only when cfg.pm_execution AND the gate verdict is PASS.

Usage (cron, after analyze, before execute):
    python binding_gate.py --date $(date +%F)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pm_execution import EXECUTION_VALID, extract_execution

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"


def gate_path(results_dir: str | Path, date_str: str) -> Path:
    return Path(results_dir) / f"binding_gate_{date_str}.json"


def _load_ratings(results_dir: Path, date_str: str) -> dict | None:
    path = results_dir / f"ratings_{date_str}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def evaluate(cfg: dict, results_dir: str | Path, date_str: str,
             holdings: dict[str, int] | None = None,
             last_close: dict[str, float] | None = None,
             write: bool = True) -> dict:
    """Fail-closed evaluation of the morning's ratings for binding."""
    from decisions import orders_from_execution

    results_dir = Path(results_dir)
    holdings = holdings or {}
    last_close = last_close or {}
    reasons: list[str] = []
    counts = {"valid": 0, "invalid": 0, "absent": 0, "empty_on_buy": 0,
              "engine_fallback": 0}
    preview: list[dict] = []

    payload = _load_ratings(results_dir, date_str)
    if payload is None:
        reasons.append(f"no ratings file for {date_str}")
    else:
        ratings = payload.get("ratings") if isinstance(payload, dict) else {}
        execution = payload.get("execution") or {}
        if payload.get("schema_version", 1) < 2 or not execution:
            reasons.append("no execution blocks in ratings (v1 or absent) — "
                           "binding would be empty")
        for ticker, block in sorted(execution.items()):
            status, intent, reason = extract_execution({"execution": block})
            rating = ratings.get(ticker, "")
            if status != EXECUTION_VALID:
                counts["invalid"] += 1
                reasons.append(f"{ticker}: invalid execution block ({reason})")
                continue
            counts["valid"] += 1
            orders, clamps = orders_from_execution(
                intent, ticker=ticker, holdings=holdings,
                last_close=last_close,
                entry_protection_pct=float(cfg.get("screener", {}).get(
                    "entry_protection_pct", 2.0)),
                stop_loss_pct=float(cfg.get("stop_loss_pct", 8.0)),
                stop_px_band_pct=tuple(cfg.get("stop_px_band_pct",
                                               [3.0, 25.0])),
                min_order_value_usd=float(cfg.get("min_order_value_usd",
                                                  50.0)))
            if orders is None:
                counts["engine_fallback"] += 1
                reasons.append(f"{ticker}: block not honorable by the engine "
                               "(legacy fallback)")
                continue
            preview.append({
                "ticker": ticker,
                "rating": rating,
                "orders": [(o.action, o.shares) for o in orders],
                "clamps": clamps,
            })
            if not orders and rating in ("Buy", "Overweight"):
                # Explicit empty on a buy-rated name = the model ignored the
                # field (silent inaction) — never bind a day that would
                # quietly skip intended buys.
                counts["empty_on_buy"] += 1
                reasons.append(f"{ticker}: empty execution orders on a "
                               f"{rating} rating")
            elif not orders and ticker in holdings:
                counts["valid"] += 0  # explicit no-order on held = deliberate

    verdict = GATE_PASS if not reasons else GATE_FAIL
    result = {
        "date": date_str,
        "verdict": verdict,
        "reasons": reasons,
        "counts": counts,
        "preview": preview,
    }
    if write:
        path = gate_path(results_dir, date_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--results-dir", default=None)
    args = parser.parse_args(argv)

    import datetime as dt
    date_str = args.date or dt.date.today().isoformat()

    from config import load_watchlist_config
    cfg = load_watchlist_config()
    results_dir = args.results_dir or cfg.get("results_dir") or \
        Path.home() / ".tradingagents" / "logs"

    # Holdings + closes for engine evaluation (mirror run_execute's inputs).
    holdings: dict[str, int] = {}
    last_close: dict[str, float] = {}
    from broker import create_broker
    broker = create_broker(cfg)
    try:
        broker.connect()
        holdings, _cash = broker.get_positions_and_cash()
    except Exception as exc:  # noqa: BLE001 — gate fails closed without a book
        result = {"date": date_str, "verdict": GATE_FAIL,
                  "reasons": [f"broker snapshot failed: {exc}"],
                  "counts": {}, "preview": []}
        gate_path(results_dir, date_str).write_text(
            json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 1
    finally:
        broker.disconnect()
    import yfinance as yf  # noqa: F401  (daily_run-style close fetch)

    from daily_run import _last_close
    for ticker in set(holdings) | set((_load_ratings(
            Path(results_dir), date_str) or {}).get("ratings", {})):
        price = _last_close(ticker)
        if price:
            last_close[ticker] = price

    result = evaluate(cfg, results_dir, date_str, holdings, last_close)
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == GATE_PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
