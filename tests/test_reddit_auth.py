"""tests/test_reddit_auth.py — OAuth Reddit fetcher tests (mocked HTTP)."""

import json
from unittest.mock import patch

import pytest

import reddit_auth


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "test-client")
    monkeypatch.setenv("REDDIT_SECRET", "test-secret")


def _search_payload(title="NVDA pops", score=1234, comments=56, selftext="great quarter"):
    return {
        "data": {
            "children": [
                {"data": {
                    "title": title, "score": score, "num_comments": comments,
                    "created_utc": 1748000000, "selftext": selftext,
                    "permalink": "/r/stocks/comments/abc/",
                }}
            ]
        }
    }


def _mock_token_and_search(monkeypatch, posts, token_calls=1):
    from requests import Response

    def fake_post(url, **kwargs):
        resp = Response()
        resp.status_code = 200
        resp._content = json.dumps(
            {"access_token": "tok", "token_type": "bearer", "expires_in": 3600}
        ).encode()
        return resp

    def fake_get(url, **kwargs):
        resp = Response()
        resp.status_code = 200
        resp._content = json.dumps(posts).encode()
        return resp

    monkeypatch.setattr(reddit_auth.requests, "post", fake_post)
    monkeypatch.setattr(reddit_auth.requests, "get", fake_get)


def test_credentials_available(creds):
    assert reddit_auth.credentials_available() is True


def test_credentials_missing(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_SECRET", raising=False)
    assert reddit_auth.credentials_available() is False


def test_token_request_uses_client_credentials(creds, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["auth"] = kwargs.get("auth")
        captured["data"] = kwargs.get("data")
        from requests import Response
        resp = Response()
        resp.status_code = 200
        resp._content = json.dumps(
            {"access_token": "tok", "expires_in": 3600}).encode()
        return resp

    monkeypatch.setattr(reddit_auth.requests, "post", fake_post)
    token = reddit_auth._get_token()
    assert token == "tok"
    assert captured["url"].endswith("/api/v1/access_token")
    assert captured["data"] == "grant_type=client_credentials"
    assert captured["auth"][0] == "test-client"  # basic auth user = client id


def test_token_cached(creds, monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        from requests import Response
        resp = Response()
        resp.status_code = 200
        resp._content = json.dumps(
            {"access_token": "tok", "expires_in": 3600}).encode()
        return resp

    monkeypatch.setattr(reddit_auth.requests, "post", fake_post)
    reddit_auth._token_cache["token"] = None
    reddit_auth._get_token()
    reddit_auth._get_token()
    assert calls["n"] == 1  # second call served from cache


def test_fetch_posts_returns_framework_shape(creds, monkeypatch):
    _mock_token_and_search(monkeypatch, _search_payload())
    reddit_auth._token_cache["token"] = None
    posts = reddit_auth.fetch_posts("NVDA", ("stocks",), limit=5)
    assert len(posts) == 1
    p = posts[0]
    assert p["title"] == "NVDA pops"
    assert p["score"] == 1234
    assert p["num_comments"] == 56
    assert p["source"] == "oauth"
    assert p["created_utc"] > 0


def test_fetch_reddit_posts_formats_like_framework(creds, monkeypatch):
    _mock_token_and_search(monkeypatch, _search_payload())
    reddit_auth._token_cache["token"] = None
    out = reddit_auth.fetch_reddit_posts("NVDA", subreddits=("stocks",))
    assert "r/stocks — 1 recent posts mentioning NVDA" in out
    assert "↑" in out and "c" in out            # real scores/comments present
    assert "NVDA pops" in out
    assert "body excerpt: great quarter" in out


def test_degrades_gracefully_on_failure(creds, monkeypatch):
    def fake_get(url, **kwargs):
        from requests import Response
        resp = Response()
        resp.status_code = 500
        return resp

    monkeypatch.setattr(reddit_auth.requests, "get", fake_get)
    monkeypatch.setattr(reddit_auth.requests, "post", lambda *a, **k: None)
    reddit_auth._token_cache["token"] = "stale"
    out = reddit_auth.fetch_reddit_posts("NVDA", subreddits=("stocks",))
    assert "<no Reddit posts found" in out  # placeholder, never raises


def test_resilient_retries_on_placeholder():
    """A placeholder (fetch failure) triggers backoff retries, not silence."""
    calls = {"n": 0}

    def impl(ticker, subreddits=None, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return "<no Reddit posts found mentioning NVDA across r/stocks in the past 7 days>"
        return "r/stocks — 2 recent posts mentioning NVDA:"

    with patch("reddit_auth.time.sleep"):
        out = reddit_auth.make_resilient(impl)("NVDA")
    assert calls["n"] == 3
    assert out == "r/stocks — 2 recent posts mentioning NVDA:"


def test_emits_fetch_event(tmp_path, monkeypatch):
    """The RSS/OAuth resilient wrapper reports its outcome to the structured log."""
    import json

    import structured_log
    monkeypatch.setenv("REDDIT_CACHE_DIR", str(tmp_path / "cache"))
    logger = structured_log.StructuredRunLogger(ticker="NVDA", out_dir=str(tmp_path))
    structured_log.set_active_logger(logger)
    try:
        reddit_auth.make_resilient(
            lambda ticker, subreddits=None, **kw:  # noqa: E731
            "r/stocks — 2 recent posts mentioning NVDA:" )("NVDA")
    finally:
        structured_log.clear_active_logger()
    events = [json.loads(line) for line in logger.path.read_text().strip().splitlines()]
    fetch = [e for e in events if e["type"] == "fetch_end"]
    assert fetch[-1]["source"] == "reddit_rss"
    assert fetch[-1]["mode"] == "live"


def test_resilient_serves_cache_when_all_fail(tmp_path, monkeypatch):
    """Total failure must still give the agent Reddit data: cached block."""
    monkeypatch.setenv("REDDIT_CACHE_DIR", str(tmp_path))
    reddit_auth._store_cache("NVDA", "old block", date="2026-08-29")

    def impl(ticker, subreddits=None, **kwargs):
        return "<no Reddit posts found mentioning NVDA across r/stocks in the past 7 days>"

    with patch("reddit_auth.time.sleep"):
        out = reddit_auth.make_resilient(impl)("NVDA")
    assert "old block" in out
    assert "cached from 2026-08-29" in out


def test_resilient_placeholder_only_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_CACHE_DIR", str(tmp_path))

    def impl(ticker, subreddits=None, **kwargs):
        return "<no Reddit posts found mentioning NVDA across r/stocks in the past 7 days>"

    with patch("reddit_auth.time.sleep"):
        out = reddit_auth.make_resilient(impl)("NVDA")
    assert out.startswith("<no Reddit posts found")


def test_resilient_caches_success(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_CACHE_DIR", str(tmp_path))

    def impl(ticker, subreddits=None, **kwargs):
        return "r/stocks — 2 recent posts mentioning NVDA:"

    with patch("reddit_auth.time.sleep"):
        reddit_auth.make_resilient(impl)("NVDA")
    assert (tmp_path / "nvda.json").exists()
    assert reddit_auth._load_cache("NVDA")["block"].startswith("r/stocks")
