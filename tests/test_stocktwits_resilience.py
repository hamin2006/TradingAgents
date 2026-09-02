"""StockTwits fetch: transport-error resilience (#1024) and crypto symbol
mapping (#1113).

StockTwits lists crypto under ``<BASE>.X`` (Yahoo's ``BTC-USD`` 404s), and any
transport error must degrade to a placeholder rather than raise.

The second section covers the retry-with-backoff + per-ticker cache wrapper
(burst-403 defense under parallel analyze workers).
"""

from __future__ import annotations

import http.client
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

import stocktwits_resilience
from tradingagents.dataflows import stocktwits


def _raise(exc):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            raise exc
    return _Resp()


@pytest.mark.unit
class TestStockTwitsResilience:
    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_transport_errors_return_placeholder(self, exc):
        with patch.object(stocktwits, "urlopen", return_value=_raise(exc)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "unavailable" in out.lower()
        assert out.startswith("<stocktwits unavailable")


@pytest.mark.unit
class TestStockTwitsCryptoSymbols:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("BTC-USD", "BTC.X"),
            ("eth-usd", "ETH.X"),
            ("SOL-USD", "SOL.X"),
            ("BTCUSD", "BTC.X"),      # undashed broker form
            ("BTC-USDT", "BTC.X"),    # stablecoin quote
            ("AMD", "AMD"),
            ("BRK-B", "BRK-B"),       # dashed class share: untouched
            ("GOLD", "GOLD"),         # real equity (aliases elsewhere): untouched here
            ("XYZ-USD", "XYZ-USD"),   # unknown base: not treated as crypto
        ],
    )
    def test_symbol_mapping(self, ticker, expected):
        assert stocktwits._stocktwits_symbol(ticker) == expected

    def test_crypto_pair_requests_dot_x_endpoint(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            raise TimeoutError("stop after capturing the URL")

        with patch.object(stocktwits, "urlopen", side_effect=fake_urlopen):
            stocktwits.fetch_stocktwits_messages("BTC-USD")
        assert "/symbol/BTC.X.json" in seen["url"]


def test_resilient_retries_on_failure():
    """A failure placeholder triggers backoff retries, not silence."""
    calls = {"n": 0}

    def impl(ticker, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return "<stocktwits unavailable: HTTPError>"
        return "Bullish: 1 (100%) · Bearish: 0 (0%) · Unlabeled: 0 · Total: 1"

    with patch("stocktwits_resilience.time.sleep"):
        out = stocktwits_resilience.make_resilient(impl)("AAPL")
    assert calls["n"] == 3
    assert out.startswith("Bullish:")


def test_emits_fetch_event_with_mode(tmp_path, monkeypatch):
    """With an active structured logger, the wrapper reports the fetch outcome."""
    import json

    import structured_log
    monkeypatch.setenv("STOCKTWITS_CACHE_DIR", str(tmp_path / "cache"))
    logger = structured_log.StructuredRunLogger(ticker="AAPL", out_dir=str(tmp_path))
    structured_log.set_active_logger(logger)
    try:
        with patch("stocktwits_resilience.time.sleep"):
            stocktwits_resilience.make_resilient(
                lambda ticker, **kw: "<stocktwits unavailable: boom>")(  # noqa: E731
                "AAPL", start_date="2026-08-25", end_date="2026-09-01")
    finally:
        structured_log.clear_active_logger()
    events = [json.loads(line) for line in logger.path.read_text().strip().splitlines()]
    fetch = [e for e in events if e["type"] == "fetch_end"]
    assert fetch, "fetch_end event must be emitted on failure"
    assert fetch[-1]["source"] == "stocktwits"
    assert fetch[-1]["mode"] == "placeholder"
    assert fetch[-1]["retries"] >= 0


def test_emits_live_mode_on_success(tmp_path, monkeypatch):
    import json

    import structured_log
    monkeypatch.setenv("STOCKTWITS_CACHE_DIR", str(tmp_path / "cache"))
    logger = structured_log.StructuredRunLogger(ticker="AAPL", out_dir=str(tmp_path))
    structured_log.set_active_logger(logger)
    try:
        stocktwits_resilience.make_resilient(
            lambda ticker, **kw: "Bullish: 1 (100%) · Total: 1")(  # noqa: E731
            "AAPL", start_date="2026-08-25", end_date="2026-09-01")
    finally:
        structured_log.clear_active_logger()
    events = [json.loads(line) for line in logger.path.read_text().strip().splitlines()]
    fetch = [e for e in events if e["type"] == "fetch_end"]
    assert fetch[-1]["mode"] == "live"
    assert fetch[-1]["source"] == "stocktwits"


def test_resilient_serves_cache_when_all_fail(tmp_path, monkeypatch):
    """Total failure must still give the analyst StockTwits data: cached block."""
    monkeypatch.setenv("STOCKTWITS_CACHE_DIR", str(tmp_path))
    stocktwits_resilience._store_cache("AAPL", "old block", date="2026-08-29")

    def impl(ticker, **kwargs):
        return "<stocktwits unavailable: HTTPError>"

    with patch("stocktwits_resilience.time.sleep"):
        out = stocktwits_resilience.make_resilient(impl)("AAPL")
    assert "old block" in out
    assert "cached from 2026-08-29" in out


def test_resilient_placeholder_only_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKTWITS_CACHE_DIR", str(tmp_path))

    def impl(ticker, **kwargs):
        return "<stocktwits unavailable: HTTPError>"

    with patch("stocktwits_resilience.time.sleep"):
        out = stocktwits_resilience.make_resilient(impl)("AAPL")
    assert out.startswith("<stocktwits unavailable")


def test_resilient_caches_success(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKTWITS_CACHE_DIR", str(tmp_path))

    def impl(ticker, **kwargs):
        return "Bullish: 1 (100%) · Bearish: 0 (0%) · Unlabeled: 0 · Total: 1"

    with patch("stocktwits_resilience.time.sleep"):
        stocktwits_resilience.make_resilient(impl)("AAPL")
    assert (tmp_path / "aapl.json").exists()
    assert stocktwits_resilience._load_cache("AAPL")["block"].startswith("Bullish:")


def test_resilient_does_not_retry_or_cache_empty_window(tmp_path, monkeypatch):
    """A genuinely empty window is not a failure: no retries, no cache write."""
    monkeypatch.setenv("STOCKTWITS_CACHE_DIR", str(tmp_path))
    calls = {"n": 0}

    def impl(ticker, **kwargs):
        calls["n"] += 1
        return ("<no StockTwits messages for $AAPL within 2026-08-24..2026-08-31 "
                "(public stream serves only recent messages)>")

    with patch("stocktwits_resilience.time.sleep"):
        out = stocktwits_resilience.make_resilient(impl)(
            "AAPL", start_date="2026-08-24", end_date="2026-08-31")
    assert calls["n"] == 1
    assert out.startswith("<no StockTwits messages")
    assert not (tmp_path / "aapl.json").exists()
