# PM Take-Profit OCO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let valid PM execution intents maintain a broker-side GTC OCO (take-profit limit plus protective stop) for an existing Alpaca paper position, re-affirmed on every execute pass.

**Architecture:** Add a position-level take-profit field and a pure `ProtectionIntent` derivation shared by the binding gate and execute pass. Extend broker backends with nested-aware protection and exit-fill primitives, then reconcile current protection after the open-window batch has settled. Existing pre-open disarm, stop sweep, card outcomes, and fallback behavior consume those primitives so an OCO is never mistaken for an unprotected position.

**Tech Stack:** Python 3, Pydantic v2, alpaca-py trading requests, pytest, Ruff.

## Global Constraints

- Never modify `tradingagents/`; only project-owned modules may extend its behavior.
- Alpaca paper trading only; IBKR additions are inactive stubs that raise `NotImplementedError`.
- All tests are hermetic: no network, LLM, broker, or production artifacts.
- Use America/New_York date/time semantics already provided by `daily_run.ET`.
- Preserve no-naked-position and no-double-sell invariants; a failed OCO transition must attach a plain GTC stop.
- No new configuration key: behavior remains gated by existing `pm_execution`.
- Gate with `pytest -q` and `uvx ruff check pm_execution.py decisions.py binding_gate.py daily_run.py alpaca_broker.py ibkr.py tests/test_pm_execution.py tests/test_decisions.py tests/test_binding_gate.py tests/test_alpaca.py tests/test_daily_run.py`.

---

### Task 1: Model and pure protection derivation

**Files:**
- Modify: `pm_execution.py`
- Modify: `decisions.py`
- Test: `tests/test_pm_execution.py`
- Test: `tests/test_decisions.py`

**Interfaces:**
- Produces: `ExecutionIntent.take_profit_px: float | None` validated as positive when supplied.
- Produces: `ProtectionIntent(ticker: str, target_px: float, stop_px: float)`.
- Produces: `protection_from_execution(execution, *, ticker, holdings, last_close, stop_loss_pct, stop_px_band_pct, standing_stop_px=None) -> tuple[ProtectionIntent | None, list[str]]`.

- [ ] **Step 1: Write failing schema and pure-function tests**

```python
def test_execution_intent_rejects_non_positive_take_profit():
    with pytest.raises(ValidationError, match="take_profit_px"):
        ExecutionIntent(take_profit_px=0)

def test_protection_uses_sell_stop_then_standing_then_default():
    intent = ExecutionIntent(take_profit_px=120, orders=[
        PmOrder(kind="SELL", shares=1, stop_px=91)])
    protection, reasons = protection_from_execution(
        intent, ticker="AAPL", holdings={"AAPL": 4},
        last_close={"AAPL": 100}, stop_loss_pct=8,
        stop_px_band_pct=(3, 25), standing_stop_px=89)
    assert protection == ProtectionIntent("AAPL", 120.0, 91.0)
    assert reasons == []
```

- [ ] **Step 2: Run the focused tests and verify they fail because the field/function is absent**

Run: `pytest tests/test_pm_execution.py tests/test_decisions.py -q`

- [ ] **Step 3: Add the Pydantic field, frozen protection dataclass, and minimal derivation**

```python
@dataclass(frozen=True)
class ProtectionIntent:
    ticker: str
    target_px: float
    stop_px: float

def protection_from_execution(...):
    if execution.take_profit_px is None:
        return None, []
    # reject unheld, target <= close, and stop >= target - .01;
    # select an existing SELL stop, otherwise standing, otherwise default.
```

- [ ] **Step 4: Add table tests for unheld targets, target at/below close, OCO threshold, clamped sell-stop, standing-stop reuse, and default stop**

```python
assert reasons == ["AAPL: take-profit target must exceed reference close"]
assert protection.stop_px == 92.0
```

- [ ] **Step 5: Run focused tests and commit**

Run: `pytest tests/test_pm_execution.py tests/test_decisions.py -q`

Commit: `git add pm_execution.py decisions.py tests/test_pm_execution.py tests/test_decisions.py && git commit -m "feat: derive PM OCO protection intents"`

### Task 2: Validate take-profit binding in the gate and execution binding

**Files:**
- Modify: `binding_gate.py`
- Modify: `daily_run.py`
- Test: `tests/test_binding_gate.py`
- Test: `tests/test_daily_run.py`

**Interfaces:**
- Consumes: `protection_from_execution` and gate holdings/reference-close snapshot.
- Produces: per-ticker gate `legacy` decision with a precise target validation reason.
- Produces: gate preview records containing `take_profit_px` and `oco_stop_px`.
- Produces: execute-time `protection_intents: dict[str, ProtectionIntent]` for only bound, revalidated blocks.

- [ ] **Step 1: Write failing gate tests for invalid target fallback and observability**

```python
result = evaluate(cfg, tmp_path, day, holdings={"AAPL": 3},
                  last_close={"AAPL": 100})
assert result["per_ticker"]["AAPL"]["status"] == "legacy"
assert "reference close" in result["per_ticker"]["AAPL"]["reason"]
assert result["preview"][0]["take_profit_px"] == 99.0
```

