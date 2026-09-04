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
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K", "10-Q"]))
        hit = em.find_latest_earnings_8k("REGN", window_days=60)
        assert hit is not None
        assert hit["filing_date"] == "2026-09-03"
        assert hit["accession_number"] == "0001663758-26-000002"

    def test_non_earnings_8k_is_skipped(self, http):
        """INCY 2026-08-31 class: an 8-K that is not an earnings release
        (XBRL notice / cover text, no quarterly results) must NOT be treated
        as one — its extraction would be junk."""
        http["index.json"] = edgar._jb({
            "directory": {"item": [
                {"name": "incy-20260831.htm", "size": 40000},
                {"name": "R1.htm", "size": 500},
            ]}})
        http["incy-20260831.htm"] = (
            b"<html><body>UNITED STATES SECURITIES AND EXCHANGE COMMISSION "
            b"FORM 8-K CURRENT REPORT PURSUANT TO SECTION 13 OR 15(d) "
            b"Item 8.01 Other Events.</body></html>")
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        assert em.find_latest_earnings_8k("REGN", window_days=60) is None

    def test_real_earnings_8k_behind_newer_non_earnings_one_wins(self, http):
        """INCY 2026-09-04 live shape: newest 8-K (8/31, XBRL) is junk; the
        real Q2 release sits one filing earlier (7/28, exhibit
        incy-q22026xexx991.htm). The probe must walk past the junk."""
        import json as _json
        junk_index = {"directory": {"item": [
            {"name": "incy-20260831.htm", "size": 40000},
            {"name": "R1.htm", "size": 500},
        ]}}
        # The submissions fixture's first (newest) 8-K gets the junk index;
        # the archive URL embeds the dashless accession, so route by it.
        http["000166375826000058/index.json"] = _json.dumps(junk_index).encode()
        http["incy-20260831.htm"] = (
            b"<html>FORM 8-K CURRENT REPORT Item 8.01 Other Events.</html>")
        # Second (older) 8-K = the earnings release with a 991-variant exhibit.
        http["000166375826000053/index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = (
            b"<html><body>Incyte Reports Second Quarter 2026 Financial "
            b"Results: total revenue $1,000M, GAAP net income $1.50 per "
            b"diluted share.</body></html>")
        forms = ["8-K", "8-K"]
        accessions = ["0001663758-26-000058", "0001663758-26-000053"]
        dates = ["2026-08-31", "2026-07-28"]
        docs = ["incy-20260831.htm", "incy-20260728.htm"]
        subs = {
            "cik": "872589", "name": "INCYTE CORP", "tickers": ["REGN"],
            "filings": {"recent": {"accessionNumber": accessions,
                                   "form": forms, "filingDate": dates,
                                   "primaryDocument": docs}},
        }
        http["submissions/CIK0000872589.json"] = edgar._jb(subs)
        hit = em.find_latest_earnings_8k("REGN", window_days=180)
        assert hit is not None
        assert hit["filing_date"] == "2026-07-28"  # the REAL earnings 8-K

    def test_no_8k_in_window(self, http):
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["10-Q", "4"]))
        assert em.find_latest_earnings_8k("REGN", window_days=60) is None

    def test_exhibit_locator_prefers_ex99(self, http):
        http["index.json"] = edgar._jb(INDEX_JSON)
        name = em._pick_exhibit({"directory": {"item": [
            {"name": "regeneron-8k.htm"}, {"name": "exh_991.htm"}]}})
        assert name == "exh_991.htm"

    def test_exhibit_locator_skips_image_assets(self):
        """Real 8-K indexes list exhibit IMAGES whose names carry 99 markers
        (INCY 2026-09-04: 'incy-20220802xex99d1001.jpg' was picked over the
        actual press-release htm — binary junk). Only HTML docs qualify."""
        idx = {"directory": {"item": [
            {"name": "incy-20220802xex99d1001.jpg", "size": 90000},
            {"name": "incy-q22026xexx991.htm", "size": 40000},
            {"name": "R1.htm", "size": 500},
        ]}}
        assert em._pick_exhibit(idx) == "incy-q22026xexx991.htm"

    def test_acquisition_press_release_is_not_an_earnings_8k(self, http):
        """Live 2026-09-04 INCY class: an 8-K whose exhibit is an
        acquisition PR ('Incyte Completes Acquisition of Vega...') must not
        pass the earnings probe — no narrative earnings verb + period."""
        http["index.json"] = edgar._jb({
            "directory": {"item": [
                {"name": "ax2026skylineclosingxex99.htm", "size": 40000},
            ]}})
        http["ax2026skylineclosingxex99.htm"] = (
            b"<html><body>EX-99.1 FOR IMMEDIATE RELEASE Incyte Completes "
            b"Acquisition of Vega Therapeutics, Expanding its Hematology "
            b"Portfolio. The acquisition adds VGA039, a novel investigational "
            b"monoclonal antibody in Phase 1. Revenue synergies are expected "
            b"in the third quarter of 2027.</body></html>")
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        assert em.find_latest_earnings_8k("REGN", window_days=180) is None


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

    def test_junk_cached_metrics_return_none(self, http, monkeypatch):
        """A stored extraction with no usable figures (INCY 8/31 era junk:
        "revenue not provided") must not render as a headline."""
        http["index.json"] = edgar._jb(INDEX_JSON)
        http["exh_991.htm"] = RELEASE_HTML.encode()
        http["submissions/CIK0000872589.json"] = edgar._jb(
            submissions(extra_forms=["8-K"]))
        em.reset_cache()
        with em._lock:
            em._cache[("REGN", "0001663758-26-000002")] = {
                "period": "period ended August 31, 2026",
                "revenue": "not provided", "eps": "not provided",
                "guidance": "not provided", "filed": "2026-08-31"}
        assert em.reported_headline("REGN") is None
