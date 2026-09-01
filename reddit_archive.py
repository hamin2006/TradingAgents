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
import re
import threading
import time
from datetime import datetime, timezone
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
