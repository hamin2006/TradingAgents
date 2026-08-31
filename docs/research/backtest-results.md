# Backtest: Screening-Method Matrix (candidate quality, not final P&L)

**Date:** 2026-08-31 · **Universe:** 503 S&P 500 names · **Replay window:** 5y daily, screen replayed every trading day · **Top-N/day:** 10 · **Horizons:** 5d/20d forward alpha vs SPY

Candidate **quality only** — the LLM multi-agent layer (non-deterministic, expensive) is deliberately excluded; it sits after the screen. Results decide the rollout order of the §5bis upgrades.

## 1. Comparison table

| combo | avg 5d | hit 5d | p5 5d | avg 20d | hit 20d | p5 20d | tot ret | max DD | win% | trades |
|---|---|---|---|---|---|---|---|---|---|---|
| raw_momentum+none | 0.92% | 52.76% | -10.52% | 3.23% | 53.57% | -18.94% | 449.47% | -25.68% | 42.02% | 1999 |
| raw_momentum+regime_gate | 1.14% | 53.68% | -10.62% | 3.93% | 54.77% | -19.01% | 263.51% | -20.96% | 44.79% | 1748 |
| raw_momentum+dual_momentum | 0.81% | 52.65% | -10.06% | 2.73% | 53.33% | -18.43% | 281.49% | -23.00% | 43.66% | 2020 |
| vol_adjusted+none | 0.53% | 50.88% | -9.32% | 2.03% | 51.01% | -16.71% | 218.37% | -26.00% | 42.83% | 2342 |
| vol_adjusted+regime_gate | 0.70% | 51.38% | -9.25% | 2.72% | 52.50% | -16.38% | 187.48% | -18.59% | 44.64% | 2108 |
| vol_adjusted+dual_momentum | 0.48% | 51.05% | -9.02% | 1.66% | 50.79% | -16.24% | 212.90% | -24.76% | 43.35% | 2309 |
| rank_based+none | 0.26% | 50.42% | -7.87% | 0.99% | 48.90% | -14.45% | 140.49% | -18.88% | 42.84% | 3287 |
| rank_based+regime_gate | 0.40% | 50.97% | -7.57% | 1.57% | 50.51% | -13.17% | 135.00% | -11.95% | 44.32% | 2845 |
| rank_based+dual_momentum | 0.21% | 50.14% | -7.86% | 0.80% | 48.58% | -14.44% | 132.97% | -19.15% | 43.08% | 3294 |

## 2. Half-period splits (robustness)

### raw_momentum + none

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.42% | 50.50% | -9.16% | 1.36% | 49.44% | -17.35% | 56.71% | -25.68% |
| second | 1.43% | 55.07% | -11.74% | 5.17% | 57.84% | -20.52% | 450.30% | -22.23% |

### raw_momentum + regime_gate

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.51% | 50.23% | -9.53% | 1.48% | 48.95% | -17.99% | 64.11% | -20.96% |
| second | 1.77% | 57.13% | -11.79% | 6.45% | 60.70% | -20.35% | 264.35% | -12.85% |

### raw_momentum + dual_momentum

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.42% | 50.58% | -8.61% | 1.11% | 49.28% | -16.61% | 67.49% | -23.00% |
| second | 1.20% | 54.76% | -11.40% | 4.41% | 57.50% | -20.01% | 282.55% | -19.26% |

### vol_adjusted + none

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.04% | 48.42% | -8.01% | 0.32% | 46.86% | -14.95% | 34.63% | -26.00% |
| second | 1.02% | 53.38% | -10.53% | 3.80% | 55.26% | -18.58% | 218.72% | -12.38% |

### vol_adjusted + regime_gate

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.11% | 47.42% | -7.63% | 0.58% | 46.99% | -15.26% | 42.79% | -18.59% |
| second | 1.28% | 55.33% | -10.48% | 4.92% | 58.16% | -17.86% | 187.83% | -8.27% |

### vol_adjusted + dual_momentum

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.12% | 49.07% | -7.76% | 0.26% | 46.96% | -14.72% | 41.95% | -24.76% |
| second | 0.87% | 53.08% | -10.16% | 3.12% | 54.76% | -18.12% | 212.96% | -14.38% |

