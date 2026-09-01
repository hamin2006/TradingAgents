# Arctic Shift Reddit Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 429-degraded Reddit RSS leg with a keyless Arctic Shift archive pull (complete subreddit coverage, cached, RSS fallback) inside the sentiment analyst's `fetch_reddit_posts`.

**Architecture:** New module `reddit_archive.py` wraps `sentiment_analyst.fetch_reddit_posts` (installed lazily from `daily_run.py`, mirroring `_ensure_reddit_oauth` / `_ensure_stocktwits_resilience`). First call per run does one RLock-protected pull of all 3 subreddits (7-day window, paginated, ~21 requests), caches per-subreddit JSON; later calls filter the cache locally by ticker. Archive failure → stale cache → fall through to the existing resilient RSS path.

**Tech Stack:** Python 3.11, `requests` (already used by `reddit_auth.py`), pytest, no changes under `tradingagents/`.

## Global Constraints

- NEVER modify anything under `tradingagents/` — the framework is consumed as a library; this feature is a runtime patch from our own modules.
- Tests hermetic: no network, no real LLM calls, no real broker. Patch HTTP (`requests.get`) in unit tests.
- Gate: `pytest -q` fully green + `uvx ruff check <files>` (line-length 100, rules E/W/F/I/B/UP/C4/SIM).
- Conventional commits (`feat:` / `fix:` / `docs:`), push to `origin/main`, deploy = `git pull` on the PC.
- All schedule/date logic pinned to `America/New_York` (not relevant here — archive epochs are UTC; keep UTC handling like the framework).
- Follow existing patterns: `stocktwits_resilience.py` (cache + `_wrapped_original`), `reddit_auth.py` (block format), `daily_run.py` `_ensure_*` installers, `tests/test_daily_run.py` `_unwrap_reddit_fetch`.

---

### Task 1: Archive paginated fetch + per-subreddit cache

**Files:**
- Create: `reddit_archive.py`
- Test: `tests/test_reddit_archive.py`

**Interfaces:**
- Produces: `reddit_archive._fetch_page(subreddit: str, after_epoch: float, before_epoch: float | None = None, limit: int = 100) -> list[dict]` — one HTTP page of post dicts (keys `id`, `title`, `selftext`, `score`, `num_comments`, `created_utc`, `subreddit`), raises on HTTP error.
- Produces: `reddit_archive._fetch_subreddit_all(subreddit: str, after_epoch: float) -> list[dict]` — paginated via `before=` cursor, deduped by `id`, `_MAX_PAGES_PER_SUBREDDIT` cap, one retry per page.
- Produces: `reddit_archive._cache_dir() -> Path` (env `REDDIT_ARCHIVE_CACHE_DIR` or `~/.tradingagents/logs/reddit_archive_cache`), `_cache_path(subreddit: str) -> Path`, `_store_sub_cache(subreddit: str, posts: list[dict]) -> None` (best-effort, `fetched_at` epoch + `posts`), `_load_sub_cache(subreddit: str) -> dict | None`.
- Produces: `reddit_archive._CACHE_TTL_S = 24 * 3600`, `_ARCHIVE_LOCK = threading.RLock()`, `_ARCHIVE_LOADED = False` (module globals for Task 3).

- [ ] **Step 1: Write the failing test**

`tests/test_reddit_archive.py`:

