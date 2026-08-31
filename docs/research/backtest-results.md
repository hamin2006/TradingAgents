# Backtest: Screening-Method Matrix (candidate quality, not final P&L)

**Date:** 2026-08-31 · **Universe:** 503 S&P 500 names · **Replay window:** 3y daily, screen replayed every trading day · **Top-N/day:** 10 · **Horizons:** 5d/20d forward alpha vs SPY

Candidate **quality only** — the LLM multi-agent layer (non-deterministic, expensive) is deliberately excluded; it sits after the screen. Results decide the rollout order of the §5bis upgrades.

## 1. Comparison table

| combo | avg 5d | hit 5d | p5 5d | avg 20d | hit 20d | p5 20d | tot ret | max DD | win% | trades |
|---|---|---|---|---|---|---|---|---|---|---|
| raw_momentum+none | 1.25% | 54.00% | -11.39% | 4.62% | 56.19% | -20.31% | 438.16% | -24.74% | 44.04% | 1149 |
| raw_momentum+regime_gate | 1.28% | 54.54% | -11.32% | 4.59% | 55.77% | -20.46% | 205.09% | -17.84% | 47.62% | 1325 |
| raw_momentum+dual_momentum | 1.09% | 53.67% | -11.10% | 4.09% | 56.02% | -19.86% | 239.62% | -24.09% | 44.04% | 1158 |
| vol_adjusted+none | 0.82% | 51.86% | -9.96% | 3.10% | 53.12% | -18.17% | 192.81% | -15.38% | 43.63% | 1373 |
| vol_adjusted+regime_gate | 0.84% | 52.28% | -9.84% | 3.24% | 53.15% | -17.61% | 148.69% | -14.66% | 45.82% | 1554 |
| vol_adjusted+dual_momentum | 0.65% | 51.26% | -9.83% | 2.53% | 52.64% | -17.67% | 163.29% | -16.20% | 42.69% | 1403 |
| rank_based+none | 0.44% | 51.02% | -8.27% | 1.64% | 50.46% | -14.89% | 114.81% | -14.69% | 43.28% | 2135 |
| rank_based+regime_gate | 0.46% | 51.45% | -8.16% | 1.83% | 51.04% | -13.89% | 91.66% | -14.18% | 44.49% | 2234 |
| rank_based+dual_momentum | 0.37% | 50.54% | -8.22% | 1.38% | 50.14% | -14.81% | 101.71% | -14.88% | 43.16% | 2127 |

## 2. Half-period splits (robustness)

### raw_momentum + none

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.70% | 51.57% | -10.61% | 2.24% | 50.77% | -20.49% | 86.33% | -15.77% |
| second | 1.78% | 56.42% | -12.27% | 7.08% | 61.89% | -20.19% | 439.39% | -19.13% |

### raw_momentum + regime_gate

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.79% | 51.59% | -9.79% | 3.07% | 52.75% | -18.50% | 72.66% | -14.83% |
| second | 1.75% | 57.42% | -13.28% | 6.18% | 58.94% | -22.91% | 206.47% | -17.84% |

### raw_momentum + dual_momentum

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.61% | 51.04% | -10.32% | 2.16% | 50.80% | -19.90% | 82.43% | -16.57% |
| second | 1.55% | 56.27% | -12.00% | 6.08% | 61.45% | -19.81% | 240.15% | -10.46% |

### vol_adjusted + none

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.29% | 49.25% | -8.85% | 0.87% | 47.17% | -17.54% | 39.77% | -14.81% |
| second | 1.35% | 54.52% | -10.85% | 5.41% | 59.44% | -18.70% | 194.07% | -10.30% |

### vol_adjusted + regime_gate

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.25% | 48.29% | -8.18% | 1.36% | 48.46% | -15.88% | 37.25% | -14.66% |
| second | 1.40% | 56.17% | -11.37% | 5.21% | 58.00% | -19.63% | 149.95% | -11.43% |

### vol_adjusted + dual_momentum

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.26% | 48.72% | -8.56% | 0.84% | 47.01% | -16.92% | 38.89% | -14.53% |
| second | 1.04% | 53.89% | -10.80% | 4.29% | 58.60% | -18.60% | 163.96% | -10.22% |

### rank_based + none

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.09% | 48.88% | -6.76% | 0.59% | 47.44% | -13.18% | 27.42% | -14.69% |
| second | 0.79% | 53.24% | -9.56% | 2.74% | 53.76% | -16.22% | 115.87% | -8.45% |

