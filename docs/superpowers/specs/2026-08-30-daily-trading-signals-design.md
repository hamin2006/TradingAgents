# Daily Trading Signals System — Design

Date: 2026-08-30
Status: Draft
Framework: TradingAgents v0.3.1 (cloned repo in this directory, used as a library, NOT modified)

## 1. Purpose

A daily, automated system that runs the TradingAgents multi-agent analysis pipeline once per
trading day for a fixed watchlist of US equities, and emails a concise morning brief telling
the user which stocks to buy and which to consider selling.

The system is **signals-only**: it outputs ratings and suggested price levels. It does not
track the user's real portfolio, place orders, or simulate positions. The user acts on the
brief manually.

## 2. Requirements

- Runs once per trading day, Mon–Fri, completing before the 09:30 ET US market open.
- Fixed watchlist of 3–8 US tickers (initial list: liquid large-caps — AAPL, MSFT, NVDA,
  GOOGL, AMZN, META, TSLA). Editable in a YAML config file.
- Per-ticker output is today's rating (Buy / Overweight / Hold / Underweight / Sell),
  suggested entry/stop levels when the model provides them, and a downgrade flag when
  today's rating is worse than the system's own previous rating for that ticker.
- Full markdown analysis reports per ticker (already produced by the framework) and a
  daily summary brief.
- Cheap LLM inference via OpenRouter (2-tier model split; see §7).
- Data vendors are the framework's free defaults (yfinance / FRED / Polymarket) — $0
  data cost; the only recurring cost is LLM inference.
- One ticker's failure must not kill the run or the brief.
- Email delivery mechanism is not yet chosen; the notifier must be a drop-in interface.

## 3. Out of scope (explicitly deferred)

- Position tracking, portfolio bookkeeping, auto-sell execution, stop-loss enforcement.
- Cross-ticker ranking beyond the brief's simple ordering (Buy/Overweight first).
- A web dashboard or push notifications.
- Alpha Vantage / paid data vendors.
- Modifying the `tradingagents/` framework package itself.

## 4. Architecture

Four new files at the repo root; the framework is used exactly as its README shows
(`TradingAgentsGraph(config).propagate(ticker, date)`). No framework changes.

```
daily_run.py          # orchestrator: loop tickers, extract signals, build brief, notify
watchlist.yaml        # user-facing config: tickers, models, schedule, notifier settings
notifier.py           # Notifier interface + ConsoleNotifier + EmailNotifier
tests/test_daily_run.py  # unit tests for extraction/downgrade/render/config-merge
```

Component responsibilities:

- **daily_run.py**
  - Loads `watchlist.yaml` and merges it over the framework's `DEFAULT_CONFIG`
    (framework `TRADINGAGENTS_*` env overrides still win).
  - Determines the analysis date as "today" in `America/New_York` (server TZ irrelevant).
  - For each ticker in order: create `TradingAgentsGraph`, call
    `propagate(ticker, today)`; wrap in try/except; on failure log the reason and
    retry once, then continue to the next ticker.
  - Extracts the rating from the returned decision, reads the ticker's most recent
    memory-log entry via `TradingMemoryLog.load_entries()` for downgrade detection,
    and collects "yesterday's resolved calls" from the memory log.
  - Builds the brief (see §5), writes it to disk first, then calls the notifier.
  - Writes `daily_summary_YYYY-MM-DD.md` and appends one line per ticker
    (date, rating, downgrade flag) to `results_dir/ticker_history.md` for cheap
    trend spotting.
- **watchlist.yaml**
  - `watchlist`, `llm_provider`, `quick_think_llm`, `deep_think_llm`, `output_language`,
    notifier settings (`type: console|email`, `email:`, SMTP settings once chosen).
  - `DEFAULT_CONFIG` stays the source of truth for everything else; the yaml only
    overrides what the user cares about.
- **notifier.py**
  - `Notifier` protocol: `send(brief_markdown: str) -> None`.
  - `ConsoleNotifier`: prints to stdout (used by cron logs and local runs).
  - `EmailNotifier`: mechanism TBD — Gmail SMTP app password via stdlib `smtplib`, or a
    transactional API (Resend/SendGrid) later. Failure is logged and never blocks the
    run; the brief is already on disk by the time email is attempted.

## 5. Daily brief format

Markdown email body, skimmable in ~30 seconds:

```markdown
# Trading Brief — 2026-08-31 (Mon)

## Today's Actionable Signals
| Ticker | Rating    | vs Last (date)  | Entry / Stop (suggested) |
|--------|-----------|-----------------|--------------------------|
| AAPL   | **Buy**   | Hold (08-28) ⬆  | 229.40 / 222.10          |
| NVDA   | **Buy**   | Buy (08-28) =   | 131.20 / 125.80          |
| MSFT   | Hold      | Hold (08-28) =  | —                        |
| TSLA   | **Sell**  | Hold (08-28) ⬇  | —                        |

## Downgrade Flags (what to consider selling)
- **TSLA**: Sell, downgraded from Hold (08-28) — exit or reduce.

## Failures
- (none, or: GOOGL — analysis failed, retried once, see log)

## Yesterday's Calls (resolved)
- AAPL: +2.1% raw / +1.4% alpha vs SPY (5d)
```

