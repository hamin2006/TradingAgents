# Screening Methods More Robust than Pure Price-Momentum Ranking

**Research note for the daily paper-trading pipeline (TradingAgents)**
Date: 2026-08-31 · Data constraint: FREE only (yfinance OHLCV + fundamentals, FRED) · Runtime budget: ~10 min/day, small host ·
**Job of the screen: candidate GENERATION (high recall of good setups).** Final selection happens downstream in the LLM multi-agent step, so the screen should demote crash-prone names rather than try to be a precise selector.

---

## 0. TL;DR — Ranked Shortlist

| # | Upgrade | One-liner | Impact | Complexity | New data |
|---|---------|-----------|--------|------------|----------|
| 1 | **Vol-adjusted (Sharpe-style) momentum core** | Rank by `return / realized_vol` instead of raw return | **High** | Low (~10 LOC) | None |
| 2 | **Index-level regime gate** | SPY vs 200-day SMA × VIX percentile → CALM/WARN/STRESS states for candidate generation | **High** | Low (~15 LOC) | SPY + VIX (2 fetches) |
| 3 | **Rank-based composite + winsorization** | Percentile ranks (optionally sector-neutral) replace z-scores; winsorize tails | Medium | Low (~10 LOC) | None |
| 4 | **Absolute-momentum (dual-momentum) gate** | Only surface candidates whose own 6m/12m return beats cash (Antonacci) | Medium | Low (~8 LOC) | ^IRX or FRED DTB3 |
| 5 | **Quality / anti-lottery overlay** | Penalize MAX (biggest single-day gain) + optional earnings-quality floor | Medium | Medium | yfinance fundamentals |

**Best value: Upgrade 1** (vol-adjusted momentum core) — see §4.

---

## 1. Why the Current Screen Breaks in Bad Regimes

Current composite: z-scores of 1m/3m/6m trailing returns + price-vs-50d-SMA spread + proximity to 52-week high, after a dollar-volume ≥ $10M liquidity filter. Top-ranked names go to LLM analysis.

Two structural fragilities:

1. **Cross-sectional fragility.** Raw-return z-scores put lottery-like, high-volatility names at the top of the ranking. These are exactly the names that mean-revert hardest in reversals and post-squeeze collapses.
2. **Timing fragility.** The screen has no view of the market state. It generates the same kind of candidates the day after a crash bottom (when momentum is about to crash) as in a calm uptrend.

The literature says both are fixable cheaply.

---

## 2. Evidence Base

### 2.1 Momentum crashes happen in predictable "panic states" (Daniel & Moskowitz 2016)
> "These momentum crashes are partly forecastable. They occur in what we term 'panic' states — following market declines and when market volatility is high, and are contemporaneous with market 'rebounds.'" Past losers carry option-like payoffs that are expensive in these states, so the winner-minus-loser momentum trade collapses exactly when the market rebounds off a decline. [1][2][3]

**Practical consequences for our screen:**
- Crash risk is highest when (a) the index is in/near a drawdown, (b) market vol is elevated, (c) the first violent rebound days occur. All three are observable daily with free data.
- The paper's own defenses are the two primitives behind Upgrades 1 and 2: condition momentum exposure on the market state, and scale it by expected/realized volatility (dynamic momentum).

### 2.2 Volatility-managed momentum nearly eliminates crashes (Barroso & Santa-Clara 2015)
> "We find that the risk of momentum is highly variable over time and predictable. Managing this risk virtually eliminates crashes and nearly doubles the Sharpe ratio" — by scaling the winners-minus-losers position each period to a constant target volatility using realized variance from daily returns. [4][5]

**Consequence:** dividing momentum by realized volatility (a Sharpe-style momentum term) is the single most evidence-backed robustness upgrade available. It works continuously — no on/off switch, no candidate drought.

### 2.3 The durable part of momentum is the intermediate horizon (Novy-Marx 2012)
> "Momentum is primarily driven by firms' performance twelve to seven months prior to portfolio formation, not by a tendency of rising and falling stocks to keep rising and falling." Strategies based on intermediate (12→7 month) past performance are more profitable and robust, especially among large liquid stocks (the paper reports intermediate-horizon momentum ~1.20%/month vs ~0.67%/month for recent 2→6 month performance). [6][7]

**Consequence:** the 1m term in our composite is the most fragile ingredient — it carries the recent "lottery" component and the short-term reversal. Down-weight it and/or add a 12→7m intermediate term.

