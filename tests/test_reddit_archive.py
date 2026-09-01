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
