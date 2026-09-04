"""tests/test_edgar_installers.py — hermetic installer tests for the EDGAR
fundamentals swap (config-gated, fallback) and the tape/events context
injection."""

from __future__ import annotations

import pytest

import daily_run
import edgar
import fundamentals_edgar


@pytest.fixture(autouse=True)
def _clean_installer_state():
    """Prior test files may leave the module-level installers patched; each
    test starts from a pristine seam."""
    daily_run._reset_tape_and_events()
    daily_run._reset_edgar_fundamentals()
    yield
    daily_run._reset_tape_and_events()
    daily_run._reset_edgar_fundamentals()


class TestEdgarFundamentalsInstaller:
    def test_gated_off_by_default(self):
        """fundamentals_source yfinance (default) -> no tool swap."""

        daily_run._reset_edgar_fundamentals()
        try:
            daily_run._ensure_edgar_fundamentals({"fundamentals_source": "yfinance"})
            assert not daily_run._EDGAR_FUNDAMENTALS_PATCHED
        finally:
            daily_run._reset_edgar_fundamentals()

    def test_swap_and_reset(self):
        from tradingagents.agents.utils import fundamental_data_tools as fdt

        daily_run._reset_edgar_fundamentals()
        originals = {n: getattr(fdt, n).func for n in
                     ("get_fundamentals", "get_balance_sheet")}
        try:
            daily_run._ensure_edgar_fundamentals(
                {"fundamentals_source": "edgar"})
            assert daily_run._EDGAR_FUNDAMENTALS_PATCHED
            assert fdt.get_fundamentals.func is not originals["get_fundamentals"]
            assert (fdt.get_fundamentals.func._wrapped_original
                    is originals["get_fundamentals"])
        finally:
            daily_run._reset_edgar_fundamentals()
        assert fdt.get_fundamentals.func is originals["get_fundamentals"]
        assert fdt.get_balance_sheet.func is originals["get_balance_sheet"]

    def test_edgar_payload_served_when_healthy(self, monkeypatch):
        from tradingagents.agents.utils import fundamental_data_tools as fdt

        daily_run._reset_edgar_fundamentals()
        monkeypatch.setattr(
            fundamentals_edgar, "payload_for",
            lambda t, d: f"EDGAR-PAYLOAD-{t}")
        try:
            daily_run._ensure_edgar_fundamentals(
                {"fundamentals_source": "edgar"})
            out = fdt.get_fundamentals.func("REGN", "2026-09-03")
            assert out == "EDGAR-PAYLOAD-REGN"
        finally:
            daily_run._reset_edgar_fundamentals()

    def test_edgar_failure_falls_back_to_yfinance(self, monkeypatch):
        """An EdgarError (ingest/parse) must serve the recorded original —
        fundamentals never go dark mid-batch."""
        from tradingagents.agents.utils import fundamental_data_tools as fdt

        daily_run._reset_edgar_fundamentals()
        fdt.get_fundamentals.func = lambda t, d: "YF-FALLBACK"  # fake original

        def boom(_t, _d):
            raise edgar.EdgarError("no net")

        monkeypatch.setattr(fundamentals_edgar, "payload_for", boom)
        try:
            daily_run._ensure_edgar_fundamentals(
                {"fundamentals_source": "edgar"})
            out = fdt.get_fundamentals.func("REGN", "2026-09-03")
            assert out == "YF-FALLBACK"
        finally:
            daily_run._reset_edgar_fundamentals()
            fdt.get_fundamentals.func = fdt.get_fundamentals.func._wrapped_original \
                if hasattr(fdt.get_fundamentals.func, "_wrapped_original") else None

    def test_unexpected_edgar_error_still_falls_back(self, monkeypatch):
        """The never-dark guarantee covers UNEXPECTED failures too: a latent
        bug or a freak XBRL shape raising KeyError/TypeError deep in the
        render path must never leave an agent with a tool error — log loudly
        and serve the recorded yfinance original."""
        from tradingagents.agents.utils import fundamental_data_tools as fdt

        daily_run._reset_edgar_fundamentals()
        fdt.get_fundamentals.func = lambda t, d: "YF-FALLBACK"  # fake original

        def boom(_t, _d):
            raise KeyError("freak XBRL shape")

        monkeypatch.setattr(fundamentals_edgar, "payload_for", boom)
        try:
            daily_run._ensure_edgar_fundamentals(
                {"fundamentals_source": "edgar"})
            out = fdt.get_fundamentals.func("REGN", "2026-09-03")
            assert out == "YF-FALLBACK"
        finally:
            daily_run._reset_edgar_fundamentals()
            fdt.get_fundamentals.func = fdt.get_fundamentals.func._wrapped_original \
                if hasattr(fdt.get_fundamentals.func, "_wrapped_original") else None


class TestTapeAndEventsInstaller:
    def _install_over_fake(self, monkeypatch):
        import tradingagents.graph.trading_graph as tg_mod

        monkeypatch.setattr(daily_run, "_instrument_extras",
                            lambda t: f"EXTRAS-FOR-{t}")

        def fake_resolve(self, ticker, asset_type="stock"):
            return f"BASE-{ticker}-{asset_type}"

        daily_run._reset_tape_and_events()  # clear any prior suite pollution
        monkeypatch.setattr(tg_mod.TradingAgentsGraph,
                            "resolve_instrument_context", fake_resolve)
        daily_run._ensure_tape_and_events()
        return tg_mod

    def test_context_gains_extras_for_stocks(self, monkeypatch):
        tg_mod = self._install_over_fake(monkeypatch)
        try:
            wrap = tg_mod.TradingAgentsGraph.resolve_instrument_context
            out = wrap(None, "REGN")
            assert out == "BASE-REGN-stock\n\nEXTRAS-FOR-REGN"
            # non-stock assets skip the extras block
            assert wrap(None, "BTC", asset_type="crypto") == "BASE-BTC-crypto"
        finally:
            daily_run._reset_tape_and_events()

    def test_extras_empty_leaves_context_unchanged(self, monkeypatch):
        import tradingagents.graph.trading_graph as tg_mod

        monkeypatch.setattr(daily_run, "_instrument_extras", lambda t: "")
        daily_run._reset_tape_and_events()
        try:
            tg_mod.TradingAgentsGraph.resolve_instrument_context = (
                lambda self, t, asset_type="stock": f"BASE-{t}")
            daily_run._ensure_tape_and_events()
            wrap = tg_mod.TradingAgentsGraph.resolve_instrument_context
            assert wrap(None, "REGN") == "BASE-REGN"
        finally:
            daily_run._reset_tape_and_events()

    def test_reset_restores_seam(self, monkeypatch):
        tg_mod = self._install_over_fake(monkeypatch)
        daily_run._reset_tape_and_events()
        # fake_resolve from the monkeypatch is still in place after reset
        assert tg_mod.TradingAgentsGraph.resolve_instrument_context(
            None, "REGN") == "BASE-REGN-stock"
        assert not daily_run._TAFE_PATCHED

    def test_idempotent(self, monkeypatch):
        tg_mod = self._install_over_fake(monkeypatch)
        try:
            daily_run._ensure_tape_and_events()
            wrap = tg_mod.TradingAgentsGraph.resolve_instrument_context
            assert wrap(None, "REGN") == "BASE-REGN-stock\n\nEXTRAS-FOR-REGN"
        finally:
            daily_run._reset_tape_and_events()
