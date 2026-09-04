"""tests/test_edgar.py — hermetic EDGAR client tests (fixture payloads,
injected HTTP seam; no network)."""

from __future__ import annotations

import pytest

import edgar
from tests.fixtures_edgar import companyfacts, submissions


@pytest.fixture
def http(tmp_path, monkeypatch):
    """Seam registry keyed by URL substring."""
    routes: dict[str, bytes] = {}
    calls: list[str] = []
    routes["company_tickers.json"] = (
        b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"},'
        b'{"cik_str":320193,"ticker":"AAPL","title":"Apple"}]')

    def fake_get(url: str) -> bytes:
        calls.append(url)
        for key, payload in routes.items():
            if key in url:
                return payload
        raise edgar.EdgarError(f"no route for {url}")

    monkeypatch.setattr(edgar, "_http_get", fake_get)
    monkeypatch.setenv("EDGAR_CACHE_DIR", str(tmp_path / "cache"))
    edgar.clear_cache()
    return routes, calls


class TestCikResolution:
    def test_cik_map_cached_and_padded(self, http):
        routes, calls = http
        routes["company_tickers.json"] = (
            b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"},'
            b'{"cik_str":320193,"ticker":"AAPL","title":"Apple"}]')
        assert edgar.resolve_cik("REGN") == "0000872589"
        assert edgar.resolve_cik("aapl") == "0000320193"
        assert edgar.resolve_cik("REGN") == "0000872589"  # cached, no refetch
        assert len(calls) == 1
        assert "company_tickers.json" in calls[0]

    def test_unknown_ticker_raises(self, http):
        routes, _ = http
        routes["company_tickers.json"] = (
            b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"}]')
        with pytest.raises(edgar.EdgarError):
            edgar.resolve_cik("ZZZZ")


class TestAsOfSemantics:
    def test_latest_quarter_excludes_future_filings(self, http):
        """Point-in-time: a 10-Q filed after the as-of date must not leak."""
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(companyfacts())
        f = edgar.load_facts("REGN")
        # as-of 2026-06-15: Q2 2026 10-Q (filed 07-24) is in the future.
        q1 = f.quarters("Revenues", as_of="2026-06-15")
        assert [r["end"] for r in q1] == ["2025-09-30", "2025-12-31", "2026-03-31"]
        ttm = f.ttm("Revenues", as_of="2026-06-15")
        assert ttm == pytest.approx(3800 + 3900 + 4000)  # 3 quarters on file
        ttm2 = f.ttm("Revenues", as_of="2026-08-01")
        assert ttm2 == pytest.approx(3800 + 3900 + 4000 + 4290)

    def test_amendment_dedupe_latest_filed_wins(self, http):
        """Two filings for the same quarter: the later-filed row wins."""
        raw = companyfacts()
        q2_rows = raw["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        q2_rows.append({
            "start": "2026-04-01", "end": "2026-06-30", "val": 4300,
            "accn": "ACC-AMEND", "fy": 2026, "fp": "Q2",
            "form": "10-Q/A", "filed": "2026-08-20", "frame": "CY2026Q2"})
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(raw)
        f = edgar.load_facts("REGN")
        ttm = f.ttm("Revenues", as_of="2026-09-01")
        assert ttm == pytest.approx(3800 + 3900 + 4000 + 4300)  # restated 4300


class TestTagAndComputation:
    def test_tag_fallback_chain(self, http):
        """When the canonical tag is absent, the fallback chain applies."""
        raw = companyfacts()
        del raw["facts"]["us-gaap"]["Revenues"]
        raw["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = (
            raw["facts"]["us-gaap"].pop("Revenues_fy"))
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(raw)
        f = edgar.load_facts("REGN")
        assert f.ttm(["RevenueFromContractWithCustomerExcludingAssessedTax",
                      "Revenues"], as_of="2026-08-01") is not None

    def test_ttm_missing_tag_returns_none(self, http):
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(companyfacts())
        f = edgar.load_facts("REGN")
        assert f.ttm("NoSuchTag", as_of="2026-08-01") is None

    def test_latest_instant_and_derived_metrics(self, http):
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(companyfacts())
        f = edgar.load_facts("REGN")
        assert f.latest_instant("StockholdersEquity",
                                as_of="2026-08-01") == pytest.approx(22900.0)
        assert f.shares_outstanding(as_of="2026-08-01") == 103_100_000
        assert f.ttm("NetCashProvidedByUsedInOperatingActivities",
                     as_of="2026-08-01") == pytest.approx(2350.0)


class TestClient:
    def test_fetch_error_raises_edgar_error(self, http):
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = b"<html>rate limited</html>"
        with pytest.raises(edgar.EdgarError):
            edgar.load_facts("REGN")

    def test_disk_cache_avoids_refetch(self, http, tmp_path):
        routes, calls = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(companyfacts())
        edgar.load_facts("REGN")
        edgar.load_facts("REGN")
        assert sum("companyfacts" in c for c in calls) == 1

    def test_submissions_listing(self, http):
        routes, _ = http
        routes["submissions/CIK0000872589.json"] = edgar._jb(submissions())
        s = edgar.load_submissions("REGN")
        forms = [x["form"] for x in s.recent("2026-08-25")]
        assert forms == ["4", "4", "8-K"]
