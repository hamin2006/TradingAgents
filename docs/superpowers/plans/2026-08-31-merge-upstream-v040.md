# Merge Upstream v0.4.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge TauricResearch v0.4.0 (16 commits, `a33fd4c..2448d0a`) into our fork with zero behavioral regressions, verified locally and on the PC.

**Architecture:** Framework-only merge. Git analysis is definitive: single merge base `a33fd4c`; our 24 files are pure additions (4,344 insertions, zero modifications to existing files); upstream's 20 top-level + ~35 framework files are modifications to files we never touched. `git merge-tree --write-tree` → exit 0, 0 conflicts. Our runtime patches all use `*args, **kwargs` passthrough so upstream's new params (reddit `start_date`/`end_date`, memory `resolution_date`) are tolerated.

**Tech Stack:** git merge, pytest, ruff, PC deploy (expect + ssh).

## Global Constraints

- **Never modify anything under `tradingagents/`** — framework is consumed as a library. All conflicts/fallout handled in our own modules only.
- Gate: `pytest -q` fully green + `uvx ruff check <files>` (line-length 100, rules E/W/F/I/B/UP/C4/SIM).
- Conventional commits, push to `origin/main`, deploy = `git pull` on the PC.
- All verification on PC uses `expect ~/.config/opencode/skills/pc-dev/scripts/pc_ssh.exp` + `PC_PASSWORD`.
- Execution is inline (user choice) on `main` (repo convention; user consent given).

---

### Task 1: Perform the merge

**Files:** none (git operation only)

- [ ] **Step 1: Create the merge commit**

```bash
git merge upstream/main --no-ff -m "merge: upstream v0.4.0 (framework-only; 16 commits, clean by merge-tree)"
```

Expected: clean, 0 conflicts. If a surprise conflict appears, resolve keeping our side for anything outside `tradingagents/`, upstream's side inside `tradingagents/` — never hand-edit a conflict inside the framework.

- [ ] **Step 2: Verify merge state**

```bash
git log --oneline -2          # expect the merge commit on top of HEAD
git status --short            # clean
git diff HEAD^1 HEAD --stat -- tradingagents/ | tail -5   # framework deltas landed
```

---

### Task 2: Local gates — full suite + ruff

**Files:** possible fixes in `daily_run.py`, `reddit_auth.py`, `analyze_results.py`, `tests/` (only if fallout found)

- [ ] **Step 1: Run the gates**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -1
uvx ruff check . 2>&1 | tail -1
```

Expected: ~700+ tests green (upstream's new tests included). Failure categories:
1. Upstream test vs. upstream code mismatch — do not fix framework code; investigate, record, defer.
2. Our module/test vs. changed framework surface — fix in our module/test, keeping behavior.

- [ ] **Step 2: Commit any fallout fixes separately** (only if failures occurred)

```bash
git add -A && git commit -m "fix: adapt to upstream v0.4.0 (<what changed>)"
```

---

### Task 3: TDD — REVIEW rating no-op + resolved-tag tolerance

**Files:**
- Modify: `tests/test_decisions.py`
- Modify: `tests/test_analyze_results.py` (only if Task 2's suite doesn't already cover `resolved:` tags)

Upstream now emits `REVIEW` when the model's output has no recognizable rating. Our `compute_orders` treats it as no-op (∉ BUY/SELL sets) — the desired safe default, but untested. New memory tags append optional `resolved:YYYY-MM-DD` — analytics must tolerate it.

- [ ] **Step 1: Write the failing tests**

```python
def test_review_rating_is_noop():
    orders = compute_orders(
        {"AMZN": "REVIEW"}, {}, {"AMZN": 100.0}, 100_000, 10,
        entry_protection_pct=5.0)
    assert orders == []

def test_review_rating_on_held_position_keeps_position():
    orders = compute_orders(
        {"AMZN": "REVIEW"}, {"AMZN": 10}, {"AMZN": 100.0}, 100_000, 10)
    assert orders == []  # no SELL, no re-buy
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_decisions.py -q`
Expected: the two new tests fail. If they already pass, delete them — the behavior is already correct.

- [ ] **Step 3: Verify `analyze_results.py` against a `resolved:`-tagged memory entry**

Run: `.venv/bin/python -m pytest tests/test_analyze_results.py -q` plus upstream's `test_memory_log.py`.
If our analytics fail on the new tag: fix `analyze_results.py` to ignore/parse the trailing field.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "test: REVIEW rating is a no-op; resolved-tag tolerance"
```

---

### Task 4: Push and deploy to the PC

- [ ] **Step 1: Push + pull on PC**

```bash
git push -q origin main && \
export PC_PASSWORD="2006" && \
expect ~/.config/opencode/skills/pc-dev/scripts/pc_ssh.exp 'cd /home/harsh-amin/workplace/TradingAgents && git pull -q && git log --oneline -1'
```

- [ ] **Step 2: Full suite on the PC** (backgrounded with `pytest-timeout`, poll — the PC is slow)

```bash
expect ... 'cd /home/harsh-amin/workplace/TradingAgents && nohup .venv/bin/python -m pytest -q --timeout=30 > /tmp/pc_pytest.log 2>&1 &'
```

Expected: same green count as local.

- [ ] **Step 3: Healthcheck**

```bash
expect ... 'cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python daily_run.py --healthcheck'
```

Expected: broker reachable, config loads (watchlist.yaml intact, pins = Fireworks/StreamLake).

---

### Task 5: Live smoke — one-ticker analysis on the PC

**Why:** the merge rewrites `trading_graph.py` (checkpoint/resume refactor, `_fetch_returns` 4-tuple) and `capabilities.py` (DeepSeek OpenRouter-namespaced capability detection — directly affects our pinned `deepseek/*` models). The 07:00 run tomorrow must not be the first exercise of this path.

- [ ] **Step 1: Run a single-ticker analyze**

```bash
expect ... 'cd /home/harsh-amin/workplace/TradingAgents && .venv/bin/python daily_run.py --analyze --ticker AAPL'
```

(flag name TBD from `daily_run.py --help`; if no per-ticker flag exists, run `--analyze` against the current ratings file.) Expected: pipeline completes, ratings JSON written, memory-log entry created.

- [ ] **Step 2: If the smoke fails** — invoke `superpowers:systematic-debugging` before any fix; fixes land in our modules only.

- [ ] **Step 3: Commit any smoke-driven fixes + push + pull on PC**

---

### Task 6: Documentation + max_tokens adoption

**User amendment:** we won't use `max_tokens`, but adopt the key into our config surface so future upstream merges stay clean.

- [ ] **Step 1: Add `max_tokens` to `watchlist.yaml`** (unused — tracks upstream config surface)

```yaml
max_tokens: null            # unused (tracks upstream config surface for clean merges)
```

- [ ] **Step 2: Update `tests/test_config.py::test_shipped_watchlist_yaml_loads`** to assert `cfg["max_tokens"] is None`.

- [ ] **Step 3: Update `AGENTS.md`** — one line under "Current state": framework base now upstream v0.4.0 (memory `resolved:` tags, `RATING_REVIEW` no-op, `max_tokens` passthrough available).

- [ ] **Step 4: Commit docs + push + pull on PC.**

---

**User decisions:** max_tokens = add to config surface but leave unused; smoke test = yes (one ticker); execution = inline.