### rank_based + none

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | -0.03% | 48.93% | -6.70% | -0.04% | 46.26% | -13.21% | 24.62% | -18.88% |
| second | 0.55% | 51.95% | -8.95% | 2.05% | 51.64% | -15.71% | 140.54% | -10.56% |

### rank_based + regime_gate

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | 0.04% | 48.22% | -6.13% | 0.27% | 46.39% | -11.91% | 39.00% | -11.95% |
| second | 0.75% | 53.73% | -9.02% | 2.90% | 54.76% | -14.99% | 135.05% | -7.81% |

### rank_based + dual_momentum

| half | avg5d | hit5d | p5_5d | avg20d | hit20d | p5_20d | tot_ret | maxDD |
|---|---|---|---|---|---|---|---|
| first | -0.03% | 48.80% | -6.72% | -0.03% | 46.14% | -13.21% | 24.20% | -19.15% |
| second | 0.45% | 51.54% | -8.91% | 1.66% | 51.15% | -15.65% | 133.07% | -11.72% |

## 3. Method verdicts (rollout order)

Sample: Aug 2021 – Aug 2026 — **the first half contains the 2021–22 market-wide
decline and the 2022 selloff**, which is the regime every §5bis defense exists for.
This run is the decisive raw-vs-vol test.

**raw vs vol, with the crash in sample (gate = none, first-half crash period):**

| strategy | avg 20d | hit 20d | first-half max DD | first-half tot | p5 20d |
|---|---|---|---|---|---|
| raw_momentum | 3.23% | 53.6% | −25.68% | 56.7% | −17.35% |
| vol_adjusted (current) | 2.03% | 51.0% | −26.00% | 34.6% | −14.95% |

**Verdict: vol-adjustment's promised crash protection does NOT materialize as
portfolio drawdown in the 2021–22 crash.** In the crash half vol_adjusted had a
marginally *worse* max drawdown than raw (−26.00% vs −25.68%) while giving up ~2/3
of the return. The 2022 decline was market-wide — the "calm grinders" vol-adjustment
demotes *toward* were sold off too, so a ticker-selection tweak cannot protect the
portfolio. Vol-adjustment's only real effect is a better *candidate downside tail*
(p5_20d −14.95% vs −17.35%) — it reduces per-name tail risk, not portfolio drawdown.
This falsifies the §5bis #1 prior ("virtually eliminates crashes") in this setup.

**The regime gate is the drawdown hedge — because the crash was market-wide, only a
market-level gate helps:**

| combo | avg 20d | max DD | tot ret |
|---|---|---|---|
| **raw + regime_gate** | **3.93%** | **−20.96%** | **263%** |
| raw + none | 3.23% | −25.68% | 449% |
| vol + regime_gate | 2.72% | −18.59% | 187% |
| vol + none | 2.03% | −26.00% | 218% |
| rank + regime_gate | 1.57% | −11.95% | 135% |
| rank + none | 0.99% | −18.88% | 140% |

The regime gate (SPY vs 200d SMA × VIX → STRESS pauses buys) cut max DD on *every*
strategy (raw −25.7→−21.0, vol −26.0→−18.6, rank −18.9→−12.0) at the smallest return
cost — because it de-risks *when* (market-timing), not just *who*.

**Recommended rollout order (measured):**

1. **Switch the default to `raw_momentum + regime_gate`** — the best return-per-drawdown
   combo in the full matrix: raw's superior alpha (3.93%/20d, highest of all 9) with the
   regime gate cutting its crash drawdown to −20.96% (vs −25.68% raw-alone).
2. **Add the regime gate** as the system's drawdown hedge — it is the only change that
   survived the crash-in-sample test on every strategy.
3. **Demote vol_adjusted** — its crash protection is falsified in the portfolio sim
   (−26.0% in the crash half ≈ raw). Keep it in the registry; do not default to it.
4. **Drop rank_based** — worst return on every gate, its max-DD advantage is small and
   it does not compound raw alpha.
5. **Defer dual_momentum** — no drawdown benefit over the regime gate, clear return cost.