Rules:

- **Actionable buys**: ratings Buy / Overweight, bolded, listed first.
- **Downgrade flag**: only when today's rating is strictly worse than the ticker's most
  recent memory-log rating (Buy→Hold, Buy→Sell, Hold→Sell, …). No-change and upgrades
  render as plain rows (⬆ / = markers).
- **Failures**: a ticker that failed shows in the Failures section with the reason; it is
  not silently dropped.
- **Yesterday's calls**: pulled from resolved memory-log entries (the framework resolves
  realized returns for past same-ticker decisions at the start of each run); one line
  each, skipped when no data is available yet.

## 6. Downgrade detection

- Read `TradingMemoryLog.load_entries()` (public API already used internally by the
  framework) for the ticker's most recent entry (resolved or pending).
- Compare parsed ratings on the 5-tier scale. Strictly-worse → downgrade flag, dated.
- No rating / no prior entry → no flag (brief shows "—" for vs-last).
- The memory log is the single source of truth; the daily system adds no new state.

## 7. LLM configuration

Provider: `openrouter` (`OPENROUTER_API_KEY`), cheap 2-tier split:

- `quick_think_llm` (analysts, researchers, trader, risk debators, reflections):
  `deepseek/deepseek-v4-flash` or `z-ai/glm-4.5-air`.
- `deep_think_llm` (Research Manager, Portfolio Manager judges):
  `deepseek/deepseek-v4-pro` or `z-ai/glm-4.7`.

Expected cost: ~$0.10–0.50 per ticker per day → ~$15–100/month for a 7-ticker watchlist
at one run/day. Models are configurable in `watchlist.yaml`; the CLI's "Custom model ID"
path means any OpenRouter slug works.

## 8. Data layer (verified against the code)

| Category            | Default vendor | Key | Cost |
|---------------------|----------------|-----|------|
| OHLCV prices        | yfinance       | none | $0 |
| Technical indicators| yfinance       | none | $0 |
| Fundamentals/statements | yfinance   | none | $0 |
| Ticker news, insider trades | yfinance | none | $0 |
| Sentiment (StockTwits/Reddit) | stocktwits.py / reddit.py | none (Reddit degrades to RSS) | $0 |
| Macro indicators    | FRED           | `FRED_API_KEY` (free registration) | $0 |
| Prediction markets  | Polymarket     | none | $0 |

- Data cost is $0/month with framework defaults. The only recurring expense is LLM
  inference (§7).
- yfinance is free scraping with no SLA: fine for 7 tickers/day; the framework's
  vendor router (`dataflows/interface.py`) already handles no-data degradation with
  explicit sentinels rather than fabricated values.
- FRED key is the only setup step with a registration.

## 9. Scheduling

Cron, timezone-aware to dodge DST:

```cron
CRON_TZ=America/New_York
0 7 * * 1-5  cd /opt/tradingagents && .venv/bin/python daily_run.py >> logs/cron.log 2>&1
```

- 07:00 ET start; 7 tickers × ~3–6 min sequential ≈ 30–45 min worst case → brief lands
  ~07:45–08:30 ET, well before the 09:30 open.
- The analysis date is pinned to "today in America/New_York", never UTC or server-local.

## 10. VM setup (one-time, scripted in the implementation plan)

1. Ubuntu 24.04 (or preferred distro) with Python 3.12.
2. `git clone` the repo to `/opt/tradingagents`; `python3.12 -m venv .venv`; `pip install .`
   (or `pip install -e .` for dev).
3. `.env`: `OPENROUTER_API_KEY`, `FRED_API_KEY`, optional `TRADINGAGENTS_*` overrides.
4. `watchlist.yaml` + `daily_run.py` placed in the repo root.
5. cron entry (§9); `logs/` with simple rotation (keep ~30 days).
6. Smoke test before trusting cron: `daily_run.py --tickers AAPL` manually.

## 11. Error handling

- Per-ticker try/except + one immediate retry; failure recorded with reason, run continues.
- Email failure is logged, never fatal (brief already on disk).
- Framework-level failures (e.g. a vendor outage across all tickers) leave the failed
  tickers in the brief's Failures section; cron log holds the full traceback.
- Checkpoint resume is off by default (short runs, fresh state per day is simpler).

## 12. Testing

- Unit: rating extraction from real decision strings; downgrade detection
  (Buy→Hold flags, Hold→Buy doesn't, unknown rating doesn't, no-prior-entry doesn't);
  brief rendering; `watchlist.yaml` → config merge precedence.
- Integration: single-ticker end-to-end run against the real framework with the cheap
  model pair (the VM smoke test).
- Failure drill: injected vendor error proves one bad ticker doesn't kill the run.

## 13. Deliverables

- `daily_run.py`, `notifier.py`, `watchlist.yaml` (with the 7-ticker default), `tests/test_daily_run.py`.
- A short `docs/superpowers/plans/` implementation plan (written next).
- No changes to `tradingagents/`.

## 14. Open questions

- Email mechanism (SMTP app password vs Resend/SendGrid) — deferred; interface in place.
- Whether to include SPY/QQQ as market-proxy watchlist rows — currently excluded.
