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
    from decisions import orders_from_execution, protection_from_execution

    results_dir = Path(results_dir)
    holdings = holdings or {}
    last_close = last_close or {}
    reasons: list[str] = []
    counts = {"valid": 0, "invalid": 0, "absent": 0, "empty_on_buy": 0,
              "engine_fallback": 0, "protection_invalid": 0}
    preview: list[dict] = []
    per_ticker: dict[str, dict] = {}  # NEW: per-ticker gate status

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
                per_ticker[ticker] = {"status": "legacy", "reason": reason}
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
                fallback_reason = "block not honorable by the engine"
                reasons.append(f"{ticker}: {fallback_reason}")
                per_ticker[ticker] = {"status": "legacy", "reason": fallback_reason}
                continue
            protection, protection_reasons = protection_from_execution(
                intent, ticker=ticker, holdings=holdings,
                last_close=last_close,
                stop_loss_pct=float(cfg.get("stop_loss_pct", 8.0)),
                stop_px_band_pct=tuple(cfg.get("stop_px_band_pct",
                                               [3.0, 25.0])))
            protection_fields = {
                "take_profit_px": intent.take_profit_px,
                "oco_stop_px": protection.stop_px if protection else None,
            }
            if protection_reasons:
                counts["protection_invalid"] += 1
                protection_reason = "; ".join(protection_reasons)
                reasons.append(protection_reason)
                per_ticker[ticker] = {"status": "legacy",
                                      "reason": protection_reason,
                                      **protection_fields}
                continue
            preview.append({
                "ticker": ticker,
                "rating": rating,
                "orders": [(o.action, o.shares) for o in orders],
                "clamps": clamps,
                **protection_fields,
            })
            if not orders and rating in ("Buy", "Overweight") \
                    and ticker not in holdings:
                # Explicit empty on a buy-rated name we DON'T hold = the
                # model ignored the field (silent inaction) — never bind a
                # day that would quietly skip intended buys. Empty on a
                # HELD buy-rated name is a legitimate maintain (E2E 09-05:
                # HPE OW with 13 shares, "no additions at $52.00").
                counts["empty_on_buy"] += 1
                empty_reason = "empty execution orders on a " \
                    f"{rating} rating with no position held"
                reasons.append(f"{ticker}: {empty_reason}")
                per_ticker[ticker] = {"status": "legacy", "reason": empty_reason,
                                      **protection_fields}
            elif not orders and ticker in holdings:
                counts["valid"] += 0  # explicit no-order on held = deliberate
                per_ticker[ticker] = {"status": "bind", "reason": None,
                                      **protection_fields}
            else:
                per_ticker[ticker] = {"status": "bind", "reason": None,
                                      **protection_fields}

    verdict = GATE_PASS if not reasons else GATE_FAIL
    result = {
        "date": date_str,
        "verdict": verdict,
        "reasons": reasons,
        "counts": counts,
        "preview": preview,
        "per_ticker": per_ticker,  # NEW: per-ticker binding verdicts
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
    result = run(cfg, date_str, args.results_dir)
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == GATE_PASS else 1


def run(cfg: dict, date_str: str | None = None,
        results_dir: str | Path | None = None) -> dict:
    """Snapshot the broker and evaluate the gate, writing the artifact.

    This is the entrypoint both the CLI and the chained post-analyze call
    use (daily_run.run_analyze). A broker failure is not raised: it writes a
    fail-closed FAIL artifact and returns it, so the caller's day always has
    an explicit gate result (and execute can fall back to legacy).
    """
    import datetime as dt

    date_str = date_str or dt.date.today().isoformat()
    results_dir = Path(results_dir or cfg.get("results_dir") or
                       Path.home() / ".tradingagents" / "logs")

    # Holdings + closes for engine evaluation (mirror run_execute's inputs).
    holdings: dict[str, int] = {}
    last_close: dict[str, float] = {}
    broker = None
    try:
        from broker import create_broker
        broker = create_broker(cfg)
        broker.connect()
        holdings, _cash = broker.get_positions_and_cash()
    except Exception as exc:  # noqa: BLE001 — gate fails closed without a book
        result = {"date": date_str, "verdict": GATE_FAIL,
                  "reasons": [f"broker snapshot failed: {exc}"],
                  "counts": {}, "preview": [], "per_ticker": {}}
        path = gate_path(results_dir, date_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    finally:
        try:
            if broker is not None:
                broker.disconnect()
        except Exception:  # noqa: BLE001 — disconnect noise must not mask the gate
            pass

    from daily_run import _last_close
    for ticker in set(holdings) | set((_load_ratings(
            Path(results_dir), date_str) or {}).get("ratings", {})):
        price = _last_close(ticker)
        if price:
            last_close[ticker] = price

    return evaluate(cfg, results_dir, date_str, holdings, last_close)


if __name__ == "__main__":
    raise SystemExit(main())