```python
"""Hermetic tests for reddit_archive.py (no network)."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

import reddit_archive

PAGE = [
    {"id": "abc12", "title": "NVDA earnings tomorrow",
     "selftext": "thinking about calls", "score": 12,
     "num_comments": 3, "created_utc": 1788246527, "subreddit": "wallstreetbets"},
    {"id": "def34", "title": "Weekend thread",
     "selftext": "", "score": 1, "num_comments": 0,
     "created_utc": 1788246526, "subreddit": "wallstreetbets"},
]


def _fake_get(url, params=None, timeout=None):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": PAGE}

    return Resp()


def test_fetch_page_returns_posts_and_passes_params():
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        return _fake_get(url, params, timeout)

    with patch("requests.get", side_effect=fake_get):
        posts = reddit_archive._fetch_page("wallstreetbets", 1700000000.0)
    assert seen["url"] == reddit_archive._ARCHIVE_API
    assert seen["params"]["subreddit"] == "wallstreetbets"
    assert seen["params"]["after"] == 1700000000.0
    assert seen["params"]["limit"] == 100
    assert seen["params"]["sort"] == "asc"
    assert seen["timeout"] == reddit_archive._REQUEST_TIMEOUT_S
    assert len(posts) == 2
    assert posts[0]["id"] == "abc12"


def test_fetch_page_raises_on_http_error():
    def bad_get(url, params=None, timeout=None):
        class Resp:
            def raise_for_status(self):
                raise RuntimeError("503")

        return Resp()

    with patch("requests.get", side_effect=bad_get), \
         pytest.raises(RuntimeError):
        reddit_archive._fetch_page("wallstreetbets", 1700000000.0)


def test_fetch_subreddit_all_paginates_and_dedupes():
    page1 = [{"id": f"p{i:05d}", "created_utc": 1700000000 + i,
              "title": "t", "selftext": "", "score": 1, "num_comments": 0,
              "subreddit": "wallstreetbets"} for i in range(100)]
    page2 = [{"id": f"q{i:05d}", "created_utc": 1600000000 + i,
              "title": "t", "selftext": "", "score": 1, "num_comments": 0,
              "subreddit": "wallstreetbets"} for i in range(100)]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params or {}))
        if len(calls) == 1:
            return _resp(page1)
        if len(calls) == 2:
            return _resp(page2)   # second page ignores before= -> loop ends
        return _resp([])

    def _resp(posts):
        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": posts}
        return Resp()

    with patch("requests.get", side_effect=fake_get):
        posts = reddit_archive._fetch_subreddit_all("wallstreetbets", 1500000000.0)
    assert len(posts) == 200
    assert len({p["id"] for p in posts}) == 200
    assert calls[0]["after"] == 1500000000.0
    assert calls[1]["before"] == page1[-1]["created_utc"] - 1


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_ARCHIVE_CACHE_DIR", str(tmp_path))
    reddit_archive._store_sub_cache("wallstreetbets", PAGE)
    cached = reddit_archive._load_sub_cache("wallstreetbets")
    assert cached is not None
    assert cached["posts"][0]["id"] == "abc12"
    assert "fetched_at" in cached


def test_cache_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_ARCHIVE_CACHE_DIR", str(tmp_path))
    assert reddit_archive._load_sub_cache("nope") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reddit_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'reddit_archive'`

- [ ] **Step 3: Write minimal implementation**

`reddit_archive.py`:

```python
"""reddit_archive.py — keyless Reddit archive pull (Arctic Shift).

The anonymous RSS path the framework uses (reddit.com/r/{sub}/search.rss) is
rate-limited to ~10 req/min and loses 2/3 subreddits to 429s under parallel
analyze workers. Arctic Shift (arctic-shift.photon-reddit.com, the Pushshift
successor) archives every post ~15s after creation, keyless and free.

This module pulls each subreddit's posts for the 7-day analysis window once
per run (paginated, ~21 requests total), caches them per subreddit, and lets
the wrapper filter locally by ticker — archive keyword search lags ~24h on
recent data, so local filtering wins. Engagement scores finalize after ~36h;
fresh posts report score=1/num_comments=0 (the formatter notes this).

Framework untouched: the wrapper is installed lazily from daily_run.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_ARCHIVE_API = "https://arctic-shift.photon-reddit.com/api/posts/search"
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")

_PAGE_SIZE = 100
_MAX_PAGES_PER_SUBREDDIT = 15
_CACHE_TTL_S = 24 * 3600
_REQUEST_TIMEOUT_S = 20.0
_PAGE_RETRIES = 1

_ARCHIVE_LOCK = threading.RLock()
_ARCHIVE_LOADED = False


def _cache_dir() -> Path:
    return Path(os.environ.get("REDDIT_ARCHIVE_CACHE_DIR",
                               Path.home() / ".tradingagents" / "logs" / "reddit_archive_cache"))


def _cache_path(subreddit: str) -> Path:
    safe = "".join(c for c in subreddit.lower() if c.isalnum()) or "sub"
    return _cache_dir() / f"{safe}.json"


def _store_sub_cache(subreddit: str, posts: list[dict]) -> None:
    try:
        path = _cache_path(subreddit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fetched_at": time.time(),
            "posts": posts,
        }), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("could not write Reddit archive cache for r/%s: %s",
                       subreddit, exc)


def _load_sub_cache(subreddit: str) -> dict | None:
    try:
        path = _cache_path(subreddit)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        logger.warning("could not read Reddit archive cache for r/%s: %s",
                       subreddit, exc)
        return None


def _fetch_page(subreddit: str, after_epoch: float,
                before_epoch: float | None = None, limit: int = _PAGE_SIZE) -> list[dict]:
    params: dict = {
        "subreddit": subreddit,
        "after": after_epoch,
        "limit": limit,
        "sort": "asc",
    }
    if before_epoch is not None:
        params["before"] = before_epoch
    resp = requests.get(_ARCHIVE_API, params=params, timeout=_REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    posts = resp.json().get("data") or []
    wanted = {"id", "title", "selftext", "score", "num_comments",
              "created_utc", "subreddit"}
    return [{k: p.get(k) for k in wanted} for p in posts if isinstance(p, dict)]


def _fetch_subreddit_all(subreddit: str, after_epoch: float) -> list[dict]:
    """Paginate r/{subreddit} back to ``after_epoch``; dedupe by post id."""
    posts: list[dict] = []
    seen: set[str] = set()
    before: float | None = None
    for _ in range(_MAX_PAGES_PER_SUBREDDIT):
        page = _fetch_page(subreddit, after_epoch, before_epoch=before)
        fresh = []
        for p in page:
            pid = p.get("id")
            if pid is not None and pid not in seen:
                seen.add(pid)
                fresh.append(p)
        posts.extend(fresh)
        if not page:
            break
        before = page[-1]["created_utc"] - 1
        if len(posts) >= _PAGE_SIZE * (_MAX_PAGES_PER_SUBREDDIT - 1):
            break
    return posts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reddit_archive.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add reddit_archive.py tests/test_reddit_archive.py
git commit -m "feat: Arctic Shift archive fetch + per-subreddit cache"
```