- [ ] **Step 2: Run focused tests and verify the new artifact fields/guard fail**

Run: `pytest tests/test_binding_gate.py tests/test_daily_run.py -q`

- [ ] **Step 3: Call the pure validator after `orders_from_execution` in `binding_gate.evaluate`**

```python
protection, protection_reasons = protection_from_execution(...)
if protection_reasons:
    per_ticker[ticker] = {"status": "legacy", "reason": protection_reasons[0]}
    continue
preview.append({..., "take_profit_px": intent.take_profit_px,
                "oco_stop_px": protection.stop_px if protection else None})
```

- [ ] **Step 4: Re-derive protection after execute-time binding and only retain it for a successfully bound ticker**

```python
if protection_reasons:
    logger.warning("%s: %s; legacy path", ticker, protection_reasons[0])
    continue
protection_intents[ticker] = protection
```

- [ ] **Step 5: Run focused tests and commit**

Run: `pytest tests/test_binding_gate.py tests/test_daily_run.py -q`

Commit: `git add binding_gate.py daily_run.py tests/test_binding_gate.py tests/test_daily_run.py && git commit -m "feat: validate take-profit intents at binding"`

### Task 3: Add nested-aware broker protection primitives

**Files:**
- Modify: `alpaca_broker.py`
- Modify: `ibkr.py`
- Test: `tests/test_alpaca.py`
- Test: `tests/test_ibkr.py`

**Interfaces:**
- Produces: `AlpacaBroker.get_resting_protection() -> dict[str, list[dict]]`, each row `{kind, order_id, qty, stop_price, target_price?}`.
- Produces: `cancel_protection(order_id) -> None`, `cancel_protection_for(tickers) -> dict[str, list[dict]]`, `place_stop(symbol, qty, stop_px) -> bool`, and `place_oco(symbol, qty, stop_px, target_px) -> str`.
- Produces: `get_filled_exit_orders(since, until) -> list[dict]`, where `kind` is `STOP` or `TP`.
- IBKR exposes all three new primitives as explicit `NotImplementedError` stubs.

- [ ] **Step 1: Write failing nested-order parsing and OCO request tests**

```python
resting = b.get_resting_protection()
assert resting["MSFT"] == [{"kind": "oco", "order_id": "parent-1",
                             "qty": 1, "target_price": 450.0,
                             "stop_price": 400.0}]
request = mock_client.submit_order.call_args[0][0]
assert request.order_class == OrderClass.OCO
assert request.take_profit.limit_price == 450.0
assert request.stop_loss.stop_price == 400.0
```

- [ ] **Step 2: Run focused tests and verify absent broker methods fail**

Run: `pytest tests/test_alpaca.py tests/test_ibkr.py -q`

- [ ] **Step 3: Implement nested order parsing and guarded OCO placement**

```python
request = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500)
# Parse standalone GTC STOPs and OCO LIMIT parents whose legs include their STOP.
# In place_stop/place_oco, query the live position for every one of three
# attempts.  The former submits StopOrderRequest; the latter submits
# LimitOrderRequest(... order_class=OrderClass.OCO, time_in_force=GTC).
```

- [ ] **Step 4: Add tests for standalone/multiple protections, parent cancellation, position-lag retry, and nested TP/stop exit fills**

```python
assert fills == [{"symbol": "F", "kind": "TP", "qty": 1,
                  "avg_price": 13.99, "filled_at": inside.isoformat()}]
assert any(fill["kind"] == "STOP" for fill in fills)
```

- [ ] **Step 5: Rename the old stop-fill method at all call sites, retain no obsolete behavior, then run focused tests and commit**

Run: `pytest tests/test_alpaca.py tests/test_ibkr.py -q`

Commit: `git add alpaca_broker.py ibkr.py tests/test_alpaca.py tests/test_ibkr.py && git commit -m "feat: add nested OCO broker protection primitives"`

### Task 4: Make the sweep, exit disarm, and outcome path OCO-aware

**Files:**
- Modify: `daily_run.py`
- Modify: `alpaca_broker.py`
- Test: `tests/test_daily_run.py`
- Test: `tests/test_alpaca.py`

**Interfaces:**
- Consumes: `get_resting_protection`, `cancel_protection_for`, and `get_filled_exit_orders`.
- Produces: stop sweep that treats a valid OCO as protection; disarm returns its stop-leg level; outcome rows use `SELL(STOP)` or `SELL(TP)` and source `broker-exit`.

- [ ] **Step 1: Write failing tests proving a resting OCO prevents a second sweep stop and is cancelled before an open SELL**

```python
broker.get_resting_protection.return_value = {"AAPL": [{"kind": "oco", ...}]}
daily_run._ensure_stop_sweep(cfg)
broker._client.submit_order.assert_not_called()

cancelled = b.cancel_stops_for(["MSFT"])
assert cancelled["MSFT"][0]["stop_price"] == 400.0
```

- [ ] **Step 2: Run focused tests and verify current flat-order scan misses the OCO**

