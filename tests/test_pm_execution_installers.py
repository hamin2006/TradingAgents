"""PM-execution installers + decision-card wiring tests (hermetic)."""

import json
from unittest.mock import patch

import pytest

import daily_run
import structured_log
import tradingagents.agents.managers.portfolio_manager as pm_mod
from tradingagents.agents.schemas import PortfolioDecision
from tradingagents.agents.utils import memory as memory_mod, structured as structured_mod


@pytest.fixture
def cfg(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    c = DEFAULT_CONFIG.copy()
    c["results_dir"] = str(tmp_path / "results")
    c["data_cache_dir"] = str(tmp_path / "cache")
    c["memory_log_path"] = str(tmp_path / "memory" / "trading_memory.md")
    return c


@pytest.fixture(autouse=True)
def _isolate_structured_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))


@pytest.fixture
def pm_cfg(cfg):
    cfg["execution_intent"] = True
    return cfg


def _pm_decision(payload: dict):
    from pm_execution import ExecutionPortfolioDecision
    return ExecutionPortfolioDecision(
        rating=payload.get("rating", "Overweight"),
        executive_summary=payload.get("executive_summary", "s"),
        investment_thesis=payload.get("investment_thesis", "t"),
        **({"execution": payload["execution"]} if payload.get("execution") is not None else {}),
    )


# --- schema swap installer -------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_installers(monkeypatch):
    """Each test starts from pristine installer state (module globals leak
    across tests in one process otherwise) and never touches the network:
    the card reference close is stubbed unless a test overrides it."""
    import tradingagents.dataflows.reddit as reddit_mod

    daily_run._PM_SCHEMA_PATCHED = False
    daily_run._CARD_INJECTION_PATCHED = False
    daily_run._CARD_CLOSE_CACHE.clear()
    monkeypatch.setattr(daily_run, "_last_close", lambda t: None)
    daily_run._reset_pm_execution_schema()
    daily_run._reset_decision_card_injection()
    rss_original = reddit_mod._fetch_subreddit_rss
    json_original = reddit_mod._fetch_subreddit_json
    yield
    daily_run._reset_pm_execution_schema()
    daily_run._reset_decision_card_injection()
    # run_analyze's installer chain permanently patches the reddit fetchers;
    # undo it so later test files (test_reddit_fallback) see pristine state.
    reddit_mod._fetch_subreddit_rss = rss_original
    reddit_mod._fetch_subreddit_json = json_original
    daily_run._REDDIT_PATCHED = False
    daily_run._REDDIT_OAUTH_PATCHED = False
    daily_run._REDDIT_ARCHIVE_PATCHED = False
    daily_run._STOCKTWITS_PATCHED = False


class TestSchemaSwapInstaller:
    def test_swap_and_reset_restore_original(self, pm_cfg):
        daily_run._PM_SCHEMA_PATCHED = False
        daily_run._ensure_pm_execution_schema(pm_cfg)
        import tradingagents.agents.schemas as schemas_mod
        from pm_execution import ExecutionPortfolioDecision
        assert schemas_mod.PortfolioDecision is ExecutionPortfolioDecision
        assert pm_mod.PortfolioDecision is ExecutionPortfolioDecision
        daily_run._reset_pm_execution_schema()
        import tradingagents.agents.schemas as schemas_mod
        assert schemas_mod.PortfolioDecision is PortfolioDecision
        assert pm_mod.PortfolioDecision is PortfolioDecision

    def test_disabled_config_does_not_swap(self, cfg):
        cfg["execution_intent"] = False
        daily_run._PM_SCHEMA_PATCHED = False
        daily_run._ensure_pm_execution_schema(cfg)
        import tradingagents.agents.schemas as schemas_mod
        assert schemas_mod.PortfolioDecision is PortfolioDecision

    def test_idempotent(self, pm_cfg):
        daily_run._PM_SCHEMA_PATCHED = False
        daily_run._ensure_pm_execution_schema(pm_cfg)
        first = pm_mod.PortfolioDecision
        daily_run._ensure_pm_execution_schema(pm_cfg)
        assert pm_mod.PortfolioDecision is first


# --- structured-output capture --------------------------------------------


