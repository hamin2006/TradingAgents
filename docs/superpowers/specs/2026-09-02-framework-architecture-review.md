# TradingAgents Framework — Architecture Review

Date: 2026-09-02
Status: Draft
Framework: TradingAgents v0.4.0 (upstream `0e9de89`; used as a library, NOT modified)

## 1. Purpose

Structured review of the framework's agent architecture (graph flow, agent
prompts, tool bindings, structured-output mechanism, state/memory flow) to
track weaknesses found during the 2026-09-02 audit. Anything here that needs
a fix is implemented on our side via runtime patches or config levers — never
by editing files under `tradingagents/`.

## 2. Pipeline shape (ground truth)

```
START → [Market ⇄ tools_market] → clear → [Sentiment (pre-fetch only, no tools)]
      → [News ⇄ tools_news] → clear → [Fundamentals ⇄ tools_fundamentals] → clear
      → Bull ⇄ Bear (debate loop, max_debate_rounds=1 → exactly 2 speeches)
      → Research Manager (deep) → Trader
      → Aggressive ⇄ Conservative ⇄ Neutral (risk loop, max_risk_discuss_rounds=1 → 3 speeches)
      → Portfolio Manager (deep) → END
```

- Tiers: analysts/bull/bear/trader/3 risk debators = quick (flash); Research
  Manager + Portfolio Manager = deep. Both judges (RM, PM) are deep; the
  trader and debators are quick.
- Sentiment Analyst is single-shot (reddit/stocktwits/news pre-fetched into
  the prompt, no tool calls) → `tools_social` node is unreachable.
- Structured-output agents: Sentiment Analyst, RM, Trader, PM. Analysts
  market/news/fundamentals are free prose via tool-calling.
- Termination of both debates is a hard round count, no convergence check.

## 3. Findings (priority order)

### F1 — No portfolio/position truth anywhere in the graph (HIGH)
No agent is ever told whether the analyzed ticker is held; the PM rating
scale's holder verbs ("Hold: maintain current position", "Underweight:
reduce exposure") invite fabrication. Measured live 2026-09-02: 10 of 15 PM
decisions referenced phantom positions on a flat book.
**Tracked separately:** `2026-09-02-phantom-portfolio-positions-fix.md`
(tiered stance + book-shape injection design; not yet implemented).

### F2 — Information is re-judged from scratch, not forwarded (DROPPED — intentional design)

- RM sees only the debate history, NOT the four analyst reports
  (`research_manager.py:43-44`); a distortion in debate text is invisible to
  correct.
- Debators re-embed all four full reports every turn plus the growing
  history (≈quadratic token growth; `risk_mgmt/*.py`, `researchers/*.py`).
- Memory lessons (`past_context`) reach only the PM
  (`portfolio_manager.py:36-41`); RM — who sets the directional
  recommendation everything follows — never sees them.

