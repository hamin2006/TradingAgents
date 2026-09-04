# Burst-Continuation Backtest Gate Results (2026-09-04)

Gate for `docs/superpowers/specs/2026-09-04-catalyst-aware-screening-pilot-design.md`
§4.4. Run on the PC against the 6y crash-in-sample cache
(`~/.tradingagents/logs/backtest_prices_y6.csv`, 2020-08-31 .. 2026-08-28,
1506 sessions x 500 tickers). Tool: `burst_gate.py` (committed; hermetic
synthetic-panel tests).

## Verdict

**ADOPT** — union(4/6) continues: +0.10% mean alpha at 5 sessions, +0.48% at
10 (n = 32,164, t = 2.58 / 9.32). The spec's written bar is met. **But the
half-period robustness split qualifies the verdict severely: the signal is
entirely a post-2023 phenomenon.** Pre-2023 (2020-08 .. 2022-12, includes the
2022 crash) mean-reverts at every horizon.

| Rule | n | alpha 1d % | alpha 3d % | alpha 5d % | alpha 10d % | pos 5d % | pos 10d % | t 5d | t 10d |
|---|---|---|---|---|---|---|---|---|---|
| 1d rule >= 3% | 42694 | +0.05 | +0.03 | +0.09 | +0.33 | 49.2 | 49.2 | +2.90 | +8.26 |
| 1d rule >= 4% | 21838 | +0.09 | +0.03 | +0.09 | +0.48 | 48.6 | 49.7 | +1.84 | +7.62 |
| 1d rule >= 5% | 12236 | +0.11 | +0.06 | +0.12 | +0.61 | 48.9 | 49.6 | +1.79 | +6.60 |
| 1d rule >= 6% | 7477 | +0.10 | +0.03 | +0.06 | +0.72 | 48.5 | 49.2 | +0.68 | +5.66 |
| 2d rule >= 3% | 86348 | +0.01 | -0.02 | +0.03 | +0.19 | 48.8 | 48.9 | +1.48 | +7.47 |
| 2d rule >= 4% | 51090 | +0.01 | -0.04 | +0.05 | +0.28 | 48.8 | 48.9 | +1.89 | +7.59 |
| 2d rule >= 5% | 31278 | +0.01 | -0.05 | +0.07 | +0.39 | 48.7 | 49.1 | +1.76 | +7.76 |
| 2d rule >= 6% | 20149 | -0.01 | -0.09 | +0.06 | +0.50 | 48.4 | 49.3 | +1.21 | +7.37 |
| **union 4/6** | **32164** | **+0.05** | **+0.01** | **+0.10** | **+0.48** | 48.7 | 49.6 | +2.58 | +9.32 |
| union 4/8 | 25569 | +0.08 | +0.02 | +0.10 | +0.54 | 48.6 | 49.7 | +2.31 | +9.05 |
| union 5/6 | 25033 | +0.04 | -0.01 | +0.11 | +0.53 | 48.8 | 49.5 | +2.60 | +8.67 |
| union 5/8 | 16924 | +0.07 | +0.02 | +0.13 | +0.65 | 48.9 | 49.6 | +2.33 | +8.18 |
| union 4/6 (pre 2023-01-01) | 13943 | -0.05 | -0.28 | -0.13 | -0.06 | 48.0 | 48.3 | -2.52 | -0.80 |
| **union 4/6 (post 2023-01-01)** | **18221** | **+0.12** | **+0.23** | **+0.27** | **+0.90** | 49.2 | 50.5 | +5.15 | +12.27 |

## Reading

1. **The mean is carried by a right tail.** Pos-5d/10d sits at ~48-49%
   full-sample (50.5% post-2023 at 10d): most bursts fade slightly, a
   minority of big winners (takeover/squeeze-class events) carry the mean.
   Positive-EV lottery, not a coin-flip edge.
2. **Horizon shape matters for the pilot.** 1-3d alpha is ~0 (or negative
   for the 2d rule — digestion after a run-up); continuation shows at +5d
   and dominates at +10d. This favors the pilot's design — enter at the
   open, let the multi-week rating machine manage — and argues against any
   quick-flip variant (which would harvest exactly the weak 1-3d window).
3. **Regime dependence (the honest caveat).** Pre-2023 (incl. the 2022
   crash) mean-reverts outright. Burst continuation is a 2023+ trending-
   tape phenomenon. Two mitigations, one by construction:
   - The production pair is burst overlay + the existing regime gate,
     which suppresses new buys in STRESS (SPY < 200d + elevated VIX) —
     precisely the 2020-2022 crisis class where bursts faded. The overlay
     will never trade the era where it mean-reverts unconditionally.
   - The pilot's analytics slice (`analyze_results --by-surfacing`) is the
     stop condition: if burst-surfaced trades underperform the rank
     baseline over the pilot window, the overlay is switched off
     (`burst_overlay.enabled: false`) with the measured answer recorded.
4. **Threshold choice.** 1d>=4/2d>=6 sits mid-family on both alpha and n
   (32k events over 6y ≈ 26/day market-wide; ~4/day after exclusions —
   comfortably inside the ≤2/day pool cap). 5/8 has modestly higher alpha
   at the cost of fewer events; 4/6 stays as provisional default. Not
   worth tuning further pre-live: the LLM filter + rating gate select from
   these, and the pilot itself is the tuning instrument.

## Reproduce

```bash
python burst_gate.py ~/.tradingagents/logs/backtest_prices_y6.csv \
    --out ~/.tradingagents/logs/burst_gate.md --split 2023-01-01
```