class TestPmDecisionCapture:
    def test_captures_pm_structured_decision(self, pm_cfg):
        daily_run._PM_SCHEMA_PATCHED = False
        daily_run._ensure_pm_execution_schema(pm_cfg)
        fake = type("FakeLLM", (), {"invoke": lambda self, p: _pm_decision({
            "rating": "Sell", "execution": {"orders": [
                {"kind": "SELL", "shares": 2, "limit_px": 100.5}]}})})()
        daily_run._clear_pm_capture()
        text = structured_mod.invoke_structured_or_freetext(
            fake, None, "prompt", lambda d: f"RENDERED {d.rating}", "Portfolio Manager")
        assert text.startswith("RENDERED")
        captured = daily_run._pop_pm_capture()
        assert captured is not None
        assert captured["rating"] == "Sell"
        assert captured["execution"]["orders"][0]["limit_px"] == 100.5

    def test_non_pm_agents_not_captured(self, pm_cfg):
        daily_run._PM_SCHEMA_PATCHED = False
        daily_run._ensure_pm_execution_schema(pm_cfg)
        fake = type("FakeLLM", (), {"invoke": lambda self, p: _pm_decision({})})()
        daily_run._clear_pm_capture()
        structured_mod.invoke_structured_or_freetext(
            fake, None, "p", lambda d: "x", "Trader")
        assert daily_run._pop_pm_capture() is None

    def test_freetext_fallback_not_captured(self, pm_cfg):
        daily_run._PM_SCHEMA_PATCHED = False
        daily_run._ensure_pm_execution_schema(pm_cfg)
        plain = type("FakeLLM", (),
                     {"invoke": lambda self, p: type("R", (), {"content": "text"})()})()
        daily_run._clear_pm_capture()
        text = structured_mod.invoke_structured_or_freetext(
            None, plain, "p", lambda d: "x", "Portfolio Manager")
        assert text == "text"
        assert daily_run._pop_pm_capture() is None

    def test_reset_restores_original_function(self, pm_cfg):
        daily_run._PM_SCHEMA_PATCHED = False
        original = structured_mod.invoke_structured_or_freetext
        daily_run._ensure_pm_execution_schema(pm_cfg)
        assert structured_mod.invoke_structured_or_freetext is not original
        daily_run._reset_pm_execution_schema()
        assert structured_mod.invoke_structured_or_freetext is original


# --- decision-card write ---------------------------------------------------


def _card_file(cfg, ticker):
    import decision_cards
    return decision_cards.cards_file(cfg["results_dir"], ticker)


