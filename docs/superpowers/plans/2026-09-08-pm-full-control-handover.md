# PM Full Control — Handover Plan (2026-09-08)

> Objective: the Portfolio Manager gets **full, reliable control of the
> holdings** — every rating must translate into an executed, protected,
> fully-completed order (full or partial, buy or sell) — with the binding
> gate, outcome events, and the reconciliation safety nets making every
> divergence between *decided* and *done* visible and self-healing.

This document is the handover: current state, the problem inventory with
code references, the fix package, the rollout sequence, and the
operational runbook. A fresh session should be able to continue from
§3 (implementation) without re-deriving anything.

---

## 1. Context

The system is a daily paper-trading pipeline: screener (04:10 ET) →
parallel multi-agent analysis (04:30 ET) → binding gate (08:00 ET) →
execution (09:00 ET, submits at the 09:30 open) → power-off (10:00 ET),
on an Ubuntu PC (`pc` SSH alias, America/Edmonton local = ET+2).

The PM already emits a structured `execution` block per ticker (observe
phase, live since 2026-09-04): `orders` (kind/shares/limit_px/stop_px/
value_usd/fraction_held/cap_value_usd), `invalidation_px` (advisory),
`future_notes`. A binding engine (`decisions.orders_from_execution`,
`decisions.py:97-185`) converts blocks into broker orders with
guardrails: buy sizing + protection ceiling, sell floor limits,
never-shorts, remainder-stop re-anchoring, band clamps. **The engine is
not the problem** — it handles full exits, partial trims, and adds
correctly and is replay-verified.

The problem is everything *around* the engine: the PM's orders are
discarded wholesale, the PM doesn't know the block's contract, and
paper-engine fill behavior breaks completion. Detail in §2.

## 2. Current state (end of 2026-09-08)

### 2.1 Deployed code (origin/main = `55683ff`, PC pulled and green)