### rank_based + regime_gate

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.11% | 48.20% | -6.49% | 0.88% | 48.00% | -12.18% | 28.04% | -14.18% |
| second | 0.79% | 54.61% | -9.71% | 2.82% | 54.21% | -15.99% | 92.72% | -10.64% |

### rank_based + dual_momentum

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.07% | 48.32% | -6.69% | 0.47% | 46.98% | -13.18% | 18.77% | -14.88% |
| second | 0.67% | 52.82% | -9.56% | 2.32% | 53.52% | -16.24% | 102.44% | -8.89% |

## 3. Method verdicts (rollout order)

Sample: Aug 2023 – Aug 2026 (a strong, essentially uninterrupted bull window — no
sustained S&P 500 drawdown ≥ 20%). Every strategy + gate is stable across the
half-period split (the return *ordering* holds in both halves), so the ranking is
not a second-half fluke. But the absence of a crash regime in-sample is the central
caveat for interpreting it.

**Scoring strategy (return vs drawdown trade, gate = none):**

| strategy | avg 20d alpha | hit 20d | max DD | total ret | reading |
|---|---|---|---|---|---|
| raw_momentum | **4.62%** | **56.2%** | −24.7% | **438%** | highest return, deepest drawdown |
| vol_adjusted (current) | 3.10% | 53.1% | −15.4% | 193% | ~half the return, ~⅔ the drawdown |
| rank_based | 1.64% | 50.5% | **−14.7%** | 115% | smallest drawdown, worst return |

**Gate effect (same strategy):** every gate traded return for a smaller drawdown.
regime_gate cut max DD on *all three* strategies (raw −24.7→−17.8, vol −15.4→−14.7,
rank −14.7→−14.2) at a modest return cost; dual_momentum's DD benefit was inconsistent
(great second-half for raw −10.5%, worse first-half).

**Verdict — evidence contradicts the literature prior in this sample.** The
literature's "best value" upgrade (vol-adjustment) *reduced* forward alpha and total
return here; it and the gates act as **drawdown reducers, not return boosters**. That
is the *expected* mechanism (vol-managed momentum pays off in crashes), but this
window had no crash, so their protection is unmeasured — the return they "cost" is the
insurance premium, sampled without the accident.

**Recommended rollout order (measured):**

1. **Keep vol_adjusted as the default core.** It is the balanced pick: consistently
   ~⅓ less drawdown than raw for ~½ the return, in both halves. raw_momentum's higher
   return comes with the −24.7% drawdown — exactly the crash-prone profile this system
   ships vol-adjustment to avoid.
2. **Add the regime gate next** (SPY-vs-200d-SMA × VIX → CALM/WARN/STRESS). It is the
   only change that improved max DD on every strategy at the smallest return cost — the
   best drawdown-per-return trade in this sample. Its crash payoff is out-of-sample.
3. **Drop rank_based** — worst return with no drawdown advantage over vol_adjusted.
   Deprioritize (keep it in the registry; do not make it a default).
4. **Defer dual_momentum** — inconsistent drawdown benefit, clear return cost.
5. **Do not switch to raw_momentum** on this evidence — the bull-only window flatters
   it; its drawdown confirms the fragility the research warned about, and its crash
   regime is exactly what is not in-sample.

**Decisive next test:** extend the window to `--years 5` (includes the 2021–22 crash)
so the vol-adjustment/regime insurance can be measured where it is supposed to pay off.
Until that run, the return ranking above is bull-regime evidence only and should not
override the shipped vol-adjusted default. Production defaults change only with user
approval.

## 4. Caveats