class TestCardWrite:
    def _stash_and_write(self, cfg, ticker="EL", payload=None, rating="Underweight"):
        daily_run._clear_pm_capture()
        if payload is not None:
            daily_run._stash_pm_capture(_pm_decision(payload).model_dump(mode="json"))
        card = daily_run._write_decision_card(
            ticker, "2026-09-05", cfg, rating)
        return card

    def test_writes_card_from_capture(self, pm_cfg, tmp_path, monkeypatch):
        monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
        run_log = structured_log.StructuredRunLogger(ticker="EL", today="2026-09-05",
                                                     out_dir=str(tmp_path / "structured"))
        structured_log.set_active_logger(run_log)
        try:
            card = self._stash_and_write(pm_cfg, payload={
                "rating": "Underweight",
                "executive_summary": "exit thesis",
                "execution": {"orders": [{"kind": "SELL", "shares": 2}],
                              "future_notes": "redeploy below 95"}})
        finally:
            structured_log.clear_active_logger()
        assert card is not None
        assert card["date"] == "2026-09-05" and card["ticker"] == "EL"
        assert card["rating"] == "Underweight"
        assert card["executive_summary"] == "exit thesis"
        assert card["execution"]["future_notes"] == "redeploy below 95"
        assert card["schema_version"] == 1
        lines = _card_file(pm_cfg, "EL").read_text().strip().splitlines()
        assert json.loads(lines[-1])["ticker"] == "EL"

    def test_rating_flip_event_on_prior_card(self, pm_cfg, tmp_path, monkeypatch):
        monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
        import decision_cards
        decision_cards.append_card(pm_cfg["results_dir"], {
            "date": "2026-09-04", "ticker": "EL", "rating": "Overweight",
            "executive_summary": "buy", "schema_version": 1})
        run_log = structured_log.StructuredRunLogger(ticker="EL", today="2026-09-05",
                                                     out_dir=str(tmp_path / "structured"))
        structured_log.set_active_logger(run_log)
        try:
            self._stash_and_write(pm_cfg, payload={"rating": "Underweight",
                                                   "execution": {}})
        finally:
            structured_log.clear_active_logger()
        events = [json.loads(line) for line in run_log.path.read_text().splitlines()]
        flips = [e for e in events if e["type"] == "rating_flip"]
        assert len(flips) == 1
        assert flips[0]["old"] == "Overweight"
        assert flips[0]["new"] == "Underweight"

    def test_no_flip_event_when_rating_unchanged(self, pm_cfg, tmp_path, monkeypatch):
        monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
        import decision_cards
        decision_cards.append_card(pm_cfg["results_dir"], {
            "date": "2026-09-04", "ticker": "EL", "rating": "Underweight",
            "executive_summary": "s", "schema_version": 1})
        run_log = structured_log.StructuredRunLogger(ticker="EL", today="2026-09-05",
                                                     out_dir=str(tmp_path / "structured"))
        structured_log.set_active_logger(run_log)
        try:
            self._stash_and_write(pm_cfg, payload={"rating": "Underweight"})
        finally:
            structured_log.clear_active_logger()
        events = [json.loads(line) for line in run_log.path.read_text().splitlines()]
        assert not [e for e in events if e["type"] == "rating_flip"]

    def test_no_capture_no_card_no_crash(self, pm_cfg):
        assert self._stash_and_write(pm_cfg, payload=None) is None
        assert not _card_file(pm_cfg, "EL").exists()

    def test_disabled_config_writes_nothing(self, cfg):
        cfg["execution_intent"] = False
        assert self._stash_and_write(cfg, payload={"rating": "Buy",
                                                   "execution": {}}) is None
        assert not _card_file(cfg, "EL").exists()

    def test_emit_execution_intent_absent_on_freetext(self, pm_cfg, tmp_path,
                                                      monkeypatch):
        monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
        run_log = structured_log.StructuredRunLogger(ticker="EL", today="2026-09-05",
                                                     out_dir=str(tmp_path / "structured"))
        structured_log.set_active_logger(run_log)
        try:
            self._stash_and_write(pm_cfg, payload=None, rating="Buy")
        finally:
            structured_log.clear_active_logger()
        events = [json.loads(line) for line in run_log.path.read_text().splitlines()]
        assert any(e["type"] == "execution_intent" and e["status"] == "absent"
                   for e in events)


# --- past_context injection ------------------------------------------------


@pytest.fixture
def memory_log(pm_cfg):
    log = memory_mod.TradingMemoryLog(pm_cfg)
    log.store_decision("EL", "2026-08-20",
                       "**Rating**: Overweight\n\nDECISION text")
    log.update_with_outcome("EL", "2026-08-20", raw_return=-0.5,
                            alpha_return=-0.2, holding_days=3,
                            reflection="faded")
    return log