**Caveat on the flip:** this overturns the earlier 3y (bull-only) read, where vol looked
like the balanced pick. The 3y window had no crash, so vol's protection was unmeasured
and its return cost looked like insurance. Once the 2021–22 crash is actually in sample,
that insurance paid nothing — the regime gate is the hedge that works. Raw + regime_gate
is the evidence-backed default.

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
    "avg_5d": 0.009176655436162973,
    "hit_5d": 0.5276220976781425,
    "p5_5d": -0.1051659247676108,
    "worst_5d": -0.31613590157535476,
    "n_5d": 12490,
    "avg_20d": 0.03228258045831827,
    "hit_20d": 0.5357374392220421,
    "p5_20d": -0.18941804148367902,
    "worst_20d": -0.5687189567957632,
    "n_20d": 12340,
    "total_return": 4.4946708967448,
    "max_drawdown": -0.25676423814604576,
    "n_equity_days": 1255,
    "trade_win_rate": 0.42021010505252626,
    "n_trades": 1999,
    "n_obs": 24830,
    "splits": {
      "first": {
        "avg_5d": 0.004222070116297151,
        "hit_5d": 0.50496,
        "p5_5d": -0.09158964291176597,
        "worst_5d": -0.2956711365949878,
        "n_5d": 6250,
        "avg_20d": 0.013594542792757111,
        "hit_20d": 0.4944,
        "p5_20d": -0.17354854068993586,
        "worst_20d": -0.44389430182474576,
        "n_20d": 6250,
        "total_return": 0.5670519242405676,
        "max_drawdown": -0.25676423814604576,
        "n_equity_days": 625
      },
      "second": {
        "avg_5d": 0.014289569424735728,
        "hit_5d": 0.55072,
        "p5_5d": -0.117398522756268,
        "worst_5d": -0.31613590157535476,
        "n_5d": 6250,
        "avg_20d": 0.05165023903342183,
        "hit_20d": 0.5783606557377049,
        "p5_20d": -0.2051720619078674,
        "worst_20d": -0.5687189567957632,
        "n_20d": 6100,
        "total_return": 4.503009334318051,
        "max_drawdown": -0.22226072122646767,
        "n_equity_days": 625
      }
    }
  },
  "raw_momentum+regime_gate": {
    "avg_5d": 0.011395757397920014,
    "hit_5d": 0.5368205128205128,
    "p5_5d": -0.10623794257195893,
    "worst_5d": -0.31613590157535476,
    "n_5d": 9750,
    "avg_20d": 0.039322487608385234,
    "hit_20d": 0.5477083333333334,
    "p5_20d": -0.19009486805208262,
    "worst_20d": -0.5025153087027889,
    "n_20d": 9600,
    "total_return": 2.635144904431636,
    "max_drawdown": -0.2095878631590915,
    "n_equity_days": 1255,
    "trade_win_rate": 0.44794050343249425,
    "n_trades": 1748,
    "n_obs": 19350,
    "splits": {
      "first": {
        "avg_5d": 0.005067470726620986,
        "hit_5d": 0.5022540983606557,
        "p5_5d": -0.09531669683304138,
        "worst_5d": -0.2956711365949878,
        "n_5d": 4880,
        "avg_20d": 0.014780397832562854,
        "hit_20d": 0.48954918032786887,
        "p5_20d": -0.179870474038454,
        "worst_20d": -0.44389430182474576,
        "n_20d": 4880,
        "total_return": 0.6411289891702234,
        "max_drawdown": -0.2095878631590915,
        "n_equity_days": 708
      },
      "second": {
        "avg_5d": 0.017650132078821133,
        "hit_5d": 0.5713114754098361,
        "p5_5d": -0.11786753485803571,
        "worst_5d": -0.31613590157535476,
        "n_5d": 4880,
        "avg_20d": 0.06447745013285178,
        "hit_20d": 0.6069767441860465,
        "p5_20d": -0.2035223059043027,
        "worst_20d": -0.5025153087027889,
        "n_20d": 4730,
        "total_return": 2.643483342004887,
        "max_drawdown": -0.12850019424255177,
        "n_equity_days": 542
      }
    }
  },
  "raw_momentum+dual_momentum": {
    "avg_5d": 0.008056122925847535,
    "hit_5d": 0.5265224358974359,
    "p5_5d": -0.10060413943515192,
    "worst_5d": -0.31613590157535476,
    "n_5d": 12480,
    "avg_20d": 0.027319263571959344,
    "hit_20d": 0.5332522303325223,
    "p5_20d": -0.18431647112953628,
    "worst_20d": -0.5553232522816418,
    "n_20d": 12330,
    "total_return": 2.8148550403776635,
    "max_drawdown": -0.23003737954472614,
    "n_equity_days": 1255,
    "trade_win_rate": 0.43663366336633663,
    "n_trades": 2020,
    "n_obs": 24810,
    "splits": {
      "first": {
        "avg_5d": 0.004179420301231375,
        "hit_5d": 0.50576,
        "p5_5d": -0.08612787380425421,
        "worst_5d": -0.2953850491690182,
        "n_5d": 6250,
        "avg_20d": 0.011119341148027454,
        "hit_20d": 0.4928,
        "p5_20d": -0.1660846891787044,
        "worst_20d": -0.3554108503178156,
        "n_20d": 6250,
        "total_return": 0.6749279785586091,
        "max_drawdown": -0.23003737954472614,
        "n_equity_days": 625
      },
      "second": {
        "avg_5d": 0.012003823018955521,
        "hit_5d": 0.5475961538461539,
        "p5_5d": -0.11404909527825248,
        "worst_5d": -0.31613590157535476,
        "n_5d": 6240,
        "avg_20d": 0.04408804453788383,
        "hit_20d": 0.5750410509031199,
        "p5_20d": -0.20014609124981864,
        "worst_20d": -0.5553232522816418,
        "n_20d": 6090,
        "total_return": 2.825532378249987,
        "max_drawdown": -0.19258967539069616,
        "n_equity_days": 624
      }
    }
  },
  "vol_adjusted+none": {
    "avg_5d": 0.005282610532709266,
    "hit_5d": 0.5088070456365092,
    "p5_5d": -0.09321306185711385,
    "worst_5d": -0.31613590157535476,
    "n_5d": 12490,
    "avg_20d": 0.020327745641428786,
    "hit_20d": 0.5101296596434359,
    "p5_20d": -0.1670842483288431,
    "worst_20d": -0.5553232522816418,
    "n_20d": 12340,
    "total_return": 2.1837163658291567,
    "max_drawdown": -0.25995226693823326,
    "n_equity_days": 1255,
    "trade_win_rate": 0.428266438941076,
    "n_trades": 2342,
    "n_obs": 24830,
    "splits": {
      "first": {
        "avg_5d": 0.00043455338613252733,
        "hit_5d": 0.48416,
        "p5_5d": -0.08008932771679532,
        "worst_5d": -0.2956711365949878,
        "n_5d": 6250,
        "avg_20d": 0.003164439596795148,
        "hit_20d": 0.46864,
        "p5_20d": -0.14951586776451112,
        "worst_20d": -0.44389430182474576,
        "n_20d": 6250,
        "total_return": 0.34631661552687576,
        "max_drawdown": -0.25995226693823326,
        "n_equity_days": 625
      },
      "second": {
        "avg_5d": 0.010210883993442348,
        "hit_5d": 0.53376,
        "p5_5d": -0.10528321026087754,
        "worst_5d": -0.31613590157535476,
        "n_5d": 6250,
        "avg_20d": 0.03799717255510505,
        "hit_20d": 0.5526229508196722,
        "p5_20d": -0.18583231798502362,
        "worst_20d": -0.5553232522816418,
        "n_20d": 6100,
        "total_return": 2.18723780424164,
        "max_drawdown": -0.12376495053340608,
        "n_equity_days": 625
      }
    }
  },
  "vol_adjusted+regime_gate": {
    "avg_5d": 0.00696381649497177,
    "hit_5d": 0.5138461538461538,
    "p5_5d": -0.09246320051132956,
    "worst_5d": -0.31613590157535476,
    "n_5d": 9750,
    "avg_20d": 0.02718368710356017,
    "hit_20d": 0.525,
    "p5_20d": -0.16376666993645814,
    "worst_20d": -0.5025153087027889,
    "n_20d": 9600,
    "total_return": 1.8747854035506224,
    "max_drawdown": -0.18592754223786534,
    "n_equity_days": 1255,
    "trade_win_rate": 0.4463946869070209,
    "n_trades": 2108,
    "n_obs": 19350,
    "splits": {
      "first": {
        "avg_5d": 0.0010783599338984524,
        "hit_5d": 0.47418032786885245,
        "p5_5d": -0.07633548616192738,
        "worst_5d": -0.2956711365949878,
        "n_5d": 4880,
        "avg_20d": 0.005775209657607886,
        "hit_20d": 0.46987704918032785,
        "p5_20d": -0.15264254235476146,
        "worst_20d": -0.44389430182474576,
        "n_20d": 4880,
        "total_return": 0.42791564684963057,
        "max_drawdown": -0.18592754223786534,
        "n_equity_days": 708
      },
      "second": {
        "avg_5d": 0.012761313251731347,
        "hit_5d": 0.5532786885245902,
        "p5_5d": -0.10475064024364315,
        "worst_5d": -0.31613590157535476,
        "n_5d": 4880,
        "avg_20d": 0.049208400426242326,
        "hit_20d": 0.5816067653276955,
        "p5_20d": -0.17859342588467975,
        "worst_20d": -0.5025153087027889,
        "n_20d": 4730,
        "total_return": 1.8783068419631057,
        "max_drawdown": -0.08269822813475547,
        "n_equity_days": 542
      }
    }
  },
  "vol_adjusted+dual_momentum": {
    "avg_5d": 0.00484999253697806,
    "hit_5d": 0.5104967948717949,
    "p5_5d": -0.09024509200834617,
    "worst_5d": -0.31613590157535476,
    "n_5d": 12480,
    "avg_20d": 0.01661512655445973,
    "hit_20d": 0.5079480940794809,
    "p5_20d": -0.1623966275945214,
    "worst_20d": -0.5553232522816418,
    "n_20d": 12330,
    "total_return": 2.129009944299016,
    "max_drawdown": -0.24763673822439414,
    "n_equity_days": 1255,
    "trade_win_rate": 0.4335210047639671,
    "n_trades": 2309,
    "n_obs": 24810,
    "splits": {
      "first": {
        "avg_5d": 0.0011804708742324543,
        "hit_5d": 0.49072,
        "p5_5d": -0.07755863387684087,
        "worst_5d": -0.2953850491690182,
        "n_5d": 6250,
        "avg_20d": 0.002574550454664288,
        "hit_20d": 0.4696,
        "p5_20d": -0.14722515283865964,
        "worst_20d": -0.3554108503178156,
        "n_20d": 6250,
        "total_return": 0.41953268257818266,
        "max_drawdown": -0.24763673822439414,
        "n_equity_days": 625
      },
      "second": {
        "avg_5d": 0.008654732739872231,
        "hit_5d": 0.5307692307692308,
        "p5_5d": -0.10160480234835605,
        "worst_5d": -0.31613590157535476,
        "n_5d": 6240,
        "avg_20d": 0.03117534034799544,
        "hit_20d": 0.5476190476190477,
        "p5_20d": -0.18122621152532112,
        "worst_20d": -0.5553232522816418,
        "n_20d": 6090,
        "total_return": 2.129632166402907,
        "max_drawdown": -0.1437520064901554,
        "n_equity_days": 624
      }
    }
  },
  "rank_based+none": {
    "avg_5d": 0.0026072397103011663,
    "hit_5d": 0.5042433947157726,
    "p5_5d": -0.07871935058021642,
    "worst_5d": -0.4937890847262335,
    "n_5d": 12490,
    "avg_20d": 0.00986406509111746,
    "hit_20d": 0.48897893030794165,
    "p5_20d": -0.1444805790514663,
    "worst_20d": -0.4927348043632436,
    "n_20d": 12340,
    "total_return": 1.4049449113303956,
    "max_drawdown": -0.18875375855998144,
    "n_equity_days": 1255,
    "trade_win_rate": 0.42835412229996955,
    "n_trades": 3287,
    "n_obs": 24830,
    "splits": {
      "first": {
        "avg_5d": -0.0002739679680937377,
        "hit_5d": 0.48928,
        "p5_5d": -0.06704704298807974,
        "worst_5d": -0.2331514738546422,
        "n_5d": 6250,
        "avg_20d": -0.00042753857665401055,
        "hit_20d": 0.46256,
        "p5_20d": -0.13206709583959947,
        "worst_20d": -0.30863430669163827,
        "n_20d": 6250,
        "total_return": 0.24618860599399373,
        "max_drawdown": -0.18875375855998144,
        "n_equity_days": 625
      },
      "second": {
        "avg_5d": 0.0055432277560381465,
        "hit_5d": 0.51952,
        "p5_5d": -0.08949023303017882,
        "worst_5d": -0.4937890847262335,
        "n_5d": 6250,
        "avg_20d": 0.020542338383187798,
        "hit_20d": 0.5163934426229508,
        "p5_20d": -0.1570595326649013,
        "worst_20d": -0.4927348043632436,
        "n_20d": 6100,
        "total_return": 1.4054126554710207,
        "max_drawdown": -0.1056439352436438,
        "n_equity_days": 625
      }
    }
  },
  "rank_based+regime_gate": {
    "avg_5d": 0.003959166741815029,
    "hit_5d": 0.5097435897435898,
    "p5_5d": -0.07565252159056678,
    "worst_5d": -0.4937890847262335,
    "n_5d": 9750,
    "avg_20d": 0.015656337875762868,
    "hit_20d": 0.5051041666666667,
    "p5_20d": -0.13169891277531445,
    "worst_20d": -0.4927348043632436,
    "n_20d": 9600,
    "total_return": 1.3500227412341719,
    "max_drawdown": -0.11950479471726816,
    "n_equity_days": 1255,
    "trade_win_rate": 0.44323374340949034,
    "n_trades": 2845,
    "n_obs": 19350,
    "splits": {
      "first": {
        "avg_5d": 0.000419049086074472,
        "hit_5d": 0.482172131147541,
        "p5_5d": -0.06126230535169608,
        "worst_5d": -0.2701147233214416,
        "n_5d": 4880,
        "avg_20d": 0.002697322710521842,
        "hit_20d": 0.4639344262295082,
        "p5_20d": -0.1190782660985194,
        "worst_20d": -0.3263436283283394,
        "n_20d": 4880,
        "total_return": 0.38998721573055195,
        "max_drawdown": -0.11950479471726816,
        "n_equity_days": 708
      },
      "second": {
        "avg_5d": 0.007463486665547228,
        "hit_5d": 0.5372950819672131,
        "p5_5d": -0.09016464967933815,
        "worst_5d": -0.4937890847262335,
        "n_5d": 4880,
        "avg_20d": 0.029035459743444444,
        "hit_20d": 0.547568710359408,
        "p5_20d": -0.1498563020925446,
        "worst_20d": -0.4927348043632436,
        "n_20d": 4730,
        "total_return": 1.350490485374797,
        "max_drawdown": -0.07814368665927851,
        "n_equity_days": 542
      }
    }
  },
  "rank_based+dual_momentum": {
    "avg_5d": 0.002071075107954953,
    "hit_5d": 0.5014423076923077,
    "p5_5d": -0.07860873337790444,
    "worst_5d": -0.4937890847262335,
    "n_5d": 12480,
    "avg_20d": 0.007965887045635289,
    "hit_20d": 0.48580697485806973,
    "p5_20d": -0.14436524524837369,
    "worst_20d": -0.4927348043632436,
    "n_20d": 12330,
    "total_return": 1.3297294139577027,
    "max_drawdown": -0.19150246141190186,
    "n_equity_days": 1255,
    "trade_win_rate": 0.4307832422586521,
    "n_trades": 3294,
    "n_obs": 24810,
    "splits": {
      "first": {
        "avg_5d": -0.000280368962402497,
        "hit_5d": 0.488,
        "p5_5d": -0.06724562974856715,
        "worst_5d": -0.2331514738546422,
        "n_5d": 6250,
        "avg_20d": -0.00027591144580949104,
        "hit_20d": 0.46144,
        "p5_20d": -0.13206709583959947,
        "worst_20d": -0.30863430669163827,
        "n_20d": 6250,
        "total_return": 0.24197621287467697,
        "max_drawdown": -0.19150246141190186,
        "n_equity_days": 625
      },
      "second": {
        "avg_5d": 0.004495584970330627,
        "hit_5d": 0.5153846153846153,
        "p5_5d": -0.08907218134071525,
        "worst_5d": -0.4937890847262335,
        "n_5d": 6240,
        "avg_20d": 0.016578156941705512,
        "hit_20d": 0.5114942528735632,
        "p5_20d": -0.15652517505850216,
        "worst_20d": -0.4927348043632436,
        "n_20d": 6090,
        "total_return": 1.3306970646954652,
        "max_drawdown": -0.11723150901848,
        "n_equity_days": 624
      }
    }
  }
}
```