### 2.4 Relative momentum alone has no downside protection — absolute momentum does (Antonacci, dual momentum)
Dual momentum combines **relative momentum** (cross-sectional: pick the strongest vs peers) with **absolute momentum** (time-series: is the asset's own 12-month return beating T-bills?). Antonacci's GEM system holds the relative winner *only if* it passes the absolute test vs cash; otherwise it goes to cash. Relative momentum "does nothing to reduce volatility and tail risk/worst drawdown"; the absolute filter is the crash guard. [8][9][10]

**Consequence:** an absolute-momentum gate at the index level (suppress candidates when SPY's 12m return < cash) and/or ticker level (require candidate's own 6m/12m return > cash) is a cheap guard that is orthogonal to the ranking itself.

### 2.5 Index-level regime filters work and are cheap (Faber-lineage trend filters, VIX bands, breadth)
- **200-day SMA / 10-month MA trend filter:** the canonical simplest regime gate; in a 2,700-backtest comparison across SPY/QQQ/BTC through four bear markets, plain SMA filters delivered strong drawdown protection for roughly ~2 points/year of foregone return ("insurance premium"), and worked across markets; fancier MAs (TEMA/HMA/KAMA) whipsawed; "price near 52-week high" filters were blind to fast V-shaped crashes (caught only 7% of COVID). [11][12]
- **VIX bands (practitioner rule of thumb):** Low < 15 (~35% of days), Normal 15–20 (~30%), Elevated 20–30 (~25%), Crisis > 30 (~10%). [13]
- **Breadth:** % of index members above their 200-day SMA below ~40% → bear regime → halve exposure. Nearly free in our pipeline since every member's SMAs are already computed. [14]
- **Binary volatility-regime gating of momentum** (full momentum exposure in calm regimes, zero/cash in turbulent regimes) has been shown to improve momentum performance. [15]

### 2.6 Factor hygiene: winsorize → rank → (optionally) neutralize
The standard cross-sectional factor pipeline is: winsorize extreme values → z-score or **rank-normalize** → optionally neutralize against unwanted exposures (e.g., sector) → aggregate. Rank transforms are robust to fat tails; raw z-scores are not. Winsorization "limits extreme values to reduce the effect of possibly spurious outliers." [16][17]

### 2.7 The lottery/MAX effect: extreme single-day gains predict underperformance
Bali, Cakici & Whitelaw (2011): stocks with the highest maximum daily return over the past month (MAX) earn significantly *lower* future returns; the low-MAX minus high-MAX decile spread exceeds 1%/month — driven by lottery-demand. This is the empirical signature of the "post-squeeze mean reversion" set that hurts a pure momentum screen. [18][19]

---

## 3. The Shortlist — Concrete Implementation Sketches

All formulas assume a daily OHLCV panel for the ~500 liquid names (already fetched) plus two new daily fetches max. Pseudo-code is pandas-style.

### 3.1 Upgrade 1 — Vol-adjusted (Sharpe-style) momentum core ⭐ BEST VALUE

**What:** Replace every raw-return term in the momentum composite with that return divided by the stock's realized volatility. Rank calm grinders above violent movers with the same trailing return.

**Why it reduces regime fragility:**
- Crash-prone names are systematically high-vol, lottery-like winners (§2.1, §2.7). Dividing by vol demotes them *in every regime*, not just detected crash regimes — no threshold cliff.
- Direct implementation of volatility-managed momentum, which "virtually eliminates crashes and nearly doubles the Sharpe ratio" in Barroso–Santa-Clara (§2.2). It is simultaneously the simplest momentum × low-volatility blend practitioners use.

**Implementation sketch:**
```python
# per ticker, from the daily close series (already fetched)
r_L     = close[-1] / close[-L] - 1                       # L in {21, 63, 126} (1m/3m/6m)
vol_i   = daily_log_ret[-126:].std() * np.sqrt(252)       # annualized realized vol, fixed window
m_L     = r_L / max(vol_i, 0.10)                          # vol floor 10% ann.: stops r/vol
                                                          # exploding for ultra-low-vol names

# composite: swap z(raw return) -> z(m_L) or rank(m_L); keep SMA/52w-high factors as-is
score =  z(m_21) + z(m_63) + z(m_126) + z(sma50_spread) + z(high_52w_prox)

# optional refinement (Novy-Marx, §2.3): add an intermediate-horizon term and cut 1m weight
r_12_7 = close[-126] / close[-252] - 1                    # 12→7 month "echo" momentum
score += 0.5 * z(r_12_7)                                  # and reduce the m_21 weight to ≤0.15
```

**Parameters:** vol window 126d (any of 63–252d behaves similarly — parameterization-robust); vol floor 0.10 (sensible range 0.05–0.15).

**Expected impact: High** — attacks the exact crash mechanism, evidence-backed. **Complexity: Low** — ~10 LOC, zero new data, no state to manage.

---

### 3.2 Upgrade 2 — Index-level Regime Gate (SPY × VIX × optional breadth)

**What:** A daily market-state label that scales or pauses candidate generation: CALM / WARN / STRESS.

**Why it reduces regime fragility:** Daniel–Moskowitz panic states are precisely "index in/near a drawdown + high market vol + rebound" (§2.1); a 200-day SMA trend filter plus a VIX percentile observes that state directly and cheaply (§2.5). The screen's job is recall, but in a panic state *recall of momentum setups is the problem* — suppressing them is the correct behavior, and the LLM downstream never sees the most mean-reversion-prone names.

**Implementation sketch:**
```python
spy   = yf.download("^GSPC", period="15mo")               # or "SPY"
vix   = yf.download("^VIX",  period="13mo")               # alt: FRED series VIXCLS [20]

above    = spy.Close.iloc[-1] > spy.Close.rolling(200).mean().iloc[-1]
vix_pct  = (vix.Close.iloc[-1] >= vix.Close[-252:]).mean() # trailing-1y percentile of VIX

# optional breadth check — nearly free, the 500 SMA200s are already computed upstream
pct_above_200 = (panel.close > panel.sma200).mean()        # marketclutch: <0.40 ⇒ bear [14]

if not above or pct_above_200 < 0.40:   REGIME = "STRESS"  # 2 consecutive days to confirm
elif vix_pct >= 0.80:                   REGIME = "WARN"    # (see whipsaw note below)
else:                                   REGIME = "CALM"

# actions
# CALM   -> normal: top N candidates as today
# WARN   -> N candidates but EXCLUDE the top decile of 1m-momentum names (post-squeeze set)
# STRESS -> pause new buy candidates (or only pass names that also clear §3.4 + §3.5 gates)
```

**Whipsaw control:** require 2 consecutive closes below the SMA200 before flipping to STRESS (cheaper and more stable than adding an SMA band — the 2,700-backtest study found band dials "added wobble, not safety" [11]).

**Expected impact: High.** **Complexity: Low** — ~15–20 LOC + 2 extra daily fetches.

---

### 3.3 Upgrade 3 — Rank-Based Composite + Winsorization (replace z-scores)

**What:** Convert each factor to a cross-sectional percentile rank (0–1) after winsorizing tails, then take a weighted sum. Optionally rank within sectors (sector-neutral momentum) and/or average across lookbacks (lookback ensemble).

**Why it reduces regime fragility:** z-scores explode when a factor's cross-section has fat tails — a post-squeeze name with +60% in 1 month gets z ≈ +5–6 and dominates the entire composite, which is exactly how squeeze names occupy the top of the ranking. Ranks bound every factor to [0,1] and are invariant to outliers; this is the standard factor-construction hygiene (§2.6). Sector-neutral ranks additionally prevent one hot sector from filling the whole candidate list during rotations.

**Implementation sketch:**
```python
def cs_score(x):                       # winsorize -> percentile rank
    lo, hi = x.quantile([0.05, 0.95])
    return x.clip(lo, hi).rank(pct=True)

score = (0.20 * cs_score(m_21)          # momentum terms: use §3.1's vol-adjusted returns
       + 0.25 * cs_score(m_63)
       + 0.30 * cs_score(m_126)
       + 0.15 * cs_score(sma50_spread)
       + 0.10 * cs_score(high_52w_prox))

# optional sector-neutrality (yfinance .info["sector"]): rank within sector groups;
# fall back to whole-universe rank when sector is missing
# optional ensemble: single term = mean of cs_score(m_21), cs_score(m_63), cs_score(m_126)
# optional stability gate: candidate only if rank in top N today AND top 1.5N five sessions ago
```

**Expected impact: Medium.** **Complexity: Low.**

---

### 3.4 Upgrade 4 — Absolute-Momentum (Dual-Momentum) Gate

**What:** Antonacci's absolute-momentum test, applied (a) at index level and (b) per candidate: only surface names whose own trailing return beats cash.

**Why it reduces regime fragility:** Relative (cross-sectional) momentum has no tail protection on its own; the absolute filter is the component that keeps you out of broken stocks and broken markets (§2.4). The index-level version is GEM's "go to cash" logic transplanted into candidate generation: after a 12-month drawdown regime, stop generating buy candidates entirely. The ticker-level version catches single-name breakdowns even inside an index uptrend (complements §3.2, does not duplicate it).

**Implementation sketch:**
```python
irx   = yf.download("^IRX", period="5d").Close.iloc[-1]   # 13-week T-bill yield, %
      # alt: FRED series DTB3 [21]
c_12m = irx / 100                                          # ~annualized cash return

# (a) index gate — if SPY fails absolute momentum, suppress new buys (GEM analog)
spy_r12 = spy.Close.iloc[-1] / spy.Close.iloc[-252] - 1
if spy_r12 < c_12m: return []                              # go to cash

# (b) ticker gate — candidate must beat cash over 12m and be positive over 6m
universe = [t for t in liquid
            if (close[t][-1]/close[t][-252] - 1) >= c_12m
            and (close[t][-1]/close[t][-126]  - 1) > 0]
```

**Expected impact: Medium.** **Complexity: Low** — ~8 LOC + 1 fetch. (Note: this gate deliberately reduces candidate flow after major drawdowns; given the LLM step downstream and the cash alternative, that is the intended behavior.)

---

### 3.5 Upgrade 5 — Quality / Anti-Lottery Overlay

**What:** (a) Penalize the MAX factor — each stock's largest single-day gain over the last month; (b) optional earnings-quality floor from yfinance fundamentals.

**Why it reduces regime fragility:** High-MAX stocks (lottery demand) significantly underperform (~1%/month low-MAX decile spread, §2.7) — and the top-MAX names are precisely the short-squeeze/post-surge cohort the current screen keeps surfacing. An earnings floor (positive EPS / above-median ROE, the Novy-Marx profitability idea) removes the low-quality melt-up subset of momentum winners.

**Implementation sketch:**
```python
MAX_i   = daily_ret_i[-21:].max()                          # biggest single-day gain, 1m
score_i = score_i - 0.5 * cs_rank(MAX_i)                   # penalty weight 0.25–1.0, tune

# cheap heuristic alternative (no extra data): hard-exclude the blow-off signature
#   exclude if z(m_21) > +3 AND z(m_126) < +1              # recent spike, mediocre trend

# optional quality floor (NaN-tolerant — never hard-fail on missing fundamentals):
info  = yf.Ticker(t).info
ok_q  = (info.get("trailingEps") or 0) > 0                 # or ROE >= cross-sectional median
if info has any of the fields and not ok_q: drop candidate
```

**Expected impact: Medium** (largest benefit concentrated in squeeze-heavy regimes). **Complexity: Medium** — yfinance fundamental fields are patchy; must be NaN-tolerant to preserve recall.

---

## 4. Recommended Best-Value Upgrade

**Upgrade 1 — vol-adjusted (Sharpe-style) momentum core**, followed closely by Upgrade 2 as the natural second step.

Rationale (robustness per line of code):
1. **Smallest diff, biggest mechanism fix.** It changes one expression per lookback term (`r → r/vol`), ~10 LOC, zero new data sources, no pipeline state. The regime gate (U2) is nearly as cheap but introduces on/off behavior; U1 changes nothing structural.
2. **Continuous, not binary.** It demotes lottery names *every day*, including the ambiguous days where a threshold gate flickers. There is no threshold cliff and no candidate drought: the screen still returns N names daily.
3. **Strongest evidence.** Volatility-managed momentum "virtually eliminates crashes and nearly doubles the Sharpe ratio" (Barroso–Santa-Clara); it is simultaneously the simplest momentum × low-volatility factor blend; Novy-Marx's intermediate-horizon refinement slots into the same change.
4. **Parameterization-robust.** Any vol window in 63–252d and floor in 5–15% produces similar orderings — no fragile "best setting" to overfit.

Recommended rollout order: **U1 → U3 → U2 → U4 → U5.** U1+U3 reshape *who* is a candidate; U2+U4 reshape *when* candidates flow; U5 is the finishing overlay once the others are stable. Log the REGIME label every day so later per-regime performance attribution is possible.

**Evaluation plan (paper-trading):** track 21-day forward returns and LLM-Buy hit-rate of generated candidates *by regime*; compare top-decile composition (realized vol, MAX, sector concentration) before/after each change; verify candidate count stays within a sane band (e.g., 5–15 names/day) so the LLM budget is stable.

---

## 5. Runtime & Data Notes

- New fetches per day: `^GSPC` (or `SPY`) + `^VIX` + `^IRX` — 3 tiny calls; all scoring is vectorized pandas on the existing 500-ticker panel. Well inside the ~10 min/day budget.
- Free-data sources: yfinance OHLCV + `.info` fundamentals; FRED `VIXCLS` (VIX daily close) [20] and `DTB3` (3-month T-bill) [21] as robust fallbacks if Yahoo's index endpoints misbehave.
- No alternative data, no paid feeds required for any upgrade.

---

## 6. Sources

1. Daniel, K. & Moskowitz, T., **"Momentum Crashes"** (JFE 2016) — NBER working paper page: https://www.nber.org/papers/w20439 · SSRN abstract: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2371227 · Author PDF: https://www.kentdaniel.net/papers/published/mom12.pdf (panic-state predictability; market-state and volatility-conditioned defenses)
2. NBER w20439 full PDF: https://www.nber.org/system/files/working_papers/w20439/w20439.pdf
3. NYU Stern conference PDF (same findings): https://www.stern.nyu.edu/sites/default/files/assets/documents/con_038332.pdf
4. Barroso, P. & Santa-Clara, P., **"Momentum has its moments"** (JFE 2015) — SSRN abstract: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2041429 (realized-variance scaling to constant target vol; "virtually eliminates crashes and nearly doubles the Sharpe ratio")
5. Practitioner summary with backtest: https://paperswithbacktest.com/strategies/momentum-has-its-moments
6. Novy-Marx, R., **"Is momentum really momentum?"** (JFE 2012) — https://www.sciencedirect.com/science/article/pii/S0304405X11001152 · https://ideas.repec.org/a/eee/jfinec/v103y2012i3p429-453.html (intermediate 12→7m horizon drives momentum; recent returns weaker/fragile)
7. Summary with per-horizon figures (~1.20%/mo intermediate vs ~0.67%/mo recent): https://alex30free.github.io/swedish-compound-momentum/research.html
8. Antonacci, G., **Dual Momentum** — overview: https://www.quantifiedstrategies.com/dual-momentum-trading-strategy/ · https://hedgeyourown.com/learn/dual-momentum (relative + absolute momentum; absolute = 12m vs T-bills, else cash)
9. Antonacci site, "Dual, Relative & Absolute Momentum": https://www.optimalmomentum.com/dual-relative-absolute-momentum/ (relative momentum alone "does nothing to reduce volatility and tail risk/worst drawdown" — page returned 403 to our fetcher; quote from indexed excerpt)
10. GEM mechanics (12-month lookback, SPY/ACWX/AGG vs T-bill hurdle): https://www.backtestedstrategies.com/strategies/dual-momentum-backtest/
11. SetupAlpha, **"I Tested 20 Trend-Based Regime Filters"** (2,700+ backtests, SPY/QQQ/BTC): https://setup4alpha.substack.com/p/i-tested-20-trend-based-regime-filters (SMA-200-class filters ≈ best cost-adjusted protection, ~2 pts/yr premium; bands add wobble; new-high filters blind to V-crashes)
12. Alpha Architect trend-filter series (10-month PMA indicator, MA filter comparisons): https://medium.com/@alphaarchitect/trend-following-filters-part-6-c7f51c5ff4ac · https://alphaarchitect.com/trend-following-filters-part-7/
13. Volatility regimes / VIX bands (Low <15, Normal 15–20, Elevated 20–30, Crisis >30, with frequencies): https://volatilitybox.com/research/volatility-regimes-explained/
14. Breadth regime rule (% of index above 200-day SMA < 40% ⇒ bear, halve exposure): https://marketclutch.com/systematic-alpha-the-architecture-of-rules-based-momentum/
15. Binary volatility-regime gate applied to momentum (full exposure calm / zero turbulent): https://harbourfrontquant.substack.com/p/improving-momentum-with-a-volatility
16. Portfolio Optimizer, factor standardization guide (winsorize → z-score/rank → neutralize pipeline): https://silviobaratto.github.io/optimizer/guide/factors/
17. Winsorization definition: https://en.wikipedia.org/wiki/Winsorizing
18. Bali, T., Cakici, N. & Whitelaw, R., **"Maxing out: Stocks as lotteries…"** (JFE 2011) — https://www.sciencedirect.com/science/article/abs/pii/S0304405X1000190X (MAX negatively predicts returns; low–high MAX decile spread > 1%/month) · author PDF: https://pages.stern.nyu.edu/~rwhitela/papers/max%20jfe.pdf
19. MAX effect primer: https://blankcapitalresearch.com/learn/bali-cakici-whitelaw-max
20. FRED — CBOE Volatility Index, daily close (`VIXCLS`): https://fred.stlouisfed.org/series/VIXCLS
21. FRED — 3-Month Treasury Bill, daily (`DTB3`): https://fred.stlouisfed.org/series/DTB3