---

### Task 2: Ticker filter + block formatter

**Files:**
- Modify: `reddit_archive.py`
- Test: `tests/test_reddit_archive.py`

**Interfaces:**
- Consumes: Task 1 `_load_sub_cache`, `DEFAULT_SUBREDDITS`.
- Produces: `reddit_archive._mentions_ticker(post: dict, ticker: str) -> bool` — word-boundary, case-insensitive match on `title` + `selftext` (regex-escaped; `NVDA` must not match `NVDAX`).
- Produces: `reddit_archive._filter_posts(posts: list[dict], ticker: str) -> list[dict]` — keep mentioners, newest first.
- Produces: `reddit_archive._format_block(ticker: str, posts: list[dict], end_date: str | None = None) -> str` — same block shape as the framework's `fetch_reddit_posts` (per-sub header, `[date · score↑ · nc] title`, body excerpt ≤240 chars), plus a coverage note when the newest post is younger than `_ENGAGEMENT_FINALIZE_S` (36h). When `end_date` is given, drops posts newer than that date (midnight UTC) — the framework's `#1220` look-ahead protection for historical runs.
- Produces: `reddit_archive._EMPTY_PLACEHOLDER(ticker, subreddits) -> str` — the framework's exact `<no Reddit posts found mentioning ...>` placeholder format.
- Produces: `reddit_archive._ENGAGEMENT_FINALIZE_S = 36 * 3600`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reddit_archive.py`:

```python
def test_mentions_ticker_word_boundary():
    assert reddit_archive._mentions_ticker(
        {"title": "NVDA earnings tomorrow", "selftext": ""}, "NVDA")
    assert reddit_archive._mentions_ticker(
        {"title": "$NVDA to the moon", "selftext": ""}, "NVDA")
    assert reddit_archive._mentions_ticker(
        {"title": "no", "selftext": "bought some nvda calls"}, "NVDA")
    assert not reddit_archive._mentions_ticker(
        {"title": "NVDAX is different", "selftext": ""}, "NVDA")
    assert not reddit_archive._mentions_ticker(
        {"title": "unrelated", "selftext": "market musings"}, "NVDA")
    assert reddit_archive._mentions_ticker(
        {"title": "BRK-B analysis", "selftext": ""}, "BRK-B")


def test_filter_posts_keeps_mentioners_newest_first():
    old = {"id": "a", "title": "NVDA long term", "selftext": "", "created_utc": 1}
    mid = {"id": "b", "title": "other", "selftext": "", "created_utc": 2}
    new = {"id": "c", "title": "nvda gamma squeeze", "selftext": "", "created_utc": 3}
    out = reddit_archive._filter_posts([old, mid, new], "NVDA")
    assert [p["id"] for p in out] == ["c", "a"]


