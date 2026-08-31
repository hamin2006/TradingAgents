"""tests/test_reddit_auth.py — OAuth Reddit fetcher tests (mocked HTTP)."""

import json

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
