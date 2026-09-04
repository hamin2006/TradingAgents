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
    def test_annual_10k_row_excluded_from_quarters(self, http):
        """A 10-K full-year row ending Dec-31 shares the Revenue tag with the
        quarters (real REGN payload); it must never masquerade as Q4."""
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(companyfacts())
        f = edgar.load_facts("REGN")
        ends = [r["end"] for r in f.quarters("Revenues", as_of="2026-08-01")]
        assert ends == ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
        # Q4'25 is the 10-Q row (3.9B), NOT the 10-K annual (15.2B)
        q4 = [r for r in f.quarters("Revenues", as_of="2026-08-01")
              if r["end"] == "2025-12-31"][0]
        assert q4["val"] == pytest.approx(3_900_000_000)

    def test_latest_quarter_excludes_future_filings(self, http):
        """Point-in-time: a 10-Q filed after the as-of date must not leak."""
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(companyfacts())
        f = edgar.load_facts("REGN")
        # as-of 2026-06-15: Q2 2026 10-Q (filed 07-24) is in the future.
        q1 = f.quarters("Revenues", as_of="2026-06-15")
        assert [r["end"] for r in q1] == ["2025-09-30", "2025-12-31", "2026-03-31"]
        ttm = f.ttm("Revenues", as_of="2026-06-15")
        assert ttm == pytest.approx((3800 + 3900 + 4000) * 1e6)
        ttm2 = f.ttm("Revenues", as_of="2026-08-01")
        assert ttm2 == pytest.approx((3800 + 3900 + 4000 + 4290) * 1e6)

    def test_amendment_dedupe_latest_filed_wins(self, http):
        """Two filings for the same quarter: the later-filed row wins."""
        raw = companyfacts()
        q2_rows = raw["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        q2_rows.append({
            "start": "2026-04-01", "end": "2026-06-30", "val": 4_300_000_000,
            "accn": "ACC-AMEND", "fy": 2026, "fp": "Q2",
            "form": "10-Q/A", "filed": "2026-08-20", "frame": "CY2026Q2"})
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(raw)
        f = edgar.load_facts("REGN")
        ttm = f.ttm("Revenues", as_of="2026-09-01")
        assert ttm == pytest.approx((3800 + 3900 + 4000 + 4300) * 1e6)


class TestTagAndComputation:
    def test_tag_fallback_chain(self, http):
        """When the canonical tag is absent, the fallback chain applies."""
        raw = companyfacts()
        fallback_rows = [r for r in raw["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
                         if r["form"] == "10-Q"]
        del raw["facts"]["us-gaap"]["Revenues"]
        raw["facts"]["us-gaap"][
            "RevenueFromContractWithCustomerExcludingAssessedTax"] = {
            "label": "x", "description": "x", "units": {"USD": fallback_rows}}
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(raw)
        f = edgar.load_facts("REGN")
        ttm = f.ttm(["RevenueFromContractWithCustomerExcludingAssessedTax",
                     "Revenues"], as_of="2026-08-01")
        assert ttm == pytest.approx((3800 + 3900 + 4000 + 4290) * 1e6)

    def test_tag_chain_prefers_newest_coverage(self, http):
        """Live PFE class (2026-09-04 QA): the company switched revenue tags
        after 2023 — the FIRST tag in the chain has rows (stale ones) while
        the second is current. 'First tag with any rows' served 2022-era
        numbers; the chain must pick the candidate with the newest coverage."""
        raw = companyfacts()
        stale = [{"start": "2023-10-01", "end": "2023-12-31",
                  "val": 9_000_000_000, "accn": "ACC-OLD", "fy": 2023,
                  "fp": "Q4", "form": "10-K", "filed": "2024-02-09",
                  "frame": "CY2023Q4"}]
        raw["facts"]["us-gaap"][
            "RevenueFromContractWithCustomerExcludingAssessedTax"] = {
            "label": "x", "description": "x", "units": {"USD": stale}}
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(raw)
        f = edgar.load_facts("REGN")
        ttm = f.ttm(["RevenueFromContractWithCustomerExcludingAssessedTax",
                     "Revenues"], as_of="2026-08-01")
        assert ttm == pytest.approx((3800 + 3900 + 4000 + 4290) * 1e6)

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
                                as_of="2026-08-01") == pytest.approx(22_900_000_000.0)
        assert f.shares_outstanding(as_of="2026-08-01") == 103_100_000
        assert f.ttm("NetCashProvidedByUsedInOperatingActivities",
                     as_of="2026-08-01") == pytest.approx(2_350_000_000.0)

    def test_shares_falls_back_to_weighted_average(self, http):
        """EL class (live QA): no dei outstanding-shares instant ever filed;
        the diluted weighted average (duration rows) is the fallback."""
        raw = companyfacts()
        del raw["facts"]["dei"]["EntityCommonStockSharesOutstanding"]
        raw["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
            "label": "x", "description": "x", "units": {"shares": [{
                "start": "2026-04-01", "end": "2026-06-30",
                "val": 105_000_000, "accn": "ACC-W", "fy": 2026, "fp": "Q2",
                "form": "10-Q", "filed": "2026-07-24", "frame": "CY2026Q2"}]}}
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = edgar._jb(raw)
        f = edgar.load_facts("REGN")
        assert f.shares_outstanding(as_of="2026-08-01") == 105_000_000


class TestClient:
    def test_fetch_error_raises_edgar_error(self, http):
        routes, _ = http
        routes["companyfacts/CIK0000872589.json"] = b"<html>rate limited</html>"
        with pytest.raises(edgar.EdgarError):
            edgar.load_facts("REGN")

    def test_throttle_and_5xx_retried_then_succeed(self, monkeypatch):
        """SEC throttles (429/403) and 5xx are transient: retry with backoff
        instead of killing the run's data."""
        from urllib.error import HTTPError

        calls = {"n": 0}

        def flaky(url):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise HTTPError(url, 429 if calls["n"] == 1 else 500,
                                "throttled", {}, None)
            return b'{"ok": true}'

        monkeypatch.setattr(edgar, "_http_get_impl", flaky)
        monkeypatch.setattr(edgar, "_MIN_INTERVAL_S", 0.0)
        monkeypatch.setattr(edgar.time, "sleep", lambda s: None)
        edgar._reset_pacing()
        assert edgar._http_get("https://x") == b'{"ok": true}'
        assert calls["n"] == 3

    def test_throttle_exhaustion_raises(self, monkeypatch):
        from urllib.error import HTTPError

        def always_429(url):
            raise HTTPError(url, 429, "throttled", {}, None)

        monkeypatch.setattr(edgar, "_http_get_impl", always_429)
        monkeypatch.setattr(edgar, "_MIN_INTERVAL_S", 0.0)
        monkeypatch.setattr(edgar.time, "sleep", lambda s: None)
        edgar._reset_pacing()
        with pytest.raises(edgar.EdgarError):
            edgar._http_get("https://x")

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