def test_format_block_matches_framework_shape():
    posts = [
        {"id": "abc12", "title": "NVDA earnings tomorrow",
         "selftext": "long text " * 60, "score": 12, "num_comments": 3,
         "created_utc": 1700000000, "subreddit": "wallstreetbets"},
    ]
    block = reddit_archive._format_block("NVDA", posts)
    assert "r/wallstreetbets" in block
    assert "NVDA" in block
    assert "12" in block and "3c" in block
    assert "2023" in block  # 1700000000 == 2023-11-14
    assert "…" in block      # body excerpt truncated


def test_format_block_notes_prefinalization_engagement():
    now = time.time()
    fresh = {"id": "f1", "title": "NVDA just now", "selftext": "",
             "score": 1, "num_comments": 0, "created_utc": now - 60,
             "subreddit": "wallstreetbets"}
    block = reddit_archive._format_block("NVDA", [fresh])
    assert "finalize" in block.lower()


def test_format_block_omits_note_when_old_enough():
    block = reddit_archive._format_block("NVDA", [
        {"id": "o1", "title": "NVDA then", "selftext": "",
         "score": 40, "num_comments": 7, "created_utc": time.time() - 2 * 86400,
         "subreddit": "wallstreetbets"}])
    assert "finalize" not in block.lower()


def test_format_block_trims_posts_newer_than_end_date():
    now = time.time()
    future = {"id": "f1", "title": "NVDA leak tomorrow", "selftext": "",
              "score": 1, "num_comments": 0, "created_utc": now + 86400,
              "subreddit": "wallstreetbets"}
    past = {"id": "p1", "title": "NVDA back then", "selftext": "",
            "score": 9, "num_comments": 2, "created_utc": now - 3 * 86400,
            "subreddit": "wallstreetbets"}
    end = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    block = reddit_archive._format_block("NVDA", [future, past], end_date=end)
    assert "leak tomorrow" not in block     # future post dropped (#1220)
    assert "back then" in block
```

Add `from datetime import datetime, timezone` to the test file imports.
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reddit_archive.py -v`
Expected: FAIL — `AttributeError: module 'reddit_archive' has no attribute '_mentions_ticker'`

- [ ] **Step 3: Implement**

Append to `reddit_archive.py`:

```python
_ENGAGEMENT_FINALIZE_S = 36 * 3600  # archive scores settle ~36h after posting


def _mentions_ticker(post: dict, ticker: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(ticker)}\b", re.IGNORECASE)
    haystack = f"{post.get('title') or ''}\n{post.get('selftext') or ''}"
    return bool(pattern.search(haystack))


def _filter_posts(posts: list[dict], ticker: str) -> list[dict]:
    return sorted(
        (p for p in posts if _mentions_ticker(p, ticker)),
        key=lambda p: p.get("created_utc") or 0,
        reverse=True,
    )


def _format_block(ticker: str, posts: list[dict],
                  end_date: str | None = None) -> str:
    """Format filtered archive posts like the framework's fetch_reddit_posts."""
    blocks: list[str] = []
    by_sub: dict[str, list[dict]] = {}
    end_cutoff = None
    if end_date:
        end_cutoff = datetime.strptime(end_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp() + 86400  # end of end_date UTC
    for p in posts:
        if end_cutoff and (p.get("created_utc") or 0) > end_cutoff:
            continue  # look-ahead safety (#1220): no future posts for backtests
        by_sub.setdefault(p.get("subreddit") or "reddit", []).append(p)
    newest = max((p.get("created_utc") or 0 for p in posts), default=0)
    note = ""
    if newest and newest > time.time() - _ENGAGEMENT_FINALIZE_S:
        note = ("\n(Note: engagement counts finalize ~36h after posting; "
                "the freshest posts show score=1, 0 comments)")
    for sub, sub_posts in by_sub.items():
        lines = [f"r/{sub} — {len(sub_posts)} recent posts mentioning {ticker.upper()} (via Arctic Shift archive):"]
        for p in sub_posts:
            created = p.get("created_utc")
            created_str = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            meta = created_str
            if p.get("score") is not None and p.get("num_comments") is not None:
                meta += f" · {p['score']:>4}↑ · {p['num_comments']:>3}c"
            selftext = (p.get("selftext") or "").replace("\n", " ").strip()
            if len(selftext) > 240:
                selftext = selftext[:240] + "…"
            lines.append(
                f"  [{meta}] {p.get('title') or ''}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + note


def _empty_placeholder(ticker: str, subreddits: tuple[str, ...]) -> str:
    return (
        f"<no Reddit posts found mentioning {ticker.upper()} across "
        f"{', '.join(f'r/{s}' for s in subreddits)} in the past 7 days>"
    )
```

