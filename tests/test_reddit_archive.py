"""Hermetic tests for reddit_archive.py (no network)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
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

    with patch("requests.get", side_effect=fake_get):
        posts = reddit_archive._fetch_subreddit_all("wallstreetbets", 1500000000.0)
    assert len(posts) == 200
    assert len({p["id"] for p in posts}) == 200
    assert calls[0]["after"] == 1500000000.0
    assert calls[1]["before"] == page1[-1]["created_utc"] - 1


def _resp(posts):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": posts}
    return Resp()


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
