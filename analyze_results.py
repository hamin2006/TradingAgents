"""analyze_results.py — outcome analytics over the decision memory log.

Answers the meta-questions the paper account exists to answer:
- Does a Buy/Overweight rating actually predict positive alpha?
- Do Sell/Underweight ratings catch real downside?
- Which tickers carry the signal, which drag it?
- Is the system on a losing streak?

Reads the framework's TradingMemoryLog (every decision + realized return +
alpha vs the ticker's benchmark), computes stats, prints a markdown report
and saves it next to the other artifacts. Pure stdlib + the framework's own
log parser — no LLM, no network.

Usage:
    .venv/bin/python analyze_results.py            # uses the configured memory log
    .venv/bin/python analyze_results.py --log-path ~/.tradingagents/memory/trading_memory.md
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

BUY_RATINGS = {"Buy", "Overweight"}
SELL_RATINGS = {"Sell", "Underweight"}


def parse_pct(value) -> float | None:
    """'+2.3%' -> 2.3 ; None/invalid -> None."""
    if not isinstance(value, str):
        return None
    s = value.strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _tier_of(rating: str) -> str | None:
    if rating in BUY_RATINGS:
        return "Buy/Overweight"
    if rating in SELL_RATINGS:
        return "Sell/Underweight"
    if rating == "Hold":
        return "Hold"
    return None


def compute_stats(entries: list[dict]) -> dict:
    """Aggregate resolved decisions into tier/ticker/streak statistics."""
    resolved = []
    for e in entries:
        if e.get("pending"):
            continue
        raw, alpha = parse_pct(e.get("raw")), parse_pct(e.get("alpha"))
        if raw is None or alpha is None:
            continue
        resolved.append({**e, "_raw": raw, "_alpha": alpha})

    tiers: dict[str, dict] = {}
    for name in ("Buy/Overweight", "Hold", "Sell/Underweight"):
        tiers[name] = {"n": 0, "avg_raw": None, "avg_alpha": None, "hit_rate": None}
    tier_alphas: dict[str, list[float]] = defaultdict(list)
    tier_raws: dict[str, list[float]] = defaultdict(list)
    tier_correct: dict[str, list[int]] = defaultdict(list)

    tickers: dict[str, dict] = defaultdict(lambda: {"n": 0, "alphas": []})

    for e in sorted(resolved, key=lambda e: e["date"]):
        tier = _tier_of(e["rating"])
        if tier:
            t = tiers[tier]
            t["n"] += 1
            tier_alphas[tier].append(e["_alpha"])
            tier_raws[tier].append(e["_raw"])
            if e["_alpha"] > 0:
                correct = 1 if tier == "Buy/Overweight" else 0
            elif e["_alpha"] < 0:
                correct = 1 if tier == "Sell/Underweight" else 0
            else:
                correct = 0
            if tier != "Hold":  # Hold makes no directional claim
                tier_correct[tier].append(correct)

        tk = tickers.setdefault(e["ticker"], {"n": 0, "alphas": []})
        tk["n"] += 1
        tk["alphas"].append(e["_alpha"])

    for tier, t in tiers.items():
        if t["n"]:
            t["avg_raw"] = sum(tier_raws[tier]) / t["n"]
            t["avg_alpha"] = sum(tier_alphas[tier]) / t["n"]
        if tier_correct[tier]:
            t["hit_rate"] = sum(tier_correct[tier]) / len(tier_correct[tier])

    tickers_out = {
        t: {"n": v["n"], "avg_alpha": sum(v["alphas"]) / v["n"]}
        for t, v in sorted(tickers.items())
    }

    # streaks over the resolved timeline (a "loss" = negative alpha)
    longest_loss = longest_win = cur = 0
    cur_kind, cur_kind_name = None, None
    for e in sorted(resolved, key=lambda e: e["date"]):
        kind = "win" if e["_alpha"] > 0 else ("loss" if e["_alpha"] < 0 else None)
        if kind is None:
            continue
        if kind == cur_kind_name:
            cur += 1
        else:
            cur_kind_name, cur = kind, 1
        if kind == "loss":
            longest_loss = max(longest_loss, cur)
        else:
            longest_win = max(longest_win, cur)
    cur_kind = cur_kind_name
    streaks = {
        "longest_loss": longest_loss,
        "longest_win": longest_win,
        "current": (cur_kind, cur) if cur_kind else (None, 0),
    }

    overall = {
        "n": len(resolved),
        "avg_alpha": (sum(e["_alpha"] for e in resolved) / len(resolved)
                      if resolved else None),
        "best": (max(resolved, key=lambda e: e["_alpha"]) if resolved else None),
        "worst": (min(resolved, key=lambda e: e["_alpha"]) if resolved else None),
    }
    return {
        "total_resolved": len(resolved),
        "tiers": tiers,
        "tickers": tickers_out,
        "streaks": streaks,
        "overall": overall,
    }


def _fmt_pct(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else "—"


def render_report(stats: dict, as_of: str) -> str:
    lines = [f"# Outcome Analytics — {stats['total_resolved']} resolved decisions "
             f"(as of {as_of})", ""]

    if stats["total_resolved"] == 0:
        lines.append("No resolved decisions yet. Ratings become resolvable on the "
                     "first same-ticker re-run after their holding window.")
        return "\n".join(lines)

    lines += ["## By rating tier", "",
              "| Tier | N | Avg raw | Avg alpha | Directional hit rate |",
              "|---|---|---|---|---|"]
    for tier, t in stats["tiers"].items():
        hit = (f"{t['hit_rate']:.0%}" if t["hit_rate"] is not None else "—")
        lines.append(f"| {tier} | {t['n']} | {_fmt_pct(t['avg_raw'])} | "
                     f"{_fmt_pct(t['avg_alpha'])} | {hit} |")

    lines += ["", "## Per ticker", "",
              "| Ticker | N | Avg alpha |", "|---|---|---|"]
    for ticker, t in stats["tickers"].items():
        lines.append(f"| {ticker} | {t['n']} | {_fmt_pct(t['avg_alpha'])} |")

    s = stats["streaks"]
    cur_kind, cur_n = s["current"]
    lines += ["", "## Streaks", "",
              f"- Current: {f'{cur_n} {cur_kind}(s)' if cur_kind else 'none'}",
              f"- Longest losing streak: {s['longest_loss']}",
              f"- Longest winning streak: {s['longest_win']}"]

    o = stats["overall"]
    if o["best"] and o["worst"]:
        lines += ["", "## Extremes", "",
                  f"- Best: {o['best']['ticker']} {o['best']['rating']} on "
                  f"{o['best']['date']} → {_fmt_pct(o['best']['_alpha'])} alpha",
                  f"- Worst: {o['worst']['ticker']} {o['worst']['rating']} on "
                  f"{o['worst']['date']} → {_fmt_pct(o['worst']['_alpha'])} alpha"]

    lines += ["", "*Interpretation guide: Buy/Overweight hit rate > 50% and positive "
              "avg alpha means the entry signal carries edge; Sell/Underweight hit "
              "rate > 50% means the exit signal catches real downside. A losing "
              "streak of 4+ is a regime warning — consider pausing new entries.*"]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Outcome analytics over the decision log")
    parser.add_argument("--log-path", default=None,
                        help="override the memory log path")
    args = parser.parse_args(argv)

    from config import load_watchlist_config
    from tradingagents.agents.utils.memory import TradingMemoryLog
    from tradingagents.dataflows.config import set_config

    cfg = load_watchlist_config()
    set_config(cfg)
    if args.log_path:
        cfg["memory_log_path"] = args.log_path

    entries = TradingMemoryLog(cfg).load_entries()
    stats = compute_stats(entries)
    today = __import__("daily_run").TODAY_ET().isoformat()
    report = render_report(stats, as_of=today)

    print(report)
    out = __import__("pathlib").Path(cfg["results_dir"]) / "analysis_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"\nsaved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