Run: `pytest tests/test_daily_run.py tests/test_alpaca.py -q`

- [ ] **Step 3: Replace direct Alpaca client scans with broker primitives**

```python
resting = broker.get_resting_protection()
protected_tickers = set(resting)
# `cancel_protection_for` cancels an OCO parent, not its invisible leg, and
# returns the stop-leg price for _anchor_sell_remainders.
```

- [ ] **Step 4: Update outcome reconciliation to classify TP separately and add an idempotence test**

```python
actual.append({"action": f"SELL({fill['kind']})", ...,
               "source": "broker-exit"})
```

- [ ] **Step 5: Run focused tests and commit**

Run: `pytest tests/test_daily_run.py tests/test_alpaca.py -q`

Commit: `git add daily_run.py alpaca_broker.py tests/test_daily_run.py tests/test_alpaca.py && git commit -m "feat: make broker safety seams OCO-aware"`

### Task 5: Reconcile post-batch protection and persist observability

**Files:**
- Modify: `daily_run.py`
- Test: `tests/test_daily_run.py`
- Modify: `decision_cards.py` only if its outcome renderer needs a new protection field
- Test: `tests/test_decision_cards.py` only if the renderer is changed

**Interfaces:**
- Produces: `_reconcile_protection(cfg, broker, intents) -> list[dict]` protection rows with actions `oco_set`, `oco_keep`, `oco_replace`, `downgrade`, `cancel_unheld`, or `stop_fallback`.
- Consumes: final broker positions, nested protection snapshot, `cancel_protection`, `place_oco`, and `place_stop`.
- Produces: executed logs and execution-card outcomes that contain `protection` data.

- [ ] **Step 1: Write failing decision-table tests using a specific fake broker**

```python
rows = _reconcile_protection(cfg, broker, {"AAPL": ProtectionIntent("AAPL", 120, 92)})
assert rows == [{"ticker": "AAPL", "action": "oco_keep", "qty": 3,
                 "target_px": 120.0, "stop_px": 92.0}]
```

- [ ] **Step 2: Run focused tests and verify the reconciler does not yet exist**

Run: `pytest tests/test_daily_run.py -q`

- [ ] **Step 3: Implement the fail-safe decision table**

```python
# Never mutate after a failed snapshot.  For a replace/downgrade, cancel first.
# If OCO placement fails after cancellation, submit the plain stop at the known
# stop level and emit stop_fallback.  Cancel all protection for a flat symbol.
```

- [ ] **Step 4: Invoke reconciliation after `place_market_orders` and before final executed-log/outcome writes**

```python
protection_rows = _reconcile_protection(cfg, broker, protection_intents)
log["protection"] = protection_rows
outcomes = _execution_outcomes(..., protection_rows=protection_rows)
```

- [ ] **Step 5: Add tests for replace, downgrade, unheld cancellation, failed query (no mutation), failed OCO fallback, and card/log content**

```python
assert broker.cancel_protection.call_count == 1
assert broker.place_oco.assert_not_called()
assert rows[0]["action"] == "downgrade"
```

- [ ] **Step 6: Run focused tests and commit**

Run: `pytest tests/test_daily_run.py tests/test_decision_cards.py -q`

Commit: `git add daily_run.py decision_cards.py tests/test_daily_run.py tests/test_decision_cards.py && git commit -m "feat: reconcile PM OCO take-profit protection"`

### Task 6: Disclosure, documentation, and mechanical verification

**Files:**
- Modify: `daily_run.py`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/specs/2026-09-11-pm-take-profit-oco-design.md`
- Test: relevant existing test files only when disclosure behavior is already covered

- [ ] **Step 1: Add the PM contract disclosure for held-only above-market daily re-affirmed targets**

```text
`take_profit_px` is a held-position-only GTC target. Re-emit it every day
to keep it; it must be above current/reference price and at least $0.01 above stop.
```

- [ ] **Step 2: Update operational documentation with live feature status and nested-order safety gotchas**

- [ ] **Step 3: Run all tests in isolated artifact paths**

Run: `TRADINGAGENTS_RESULTS_DIR=$(mktemp -d) TRADINGAGENTS_MEMORY_LOG_PATH=$(mktemp) STRUCTURED_LOG_DIR=$(mktemp -d) pytest -q`

- [ ] **Step 4: Run Ruff on every changed Python file**

Run: `uvx ruff check pm_execution.py decisions.py binding_gate.py daily_run.py alpaca_broker.py ibkr.py tests/test_pm_execution.py tests/test_decisions.py tests/test_binding_gate.py tests/test_alpaca.py tests/test_daily_run.py`

- [ ] **Step 5: Inspect the diff, confirm no framework files changed, and commit**

Run: `git diff --check && git diff --stat && git status --short`

Commit: `git add AGENTS.md docs/superpowers/specs/2026-09-11-pm-take-profit-oco-design.md docs/superpowers/plans/2026-09-11-pm-take-profit-oco.md daily_run.py && git commit -m "docs: record PM OCO take-profit rollout"`
