# Arctic Shift Reddit Archive — Design

Date: 2026-09-01
Status: Draft
Framework: TradingAgents v0.4.0 (upstream `0e9de89`; used as a library, NOT modified)

## 1. Purpose

Replace the degraded Reddit leg of the sentiment analyst's pre-fetched sources.
The anonymous RSS path (r/wallstreetbets, r/stocks, r/investing) is rate-limited
(~10 req/min) and loses 2/3 subreddits to 429s on both the dev Mac and the
production PC. Verified alternatives that motivated this design:

- **Finnhub `stock/social-sentiment`** — tested live with a fresh free-tier
  token; returns `"You don't have access to this resource."` — premium-only.
  Dead on arrival.
- **Reddit OAuth app creation** (`reddit.com/prefs/apps`) — still blocked by the
  signup captcha; a widespread current issue, retried multiple times.
- **Devvit app pipeline** — possible (scheduled job + external endpoint) but
  requires a Tailscale-Funnel listener daemon on the PC + Devvit CLI project,
  and the app-signup page may carry the same block. Deferred.
- **Arctic Shift** (`arctic-shift.photon-reddit.com`) — the Pushshift successor;
  keyless, free, no uptime guarantees. **Verified live** (2026-09-01):
  - Posts archived ~15 s after creation (retrieved 00:09:02 for created 00:08:47).
  - Subreddit pulls (`/api/posts/search?subreddit=X&after=<epoch>&limit=100`)
    return complete coverage, paginated by `before=` cursor, `sort=asc`.
  - Keyword search (`query=`) lags ~24 h on recent data and flakes on wide
    windows → **do not use it**; pull subreddit-level and filter locally.
  - Engagement fields (score/num_comments) finalize after ~36 h; fresher posts
    show `score=1`/`num_comments=0` (same freshness profile as RSS, but with
    complete subreddit coverage instead of losing 2/3 to 429s).

## 2. Requirements

- Sentiment analyst's `fetch_reddit_posts` returns complete WSB + stocks +
  investing coverage for the 7-day analysis window, without rate-limit loss.
- No new accounts, no new keys, no new cron jobs, no changes under
  `tradingagents/`.
- A failure of the archive must degrade to today's behavior (paced RSS), never
  to nothing.
- Hermetic tests (no network, no live API calls).

## 3. Out of scope (deferred)

- Per-ticker keyword search on the archive (lags ~24 h; local filtering wins).
- Comment-level sentiment (posts only; the analyst already reads body excerpts).
- GDELT / AV / Devvit integration (evaluated, rejected above).
- Anything under `tradingagents/`.

## 4. Architecture

New module `reddit_archive.py` (repo root, alongside `reddit_auth.py` and
`stocktwits_resilience.py`):

### 4.1 Module surface

- `make_archive_aware(impl)` → wrapper with `_wrapped_original` tag (same
  contract as `reddit_auth.make_resilient` and `stocktwits_resilience.make_resilient`).
- `_fetch_subreddit_posts(subreddit, after_epoch, limit=100)` → paginated pull,
  one request per page (~21 requests/day total at 1–2 req/s ≈ 20–40 s).
- Cache: per-subreddit JSON at `~/.tradingagents/logs/reddit_archive_cache/`
  (`REDDIT_ARCHIVE_CACHE_DIR` env override, same pattern as stocktwits cache),
  `fetched_at` timestamp, daily TTL (24 h).
- Local ticker filter: case-insensitive, word-boundary regex on
  `title` + `selftext` (a ticker like `NVDA` must not match `NVDAX`).
- Block formatter: same shape the analyst already consumes — per post:
  subreddit, title, score, num_comments, selftext snippet, permalink, date —
  plus a coverage note when engagement counts are pre-finalization.

### 4.2 Integration (`daily_run.py`)

- `_ensure_reddit_archive()` installed in `run_analyze` alongside
  `_ensure_reddit_oauth()` / `_ensure_reddit_pacing()`, wraps
  `sentiment_analyst.fetch_reddit_posts`.
- Fallback chain: archive (fresh cache or one-time inline pull) → paced RSS
  (existing resilient path) → placeholder.
- RLock-protected single fill per run: the first `fetch_reddit_posts` call
  triggers the pull; concurrent worker threads block on the lock and reuse the
  result (mirrors the reddit pacing RLock pattern).

### 4.3 Data flow

```
fetch_reddit_posts(ticker, window)
  └─ archive wrapper
      ├─ cache fresh? → filter locally → format block
      ├─ cache stale/absent → RLock single pull (3 subreddits, 7d, paginated)
      │     └─ write cache → filter → format block
      ├─ pull failed → stale cache? → serve stale + note
      └─ nothing → delegate to impl (paced RSS) → placeholder
```

## 5. Error handling

- Archive unreachable/timeouts: fall back to stale cache if any, else RSS.
- RSS also down: placeholder (today's behavior).
- No new failure mode worse than today's.
- Retries: one re-attempt per page on transient errors, consistent with the
  existing resilience wrappers; no unbounded loops.

## 6. Testing

- `tests/test_reddit_archive.py` (hermetic, patched HTTP):
  pagination loop, ticker word-boundary filter, cache TTL + stale-fallback,
  RLock single-fill under concurrency, block formatting, fallback ordering.
- `tests/test_daily_run.py`: `test_reddit_archive_swapped_when_installed` —
  swap applied + restored in `finally` (mirrors `_unwrap_reddit_fetch`).
- Gate: `pytest -q` fully green + `uvx ruff check` (line-length 100,
  E/W/F/I/B/UP/C4/SIM).

## 7. Ops

- Zero new cron entries, zero env vars, zero accounts.
- Deploy = `git pull` on the production PC (existing flow).
- Artifacts: `~/.tradingagents/logs/reddit_archive_cache/*.json`.