1. Candidate quality ≠ final P&L (the LLM layer filters further — excluded for determinism).
2. Survivorship bias (today's S&P 500 used for all past dates) inflates absolute numbers but affects all methods equally → comparisons valid.
3. Overlapping forward windows inflate sample correlation → half-period splits are the robustness check.
4. Literature params only, no tuning — validation of pre-registered methods, not a parameter search.
5. Portfolio sim uses equal-weight sizing at config capital ($100k), deterministic proxy exits (pool-drop, stop-loss, optional time stop) — absolute P&L won't match live (no LLM, no costs); the method *ranking* is the deliverable.
6. Entry fills at the next open with a 2% gap cap (skipped if gapped past prev_close × 1.02).
7. The screen replays with top-10/day (deliberate divergence from production's 3/day + 3-day exclusion) for statistical mass.
8. Regime/dual gates need a 200d SMA and 12m returns → a ~1y warm-up precedes the replay window.

_Machine-readable results embedded below._

```json
{
  "raw_momentum+none": {
    "avg_5d": 0.012451182267189345,
    "hit_5d": 0.5399732620320855,
    "p5_5d": -0.11391546837076022,
    "worst_5d": -0.3161357998180979,
    "n_5d": 7480,
    "avg_20d": 0.04615669914902695,
    "hit_20d": 0.5619372442019099,
    "p5_20d": -0.20312398374878587,
    "worst_20d": -0.5687186711732677,
    "n_20d": 7330,
    "total_return": 4.381559244675454,
    "max_drawdown": -0.24740957639159844,
    "n_equity_days": 754,
    "trade_win_rate": 0.44038294168842473,
    "n_trades": 1149,
    "n_obs": 14810,
    "splits": {
      "first": {
        "avg_5d": 0.006994016810037885,
        "hit_5d": 0.5157333333333334,
        "p5_5d": -0.10613564580544546,
        "worst_5d": -0.3161357998180979,
        "n_5d": 3750,
        "avg_20d": 0.022431869986221823,
        "hit_20d": 0.5077333333333334,
        "p5_20d": -0.20489836514092477,
        "worst_20d": -0.43286532517645204,
        "n_20d": 3750,
        "total_return": 0.8632822352753469,
        "max_drawdown": -0.1576884730394561,
        "n_equity_days": 375
      },
      "second": {
        "avg_5d": 0.017836859409883193,
        "hit_5d": 0.5641711229946524,
        "p5_5d": -0.1226952826200226,
        "worst_5d": -0.29027710636102566,
        "n_5d": 3740,
        "avg_20d": 0.07077807363353694,
        "hit_20d": 0.618941504178273,
        "p5_20d": -0.20194413049051585,
        "worst_20d": -0.5687186711732677,
        "n_20d": 3590,
        "total_return": 4.393929270879382,
        "max_drawdown": -0.19131703858660776,
        "n_equity_days": 374
      }
    }
  },
  "raw_momentum+regime_gate": {
    "avg_5d": 0.012781890327174096,
    "hit_5d": 0.5454281567489114,
    "p5_5d": -0.11320950431040837,
    "worst_5d": -0.3161357998180979,
    "n_5d": 6890,
    "avg_20d": 0.0459032497419811,
    "hit_20d": 0.5577151335311573,
    "p5_20d": -0.20459065709140029,
    "worst_20d": -0.5025153087027889,
    "n_20d": 6740,
    "total_return": 2.0508647050795985,
    "max_drawdown": -0.17844448027446447,
    "n_equity_days": 754,
    "trade_win_rate": 0.4762264150943396,
    "n_trades": 1325,
    "n_obs": 13630,
    "splits": {
      "first": {
        "avg_5d": 0.007858961762706779,
        "hit_5d": 0.5159420289855072,
        "p5_5d": -0.09787219879307907,
        "worst_5d": -0.29538511131820955,
        "n_5d": 3450,
        "avg_20d": 0.030745453464994427,
        "hit_20d": 0.527536231884058,
        "p5_20d": -0.18502673393615773,
        "worst_20d": -0.40628087442017413,
        "n_20d": 3450,
        "total_return": 0.7266068235403436,
        "max_drawdown": -0.14833731366331482,
        "n_equity_days": 350
      },
      "second": {
        "avg_5d": 0.0175378387311445,
        "hit_5d": 0.5742028985507246,
        "p5_5d": -0.13279872835779127,
        "worst_5d": -0.3161357998180979,
        "n_5d": 3450,
        "avg_20d": 0.061792782127064384,
        "hit_20d": 0.5893939393939394,
        "p5_20d": -0.22910735635557547,
        "worst_20d": -0.5025153087027889,
        "n_20d": 3300,
        "total_return": 2.064736930685381,
        "max_drawdown": -0.17844448027446447,
        "n_equity_days": 399
      }
    }
  },
  "raw_momentum+dual_momentum": {
    "avg_5d": 0.01089974012420986,
    "hit_5d": 0.5367292225201072,
    "p5_5d": -0.11097336214288667,
    "worst_5d": -0.3161357998180979,
    "n_5d": 7460,
    "avg_20d": 0.04089020316610657,
    "hit_20d": 0.5601915184678523,
    "p5_20d": -0.19861824683989085,
    "worst_20d": -0.5553232522816418,
    "n_20d": 7310,
    "total_return": 2.3961584635944577,
    "max_drawdown": -0.24088716763258766,
    "n_equity_days": 754,
    "trade_win_rate": 0.44041450777202074,
    "n_trades": 1158,
    "n_obs": 14770,
    "splits": {
      "first": {
        "avg_5d": 0.006143519346405553,
        "hit_5d": 0.510427807486631,
        "p5_5d": -0.1032493645081307,
        "worst_5d": -0.3161357998180979,
        "n_5d": 3740,
        "avg_20d": 0.02158740183676652,
        "hit_20d": 0.5080213903743316,
        "p5_20d": -0.19900489856914327,
        "worst_20d": -0.40628087442017413,
        "n_20d": 3740,
        "total_return": 0.8242950322127662,
        "max_drawdown": -0.16571608055883014,
        "n_equity_days": 374
      },
      "second": {
        "avg_5d": 0.01550197730430266,
        "hit_5d": 0.5627345844504021,
        "p5_5d": -0.12003280920469112,
        "worst_5d": -0.29027710636102566,
        "n_5d": 3730,
        "avg_20d": 0.06083440239523555,
        "hit_20d": 0.6145251396648045,
        "p5_20d": -0.1981360646356966,
        "worst_20d": -0.5553232522816418,
        "n_20d": 3580,
        "total_return": 2.401464483412582,
        "max_drawdown": -0.10456150133102105,
        "n_equity_days": 373
      }
    }
  },
  "vol_adjusted+none": {
    "avg_5d": 0.008164752383209361,
    "hit_5d": 0.5185828877005347,
    "p5_5d": -0.0996459882012602,
    "worst_5d": -0.3161357998180979,
    "n_5d": 7480,
    "avg_20d": 0.0309511246552404,
    "hit_20d": 0.5312414733969987,
    "p5_20d": -0.18169100254040474,
    "worst_20d": -0.5553232522816418,
    "n_20d": 7330,
    "total_return": 1.9280852802503876,
    "max_drawdown": -0.15378574852184967,
    "n_equity_days": 754,
    "trade_win_rate": 0.4362709395484341,
    "n_trades": 1373,
    "n_obs": 14810,
    "splits": {
      "first": {
        "avg_5d": 0.0028571437431860426,
        "hit_5d": 0.4925333333333333,
        "p5_5d": -0.08854261136719764,
        "worst_5d": -0.3161357998180979,
        "n_5d": 3750,
        "avg_20d": 0.008742769009120827,
        "hit_20d": 0.47173333333333334,
        "p5_20d": -0.1753705884271837,
        "worst_20d": -0.43286532517645204,
        "n_20d": 3750,
        "total_return": 0.39768762170084626,
        "max_drawdown": -0.14812423218812887,
        "n_equity_days": 375
      },
      "second": {
        "avg_5d": 0.013508240820754644,
        "hit_5d": 0.545187165775401,
        "p5_5d": -0.10846030659226821,
        "worst_5d": -0.29027710636102566,
        "n_5d": 3740,
        "avg_20d": 0.05414333326790841,
        "hit_20d": 0.5944289693593314,
        "p5_20d": -0.1869733852991988,
        "worst_20d": -0.5553232522816418,
        "n_20d": 3590,
        "total_return": 1.9407241108976745,
        "max_drawdown": -0.10295057395333929,
        "n_equity_days": 374
      }
    }
  },
  "vol_adjusted+regime_gate": {
    "avg_5d": 0.008368058680210497,
    "hit_5d": 0.5227866473149492,
    "p5_5d": -0.0983615028663458,
    "worst_5d": -0.3161357998180979,
    "n_5d": 6890,
    "avg_20d": 0.03243077717824842,
    "hit_20d": 0.5314540059347181,
    "p5_20d": -0.1761211508981975,
    "worst_20d": -0.5025153087027889,
    "n_20d": 6740,
    "total_return": 1.4868655096905372,
    "max_drawdown": -0.14658479089452892,
    "n_equity_days": 754,
    "trade_win_rate": 0.45817245817245816,
    "n_trades": 1554,
    "n_obs": 13630,
    "splits": {
      "first": {
        "avg_5d": 0.002527178253382802,
        "hit_5d": 0.48289855072463767,
        "p5_5d": -0.0818213805083352,
        "worst_5d": -0.29538511131820955,
        "n_5d": 3450,
        "avg_20d": 0.013605179886275494,
        "hit_20d": 0.4846376811594203,
        "p5_20d": -0.15881273772105306,
        "worst_20d": -0.40628087442017413,
        "n_20d": 3450,
        "total_return": 0.3725437499215294,
        "max_drawdown": -0.14658479089452892,
        "n_equity_days": 350
      },
      "second": {
        "avg_5d": 0.013985719671454797,
        "hit_5d": 0.5617391304347826,
        "p5_5d": -0.11370504972682278,
        "worst_5d": -0.3161357998180979,
        "n_5d": 3450,
        "avg_20d": 0.05212791421515451,
        "hit_20d": 0.58,
        "p5_20d": -0.19626310727183008,
        "worst_20d": -0.5025153087027889,
        "n_20d": 3300,
        "total_return": 1.4995043403378236,
        "max_drawdown": -0.1142811762063517,
        "n_equity_days": 399
      }
    }
  },
  "vol_adjusted+dual_momentum": {
    "avg_5d": 0.006482208745844502,
    "hit_5d": 0.5126005361930295,
    "p5_5d": -0.09828456708686631,
    "worst_5d": -0.3161357998180979,
    "n_5d": 7460,
    "avg_20d": 0.025275032620992736,
    "hit_20d": 0.5264021887824898,
    "p5_20d": -0.17670451313000426,
    "worst_20d": -0.5553232522816418,
    "n_20d": 7310,
    "total_return": 1.632928467046988,
    "max_drawdown": -0.16203887698520547,
    "n_equity_days": 754,
    "trade_win_rate": 0.4269422665716322,
    "n_trades": 1403,
    "n_obs": 14770,
    "splits": {
      "first": {
        "avg_5d": 0.002589650321166478,
        "hit_5d": 0.48716577540106953,
        "p5_5d": -0.08558986487903855,
        "worst_5d": -0.3161357998180979,
        "n_5d": 3740,
        "avg_20d": 0.008427624807076102,
        "hit_20d": 0.47005347593582886,
        "p5_20d": -0.16921127542117106,
        "worst_20d": -0.40628087442017413,
        "n_20d": 3740,
        "total_return": 0.38886358445879443,
        "max_drawdown": -0.14528929511739053,
        "n_equity_days": 374
      },
      "second": {
        "avg_5d": 0.01042410636552812,
        "hit_5d": 0.5388739946380697,
        "p5_5d": -0.10797897613061724,
        "worst_5d": -0.29027710636102566,
        "n_5d": 3730,
        "avg_20d": 0.04285811666249681,
        "hit_20d": 0.5860335195530726,
        "p5_20d": -0.18598577221476129,
        "worst_20d": -0.5553232522816418,
        "n_20d": 3580,
        "total_return": 1.63957029207904,
        "max_drawdown": -0.10219295225139902,
        "n_equity_days": 373
      }
    }
  },
  "rank_based+none": {
    "avg_5d": 0.004405823273578421,
    "hit_5d": 0.5101604278074866,
    "p5_5d": -0.0826714030669777,
    "worst_5d": -0.49378908472623373,
    "n_5d": 7480,
    "avg_20d": 0.016381038776404216,
    "hit_20d": 0.5046384720327421,
    "p5_20d": -0.1488900264234308,
    "worst_20d": -0.4927348043632437,
    "n_20d": 7330,
    "total_return": 1.1481139734197372,
    "max_drawdown": -0.14691492959085273,
    "n_equity_days": 754,
    "trade_win_rate": 0.43278688524590164,
    "n_trades": 2135,
    "n_obs": 14810,
    "splits": {
      "first": {
        "avg_5d": 0.0009310085769831351,
        "hit_5d": 0.4888,
        "p5_5d": -0.06760191971829398,
        "worst_5d": -0.2701149678379191,
        "n_5d": 3750,
        "avg_20d": 0.0058599637047241,
        "hit_20d": 0.4744,
        "p5_20d": -0.1318157895766674,
        "worst_20d": -0.40628087442017413,
        "n_20d": 3750,
        "total_return": 0.2742283324257482,
        "max_drawdown": -0.14691492959085273,
        "n_equity_days": 375
      },
      "second": {
        "avg_5d": 0.007928572804027813,
        "hit_5d": 0.5323529411764706,
        "p5_5d": -0.0955665778873763,
        "worst_5d": -0.49378908472623373,
        "n_5d": 3740,
        "avg_20d": 0.027441008033998442,
        "hit_20d": 0.5376044568245125,
        "p5_20d": -0.1621757212054283,
        "worst_20d": -0.4927348043632437,
        "n_20d": 3590,
        "total_return": 1.1587056345803335,
        "max_drawdown": -0.08453910809877263,
        "n_equity_days": 374
      }
    }
  },
  "rank_based+regime_gate": {
    "avg_5d": 0.0045778838831239165,
    "hit_5d": 0.5145137880986937,
    "p5_5d": -0.08162866121932454,
    "worst_5d": -0.49378908472623373,
    "n_5d": 6890,
    "avg_20d": 0.01829777932797737,
    "hit_20d": 0.5103857566765578,
    "p5_20d": -0.1389180663095701,
    "worst_20d": -0.4927348043632437,
    "n_20d": 6740,
    "total_return": 0.9166187205054814,
    "max_drawdown": -0.1417946261360723,
    "n_equity_days": 754,
    "trade_win_rate": 0.4449418084153984,
    "n_trades": 2234,
    "n_obs": 13630,
    "splits": {
      "first": {
        "avg_5d": 0.0011446980073616516,
        "hit_5d": 0.4820289855072464,
        "p5_5d": -0.06486874195871471,
        "worst_5d": -0.2701149678379191,
        "n_5d": 3450,
        "avg_20d": 0.008837398757619509,
        "hit_20d": 0.48,
        "p5_20d": -0.12181794739314038,
        "worst_20d": -0.40628087442017413,
        "n_20d": 3450,
        "total_return": 0.28036369513458226,
        "max_drawdown": -0.1417946261360723,
        "n_equity_days": 350
      },
      "second": {
        "avg_5d": 0.007864666971277625,
        "hit_5d": 0.5460869565217391,
        "p5_5d": -0.0970967635691278,
        "worst_5d": -0.49378908472623373,
        "n_5d": 3450,
        "avg_20d": 0.028179815989891253,
        "hit_20d": 0.5421212121212121,
        "p5_20d": -0.15986252841149126,
        "worst_20d": -0.4927348043632437,
        "n_20d": 3300,
        "total_return": 0.9272103816660775,
        "max_drawdown": -0.10643344371540764,
        "n_equity_days": 399
      }
    }
  },
  "rank_based+dual_momentum": {
    "avg_5d": 0.0036535768850889955,
    "hit_5d": 0.5053619302949062,
    "p5_5d": -0.08222204575637196,
    "worst_5d": -0.49378908472623373,
    "n_5d": 7460,
    "avg_20d": 0.013774545512232611,
    "hit_20d": 0.5013679890560876,
    "p5_20d": -0.14813630315936271,
    "worst_20d": -0.4927348043632437,
    "n_20d": 7310,
    "total_return": 1.017104816967926,
    "max_drawdown": -0.1487537842822918,
    "n_equity_days": 754,
    "trade_win_rate": 0.4315937940761636,
    "n_trades": 2127,
    "n_obs": 14770,
    "splits": {
      "first": {
        "avg_5d": 0.0006581248373678837,
        "hit_5d": 0.48315508021390374,
        "p5_5d": -0.06689003459798587,
        "worst_5d": -0.2701149678379191,
        "n_5d": 3740,
        "avg_20d": 0.004746213499791339,
        "hit_20d": 0.4697860962566845,
        "p5_20d": -0.13176907858871484,
        "worst_20d": -0.40628087442017413,
        "n_20d": 3740,
        "total_return": 0.1877287621049053,
        "max_drawdown": -0.1487537842822918,
        "n_equity_days": 374
      },
      "second": {
        "avg_5d": 0.006661251903036679,
        "hit_5d": 0.5281501340482574,
        "p5_5d": -0.09558784908086934,
        "worst_5d": -0.49378908472623373,
        "n_5d": 3730,
        "avg_20d": 0.023231913165318568,
        "hit_20d": 0.535195530726257,
        "p5_20d": -0.16237304901590519,
        "worst_20d": -0.4927348043632437,
        "n_20d": 3580,
        "total_return": 1.0243747795201235,
        "max_drawdown": -0.08892027131847446,
        "n_equity_days": 373
      }
    }
  }
}
```
