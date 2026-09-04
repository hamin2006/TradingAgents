"""tests/test_fundamentals_edgar.py — hermetic fundamentals renderer tests.

Fixture companyfacts payload + stubbed price/consensus/identity seams; no
network. Verifies the composition rule: EDGAR statements/metrics, consensus
fields labeled as such, price-derived fields computed (never quoted from a
stale quote blob), quote-price fields absent from the payload.
"""

from __future__ import annotations

import pytest

import edgar
import fundamentals_edgar as fe
from tests.fixtures_edgar import companyfacts


@pytest.fixture
def facts():
    return edgar.Facts(companyfacts())


def _identity():
    return {"company_name": "Regeneron Pharmaceuticals, Inc.",
            "sector": "Healthcare", "industry": "Biotechnology"}


def _consensus():
    return {"forward_eps": 60.61, "target_mean_price": 900.0,
            "dividend_rate": 0.0, "dividend_yield": 0.0}


class _FakeEarnings:
    """Stand-in for the earnings_metrics module attribute (headline only)."""

    def __init__(self, headline):
        self._headline = headline

    def reported_headline(self, ticker):
        return self._headline


class TestFundamentalsPayload:
    def test_edgar_metrics_and_quarters_note(self, facts):
        out = fe.render_fundamentals(facts, "REGN", "2026-08-01",
                                     price=852.03, identity=_identity(),
                                     consensus=_consensus())
        assert "Revenue (TTM)" in out
        assert "15,990.0" in out  # Q3'25..Q2'26 = 3800+3900+4000+4290
        assert "4 quarters on file" in out
        assert "Operating Income (TTM)" in out

    def test_young_filer_quarters_note(self, facts):
        out = fe.render_fundamentals(facts, "REGN", "2026-06-15",
                                     price=852.03, identity=_identity(),
                                     consensus=_consensus())
        assert "3 quarters on file" in out

    def test_consensus_labeled_and_computed_valuation(self, facts):
        out = fe.render_fundamentals(facts, "REGN", "2026-08-01",
                                     price=852.03, identity=_identity(),
                                     consensus=_consensus())
        assert "Forward EPS consensus (Yahoo)" in out
        # market cap = 103.1M shares * 852.03 -> 87,844.3 (USD M)
        assert "87,844.3" in out
        assert "Yahoo quote" in out  # source-date labeling

    def test_no_stale_quote_price_fields(self, facts):
        out = fe.render_fundamentals(facts, "REGN", "2026-08-01",
                                     price=852.03, identity=_identity(),
                                     consensus=_consensus())
        for banned in ("50 Day Average", "200 Day Average", "52 Week"):
            assert banned not in out

    def test_no_price_or_consensus_degrades_gracefully(self, facts):
        out = fe.render_fundamentals(facts, "REGN", "2026-08-01",
                                     price=None, identity=_identity(),
                                     consensus={})
        assert "Market Cap" not in out
        assert "Forward EPS consensus" not in out
        assert "Revenue (TTM)" in out  # EDGAR core survives

    def test_quarter_gap_detected(self):
        """BDX/MRNA class (live QA): a fiscal quarter absent from the rows
        must be caught by start/end adjacency (not calendar ends — PFE's
        13-week quarters end 06-28, never a calendar month-end)."""
        rows = [
            {"start": "2025-04-01", "end": "2025-06-30"},
            {"start": "2025-10-01", "end": "2025-12-31"},  # Jul-Sep missing
            {"start": "2026-01-01", "end": "2026-03-31"},
            {"start": "2026-04-01", "end": "2026-06-30"},
        ]
        gaps = fe._quarter_gaps(rows)
        assert len(gaps) == 1
        assert "2025-06-30" in gaps[0]

    def test_no_gap_on_contiguous_13week_filer(self):
        """PFE-class cadence (quarters ending off calendar month-ends) must
        not false-positive."""
        rows = [
            {"start": "2025-10-01", "end": "2025-12-27"},
            {"start": "2025-12-28", "end": "2026-03-28"},
            {"start": "2026-03-29", "end": "2026-06-27"},
            {"start": "2026-06-28", "end": "2026-09-26"},
        ]
        assert fe._quarter_gaps(rows) == []

    def test_structural_quality_passes_on_sound_payload(self, facts):
        assert fe.structural_quality(facts, "2026-08-01") == []

    def test_structural_quality_flags_stale_statements(self, facts):
        reasons = fe.structural_quality(facts, "2026-11-15")
        assert any("old" in r for r in reasons)

    def test_structural_quality_flags_few_quarters(self, facts):
        reasons = fe.structural_quality(facts, "2026-06-15")
        assert any("quarters on file" in r for r in reasons)

    def test_structural_quality_flags_missing_shares(self, facts):
        import edgar
        from tests.fixtures_edgar import companyfacts

        raw = companyfacts()
        del raw["facts"]["dei"]["EntityCommonStockSharesOutstanding"]
        stripped = edgar.Facts(raw)
        reasons = fe.structural_quality(stripped, "2026-08-01")
        assert any("shares" in r for r in reasons)

    def test_payload_for_raises_on_quality_gate(self, monkeypatch, tmp_path):
        """payload_for must refuse to serve structurally broken EDGAR data
        (the installer then falls back to yfinance) — wrong-but-plausible
        never reaches a debate."""
        import edgar as edgar_mod
        from tests.fixtures_edgar import companyfacts

        monkeypatch.setattr(fe, "_yf_info_min", lambda t: {})
        monkeypatch.setattr(fe, "_last_close", lambda t: 100.0)
        monkeypatch.setenv("EDGAR_CACHE_DIR", str(tmp_path / "cache"))

        routes = {
            "company_tickers.json": (
                b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"}]'),
            "companyfacts/CIK0000872589.json":
                edgar_mod._jb(companyfacts()),
        }

        def fake_get(url: str) -> bytes:
            for key, payload in routes.items():
                if key in url:
                    return payload
            raise edgar_mod.EdgarError(f"no route for {url}")

        monkeypatch.setattr(edgar_mod, "_http_get", fake_get)
        edgar_mod.clear_cache()
        with pytest.raises(edgar_mod.EdgarError, match="quality gate"):
            fe.payload_for("REGN", "2026-11-15")  # statements 138d old


class TestStatementRenderers:
    def test_income_quarterly_columns(self, facts):
        out = fe.render_income_statement(facts, "REGN", "2026-08-01", "quarterly")
        assert "2026-06-30" in out  # most recent quarter-end column
        assert "Revenue" in out and "Net Income" in out

    def test_balance_sheet_latest(self, facts):
        out = fe.render_balance_sheet(facts, "REGN", "2026-08-01", "quarterly")
        assert "Stockholders Equity" in out
        assert "33,000" in out or "33000" in out  # assets at 2026-06-30

    def test_cashflow_buybacks(self, facts):
        out = fe.render_cashflow(facts, "REGN", "2026-08-01", "quarterly")
        assert "Buybacks" in out or "repurchase" in out.lower()


def _fake_edgar(monkeypatch, tmp_path, raw=None):
    """Wire the companyfacts route + price/consensus seams; returns routes."""
    import edgar as edgar_mod
    from tests.fixtures_edgar import companyfacts

    monkeypatch.setattr(fe, "_yf_info_min", lambda t: {})
    monkeypatch.setattr(fe, "_last_close", lambda t: 100.0)
    monkeypatch.setenv("EDGAR_CACHE_DIR", str(tmp_path / "cache"))
    routes = {
        "company_tickers.json": (
            b'[{"cik_str":872589,"ticker":"REGN","title":"Regeneron"}]'),
        "companyfacts/CIK0000872589.json":
            edgar_mod._jb(raw if raw is not None else companyfacts()),
    }

    def fake_get(url: str) -> bytes:
        for key, payload in routes.items():
            if key in url:
                return payload
        raise edgar_mod.EdgarError(f"no route for {url}")

    monkeypatch.setattr(edgar_mod, "_http_get", fake_get)
    edgar_mod.clear_cache()
    return routes


class TestFreshnessLayer:
    def test_stale_with_headline_renders_instead_of_raising(
            self, monkeypatch, tmp_path):
        """INCY class: statements 120d+ old but the 8-K headline is cached —
        serve EDGAR statements + the announced quarter instead of raising."""
        _fake_edgar(monkeypatch, tmp_path)
        monkeypatch.setattr(fe, "earnings_metrics",
                            _FakeEarnings({"period": "Q2 2026",
                                           "revenue": "$4,291M",
                                           "eps": "$12.23",
                                           "filed": "2026-07-30",
                                           "guidance": ""}))
        out = fe.payload_for("REGN", "2026-11-15")  # statements 138d old
        assert "Latest reported quarter (Q2 2026, 8-K filed 2026-07-30" in out
        assert "Announced quarter (8-K, 10-Q pending)" in out
        assert "Revenue (TTM)" in out  # EDGAR statements still served
        assert "Latest filed quarter-end (statements)" in out

    def test_stale_without_headline_raises(self, monkeypatch, tmp_path):
        """Staleness-only with NO cached headline (cold cache / no 8-K):
        no fresh source exists anywhere — the Yahoo fallback is correct."""
        _fake_edgar(monkeypatch, tmp_path)
        monkeypatch.setattr(fe, "earnings_metrics", _FakeEarnings(None))
        with pytest.raises(edgar.EdgarError, match="quality gate"):
            fe.payload_for("REGN", "2026-11-15")

    def test_fatal_gap_still_raises_even_with_headline(
            self, monkeypatch, tmp_path):
        """Fatal structural problems (quarter gaps after derivation, no
        shares) are never papered over by a headline — Yahoo fallback stays
        correct."""
        from tests.fixtures_edgar import companyfacts

        raw = companyfacts()
        rows = raw["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
        raw["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [
            r for r in rows if r["end"] not in ("2025-09-30", "2025-12-31",
                                                "2026-03-31", "2026-06-30")]
        del raw["facts"]["dei"]["EntityCommonStockSharesOutstanding"]
        _fake_edgar(monkeypatch, tmp_path, raw=raw)
        monkeypatch.setattr(fe, "earnings_metrics",
                            _FakeEarnings({"period": "Q2 2026",
                                           "filed": "2026-07-30"}))
        with pytest.raises(edgar.EdgarError):
            fe.payload_for("REGN", "2026-08-01")

    def test_render_fundamentals_headline_row_off_by_default(self, facts):
        """headline defaults to None: existing renderers unchanged."""
        out = fe.render_fundamentals(facts, "REGN", "2026-08-01",
                                     price=852.03, identity=_identity(),
                                     consensus=_consensus())
        assert "Announced quarter (8-K, 10-Q pending)" not in out
