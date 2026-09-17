# PM-Only Max Reasoning Effort via OpenRouter (2026-09-17)

Status: **Design proposed 2026-09-17** (short spec — two-module change; implement
via TDD from here). Resolves a gap flagged as NOT-implementable in the
2026-09-16 deepseek-v4.1-flash migration (AGENTS.md), after finding a real
mechanism during a live capability check.

## 1. Problem (and the correction to the earlier "not possible" call)

The 2026-09-16 migration entry stated the PM's requested "max thinking level"
could not be wired without touching the frozen `tradingagents/` package,
citing two blockers:

1. `trading_graph._get_provider_kwargs` never branches on `provider ==
   "openrouter"`, so no `reasoning` kwarg is ever built.
2. `openai_client._PASSTHROUGH_KWARGS` is a hardcoded allowlist with no
   `reasoning` slot, so an injected kwarg would be silently dropped.

Both statements are true, but the conclusion ("not patchable without
touching `tradingagents/`") was wrong. Live verification 2026-09-17:

- `langchain_openai.chat_models.base.BaseChatOpenAI` (LangChain's own
  library, not this project's frozen package) carries a genuine `extra_body:
  Mapping[str, Any] | None` pydantic field, documented as "the recommended
  way to pass custom parameters that are specific to your OpenAI-compatible
  API provider but not part of the standard OpenAI API" — exactly OpenRouter's
  unified `reasoning: {"effort": ...}` request-body field.
- `_PASSTHROUGH_KWARGS` is a **plain module-level tuple** in
  `openai_client.py` — the same category of frozen-but-patchable data
  structure as `capabilities._BY_ID` (already extended at runtime by
  `_ensure_deepseek_v41_capabilities`). Appending `"extra_body"` to the tuple
  at runtime is a data patch, not a code edit, and is consistent with the
  hard constraint the same way the capabilities patch is.
- Live test through this project's actual `create_llm_client` path: with the
  tuple patched, `extra_body={"reasoning": {"effort": "max"}}` reached the
  constructed `ChatOpenAI` instance (`llm.extra_body` confirmed set) and
  produced a measurable, real behavior change against the live OpenRouter
  endpoint — a two-effort-level test on a multi-step word problem showed
  `low` effort spending 60 reasoning tokens vs. `max` effort spending 161
  (both landed on the correct answer; the point is the knob visibly moves
  spend, not that the easy question needed it).

**The remaining, still-real problem**: `TradingAgentsGraph.__init__`
constructs exactly ONE `deep_thinking_llm` object and passes the *same*
instance into both `create_research_manager(...)` and
`create_portfolio_manager(...)` (`graph/setup.py`). A config-level
`extra_body` override on `deep_think_llm` would raise effort for the
Research Manager too — the user asked for the PM specifically.

## 2. Decisions

1. **Patch #1 — allowlist extension (`daily_run._ensure_openrouter_reasoning_passthrough`)**:
   idempotent, appends `"extra_body"` to `openai_client._PASSTHROUGH_KWARGS`
   exactly once (guard against duplicate appends across repeated installer
   calls). `_reset_*` restores the original tuple. This alone does nothing
   until a caller actually passes `extra_body`; it only removes the
   allowlist block.
2. **Patch #2 — PM-only effort injection, not a config-level kwarg.**
   Rather than setting `extra_body` at LLM-construction time (which reaches
   both PM and Research Manager, since they share one instance), wrap the
   **already-constructed** `create_portfolio_manager`-returned node closure
   (`daily_run._ensure_pm_max_reasoning_effort`, chained into the analyze
   installer list, same pattern as `_ensure_portfolio_context` /
   `_ensure_analyst_report_recovery` which already wrap agent factories from
   `tradingagents.agents`): before the PM's own LLM call inside the node,
   temporarily set `graph.deep_thinking_llm.extra_body = {"reasoning":
   {"effort": cfg["pm_reasoning_effort"]}}` on the shared instance, invoke
   the original node function, then restore whatever `extra_body` value was
   present before (`None` today, but never assume — restore the captured
   prior value, not a hardcoded `None`) in a `finally` block. This means the
   mutation window is exactly the duration of one PM node call; the Research
   Manager's calls happen in an earlier graph phase (never concurrently with
   the PM for the same ticker-analysis run) so it is never affected.
3. **Concurrency scope.** `daily_run.py` runs multiple tickers concurrently
   via `ThreadPoolExecutor` (`analyze_max_workers`), each with its OWN
   `TradingAgentsGraph` instance (confirmed: `TradingAgentsGraph(...)` is
   constructed once per `_analyze_one` call) — so `deep_thinking_llm` is
   NOT shared across tickers, only across node calls within one ticker's
   graph. The PM-node-call mutation window is safe per-ticker; no cross-
   ticker race.
4. **Config.** New key `pm_reasoning_effort` (default `None` — disabled;
   values `"low"|"medium"|"high"|"max"` per OpenRouter's vocabulary), gated
   the same way `pm_execution`/`execution_intent` are — only wired when set,
   a no-op otherwise. Scoped to the `deepseek` model family for now (the
   `reasoning.effort` semantics are OpenRouter-unified but this is only
   verified live for `deepseek/deepseek-v4.1-flash`; guard on
   `llm_provider == "openrouter"` and the configured `deep_think_llm`
   starting with `"deepseek/"` — extending to other providers is a
   follow-up, not blocking, since OpenRouter normalizes `effort` per-model
   and unverified providers should not silently get an unrequested
   parameter).
5. **Failure mode.** If the wrapped node call raises for any reason unrelated
   to the effort injection, the `finally` restore still runs (never leaves
   `extra_body` stuck at an elevated value for a later, unrelated call on
   the same shared instance) and the original exception propagates
   unchanged — this patch must never mask or alter existing PM error
   handling (`_EmptyDecisionError` retry, etc.).
6. **Non-goal:** no attempt to give the PM a fully separate LLM instance,
   config knob, or model choice independent of `deep_think_llm` — the model
   itself stays shared; only the per-call reasoning-effort request field is
   scoped to the PM's own invocation.

## 3. Tests (hermetic TDD)

- `_ensure_openrouter_reasoning_passthrough` / `_reset_*`: idempotent,
  extends/restores the exact tuple, no duplicate entries on repeated calls.
- `_ensure_pm_max_reasoning_effort`: wraps `create_portfolio_manager`'s
  returned node; the node call sets `extra_body` on the shared
  `deep_thinking_llm` for the duration of the call and restores the prior
  value afterward (assert the mock LLM object's `extra_body` before, during
  — via a side-effecting stub — and after the call); a raised exception
  inside the wrapped node still restores `extra_body` (assert via `finally`
  semantics, not just the happy path); disabled when `pm_reasoning_effort`
  is unset (no wrapping installed, `extra_body` untouched); disabled when
  the model is not a `deepseek/` slug on `openrouter` (guard fires, node
  unwrapped, logged once).
- Regression: existing PM/Research-Manager tests (`_ensure_pm_execution_schema`
  et al.) unaffected — the wrap composes with, not replaces, the existing
  factory-swap installers.

## 4. Rollout

Ship behind `pm_reasoning_effort` unset by default (no behavior change until
explicitly configured). Before flipping it on in `watchlist.yaml`, run one
live smoke test (matching the deepseek-v4.1-flash migration's own gate):
confirm the PM's live prompt/response round-trip still succeeds with
`extra_body` present (no 400, no dropped structured output) and that
`usage_metadata.output_token_details.reasoning` visibly increases for the
PM's own call relative to an unpatched baseline run, the same way the two-
effort-level test proved the knob moves spend live.