**Disposition (2026-09-02): deliberate upstream design, not a defect.**
Evidence: (1) the upstream README explicitly documents PM-only lesson
injection ("injects ... lessons into the Portfolio Manager prompt"); (2) the
RM's role contract is debate adjudication — module docstring "turns the
bull/bear debate into a structured investment plan", prompt "critically
evaluate this round of debate" — piping raw reports in would make the
adversarial debate ceremonial; (3) bull/bear and the risk debators ALREADY
see all four reports (F2's "reports into debators" premise was wrong); (4)
upstream's evolution pattern is targeted grounding at the failing layer
(#814 identity, #1167 trader market report), never broadcast. The original
F2 motivation (RM escalating phantom positions from debate text) is resolved
by the tier-1 stance injection of the phantom fix, which reaches RM's
context at the source. Revisit only if memory-log analytics show systematic
repeat errors at one layer — then inject narrowly at that layer.

### F3 — Structured-output fallback is unvalidated and invisible (RESOLVED 2026-09-02)

On any structured failure `invoke_structured_or_freetext` falls back to one
plain invoke whose text is used as-is (`structured.py:82-89`) — no rating
re-extraction, and no consumer can tell a run fell back. 2026-09-02: 3 of
~30 deep-tier calls fell back (MRK/PSX RM, VLO PM). The downstream rating
extraction is then a fragile two-pass regex (`rating.py`) that can misread
"we should not sell into weakness" as a Sell when the `**Rating**:` header is
absent.

**Fix (runtime, shipped 2026-09-02):**
1. *Observability* — `daily_run._ensure_structured_fallback_logging()`
   attaches a logging handler to the framework's `structured` module logger;
   every fallback warning (agent + cause, retry vs permanent-freetext) is
   routed into the per-ticker structured log as a `structured_fallback`
   event. Fallback rate is now measurable per agent/ticker/run.
2. *Safety guard* — structured-success output always carries the
   `**Rating**:` header (the renderer emits it); a header-less decision can
   only come from a freetext fallback, so `_propagate_with_structured_log`
   now re-checks the decision text with a header-only parse
   (`_header_rating`, reusing the framework's pinned label regex). No
   header ⇒ the framework signal came from the prose-word pass ⇒ forced
   `REVIEW` (visible no-op) + loud log + `structured_fallback` event
   (`mode=rating_guard`).
3. *Honest REVIEW* — `daily_run.extract_rating` no longer maps `REVIEW` to a
   fabricated `Hold` (via `parse_rating` default); REVIEW passes through and
   trades nothing (`compute_orders` no-op).

**Known residual (documented):** the framework stores the memory-log tag
inside `propagate`, before the guard, using the same prose-word parse — on a
guard-triggered run the memory tag may carry the guessed word while orders
skip. Rare, logged loudly; analytics caveat accepted.

### F4 — Unbounded outputs + "more detail" pressure (MED)
Market/fundamentals prompts demand "very detailed"/"as much detail as
possible" reports; no `max_tokens` (default None); all reports re-embedded
downstream. Live: TGT's Bear Researcher generated 17,631 output tokens in one
call (23 min at 13 tok/s).
**Our lever:** `max_tokens` is a config key we can set in `watchlist.yaml`
(careful: structured-bound calls are schema-bounded; a cap mostly affects
free-text stages like bull/bear).

### F5 — Dead protocol text and vestigial nodes (LOW)
- Analysts are told to prefix "FINAL TRANSACTION PROPOSAL: BUY/HOLD/SELL"
  (`market_analyst.py:66-67`, `news_analyst.py:41-42`,
  `fundamentals_analyst.py:40-41`, `sentiment_analyst.py:92-93`) — nothing
  consumes it; `render_trader_proposal` keeps emitting it "for backward
  compatibility" (`schemas.py:161-164`).
- `tools_social` ToolNode unreachable (sentiment never calls tools);
  `get_insider_transactions` in the news ToolNode (`trading_graph.py:236`)
  but not bound to the news analyst → never called.
- `sender` (trader) and `judge_decision` (both debates) written but never
  read by graph logic.

### F6 — Same-model self-play + fixed round counts (LOW-MED)
Bull/bear and all three risk personalities share one LLM instance and one
temperature (`setup.py:83-91`); debates are exactly 2 and 3 speeches with
`max_debate_rounds=1`, `max_risk_discuss_rounds=1`
(`default_config.py:116-117`). No convergence or stalemate detection.
**Our lever:** both are upstream config keys our merge layer can override —
bump rounds to 2 on a dev run and measure quality/latency before production.

### F7 — Report capture can silently drop content (MED)
Analysts persist their report only when the final message has zero tool_calls
(`market_analyst.py:87-88` etc.); a message mixing text + tool call yields an
empty report, which then flows downstream as an empty context section — no
guard, no retry, no graph assertion.

### F8 — Minor (LOW)
- Inconsistent prompt API shapes (raw strings / message lists /
  ChatPromptTemplate) through the same invoke helper.
- `past_context` budget asymmetry: 5 full same-ticker decisions + 3 capped
  cross-ticker lessons, no combined token budget.
- Trader produces entry/stop/sizing with no tools and no account context
  (grounding only via market report when selected, `trader.py:33-44`).
- Reflection (memory resolution) uses only the final decision + two return
  numbers on the quick tier — cannot discriminate why a call failed.

## 4. Decision record

- F1: do NOT build standalone — the tiered stance/book-shape injection is
  already speced in the phantom-position fix doc.
- F2: DROPPED — intentional upstream design (see F2 entry); phantom-fix
  tier-1 stance resolves the original motivation.
- F3: RESOLVED — fallback observability handler + header-only rating guard +
  honest REVIEW passthrough (see F3 entry).
- F4/F6: config levers; test on dev runs first.
- F5/F7/F8: accepted as upstream design debt; revisit only if a fork-patch
  registry is ever established.

## 5. Reference

Full per-file evidence (state schema table, per-agent prompt breakdown,
structured-output mechanics, memory flow) was gathered 2026-09-02 from
`tradingagents/graph/{setup,conditional_logic,propagation,reflection,
signal_processing,trading_graph}.py`, `agents/{analysts,researchers,
managers,risk_mgmt,trader}/`, `agents/schemas.py`,
`agents/utils/{structured,agent_utils,agent_states,memory,rating}.py`,
`llm_clients/`, `default_config.py`.