class TestPastContextInjection:
    def _install(self, pm_cfg):
        daily_run._CARD_INJECTION_PATCHED = False
        daily_run._ensure_decision_card_injection(pm_cfg)

    def test_lessons_still_returned_without_cards(self, pm_cfg, memory_log):
        self._install(pm_cfg)
        ctx = memory_log.get_past_context("EL", n_same=5, n_cross=3)
        assert "Past analyses of EL" in ctx
        assert "Prior PM decisions on EL" not in ctx
        daily_run._reset_decision_card_injection()

    def test_stable_injects_latest_card(self, pm_cfg, memory_log):
        import decision_cards
        decision_cards.append_card(pm_cfg["results_dir"],
                                   _card("2026-09-03", "Overweight"))
        decision_cards.append_card(pm_cfg["results_dir"],
                                   _card("2026-09-04", "Overweight"))
        self._install(pm_cfg)
        ctx = memory_log.get_past_context("EL", n_same=5, n_cross=3)
        assert ctx.count("[2026-09-04] Overweight") == 1
        assert "[2026-09-03]" not in ctx
        daily_run._reset_decision_card_injection()

    def test_flip_injects_arc(self, pm_cfg, memory_log):
        import decision_cards
        decision_cards.append_card(pm_cfg["results_dir"],
                                   _card("2026-09-03", "Overweight"))
        decision_cards.append_card(pm_cfg["results_dir"],
                                   _card("2026-09-04", "Underweight"))
        self._install(pm_cfg)
        ctx = memory_log.get_past_context("EL", n_same=5, n_cross=3)
        assert "[2026-09-04] Underweight" in ctx
        assert "[2026-09-03] Overweight" in ctx
        daily_run._reset_decision_card_injection()

    def test_expired_cards_not_injected(self, pm_cfg, memory_log):
        import decision_cards
        decision_cards.append_card(pm_cfg["results_dir"],
                                   _card("2026-08-01", "Buy"))
        self._install(pm_cfg)
        ctx = memory_log.get_past_context("EL", n_same=5, n_cross=3)
        assert "Prior PM decisions on EL" not in ctx
        daily_run._reset_decision_card_injection()

    def test_reset_restores_original(self, pm_cfg, memory_log):
        original = memory_mod.TradingMemoryLog.get_past_context
        self._install(pm_cfg)
        assert memory_mod.TradingMemoryLog.get_past_context is not original
        daily_run._reset_decision_card_injection()
        assert memory_mod.TradingMemoryLog.get_past_context is original


def _card(date, rating, ticker="EL", summary="thesis"):
    return {"date": date, "ticker": ticker, "rating": rating,
            "ref_close": 100.0, "schema_version": 1,
            "executive_summary": summary, "investment_thesis": "long"}


class TestRunAnalyzeEndToEnd:
    def test_execution_intent_writes_cards_and_ratings_v2(self, cfg, tmp_path,
                                                          monkeypatch):
        import decision_cards
        cfg["execution_intent"] = True
        monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
        monkeypatch.setattr(daily_run, "_last_close", lambda t: 54.25)
        ratings_by_ticker = {"EL": "Underweight", "AAPL": "Overweight"}

        class FakeGraph:
            def __init__(self, **kwargs):
                pass

            def propagate(self, ticker, date, asset_type="stock"):
                daily_run._stash_pm_capture({
                    "rating": ratings_by_ticker[ticker],
                    "executive_summary": f"{ticker} thesis",
                    "investment_thesis": "long",
                    "execution": {"orders": [
                        {"kind": "SELL", "shares": 2, "limit_px": 100.5}]},
                })
                return None, f"**Rating**: {ratings_by_ticker[ticker]}"

        with patch("daily_run.load_watchlist_config", return_value=cfg), \
             patch("daily_run.TradingAgentsGraph", FakeGraph), \
             patch("daily_run.TradingMemoryLog") as mock_log:
            mock_log.return_value.load_entries.return_value = []
            payload = daily_run.run_analyze(cfg, tickers=["EL", "AAPL"])
        assert payload["schema_version"] == 2
        assert payload["execution"]["EL"]["orders"][0]["limit_px"] == 100.5
        assert payload["execution"]["AAPL"]["orders"][0]["shares"] == 2
        card = decision_cards.latest_card(cfg["results_dir"], "EL")
        assert card["rating"] == "Underweight"
        assert card["ref_close"] == 54.25
        assert card["execution"]["orders"][0]["kind"] == "SELL"

    def test_gated_off_leaves_legacy_ratings_shape(self, cfg, tmp_path,
                                                   monkeypatch):
        cfg["execution_intent"] = False
        monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))

        class FakeGraph:
            def __init__(self, **kwargs):
                pass

            def propagate(self, ticker, date, asset_type="stock"):
                return None, "**Rating**: Buy"

        with patch("daily_run.load_watchlist_config", return_value=cfg), \
             patch("daily_run.TradingAgentsGraph", FakeGraph), \
             patch("daily_run.TradingMemoryLog") as mock_log:
            mock_log.return_value.load_entries.return_value = []
            payload = daily_run.run_analyze(cfg, tickers=["AAPL"])
        assert payload["ratings"] == {"AAPL": "Buy"}
        assert "execution" not in payload
        assert "schema_version" not in payload
        assert not (daily_run.Path(cfg["results_dir"]) / "decision_cards").exists()
