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

    def test_missing_quarter_warning(self, facts):
        """BDX/MRNA class (live QA): a fiscal quarter absent from the rows
        must be called out — a TTM built over the gap silently undercounts."""
        ends = ["2025-06-30", "2025-12-31", "2026-03-31", "2026-06-30"]
        missing = fe._missing_quarters(ends)
        assert missing == ["2025-09-30"]

    def test_no_warning_on_contiguous_quarters(self, facts):
        ends = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
        assert fe._missing_quarters(ends) == []


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
