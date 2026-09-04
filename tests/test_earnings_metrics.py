"""tests/test_earnings_metrics.py — hermetic 8-K exhibit + extraction tests."""

from __future__ import annotations

import pytest

import earnings_metrics as em
import edgar
from tests.fixtures_edgar import submissions


@pytest.fixture(autouse=True)
def _no_cache_leak():
    em.reset_cache()
    yield
    em.reset_cache()


@pytest.fixture
def http(tmp_path, monkeypatch):
    routes: dict[str, bytes] = {}
    routes["company_tickers.json"] = (
        b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"}]')

    def fake_get(url: str) -> bytes:
        for key, payload in routes.items():
            if key in url:
                return payload
        raise edgar.EdgarError(f"no route for {url}")

    monkeypatch.setattr(edgar, "_http_get", fake_get)
    monkeypatch.setenv("EDGAR_CACHE_DIR", str(tmp_path / "cache"))
    edgar.clear_cache()
    return routes


INDEX_JSON = {
    "directory": {"item": [
        {"name": "regeneron-8k.htm", "size": 5000},
        {"name": "exh_991.htm", "size": 40000},
        {"name": "edgardoc.xml", "size": 100},
    ]},
}

RELEASE_HTML = """<html><body><p>REGN reports Q2 2026 revenue of
$4.29 billion, up 16.7%. GAAP EPS was $15.50. For FY27 the company
guides adjusted EPS to $60.61-$62.00.</p></body></html>"""


class TestExhibitLocation:
    def test_earnings_8k_detected(self, http):
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K", "10-Q"]))
        hit = em.find_latest_earnings_8k("REGN", window_days=60)
        assert hit is not None
        assert hit["filing_date"] == "2026-09-03"
        assert hit["accession_number"] == "0001663758-26-000002"

    def test_no_8k_in_window(self, http):
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["10-Q", "4"]))
        assert em.find_latest_earnings_8k("REGN", window_days=60) is None

    def test_exhibit_locator_prefers_ex99(self, http):
        http["index.json"] = edgar._jb(INDEX_JSON)
        name = em._pick_exhibit({"directory": {"item": [
            {"name": "regeneron-8k.htm"}, {"name": "exh_991.htm"}]}})
        assert name == "exh_991.htm"


class TestExtraction:
    def test_cached_per_filing(self, http, tmp_path, monkeypatch):
        calls = {"n": 0}

        def fake_extract(text, filing_date):
            calls["n"] += 1
            return {"revenue": "$4.29B", "eps": "$15.50", "period": "Q2 2026",
                    "guidance": "adjusted EPS $60.61-$62.00"}

        monkeypatch.setattr(em, "_call_extract_llm", fake_extract)
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        em.reset_cache()
        line1 = em.earnings_line("REGN")
        line2 = em.earnings_line("REGN")
        assert calls["n"] == 1  # second call served from cache
        assert line1 == line2
        assert "Q2 2026" in line1
        assert "revenue" in line1.lower()

    def test_failure_returns_empty(self, http, monkeypatch):
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["10-Q"]))
        assert em.earnings_line("REGN") == ""

    def test_extract_failure_returns_empty_not_raises(self, http, monkeypatch):
        def boom(_text, _date):
            raise RuntimeError("provider down")

        monkeypatch.setattr(em, "_call_extract_llm", boom)
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        em.reset_cache()
        assert em.earnings_line("REGN") == ""

    def test_extraction_persisted_across_processes(self, http, tmp_path,
                                                   monkeypatch):
        """The spec promise: cache per filing on disk so a ticker analyzed
        daily reuses the extraction all quarter (no LLM re-burn)."""
        calls = {"n": 0}

        def fake_extract(text, filing_date):
            calls["n"] += 1
            return {"period": "Q2 2026", "revenue": "$4.29B", "eps": "$15.50",
                    "guidance": ""}

        monkeypatch.setattr(em, "_call_extract_llm", fake_extract)
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        em.reset_cache()
        assert em.earnings_line("REGN") != ""
        assert calls["n"] == 1
        em.reset_cache()  # simulates a fresh process next morning
        line2 = em.earnings_line("REGN")
        assert calls["n"] == 1  # served from disk, no new extraction
        assert line2 != ""

    def test_html_stripped_before_extraction(self):
        text = em._strip_html("<p>Q2 <b>revenue</b> up</p>")
        assert "revenue" in text and "<" not in text


class TestReportedHeadline:
    def test_returns_cached_metrics_without_extracting(self, http, tmp_path,
                                                       monkeypatch):
        """The fundamentals freshness layer must read the 8-K headline from
        cache only — it must never trigger a fresh LLM extraction."""
        calls = {"n": 0}

        def fake_extract(text, filing_date):
            calls["n"] += 1
            return {"period": "Q2 2026", "revenue": "$4.29B", "eps": "$15.50",
                    "guidance": "FY26 GAAP EPS $60.61-$62.00"}

        monkeypatch.setattr(em, "_call_extract_llm", fake_extract)
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        em.reset_cache()
        assert em.earnings_line("REGN") != ""   # warms the disk cache
        assert calls["n"] == 1
        em.reset_cache()                        # fresh process simulation
        head = em.reported_headline("REGN")
        assert head is not None
        assert head["period"] == "Q2 2026"
        assert head["revenue"] == "$4.29B"
        assert head["filed"] == "2026-09-03"
        assert calls["n"] == 1                  # no new extraction

    def test_returns_none_when_cache_missing(self, http, monkeypatch):
        """Without a warm cache (and no LLM allowed), headline is None —
        the caller falls back rather than blocking on an extraction."""
        def boom(_t, _d):
            raise AssertionError("must not extract")
        monkeypatch.setattr(em, "_call_extract_llm", boom)
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        em.reset_cache()
        assert em.reported_headline("REGN") is None

    def test_no_8k_returns_none(self, http):
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["10-Q"]))
        assert em.reported_headline("REGN") is None