| Commit | What |
|---|---|
| `9ba7eca` | PM execution-capture binding fix (schema swap + capture through the PM's own import) |
| `751ea67` | Gate: empty-orders on a HELD Buy/OW ticker = maintain, not failure |
| `cfe8989` | **Sell-remainder reconciliation**: `_anchor_sell_remainders` anchors EVERY sell on a held ticker (legacy full exits included; original cancelled stop, else −8%); submit-failed sells re-anchor too, guarded by the real position query |
| `55683ff` | **Execution-outcome events**: `run_execute` appends an `execution_outcome` per analyzed ticker (gate verdict, binding state, PM block vs actual fills, remaining shares, anchored stop); `render_prior_decisions` injects them so the PM reads *decided → executed → book*; `backfill_cards --outcomes` for historical days |

### 2.2 Live broker state (Alpaca paper, verified 2026-09-08 evening)

- Positions: DELL 1 (stop 482.21), DXCM 9 (82.53), HPE 12 (50.08,
  re-anchored), MSFT 1 (469.31) — cash $7,426.77. REGN exited @ 801.71,
  DASH exited @ 201.51 (manual completion), NOW stopped out @ 133.87
  (first live stop fire — protection worked).
- Every held position has exactly one resting GTC stop.

### 2.3 What happened 2026-09-08 (the motivating day)

Gate verdict FAIL ("DELL/SNDK: empty execution orders on a Buy/OW rating
with no position held") → binding disabled for the WHOLE day → legacy
tier engine ran: HPE's valid 2+2 trim was discarded and a full exit
(SELL 13) was submitted instead; the paper engine filled 1/13 @ 52.75,
shed the remainder, and left 12 shares with a disarmed stop (naked —
manually remediated; code fixed in `cfe8989`). DASH's exit never filled
across two rounds (also manually completed). ZBRA's entry never filled.

### 2.4 Infra changes this session (already deployed)

- **Plug control from anywhere**: `kasa_cloud.py` rewritten — python-kasa
  LAN discovery first, TP-Link V2 cloud fallback (`tplink-cloud-api`
  v5.2.1, `include_tapo=True` — this HS103 lives on TP-Link's unified
  Tapo cloud). Works off the home network; `KASA_MODE=cloud` skips the
  ~4s LAN probe. Old LAN-only shim renamed `tplinkcloud_lan_shim.bak`.
- **Labor Day handled**: no holiday calendar exists (cron is Mon–Fri
  day-of-week only; `power_schedule.py` skips Sat/Sun only), so the PC
  was manually armed for Tuesday and skipped Monday entirely. The gap is
  real and recurs on every market holiday — deferred fix, see §6.

## 3. The problem: why PM executions are incomplete

Six layers, ordered by impact. 1–3 explain "the PM's decisions never
ran"; 4–5 explain "the PM's orders didn't complete"; 6 is a policy gap.

### 3.1 Binding has never engaged in production

`pm_execution` is OFF in `watchlist.yaml` (observe phase), and on 9/8
the gate artifact was `FAIL`, so even with the switch on, binding would
have been disabled. `run_execute` (`daily_run.py`, "PM execution binding
is fail-closed") requires config AND a PASS artifact. Net effect: every
PM execution block so far has been *recorded, never executed*.

### 3.2 The gate is all-or-nothing per day

`binding_gate.evaluate` collects per-ticker failures but writes ONE
day-level verdict; `run_execute` keys binding off that single verdict.
One bad ticker (empty-on-unheld-Buy) discards ~19 good blocks.
`run_execute` already falls back **per ticker** for invalid/absent
blocks — the gate just doesn't feed it per-ticker data.

### 3.3 The PM doesn't know the execution-block contract

`_ensure_pm_execution_schema` (`daily_run.py:1208`) swaps the
`PortfolioDecision` schema to the execution-bearing subclass but injects
**zero prompt guidance** about semantics. The PM has to guess what an
empty `orders` list means. DELL/SNDK's empty blocks on unheld Buy/OW
ratings — the exact "silent inaction" class — are the predictable result
of that guess, and they are what fails the gate.

### 3.4 Paper-engine fill latency breaks completion

Alpaca paper fills land 30–70s after submission normally; on 9/8
(post-holiday congestion) even market orders hadn't filled after 120s +
grace requeries + one retry round. A bound order that times out is
cancelled (ZBRA buy never entered; DASH exit never sold). The deadline
exists because the execute process must finish before the 10:00 ET
power-off and every irreversible step (stop disarm → sell → stop attach)
is kept tight against the open.

### 3.5 Partial fills are shed, never resumed

A sell that partially fills has its remainder cancelled ("partial shed"
— keeps holdings and stop quantities consistent) and, since `cfe8989`,
the remainder is stop-protected. But the PM's stated quantity is never
*completed*: HPE's intended exit ended at 1/13, and the position rode to
the next morning on a stop instead of the exit the PM ordered.

### 3.6 Policy gap: invalid blocks fall back to the legacy engine

An unparseable block today → legacy tier path → **the system trades
without the PM deciding**. That contradicts "PM full control" but is the
spec's fail-safe. Decision needed (§5, D2).

## 4. The solution

Five changes. A–D are code; E is the config switch that hands over
control. TDD throughout (repo gate: `pytest -q` green + `uvx ruff check`
clean, line-length 100).

### A. Per-ticker fail-closed gate (unblocks binding)

`binding_gate.evaluate` already computes per-ticker statuses
(`counts.valid / invalid / empty_on_buy / engine_fallback` + `preview`).
Change:

1. Gate artifact gains `"per_ticker": {ticker: {"status": "bind" |
   "legacy", "reason": str|null}}` alongside the day verdict (day
   verdict stays PASS/FAIL for observability).
2. `run_execute` binds per ticker: block present + valid + gate says
   bind → `orders_from_execution`; anything else → that ticker's legacy
   tier orders (current behavior for invalid/absent — this part already
   exists in `run_execute`).

Effect: DELL/SNDK go legacy on 9/8-class days; HPE's trim binds the same
day. **Watch the interaction**: legacy fallback for an unheld Buy/OW
*buys* the name the PM said nothing about — that is intentional only
until D2 is decided (§5).

Files: `binding_gate.py`, `daily_run.py`, `tests/test_binding_gate.py`,
`tests/test_daily_run.py`.

### B. PM prompt contract disclosure (kills the empty-block class)

When `_ensure_pm_execution_schema` installs the schema, append a
deterministic block to the PM prompt (same pattern as the FRED-alias
disclosure, `_ensure_fred_aliases`). Contract text (verbatim proposal):

- You hold positions per the "Portfolio context (ground truth)" block.
  Every ticker you rate MUST carry an `execution` block.
- `orders: []` on a ticker you HOLD is a valid maintain decision.
- `orders: []` on a ticker you DON'T hold but rate Buy/Overweight is an
  ENGINE FAILURE: either size an entry (shares/value_usd) or reconsider
  the rating. The gate halts binding for the day on it.
- Full exit = `shares` equal to the held quantity. Partial trim = fewer
  shares (or `fraction_held`); set `stop_px` for what remains.
- `limit_px` on a SELL is a floor (day-expiry if never reached); buys
  get a +2% protection ceiling automatically.
- Orders are day-expiry, fill at/after the 09:30 open; `stop_px` rides
  as a broker-side GTC stop for remainder/fill protection.

Files: `daily_run.py` (inside the schema installer), tests in
`tests/test_daily_run.py` (assert the disclosure text is appended to the
PM prompt and idempotent).

### C. Sell-resume completion (finishes the PM's stated quantity)

In `alpaca_broker` finalize (after the retry round resolves): a SELL
whose filled < intended gets **one bounded resume** — re-submit the
remaining quantity as a market order after re-querying the real position
(guards: position must still be ≥ remaining; skip when fill_unknown or
when a stop was just attached). Then the existing reconciliation
re-anchor applies to whatever still remains. Rationale: completing an
exit is the system's own invariant ("exits are never paused"); a missed
trim is completed rather than left to tomorrow's re-decision.

Buys get NO resume: a missed entry is recoverable next morning, chasing
fills risks overpaying through a gap; the outcome event makes the miss
visible and the PM re-decides with fresh data.

Files: `alpaca_broker.py` (`_place_batch` finalize + a
`_resume_sell_remainder` helper), `tests/test_alpaca.py`.

### D. Stop sweep at analyze start (no-naked invariant, other side)

In the analyze pass (installer chain), before the batch runs: query the
broker for holdings + resting GTC stops; any held ticker with NO stop
gets one at `last_close * (1 - stop_loss_pct/100)`. Covers
fill-unknown races (cancel-vs-fill left a filled buy unstopped), manual
trades, and any future hole. Must be failure-safe (broker down → skip
with a warning, never block analyze) and idempotent.

Files: `daily_run.py` (new installer `_ensure_stop_sweep`, run in the
analyze chain before workers start), `tests/test_daily_run.py`.

### E. Flip `pm_execution: true` (the handover of control)

After A–D land and one observe day confirms block quality: set
`pm_execution: true` in `watchlist.yaml` on the PC, deploy, and watch
the 09:08-class outcome events. The gate stays as the compliance layer;
per-ticker verdicts keep one bad block from poisoning the day.

## 5. Open policy decisions (need the user)

- **D2 — invalid block fallback.** Keep legacy tier path for unparseable
  blocks (fail-safe, but trades without PM intent) or go strict (no
  action + loud log + `engine_fallback` outcome)? Recommendation: keep
  legacy for now, measure `engine_fallback` frequency via outcome
  events, revisit with data.
- **Buy resume** — deliberately excluded (see C). Revisit only if
  outcome events show missed entries are material.
- **Floor-limit sells** — a PM floor that the open never reaches
  day-expires by design. Under full control the PM must understand this
  (prompt contract B covers it). No code change.

## 6. Deferred / known gaps (do not build unprompted)

- **Holiday calendar**: cron + `power_schedule.py` are weekday-only;
  every market holiday needs the manual arm-and-skip dance (done for
  Labor Day 2026-09-07). Proper fix: exchange-calendar check in the
  cron commands or in `run_analyze`/`run_execute` entry.
- **OTO/bracket retest**: paper engine broke OTO (verified 2/2 on
  08-31/09-01); if fixed server-side, buys could queue self-protected
  and the two-step attach simplifies. Low priority.
- Full problem/solution history lives in
  `docs/superpowers/specs/2026-09-04-pm-execution-and-thesis-cards-design.md`
  (amended) and this file.

## 7. Rollout sequence

1. Build A (per-ticker gate) — TDD; unit + replay tests.
2. Build B (prompt disclosure) — TDD; assert prompt text.
3. Build C (sell resume) — TDD; paper-account-safe fakes only.
4. Build D (stop sweep) — TDD.
5. Commit (`feat:` per change or one cohesive `feat: pm full control
   phase`), push `origin/main`, pull on PC.
6. Observe 1–2 trading days with `pm_execution` still OFF: check block
   quality via outcome events + gate per-ticker statuses (expect the
   empty-on-unheld class to disappear after B).
7. Flip `pm_execution: true` on the PC's `watchlist.yaml`, deploy,
   verify first bound day end-to-end (orders = blocks, stops attached,
   outcome events match).
8. Run `backfill_cards --outcomes` (idempotent) after each bound day to
   keep the card history complete.

## 8. Operational runbook

- **PC access**: `export PC_PASSWORD="2006";
  expect ~/.config/opencode/skills/pc-dev/scripts/pc_ssh.exp '<cmd>'`.
  Long jobs: `setsid bash -c '...' </dev/null >/dev/null 2>&1 &`
  (plain `&` dies with the SSH session).
- **Power**: plug control from any network —
  `source ~/.zshrc; KASA_MODE=cloud python3 ~/.config/opencode/skills/
  pc-dev/scripts/ensure_power.py --device "PC Plug"` (cycles off/on if
  already on — NEVER cycle while a run is in flight; ext4 corruption).
  Shutdown + next-weekday arm: `power_schedule.py --shutdown` (repo
  root). BIOS clears the RTC alarm on ANY boot; the `@reboot` cron
  re-arms. PC is left ON as of 2026-09-08 evening (user's call) — the
  Wed 02:05 arm cron will set the Thursday alarm.
- **Cron** (host-local MDT = ET+2): 02:05 arm · 02:10 screen · 02:25
  healthcheck · 02:30 analyze · 06:00 binding gate · 07:00 execute ·
  08:00 power off · `@reboot` arm. All `cd` to the repo first.
- **Artifacts** (`~/.tradingagents/logs/`): `ratings_*.json` (schema v2
  + `execution` map), `executed_*.json`, `binding_gate_*.json`,
  `structured/{date}/{ticker}.jsonl` + `summary.json`,
  `decision_cards/{TICKER}.jsonl` (cards + `execution_outcome` events),
  `trading_memory.md`, `logs/*.log` (cron output; INFO dropped —
  WARNING+ only).
- **Kill switch**: `DISABLE_TRADING` file at repo root = analysis-only.
- **Dev/verification isolation**: set `TRADINGAGENTS_RESULTS_DIR`,
  `TRADINGAGENTS_MEMORY_LOG_PATH`, `STRUCTURED_LOG_DIR` to scratch dirs.
- **Broker checks** (PC): `.venv/bin/python` with
  `dotenv.load_dotenv(".env")` then `from alpaca_broker import
  AlpacaBroker` (env vars are `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`).
  Mac needs `SSL_CERT_FILE=$(python3 -c "import certifi;
  print(certifi.where())")`.
- **Tests**: `.venv/bin/python -m pytest -q`; `uvx ruff check <files>`.
  Never modify `tradingagents/` — runtime patches only.
- **Model cap**: `max_tokens: 15000` bounds deepseek-v4-flash runaway
  output (upstream #1204) — do not remove.

## 9. Verification checklist for the first bound day

- [ ] Gate artifact has `per_ticker` statuses; day verdict present.
- [ ] Orders submitted == blocks marked bind (compare executed log vs
      ratings `execution` map).
- [ ] Every sell-bound ticker's stop state is explained in its outcome
      event (no naked positions — reconciliation + sweep).
- [ ] `execution_outcome` events exist for every analyzed ticker.
- [ ] No `engine_fallback` without a matching gate reason.
- [ ] Fill timeouts, if any, show as `filled < shares` with the resume/
      re-anchor trail in the outcome note.
