"""Burst-continuation backtest gate.

Spec: docs/superpowers/specs/2026-09-04-catalyst-aware-screening-pilot-design.md
§4.4. Go/no-go before any live pool change: do >=X% 1-day (or >=Y% 2-day)
price bursts on S&P 500 names CONTINUE (positive forward alpha vs the
same-day universe) or MEAN-REVERT on the crash-in-sample price cache?

Data: yf.download(group_by="ticker") wide CSV as cached on the PC
(~/.tradingagents/logs/backtest_prices_y*.csv). Layout: 3 header rows
(Ticker names / Price fields / Date index-name), rows = sessions, columns =
MultiIndex (ticker, Open/High/Low/Close/Volume). Dates are session dates
ascending.

Semantics mirror production: the 04:10 screen of day D sees closes through
the last completed session S = D-1. A burst fires on S when
close[S]/close[S-2]-1 >= one_day_pct OR close[S]/close[S-3]-1 >= two_day_pct
(r1 over one session, r2 over two sessions). Forward returns measured from
close[S] to close[S+h] (h = 1/3/5/10 sessions) — the entry-at-next-open
proxy (production buys at D's open; close[S] is the last pre-entry price).
Baseline = cross-sectional mean forward return over the same window on the
same session S (kills calendar/crash-day effects). Alpha = event forward
return - that baseline.

Run:  python burst_gate.py ~/.tradingagents/logs/backtest_prices_y6.csv
      python burst_gate.py path.csv --out report.md
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

HORIZONS = (1, 3, 5, 10)
ONE_DAY_THS = (3.0, 4.0, 5.0, 6.0)
TWO_DAY_THS = (3.0, 4.0, 5.0, 6.0)
PROD_ONE_DAY = 4.0  # provisional defaults (spec §4.5)
PROD_TWO_DAY = 6.0
MIN_ROWS = 100  # per-ticker history floor
ADOPT_MIN_N = 100  # evidence floor: fewer events than this = inconclusive


def load_close_panel(path: str | Path, min_rows: int = MIN_ROWS) -> pd.DataFrame:
    """Dates x tickers of session Close from the yf wide CSV cache."""
    frame = pd.read_csv(path, header=[0, 1], index_col=0, skiprows=[2])
    closes = {}
    for ticker in frame.columns.get_level_values(0).unique():
        ticker = str(ticker)
        if ticker.startswith("^"):
            continue
        try:
            col = frame[ticker]["Close"].dropna()
        except KeyError:
            continue
        if len(col) >= min_rows:
            closes[ticker] = pd.to_numeric(col)
    panel = pd.DataFrame(closes).sort_index()
    panel.index = pd.to_datetime(panel.index)
    return panel


def _fire_mask(panel: pd.DataFrame, one_day_pct: float | None,
               two_day_pct: float | None) -> pd.DataFrame:
    """Boolean (session, ticker) mask of burst fires, one per event session.

    Vectorized on shifted closes; the mask at session S uses only closes
    through S (no look-ahead): r1 = S vs S-1, r2 = S vs S-2.
    """
    pct = panel.pct_change()
    r2 = panel / panel.shift(2) - 1
    m = pd.DataFrame(False, index=panel.index, columns=panel.columns)
    if one_day_pct is not None:
        m |= pct >= one_day_pct / 100
    if two_day_pct is not None:
        m |= r2 >= two_day_pct / 100
    return m


def detect(panel: pd.DataFrame, one_day_pct: float | None,
           two_day_pct: float | None) -> pd.DataFrame:
    """Event rows: index = burst session S, columns ticker/r1/r2."""
    pct = panel.pct_change()
    r2 = panel / panel.shift(2) - 1
    m = _fire_mask(panel, one_day_pct, two_day_pct)
    events = []
    for s, row in m.iterrows():
        for ticker in row.index[row.values]:
            events.append({"session": s, "ticker": ticker,
                           "r1": pct.at[s, ticker], "r2": r2.at[s, ticker]})
    return pd.DataFrame(events)


def forward_alphas(panel: pd.DataFrame, events: pd.DataFrame,
                   horizons: tuple = HORIZONS) -> pd.DataFrame:
    """Attach forward alpha per horizon to each event row.

    alpha_h(S) = f_h(S) - mean over all tickers of f_h(S) (same session,
    same window) — the same-day universe baseline.
    """
    out = events.copy()
    for h in horizons:
        f = panel.shift(-h) / panel - 1
        baseline = f.mean(axis=1)  # cross-sectional, per session
        out[f"fwd{h}"] = [
            f.at[s, t] - baseline.at[s] for s, t in zip(out["session"],
                                                         out["ticker"], strict=True)
        ]
    return out


def summarize(events: pd.DataFrame, label: str) -> dict:
    """Per-horizon stats: n, mean alpha %, % positive alpha, t-stat."""
    rows = {"rule": label, "n": len(events)}
    if not len(events):
        return rows
    for h in HORIZONS:
        col = f"fwd{h}"
        a = events[col] * 100
        rows[f"alpha{h}d"] = round(a.mean(), 3)
        rows[f"pos{h}d"] = round((a > 0).mean() * 100, 1)
        rows[f"t{h}d"] = round(a.mean() / (a.std() / len(a) ** 0.5), 2) if a.std() else 0.0
    return rows


def verdict(one_day_pct: float, two_day_pct: float, rows: list[dict]) -> str:
    """Adopt the provisional defaults only on positive continuation evidence.

    Rules (spec §4.4): the production union (one_day_pct, two_day_pct) must
    show positive mean forward alpha at +5 and +10 sessions with n >=
    ADOPT_MIN_N. Any rule at the same thresholds showing mean-reversion
    (negative alpha at +5/+10) is a red flag on that rule family.
    """
    matches = (r for r in rows if r["rule"] == f"union {one_day_pct:g}/{two_day_pct:g}")
    union = next(matches, None)
    if union is None:
        return "INCONCLUSIVE: production union row missing"
    n = union["n"]
    if n < ADOPT_MIN_N:
        return f"INCONCLUSIVE: only {n} events (< {ADOPT_MIN_N}) at the production combo"
    a5, a10 = union["alpha5d"], union["alpha10d"]
    if a5 > 0 and a10 > 0:
        return (f"ADOPT: union({one_day_pct:g}/{two_day_pct:g}) continues "
                f"(+{a5:.2f}% 5d, +{a10:.2f}% 10d mean alpha, n={n})")
    return (f"DEAD: union({one_day_pct:g}/{two_day_pct:g}) does not continue "
            f"(5d alpha {a5:+.2f}%, 10d alpha {a10:+.2f}%, n={n})")


def render_report(panel: pd.DataFrame, table: list[dict], verdict_text: str,
                  csv_path: str) -> str:
    lines = [
        "# Burst-Continuation Backtest Gate",
        "",
        f"- Data: `{csv_path}` — {len(panel)} sessions x {panel.shape[1]} tickers, "
        f"{panel.index.min().date()} .. {panel.index.max().date()}",
        "- Baseline: same-session cross-sectional mean forward return (S&P universe).",
        f"- Verdict: **{verdict_text}**",
        "",
        "| Rule | n | alpha 1d % | alpha 3d % | alpha 5d % | alpha 10d % | "
        "pos 5d % | pos 10d % | t 5d | t 10d |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in table:
        def fmt(k, r=r):
            v = r.get(k)
            return f"{v:+.2f}" if isinstance(v, float) else "-"
        lines.append(
            f"| {r['rule']} | {r['n']} | {fmt('alpha1d')} | {fmt('alpha3d')} | "
            f"{fmt('alpha5d')} | {fmt('alpha10d')} | {fmt('pos5d')} | "
            f"{fmt('pos10d')} | {fmt('t5d')} | {fmt('t10d')} |")
    return "\n".join(lines) + "\n"


def run_gate(path: str | Path, one_day_pct: float = PROD_ONE_DAY,
             two_day_pct: float = PROD_TWO_DAY,
             split_date: str | None = None) -> tuple:
    """Load, detect, summarize all rule families; return (panel, table, verdict).

    split_date (YYYY-MM-DD): additionally summarize the production union on
    each side of the cut — the overlapping-window robustness check (handoff
    conventions §7.3: a signal living in only one half-period is suspect).
    """
    panel = load_close_panel(path)
    table: list[dict] = []
    for th in ONE_DAY_THS:
        ev = detect(panel, th, None)
        table.append(summarize(forward_alphas(panel, ev), f"1d rule >= {th:g}%"))
    for th in TWO_DAY_THS:
        ev = detect(panel, None, th)
        table.append(summarize(forward_alphas(panel, ev), f"2d rule >= {th:g}%"))
    for th1 in (4.0, 5.0):
        for th2 in (6.0, 8.0):
            ev = detect(panel, th1, th2)
            table.append(summarize(forward_alphas(panel, ev),
                                   f"union {th1:g}/{th2:g}"))
    ev = detect(panel, one_day_pct, two_day_pct)
    fa = forward_alphas(panel, ev)
    table.append(summarize(fa, f"union {one_day_pct:g}/{two_day_pct:g}"))
    if split_date:
        cut = pd.Timestamp(split_date)
        pre = summarize(fa[fa["session"] < cut], f"union {one_day_pct:g}/{two_day_pct:g} (pre {split_date})")
        post = summarize(fa[fa["session"] >= cut], f"union {one_day_pct:g}/{two_day_pct:g} (post {split_date})")
        table.extend([pre, post])
    v = verdict(one_day_pct, two_day_pct, table)
    return panel, table, v


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv", help="backtest_prices CSV cache (wide yf layout)")
    ap.add_argument("--out", help="report path (default: <csv>.burst_gate.md)")
    ap.add_argument("--split", default="2023-01-01",
                    help="YYYY-MM-DD half-period robustness cut (default 2023-01-01)")
    args = ap.parse_args()
    panel, table, v = run_gate(args.csv, split_date=args.split)
    out = Path(args.out) if args.out else Path(args.csv).with_suffix(".burst_gate.md")
    out.write_text(render_report(panel, table, v, args.csv), encoding="utf-8")
    print(f"verdict: {v}")
    print(f"report:  {out}")


if __name__ == "__main__":
    main()