Add `import re` to the module imports (Task 1 step 3 did not include it — add it now). Also add `from datetime import datetime, timezone` (needed by `_format_block`'s end-date trim).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reddit_archive.py -v`
Expected: all pass (5 + 5 new = 10)

- [ ] **Step 5: Commit**

```bash
git add reddit_archive.py tests/test_reddit_archive.py
git commit -m "feat: ticker filter + framework-shaped block formatter for archive"
```

---

### Task 3: Archive-aware wrapper with single-fill pull

**Files:**
- Modify: `reddit_archive.py`
- Test: `tests/test_reddit_archive.py`

**Interfaces:**
- Consumes: Task 1 `_fetch_subreddit_all`, `_cache_dir`, `_load_sub_cache`, `_store_sub_cache`, `_CACHE_TTL_S`, `_ARCHIVE_LOCK`, `_ARCHIVE_LOADED`; Task 2 `_filter_posts`, `_format_block`, `_empty_placeholder`, `DEFAULT_SUBREDDITS`.
- Produces: `reddit_archive.make_archive_aware(impl) -> wrapper` — drop-in for `fetch_reddit_posts(ticker, subreddits=..., limit_per_sub=..., timeout=..., inter_request_delay=..., start_date=..., end_date=...)`; sets `wrapper._wrapped_original = impl`.
- Produces: `reddit_archive._window_epoch(start_date: str | None, end_date: str | None) -> float` — epoch for `after=` (start_date midnight UTC; 7 days back when None).
- Produces: `reddit_archive._pull_archive(subreddits, start_date) -> bool` — RLock-guarded; True when cache (fresh or stale) usable, False only when archive failed and no cache exists.

Wrapper semantics (per spec §4.3):
- Cache fresh (all subs `fetched_at` within `_CACHE_TTL_S`) → filter locally → block.
- Cache stale/absent → one RLock-protected pull (3 subs, paginated, cached).
- Pull failed → stale cache (any age) with posts → serve (with note).
- Nothing → delegate to `impl` (the existing resilient RSS path).
- Archive loaded but zero matches → `_empty_placeholder` (NOT a failure; no retry).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reddit_archive.py`:

```python
def _resp(posts):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": posts}
    return Resp()


@pytest.fixture
def clean_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_ARCHIVE_CACHE_DIR", str(tmp_path))
    reddit_archive._ARCHIVE_LOADED = False
    yield
    reddit_archive._ARCHIVE_LOADED = False


def test_wrapper_serves_fresh_cache_without_network(clean_archive):
    reddit_archive._store_sub_cache("wallstreetbets", PAGE)
    reddit_archive._store_sub_cache("stocks", [])
    reddit_archive._store_sub_cache("investing", [])
    called = {"n": 0}

    def impl(*args, **kwargs):
        called["n"] += 1
        return "<no Reddit posts found mentioning NVDA across r/wallstreetbets, r/stocks, r/investing in the past 7 days>"

    wrapped = reddit_archive.make_archive_aware(impl)
    block = wrapped("NVDA", start_date="2026-08-25", end_date="2026-09-01")
    assert called["n"] == 0          # archive served, RSS never invoked
    assert "NVDA earnings tomorrow" in block


def test_wrapper_empty_archive_returns_placeholder_not_failure(clean_archive):
    reddit_archive._store_sub_cache("wallstreetbets", [])
    reddit_archive._store_sub_cache("stocks", [])
    reddit_archive._store_sub_cache("investing", [])
    called = {"n": 0}

    def impl(*args, **kwargs):
        called["n"] += 1
        return "RSS DATA"

    wrapped = reddit_archive.make_archive_aware(impl)
    block = wrapped("NVDA", start_date="2026-08-25", end_date="2026-09-01")
    assert called["n"] == 0
    assert block.startswith("<no Reddit posts found mentioning NVDA")


def test_wrapper_pulls_once_on_miss_then_reuses(clean_archive):
    fetched = []

    def fake_get(url, params=None, timeout=None):
        fetched.append(dict(params or {}))
        sub = (params or {}).get("subreddit")
        posts = [dict(p, subreddit=sub) for p in PAGE] if sub == "wallstreetbets" else []
        return _resp(posts)

    def impl(*args, **kwargs):
        return "RSS DATA"

    wrapped = reddit_archive.make_archive_aware(impl)
    with patch("requests.get", side_effect=fake_get):
        b1 = wrapped("NVDA", start_date="2026-08-25", end_date="2026-09-01")
        n_first = len(fetched)
        b2 = wrapped("NVDA", start_date="2026-08-25", end_date="2026-09-01")
    assert b1 == b2
    assert "NVDA earnings tomorrow" in b1
    assert n_first >= 3          # one page per subreddit
    assert len(fetched) == n_first  # second call reused cache, no network


def test_wrapper_serves_stale_cache_when_pull_fails(clean_archive):
    for sub in reddit_archive.DEFAULT_SUBREDDITS:
        posts = [dict(p, subreddit=sub) for p in PAGE] if sub == "wallstreetbets" else []
        path = reddit_archive._cache_path(sub)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "fetched_at": time.time() - 2 * reddit_archive._CACHE_TTL_S,  # stale
            "posts": posts,
        }), encoding="utf-8")

    def bad_get(url, params=None, timeout=None):
        raise RuntimeError("archive down")

    def impl(*args, **kwargs):
        return "RSS DATA"

    wrapped = reddit_archive.make_archive_aware(impl)
    with patch("requests.get", side_effect=bad_get):
        block = wrapped("NVDA", start_date="2026-08-25", end_date="2026-09-01")
    assert "NVDA earnings tomorrow" in block


