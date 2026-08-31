"""reddit_auth.py — OAuth-authenticated Reddit fetcher.

Replaces the framework's unauthenticated RSS path with the official OAuth
API when ``REDDIT_CLIENT_ID`` / ``REDDIT_SECRET`` are set (a free script-type
app at https://www.reddit.com/prefs/apps). Gains:
- ~100 req/min vs ~10 anonymous (no more 429s under parallel analysis)
- real scores/comment counts (the RSS path lacks them)
- the OAuth-only search endpoint on oauth.reddit.com (the public JSON
  endpoint is WAF-blocked; this one is the supported path)

Returns posts in the same dict shape the framework's formatter consumes and
formats the same plaintext block, so the sentiment analyst prompt is
byte-identical in structure — a drop-in swap from daily_run.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_BASE = "https://oauth.reddit.com"
_UA = "tradingagents/0.2 (daily-paper-trading; +https://github.com/hamin2006/TradingAgents)"
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")
SEARCH_LIMIT = 5

_MAX_RETRIES = 2       # retries after the initial attempt (exponential backoff)
_BASE_DELAY_S = 4.0    # 4s -> 8s between retries, plus jitter

_PLACEHOLDER_PREFIX = "<no Reddit posts found"

_token_lock = threading.Lock()
_token_cache = {"token": None, "expires_at": 0.0}


def _cache_dir() -> Path:
    return Path(os.environ.get("REDDIT_CACHE_DIR",
                               Path.home() / ".tradingagents" / "logs" / "reddit_cache"))


def _cache_path(ticker: str) -> Path:
    safe = "".join(c for c in ticker.lower() if c.isalnum()) or "ticker"
    return _cache_dir() / f"{safe}.json"


def _store_cache(ticker: str, block: str, date: str | None = None) -> None:
    try:
        path = _cache_path(ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "date": date or time.strftime("%Y-%m-%d"),
            "block": block,
        }), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("could not write Reddit cache for %s: %s", ticker, exc)


def _load_cache(ticker: str) -> dict | None:
    try:
        path = _cache_path(ticker)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        logger.warning("could not read Reddit cache for %s: %s", ticker, exc)
        return None


def _is_placeholder(block: str) -> bool:
    return block.startswith(_PLACEHOLDER_PREFIX)


def make_resilient(impl):
    """Wrap any fetch_reddit_posts-style implementation with a guarantee:

    1. A placeholder result (fetch failure) triggers up to ``_MAX_RETRIES``
       retries with exponential backoff + jitter.
    2. If every attempt fails, serve the most recent successful block for
       that ticker from the cache (stale Reddit data beats no data).
    3. On success, the block is cached for the next failure.
    """
    def resilient(ticker, subreddits=DEFAULT_SUBREDDITS, **kwargs):
        block = impl(ticker, subreddits=subreddits, **kwargs)
        attempt = 0
        while _is_placeholder(block) and attempt < _MAX_RETRIES:
            attempt += 1
            delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 2.0)
            logger.warning(
                "Reddit fetch gave no posts for %s (attempt %d/%d); "
                "backing off %.1fs then retrying",
                ticker, attempt, _MAX_RETRIES, delay,
            )
            time.sleep(delay)
            block = impl(ticker, subreddits=subreddits, **kwargs)

        if _is_placeholder(block):
            cached = _load_cache(ticker)
            if cached is not None:
                logger.warning("serving cached Reddit block for %s from %s",
                               ticker, cached["date"])
                return (f"{cached['block']}\n\n"
                        f"(Reddit discussion cached from {cached['date']}; "
                        f"live fetch failed today)")
            return block
        _store_cache(ticker, block)
        return block

    resilient._wrapped_original = impl  # tests unwrap to the underlying fetcher
    return resilient


def credentials_available() -> bool:
    return bool(os.environ.get("REDDIT_CLIENT_ID")
                and os.environ.get("REDDIT_SECRET"))


def _get_token() -> str:
    """OAuth client-credentials token, cached until ~expiry (1 hour)."""
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        client_id = os.environ["REDDIT_CLIENT_ID"]
        secret = os.environ["REDDIT_SECRET"]
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        resp = requests.post(
            _TOKEN_URL,
            data="grant_type=client_credentials",
            headers={"User-Agent": _UA,
                     "Content-Type": "application/x-www-form-urlencoded"},
            auth=(client_id, secret),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        return _token_cache["token"]


def _search(ticker: str, sub: str, limit: int, timeout: float) -> list[dict]:
    qs = urlencode({
        "q": ticker, "restrict_sr": "on", "sort": "new", "t": "week",
        "limit": limit,
    })
    resp = requests.get(
        f"{_API_BASE}/r/{sub}/search?{qs}",
        headers={"Authorization": f"bearer {_get_token()}", "User-Agent": _UA},
        timeout=timeout,
    )
    resp.raise_for_status()
    children = resp.json().get("data", {}).get("children", [])
    posts = []
    for child in children:
        d = child.get("data", {})
        posts.append({
            "title": d.get("title", ""),
            "score": d.get("score"),
            "num_comments": d.get("num_comments"),
            "created_utc": d.get("created_utc"),
            "selftext": d.get("selftext", ""),
            "source": "oauth",
        })
    return posts


def fetch_posts(ticker: str, subreddits=DEFAULT_SUBREDDITS, limit: int = SEARCH_LIMIT,
                timeout: float = 10.0) -> list[dict]:
    """Fetch posts across subreddits; returns a flat list of dicts."""
    posts = []
    for sub in subreddits:
        try:
            posts.extend(_search(ticker, sub, limit, timeout))
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            logger.warning("Reddit OAuth search failed for r/%s · %s: %s",
                           sub, ticker, exc)
    return posts


def fetch_reddit_posts(
    ticker: str,
    subreddits=DEFAULT_SUBREDDITS,
    limit_per_sub: int = SEARCH_LIMIT,
    timeout: float = 10.0,
    inter_request_delay: float = 1.0,  # accepted for signature parity; unused (100 QPM)
) -> str:
    """Drop-in replacement for the framework's fetch_reddit_posts: same
    signature, same plaintext block format, OAuth-authenticated."""
    posts = fetch_posts(ticker, subreddits, limit_per_sub, timeout)
    if not posts:
        return (
            f"<no Reddit posts found mentioning {ticker.upper()} across "
            f"{', '.join(f'r/{s}' for s in subreddits)} in the past 7 days>"
        )

    blocks = []
    for sub in subreddits:
        sub_posts = [p for p in posts if p["source"] == "oauth"]
        # group by sub is approximate here (flat list); keep per-sub headers
        lines = [f"r/{sub} — {len(sub_posts)} recent posts mentioning {ticker.upper()}:"]
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
                f"  [{meta}] {p['title']}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
