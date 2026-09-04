"""tests/test_news_dating.py — hermetic tests for dated news rendering and the
verified-snapshot anchor header (2026-09-03 audit: the yfinance news feed
extracts per-article pub_dates but drops them from the rendered string, so the
News Analyst cannot tell an Aug-28 article from a current one)."""

from __future__ import annotations

from datetime import datetime, timezone

import news_dating


def _article_nested(title: str, iso: str, publisher: str = "MarketBeat") -> dict:
    return {
        "content": {
            "title": title,
            "summary": f"{title} summary",
            "provider": {"displayName": publisher},
            "canonicalUrl": {"url": f"https://example.com/{title}"},
            "pubDate": iso,
        }
    }


def _article_flat(title: str, ts: int, publisher: str = "Yahoo") -> dict:
    return {
        "title": title,
        "summary": f"{title} summary",
        "publisher": publisher,
        "link": f"https://example.com/{title}",
        "providerPublishTime": ts,
    }


def _utc_ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


class TestTickerRendering:
    def test_articles_carry_publication_date(self, monkeypatch):
        """The audit root cause: pub_date was extracted then dropped. The
        renderer must surface it on every dated article."""
        arts = [_article_nested("REGN surges", "2026-08-28T14:30:00Z"),
                _article_flat("Eylea update", _utc_ts("2026-09-01T09:00:00Z"))]
        monkeypatch.setattr(news_dating, "fetch_articles", lambda _t, _l: arts)
        monkeypatch.setattr(news_dating, "fetch_anchor", lambda _t: None)
        out = news_dating.render_ticker_news("REGN", "2026-08-25", "2026-09-02")
        assert "published 2026-08-28" in out
        assert "published 2026-09-01" in out
        assert "(source: MarketBeat, published" in out
        assert out.startswith("## REGN News, from 2026-08-25 to 2026-09-02:")

    def test_window_filter_mirrors_upstream(self, monkeypatch):
        """Out-of-window and past-undated articles must be excluded exactly as
        the upstream feed excludes them."""
        arts = [_article_nested("too old", "2026-08-19T00:00:00Z"),
                _article_nested("in window", "2026-08-28T12:00:00Z"),
                {"title": "undated past", "summary": "s", "publisher": "X",
                 "link": "", "pub_date": None}]
        monkeypatch.setattr(news_dating, "fetch_articles", lambda _t, _l: arts)
        monkeypatch.setattr(news_dating, "fetch_anchor", lambda _t: None)
        out = news_dating.render_ticker_news("REGN", "2026-08-20", "2026-08-29")
        assert "in window" in out
        assert "too old" not in out
        assert "undated past" not in out

    def test_no_news_and_error_strings_mirror_upstream(self, monkeypatch):
        monkeypatch.setattr(news_dating, "fetch_articles",
                            lambda _t, _l: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr(news_dating, "fetch_anchor", lambda _t: None)
        out = news_dating.render_ticker_news("REGN", "2026-08-25", "2026-09-02")
        assert out.startswith("Error fetching news for REGN:")
        monkeypatch.setattr(news_dating, "fetch_articles", lambda _t, _l: [])
        out = news_dating.render_ticker_news("REGN", "2026-08-25", "2026-09-02")
        assert out == "No news found for REGN"

    def test_anchor_header_prepended_when_available(self, monkeypatch):
        arts = [_article_nested("REGN story", "2026-08-28T14:30:00Z")]
        monkeypatch.setattr(news_dating, "fetch_articles", lambda _t, _l: arts)
        monkeypatch.setattr(news_dating, "fetch_anchor",
                            lambda _t: ("2026-09-02", 852.03))
        out = news_dating.render_ticker_news("REGN", "2026-08-25", "2026-09-02")
        assert "$852.03" in out and "2026-09-02" in out
        assert "never present them as current" in out
        assert out.index("$852.03") < out.index("REGN story")

    def test_anchor_omitted_on_fetch_failure(self, monkeypatch):
        arts = [_article_nested("REGN story", "2026-08-28T14:30:00Z")]
        monkeypatch.setattr(news_dating, "fetch_articles", lambda _t, _l: arts)
        monkeypatch.setattr(news_dating, "fetch_anchor",
                            lambda _t: (_ for _ in ()).throw(OSError("no net")))
        out = news_dating.render_ticker_news("REGN", "2026-08-25", "2026-09-02")
        assert "Data anchor" not in out
        assert "published 2026-08-28" in out  # dates survive anchor loss


class TestGlobalRendering:
    def _fake_search(self, monkeypatch, articles_by_query):
        """Stub yfinance Search so the real dedupe/cap path runs hermetic."""
        calls = []

        class FakeSearch:
            def __init__(self, query, news_count, enable_fuzzy_query):
                calls.append(query)
                self.news = articles_by_query.get(query, [])

        monkeypatch.setattr(news_dating.yf, "Search", FakeSearch)
        return calls

    def test_global_articles_dated_and_deduped(self, monkeypatch):
        arts = [_article_nested("Fed decision", "2026-08-30T10:00:00Z"),
                _article_flat("Dupes me", _utc_ts("2026-08-31T10:00:00Z"))]
        queries = news_dating.get_config()["global_news_queries"]
        self._fake_search(monkeypatch, dict.fromkeys(queries, arts))
        out = news_dating.render_global_news("2026-09-02", look_back_days=5,
                                             limit=10)
        assert out.startswith("## Global Market News, from 2026-08-28")
        assert "published 2026-08-30" in out
        assert "published 2026-08-31" in out
        assert out.count("### Dupes me (source: Yahoo") == 1  # deduped across queries

    def test_global_empty_strings_mirror_upstream(self, monkeypatch):
        queries = news_dating.get_config()["global_news_queries"]
        self._fake_search(monkeypatch, {q: [] for q in queries})
        out = news_dating.render_global_news("2026-09-02", look_back_days=5,
                                             limit=10)
        assert out == "No global news found for 2026-09-02"
        # candidates exist but all fall outside the window (#993 mirror)
        self._fake_search(monkeypatch,
                          {q: [_article_nested("ancient",
                                               "2026-01-05T10:00:00Z")]
                           for q in queries})
        out = news_dating.render_global_news("2026-09-02", look_back_days=5,
                                             limit=10)
        assert out.startswith("No global news found between")


class TestAnchorMemoization:
    def test_anchor_fetched_once_per_ticker_within_ttl(self, monkeypatch):
        calls = []

        def fake(ticker):
            calls.append(ticker)
            return ("2026-09-02", 852.03)

        monkeypatch.setattr(news_dating, "_fetch_anchor_yf", fake)
        news_dating.reset_anchor_cache()
        assert news_dating.fetch_anchor("REGN") == ("2026-09-02", 852.03)
        assert news_dating.fetch_anchor("REGN") == ("2026-09-02", 852.03)
        assert news_dating.fetch_anchor("EL") == ("2026-09-02", 852.03)
        assert sorted(calls) == ["EL", "REGN"]
        news_dating.reset_anchor_cache()
        news_dating.fetch_anchor("REGN")
        assert calls == ["REGN", "EL", "REGN"]

    def test_anchor_failure_not_cached(self, monkeypatch):
        state = {"fail": True}

        def fake(ticker):
            if state["fail"]:
                raise OSError("boom")
            return ("2026-09-02", 100.0)

        monkeypatch.setattr(news_dating, "_fetch_anchor_yf", fake)
        news_dating.reset_anchor_cache()
        assert news_dating.fetch_anchor("REGN") is None
        state["fail"] = False
        assert news_dating.fetch_anchor("REGN") == ("2026-09-02", 100.0)


class TestInstaller:
    """daily_run._ensure_news_dating swaps the shared news Tool .funcs so the
    News Analyst ToolNode and the Sentiment Analyst direct pre-fetch both see
    dated output with the anchor header."""

    def _stub_fetches(self, monkeypatch):
        arts = [_article_nested("REGN surges", "2026-08-28T14:30:00Z")]
        monkeypatch.setattr(news_dating, "fetch_articles", lambda _t, _l: arts)
        monkeypatch.setattr(news_dating, "fetch_global_articles",
                            lambda _queries, _limit: arts)
        monkeypatch.setattr(news_dating, "_fetch_anchor_yf",
                            lambda _t: ("2026-09-02", 852.03))
        news_dating.reset_anchor_cache()

    def test_ensure_news_dating_wraps_both_tools(self, monkeypatch):
        import daily_run
        from tradingagents.agents.utils import news_data_tools as ndt

        daily_run._reset_news_dating()
        originals = (ndt.get_news.func, ndt.get_global_news.func)
        try:
            daily_run._ensure_news_dating()
            self._stub_fetches(monkeypatch)
            out = ndt.get_news.func("REGN", "2026-08-25", "2026-09-02")
            assert "published 2026-08-28" in out
            assert "$852.03" in out
            assert ndt.get_news.func._wrapped_original is originals[0]
            out = ndt.get_global_news.func("2026-09-02", look_back_days=5,
                                           limit=10)
            assert "published 2026-08-28" in out
            assert ndt.get_global_news.func._wrapped_original is originals[1]
        finally:
            daily_run._reset_news_dating()
        assert ndt.get_news.func is originals[0]
        assert ndt.get_global_news.func is originals[1]

    def test_ensure_news_dating_idempotent(self, monkeypatch):
        import daily_run
        from tradingagents.agents.utils import news_data_tools as ndt

        daily_run._reset_news_dating()
        original = ndt.get_news.func
        try:
            daily_run._ensure_news_dating()
            daily_run._ensure_news_dating()
            assert ndt.get_news.func._wrapped_original is original
        finally:
            daily_run._reset_news_dating()

    def test_dating_stacks_inside_news_logging(self, monkeypatch, tmp_path):
        """Chain order (dating first, logging second) means the sentiment
        analyst's direct pre-fetch emits its fetch_end AND sees dated text."""
        import json

        import daily_run
        import structured_log
        import tradingagents.agents.analysts.sentiment_analyst as sa
        from tradingagents.agents.utils import news_data_tools as ndt

        daily_run._reset_news_dating()
        daily_run._NEWS_LOGGING_PATCHED = False
        pristine = sa.get_news.func
        try:
            daily_run._ensure_news_dating()
            daily_run._ensure_news_logging()
            self._stub_fetches(monkeypatch)
            monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path))
            logger = structured_log.StructuredRunLogger(
                ticker="AAPL", out_dir=str(tmp_path))
            structured_log.set_active_logger(logger)
            try:
                out = sa.get_news.func("REGN", "2026-08-25", "2026-09-02")
            finally:
                structured_log.clear_active_logger()
            assert "published 2026-08-28" in out
            assert "$852.03" in out
            events = [json.loads(line)
                      for line in logger.path.read_text().strip().splitlines()]
            fetch = [e for e in events if e["type"] == "fetch_end"]
            assert fetch[-1]["source"] == "yahoo_news"
        finally:
            daily_run._reset_news_dating()
            sa.get_news.func = pristine
            daily_run._NEWS_LOGGING_PATCHED = False
        assert ndt.get_news.func is pristine