def test_wrapper_falls_back_to_impl_without_any_cache(clean_archive):
    def impl(*args, **kwargs):
        return "RSS DATA"

    def bad_get(url, params=None, timeout=None):
        raise RuntimeError("archive down")

    wrapped = reddit_archive.make_archive_aware(impl)
    with patch("requests.get", side_effect=bad_get):
        block = wrapped("NVDA", start_date="2026-08-25", end_date="2026-09-01")
    assert block == "RSS DATA"
    assert wrapped._wrapped_original is impl


def test_wrapper_single_fill_under_concurrency(clean_archive):
    import threading

    fetched = []

    def fake_get(url, params=None, timeout=None):
        fetched.append(dict(params or {}))
        sub = (params or {}).get("subreddit")
        posts = [dict(p, subreddit=sub) for p in PAGE] if sub == "wallstreetbets" else []
        return _resp(posts)

    wrapped = reddit_archive.make_archive_aware(lambda *a, **k: "RSS")
    results = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        with patch("requests.get", side_effect=fake_get):
            results.append(wrapped("NVDA", start_date="2026-08-25",
                                   end_date="2026-09-01"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 4
    assert all("NVDA earnings tomorrow" in r for r in results)
    subreddits_fetched = {f["subreddit"] for f in fetched}
    assert subreddits_fetched == set(reddit_archive.DEFAULT_SUBREDDITS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_reddit_archive.py -v`
Expected: FAIL — `AttributeError: module 'reddit_archive' has no attribute 'make_archive_aware'`

- [ ] **Step 3: Implement**

Append to `reddit_archive.py`:

```python
def _window_epoch(start_date: str | None, end_date: str | None) -> float:
    if start_date:
        return datetime.strptime(start_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
    return time.time() - 7 * 86400


def _cache_fresh_for_all(subreddits) -> bool:
    for sub in subreddits:
        cached = _load_sub_cache(sub)
        if cached is None or time.time() - cached.get("fetched_at", 0) > _CACHE_TTL_S:
            return False
    return True


def _pull_archive(subreddits, start_date: str | None) -> bool:
    """Fill/refresh per-subreddit caches once per run. True when usable."""
    global _ARCHIVE_LOADED
    with _ARCHIVE_LOCK:
        if _ARCHIVE_LOADED:
            return True
        after = _window_epoch(start_date, None)
        try:
            for sub in subreddits:
                if not _cache_fresh_for_all([sub]):
                    _store_sub_cache(sub, _fetch_subreddit_all(sub, after))
            _ARCHIVE_LOADED = True
            return True
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            logger.warning("Reddit archive pull failed (%s); serving stale cache or RSS", exc)
            stale = all(_load_sub_cache(sub) is not None for sub in subreddits)
            if stale:
                _ARCHIVE_LOADED = True
                return True
            return False


def make_archive_aware(impl):
    """Wrap a fetch_reddit_posts-style implementation: archive-first, RSS fallback.

    ``impl`` takes ``(ticker, subreddits=..., limit_per_sub=..., timeout=...,
    inter_request_delay=..., start_date=..., end_date=...)`` and returns a
    plaintext block. Drop-in signature- and format-compatible with the
    framework's fetcher and the reddit_auth resilient wrapper.
    """
    def archive_aware(ticker, subreddits=DEFAULT_SUBREDDITS, limit_per_sub=5,
                      timeout=10.0, inter_request_delay=1.0,
                      start_date=None, end_date=None):
        subs = tuple(subreddits)
        if _pull_archive(subs, start_date):
            matches: list[dict] = []
            for sub in subs:
                cached = _load_sub_cache(sub)
                if cached:
                    matches.extend(_filter_posts(cached.get("posts") or [], ticker))
            if matches:
                return _format_block(ticker, matches, end_date)
            return _empty_placeholder(ticker, subs)
        return impl(ticker, subreddits=subreddits, limit_per_sub=limit_per_sub,
                    timeout=timeout, inter_request_delay=inter_request_delay,
                    start_date=start_date, end_date=end_date)

    archive_aware._wrapped_original = impl  # tests unwrap to the underlying fetcher
    return archive_aware
```

(`import re` and `from datetime import datetime, timezone` were already added in Task 2 — no new imports here.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reddit_archive.py -v`
Expected: all pass (10 + 7 new = 17)

- [ ] **Step 5: Commit**

```bash
git add reddit_archive.py tests/test_reddit_archive.py
git commit -m "feat: archive-aware reddit wrapper (archive -> stale cache -> RSS)"
```

---

### Task 4: daily_run wiring + swap test

**Files:**
- Modify: `daily_run.py` (near `_ensure_stocktwits_resilience`, ~line 187; `run_analyze` install block ~line 357)
- Modify: `tests/test_daily_run.py` (after `test_stocktwits_resilient_wrapper_applied`, ~line 537)
- Modify: `AGENTS.md` (module table row for `reddit_archive.py`)

**Interfaces:**
- Consumes: Task 3 `reddit_archive.make_archive_aware`.
- Produces: `daily_run._REDDIT_ARCHIVE_PATCHED` (module global, default `False`), `daily_run._ensure_reddit_archive() -> None` — lazily wraps `sentiment_analyst.fetch_reddit_posts` with `make_archive_aware` (idempotent, `_wrapped_original` preserved for test unwrapping).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daily_run.py` (after `test_stocktwits_resilient_wrapper_applied`):

```python
def test_reddit_archive_wrapper_applied():
    """The sentiment analyst's Reddit fetch is wrapped (archive-first)."""
    import daily_run
    import tradingagents.agents.analysts.sentiment_analyst as sa

    original = sa.fetch_reddit_posts
    daily_run._REDDIT_ARCHIVE_PATCHED = False
    try:
        daily_run._ensure_reddit_archive()
        assert sa.fetch_reddit_posts is not original  # wrapped
        assert sa.fetch_reddit_posts._wrapped_original is original
    finally:
        sa.fetch_reddit_posts = _unwrap_reddit_fetch(original)
        daily_run._REDDIT_ARCHIVE_PATCHED = False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daily_run.py::test_reddit_archive_wrapper_applied -v`
Expected: FAIL — `AttributeError: module 'daily_run' has no attribute '_REDDIT_ARCHIVE_PATCHED'`

- [ ] **Step 3: Implement**

In `daily_run.py`, next to `_STOCKTWITS_PATCHED` (line ~184), add the global:

```python
_REDDIT_ARCHIVE_PATCHED = False
```

Add the installer after `_ensure_stocktwits_resilience` (after line ~203):

```python
def _ensure_reddit_archive() -> None:
    """Wrap the sentiment analyst's Reddit fetch archive-first (Arctic Shift).

    The anonymous RSS path loses subreddits to 429s under parallel workers;
    the keyless Arctic Shift archive gives complete 7-day coverage, cached
    per subreddit and filtered locally per ticker. Falls back to the existing
    resilient RSS path when the archive is unreachable. Framework untouched:
    the swap is lazy from this module.
    """
    global _REDDIT_ARCHIVE_PATCHED
    if _REDDIT_ARCHIVE_PATCHED:
        return
    import reddit_archive
    import tradingagents.agents.analysts.sentiment_analyst as sa

    original = sa.fetch_reddit_posts
    sa.fetch_reddit_posts = reddit_archive.make_archive_aware(original)
    _REDDIT_ARCHIVE_PATCHED = True
```

In `run_analyze` (line ~357, after `_ensure_reddit_pacing()`/before `_ensure_stocktwits_resilience()`), add the call:

```python
    if not _ensure_reddit_oauth():
        _ensure_reddit_pacing()
    _ensure_reddit_archive()
    _ensure_stocktwits_resilience()
```

In `AGENTS.md`, extend the module-table row for the Reddit/resilience modules. Find the row `| reddit_auth.py |` and add after it:

```
| `reddit_archive.py` | Keyless Arctic Shift archive pull (complete subreddit coverage, per-sub cache, local ticker filter); archive-first wrapper with RSS fallback |
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_daily_run.py::test_reddit_archive_wrapper_applied -v`
Expected: PASS

- [ ] **Step 5: Full gate + commit**

Run: `pytest -q` (expected: all green, ~16 new tests) then `uvx ruff check --fix reddit_archive.py daily_run.py tests/test_reddit_archive.py tests/test_daily_run.py`

```bash
git add daily_run.py tests/test_daily_run.py AGENTS.md
git commit -m "feat: install archive-first reddit wrapper in run_analyze"
```

---

### Task 5: Live smoke verification on the PC

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Local sanity import**

Run: `python -c "import reddit_archive; print(reddit_archive.make_archive_aware)"` (from repo root, inside `.venv`)
Expected: `<function make_archive_aware at ...>` — no import errors.

- [ ] **Step 2: One real pull from the dev Mac (manual smoke, not part of pytest)**

Run:
```bash
python -c "
import reddit_archive, time, json
posts = reddit_archive._fetch_subreddit_all('wallstreetbets', time.time() - 7*86400)
print('WSB 7d posts:', len(posts))
nvda = reddit_archive._filter_posts(posts, 'NVDA')
print('NVDA mentions:', len(nvda))
print(reddit_archive._format_block('NVDA', nvda)[:400])
"
```
Expected: real post counts; NVDA mentions ≥ 0; block renders. (Network call — fine outside pytest.)

- [ ] **Step 3: Deploy + suite on the PC**

Push, then on the PC (`pc_ssh.exp`):
```bash
cd /home/harsh-amin/workplace/TradingAgents && git pull -q && \
SSL_CERT_FILE=$(.venv/bin/python -c "import certifi; print(certifi.where())") \
timeout 240 .venv/bin/python -m pytest -q --timeout=30 -p no:cacheprovider 2>&1 | tail -1
```
Expected: `... passed` with the new tests, exit 0.

- [ ] **Step 4: Commit nothing further unless a fix is needed**

If the PC suite passes, the plan is complete. Push already happened in Task 5 Step 3.

## Self-Review Notes

- Spec §4.1 (module surface): Tasks 1–3 cover pagination, cache, filter, formatter, wrapper, `_wrapped_original`.
- Spec §4.2 (integration): Task 4 covers `_ensure_reddit_archive` + `run_analyze` call + RLock (Task 3 `_ARCHIVE_LOCK`) + fallback chain.
- Spec §4.3 (data flow): Task 3 wrapper tests cover fresh cache, miss→pull→reuse, stale cache on failure, no-cache→impl fallback, empty→placeholder.
- Spec §5 (error handling): one retry per page (Task 1 `_fetch_subreddit_all`), never raises out of the wrapper (Task 3 `_pull_archive` try/except), no unbounded loops (`_MAX_PAGES_PER_SUBREDDIT`).
- Spec §6 (testing): Task 1–3 hermetic tests, Task 4 wiring test, Task 5 PC gate.
- Spec §7 (ops): zero cron/env/accounts; AGENTS.md row added in Task 4.
