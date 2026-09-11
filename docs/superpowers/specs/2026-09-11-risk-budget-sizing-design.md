# Risk-Budget Sizing for Fallback Buys (2026-09-11)

Status: **Design approved 2026-09-11** (short spec — single-module change; implement
via TDD from here). Companion: `2026-09-11-pm-take-profit-oco-design.md`.

## 1. Problem

The legacy fallback sizer in `decisions.compute_orders` sizes buys as
`base_slice = capital / max_positions` where the caller passes
`capital = min(configured capital, real cash)`:

- The slice shrinks as positions consume cash (2026-09-11: ~$740 vs the nominal
  $1,000) — "equal weight" silently becomes cash-dependent.
- Hard whole-share floor: any stock priced above the slice is skipped even when cash
  can buy it (MU $975 and SNDK $1,633 were both skipped on 9/11 while ~$7.3k sat idle).
- No sum-level cash pass: legacy buys could collectively ask more than available cash.
- The PM execution path is unaffected (explicit sizing, whole-share disclosure live),
  but the fallback is the safety net for invalid/absent blocks and should be sane.

## 2. Decisions

1. **Scope: fallback buys only.** PM execution blocks stay explicit (whole-share,
   cash-capped as today). No fractional shares.
2. **Equity base.** Sizing base = portfolio equity = `cash + Σ(shares × reference
   close)`, not `min(config, cash)`. `capital` remains only as documentation — the PM
   path is bounded by the cash pass (§2.6), not by a per-order clamp.
3. **One risk knob.** `risk_budget_pct` (new config, default `1.2`) = equity % risked
   per position at its stop. Per-name ceiling weight = `risk_budget_pct /
   stop_loss_pct` = **15% at defaults** (1.2% / 8%). If `stop_loss_pct <= 0` (stops
   disabled), ceiling = the target weight (no risk trim).
4. **Targets.** `target_weight = conviction × (1 / max_positions)` — Buy 1.5× → 15%,
   Overweight 1.0× → 10% — trimmed to the ceiling. `max_positions`,
   `conviction_weights`, `max_order_value_cap` semantics unchanged.
5. **Whole shares with a min-1 rule.** `shares = floor(target_weight × equity /
   price)`; if that is 0, buy **1 share iff its weight ≤ ceiling**; else skip. At the
   fixed 8% stop, the weight check implies the risk check
   (`1-share risk = weight × stop_pct ≤ risk_budget_pct`), so one rule suffices.
6. **Cash pass.** Combined buys — PM explicit orders first, then legacy buys by
   conviction (Buy before Overweight, ties by ticker) — must fit available cash;
   anything that would exceed it is skipped (deterministic, no shaving).
7. **Stops unchanged.** Legacy −8% (or PM `stop_px` when the block is valid). No
   volatility-scaled stops in the fallback: it is a rare path, the PM is the
   volatility-aware sizer, dynamic stop policy is safety-critical, and the risk knob
   is already stop-parameterized (it auto-adapts if stop distances ever change).
8. **Missing data is conservative:** a missing holding close understates equity
   (warning); equity ≤ 0 or missing reference close for a candidate → no buy for it.

## 3. Worked examples (9/11: equity ≈ $10k, cash $7.3k, ceiling $1,500)

| Name | Price | Old behavior | New behavior |
|---|---|---|---|
| MU (OW) | $975.31 | skipped (slice $740–990) | 1 share (target yields it), 9.8% weight, 0.78% risk |
| Generic OW | $1,200.00 | skipped | **min-1 rule**: 1 share, 12% weight ≤ 15% ceiling |
| SNDK (OW) | $1,632.66 | skipped | **skipped** — 1 share = 16.3% > 15% ceiling |
| HPE (OW) | $61.12 | 11–13 shares (cash-dependent) | 16 shares ≈ $978 |

## 4. Tests (hermetic TDD)

- `compute_orders`: equity base; ceiling from `risk_budget_pct/stop_loss_pct`;
  conviction target trimmed; min-1 allowed at/below ceiling and refused above; cash
  pass ordering + skips; slots unchanged; no-price and low-equity guards;
  `stop_loss_pct: 0` ceiling fallback.
- `daily_run` execute: cash pass integrates with PM orders prioritized (existing
  per-order PM clamp replaced/absorbed); slice-math tests rewritten.
- Binding gate: unaffected (validates blocks only).

## 5. Non-goals

- Volatility-scaled stops in the legacy fallback.
- Changes to PM explicit sizing or the whole-share rule.
- Fractional shares; `max_positions` changes; PM take-profit mechanics (separate spec).
