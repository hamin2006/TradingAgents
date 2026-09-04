"""tests/test_daily_run.py"""
import json
from unittest.mock import MagicMock, patch

import pytest

from daily_run import main, run_analyze, run_execute


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
    """_analyze_one writes a per-ticker structured log to the home dir; keep
    the suite hermetic by pointing it at the tmp dir."""
    monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))


def _ratings_file(cfg, ratings, failures=None, day="2026-08-31"):
    import daily_run
    payload = {"date": day, "ratings": ratings, "failures": failures or []}
    path = daily_run.Path(cfg["results_dir"]) / f"ratings_{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_run_analyze_extracts_ratings_and_writes_json(cfg):
    fake_graph = MagicMock()
    fake_graph.propagate.return_value = (None, "**Rating**: Buy")

    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            return fake_graph.propagate(ticker)

    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("structured_log.StructuredRunLogger") as mock_logger:
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg, tickers=["AAPL", "MSFT"])
    assert payload["ratings"] == {"AAPL": "Buy", "MSFT": "Buy"}
    files = list(__import__("pathlib").Path(cfg["results_dir"]).glob("ratings_*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["ratings"]["AAPL"] == "Buy"
    # structured logging: per-ticker logger created and finished with the rating
    assert mock_logger.call_count == 2
    mock_logger.return_value.finish.assert_any_call(rating="Buy")


def test_analyze_one_sets_active_logger_and_records_rating(cfg, tmp_path, monkeypatch):
    """_analyze_one binds the structured logger to the worker thread (so the
    get_graph_args patch and the wrappers can emit) and writes the rating."""
    from unittest.mock import patch as _patch

    import daily_run
    import structured_log

    monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
    captured = {}

    class FakeGraph:
        def __init__(self, config=None, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            captured["active"] = structured_log.get_active_logger()
            return None, "**Rating**: Hold"

    with _patch("daily_run.TradingAgentsGraph", FakeGraph):
        ticker, rating, error = daily_run._analyze_one("AAPL", "2026-09-02", cfg)
    assert (ticker, rating, error) == ("AAPL", "Hold", None)
    assert isinstance(captured["active"], structured_log.StructuredRunLogger)
    assert captured["active"].ticker == "AAPL"
    assert structured_log.get_active_logger() is None  # cleared after run
    # the run_end event with the rating was written
    log_path = tmp_path / "structured" / "2026-09-02" / "AAPL.jsonl"
    assert log_path.exists()
    events = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
    assert events[-1]["type"] == "run_end"
    assert events[-1]["rating"] == "Hold"


def test_ensure_graph_tool_callbacks_injects_active_logger(cfg, tmp_path, monkeypatch):
    """The get_graph_args patch must inject the thread-local logger into the
    graph-invoke config so ToolNode executions (fred/stock/news tools) emit."""
    import daily_run
    import structured_log
    import tradingagents.graph.propagation as prop_mod

    monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
    daily_run._GRAPH_TOOL_CALLBACKS_PATCHED = False
    run_log = structured_log.StructuredRunLogger(ticker="AAPL",
                                                 today="2026-09-02",
                                                 out_dir=str(tmp_path / "structured"))
    structured_log.set_active_logger(run_log)
    try:
        daily_run._ensure_graph_tool_callbacks()
        fake_propagator = object.__new__(prop_mod.Propagator)
        fake_propagator.max_recur_limit = 25
        args = prop_mod.Propagator.get_graph_args(fake_propagator)
    finally:
        structured_log.clear_active_logger()
    assert run_log in args["config"]["callbacks"]


def test_ensure_news_logging_wraps_sentiment_get_news(tmp_path, monkeypatch):
    """The sentiment analyst's direct get_news.func call (invisible to
    LangGraph) is wrapped to emit a fetch event."""
    import json

    import daily_run
    import structured_log
    import tradingagents.agents.analysts.sentiment_analyst as sa

    monkeypatch.setenv("STRUCTURED_LOG_DIR", str(tmp_path / "structured"))
    daily_run._NEWS_LOGGING_PATCHED = False
    original = sa.get_news.func
    sa.get_news.func = lambda ticker, s, e: "mock news block"  # noqa: E731
    try:
        daily_run._ensure_news_logging()
        assert sa.get_news.func is not original  # wrapped
        logger = structured_log.StructuredRunLogger(
            ticker="AAPL", today="2026-09-02", out_dir=str(tmp_path / "structured"))
        structured_log.set_active_logger(logger)
        try:
            out = sa.get_news.func("AAPL", "2026-08-25", "2026-09-01")
            assert out == "mock news block"
        finally:
            structured_log.clear_active_logger()
    finally:
        sa.get_news.func = original
        daily_run._NEWS_LOGGING_PATCHED = False
    events = [json.loads(line) for line in logger.path.read_text().strip().splitlines()]
    fetch = [e for e in events if e["type"] == "fetch_end"]
    assert fetch[-1]["source"] == "yahoo_news"
    assert fetch[-1]["agent"] == "Sentiment Analyst"


def test_ensure_fred_aliases_adds_oil_mappings():
    """The observed 'crude_oil_wti' alias gap must resolve to DCOILWTICO."""
    import daily_run
    import tradingagents.dataflows.fred as fred_mod

    daily_run._FRED_PATCHED = False
    try:
        daily_run._ensure_fred_aliases()
        assert fred_mod._resolve_series_id("crude_oil_wti") == "DCOILWTICO"
        assert fred_mod._resolve_series_id("wti") == "DCOILWTICO"
        assert fred_mod._resolve_series_id("oil") == "DCOILWTICO"
        assert fred_mod._resolve_series_id("brent") == "DCOILBRENTEU"
        assert fred_mod._resolve_series_id("natural_gas") == "DHHNGSP"
        assert fred_mod._resolve_series_id("3m_treasury") == "DGS3MO"
        assert fred_mod._resolve_series_id("5y_treasury") == "DGS5"
        assert fred_mod._resolve_series_id("10y_3m_spread") == "T10Y3M"
        assert fred_mod._resolve_series_id("hourly_earnings") == "CES0500000003"
        assert fred_mod._resolve_series_id("cpi") == "CPIAUCSL"  # existing intact
        keys_before = set(fred_mod.MACRO_SERIES)
        daily_run._ensure_fred_aliases()  # idempotent
        assert set(fred_mod.MACRO_SERIES) == keys_before
    finally:
        for alias in daily_run._FRED_ALIAS_EXTENSIONS:
            fred_mod.MACRO_SERIES.pop(alias, None)
        daily_run._FRED_PATCHED = False


def test_ensure_fred_aliases_discloses_full_map_in_tool_description():
    """The tool the news analyst sees must list every alias and warn that
    unlisted strings go to FRED verbatim (the model invented aliases because
    only examples were disclosed)."""
    import daily_run
    import tradingagents.agents.utils.macro_data_tools as mdt
    import tradingagents.dataflows.fred as fred_mod

    daily_run._FRED_PATCHED = False
    tool = mdt.get_macro_indicators
    original_desc = tool.description
    try:
        daily_run._ensure_fred_aliases()
        desc = tool.description
        for alias in ("fed_funds_rate", "10y_treasury", "yield_curve"):
            assert alias in desc
        assert "DCOILWTICO" not in desc  # values are not exposed, names are
        assert "verbatim" in desc
        listed = desc.split("Known friendly aliases (prefer these):")[1].split(".")[0]
        names = {a.strip() for a in listed.split(",") if a.strip()}
        assert names == set(fred_mod.MACRO_SERIES)
    finally:
        tool.description = original_desc
        daily_run._FRED_PATCHED = False


# --- structured-output fallback visibility + safety (F3) ---------------------

def test_extract_rating_passes_review_through():
    """REVIEW must not silently become a fabricated Hold in the ratings file."""
    import daily_run

    assert daily_run.extract_rating("REVIEW") == "REVIEW"
    assert daily_run.extract_rating("**Rating**: Buy") == "Buy"


def test_header_rating_requires_explicit_label():
    """A Rating: header is trustworthy; prose tier words are not."""
    import daily_run

    assert daily_run._header_rating("**Rating**: Overweight\nplan") == "Overweight"
    assert daily_run._header_rating("Rating - **Sell**") == "Sell"
    assert daily_run._header_rating("we should not sell into weakness") is None
    assert daily_run._header_rating("REVIEW") is None
    assert daily_run._header_rating("") is None
    assert daily_run._header_rating(None) is None


def test_propagate_reviews_headerless_pm_decision(cfg, tmp_path):
    """A header-less PM decision (freetext fallback) must not let the
    framework's prose-word scan pick the rating -- force REVIEW instead."""
    import daily_run
    import structured_log

    class FakeGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            return ({"final_trade_decision": "The risk is contained; "
                                             "we should not sell into weakness."},
                    "Sell")  # framework pass-2 prose guess

    run_log = structured_log.StructuredRunLogger(
        ticker="AAPL", today="2026-09-02", out_dir=str(tmp_path / "s"))
    with patch("daily_run.TradingAgentsGraph", FakeGraph):
        rating = daily_run._propagate_with_structured_log(
            "AAPL", "2026-09-02", cfg, run_log)
    assert rating == "REVIEW"
    events = [json.loads(line) for line in run_log.path.read_text().strip().splitlines()]
    fb = [e for e in events if e["type"] == "structured_fallback"]
    assert fb and fb[-1]["mode"] == "rating_guard"


def test_propagate_keeps_signal_when_header_present(cfg, tmp_path):
    import daily_run
    import structured_log

    class FakeGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            return ({"final_trade_decision": "**Rating**: Buy\nPlan..."}, "Buy")

    run_log = structured_log.StructuredRunLogger(
        ticker="AAPL", today="2026-09-02", out_dir=str(tmp_path / "s"))
    with patch("daily_run.TradingAgentsGraph", FakeGraph):
        rating = daily_run._propagate_with_structured_log(
            "AAPL", "2026-09-02", cfg, run_log)
    assert rating == "Buy"


def test_propagate_keeps_signal_when_state_unavailable(cfg, tmp_path):
    """Falsy state (legacy fake graphs) falls back to signal extraction."""
    import daily_run
    import structured_log

    class FakeGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            return None, "**Rating**: Hold"

    run_log = structured_log.StructuredRunLogger(
        ticker="AAPL", today="2026-09-02", out_dir=str(tmp_path / "s"))
    with patch("daily_run.TradingAgentsGraph", FakeGraph):
        rating = daily_run._propagate_with_structured_log(
            "AAPL", "2026-09-02", cfg, run_log)
    assert rating == "Hold"


def test_structured_fallback_handler_emits_event(tmp_path):
    """F3: the framework's fallback warning is captured per ticker."""
    import logging

    import daily_run
    import structured_log

    logger_mod = logging.getLogger("tradingagents.agents.utils.structured")
    daily_run._reset_structured_fallback_logging()
    try:
        daily_run._ensure_structured_fallback_logging()
        run_log = structured_log.StructuredRunLogger(
            ticker="MRK", today="2026-09-02", out_dir=str(tmp_path / "s"))
        structured_log.set_active_logger(run_log)
        try:
            logger_mod.warning(
                "%s: structured-output invocation failed (%s); retrying once "
                "as free text", "Research Manager",
                "structured output returned no parsed result")
        finally:
            structured_log.clear_active_logger()
    finally:
        daily_run._reset_structured_fallback_logging()
    events = [json.loads(line) for line in run_log.path.read_text().strip().splitlines()]
    fb = [e for e in events if e["type"] == "structured_fallback"]
    assert fb and fb[-1]["agent"] == "Research Manager"
    assert "no parsed result" in fb[-1]["error"]
    assert fb[-1]["mode"] == "retry"


# --- analyst report recovery (F7) --------------------------------------------

@pytest.mark.parametrize("factory_name", [
    "create_market_analyst", "create_news_analyst", "create_fundamentals_analyst",
])
def test_analyst_report_rebuilt_from_stranded_history(factory_name):
    """F7: an empty final report is rebuilt from the analyst's own earlier
    prose (mixed text+tool-call turns) before the clear node wipes it."""
    from langchain_core.messages import AIMessage, ToolMessage

    import daily_run
    import tradingagents.graph.setup as setup_mod

    report_key = daily_run._ANALYST_REPORT_KEYS[factory_name]
    original_factory = getattr(setup_mod, factory_name)
    history = [
        AIMessage(content="Margins are eroding ~40bps a quarter; guide down "
                          "implies more pressure ahead."),
        ToolMessage(content="<csv table>", tool_call_id="t1", name="get_indicators"),
        AIMessage(content="Let me double-check the cash flow statement before "
                          "concluding."),
    ]

    def fake_node(state):
        return {"messages": [AIMessage(content="")], report_key: ""}

    daily_run._reset_analyst_report_recovery()
    daily_run._ANALYST_REPORT_RECOVERY_PATCHED = False
    setattr(setup_mod, factory_name, lambda llm: fake_node)
    try:
        daily_run._ensure_analyst_report_recovery()
        node = getattr(setup_mod, factory_name)(None)
        out = node({"messages": history})
    finally:
        daily_run._reset_analyst_report_recovery()
        setattr(setup_mod, factory_name, original_factory)
    rebuilt = out[report_key]
    assert "Margins are eroding" in rebuilt
    assert "double-check the cash flow" in rebuilt
    assert "csv table" not in rebuilt  # tool outputs stay out of the report
    assert "<" not in rebuilt


@pytest.mark.parametrize("factory_name", [
    "create_market_analyst", "create_news_analyst", "create_fundamentals_analyst",
])
def test_analyst_report_passthrough_when_present(factory_name):
    """A captured report is never touched."""
    from langchain_core.messages import AIMessage

    import daily_run
    import tradingagents.graph.setup as setup_mod

    report_key = daily_run._ANALYST_REPORT_KEYS[factory_name]
    original_factory = getattr(setup_mod, factory_name)

    def fake_node(state):
        return {"messages": [AIMessage(content="full report text")],
                report_key: "full report text"}

    daily_run._reset_analyst_report_recovery()
    daily_run._ANALYST_REPORT_RECOVERY_PATCHED = False
    setattr(setup_mod, factory_name, lambda llm: fake_node)
    try:
        daily_run._ensure_analyst_report_recovery()
        node = getattr(setup_mod, factory_name)(None)
        out = node({"messages": [AIMessage(content="stale prose")]})
    finally:
        daily_run._reset_analyst_report_recovery()
        setattr(setup_mod, factory_name, original_factory)
    assert out[report_key] == "full report text"


# --- reasoning capture (langchain converter drops model_extra) ---------------

def test_ensure_reasoning_capture_keeps_reasoning_in_additional_kwargs():
    """OpenRouter returns message.reasoning as an extra field; langchain_openai
    1.6 reaches the SDK via with_raw_response.parse (never Completions.create)
    and _convert_dict_to_message silently drops unknown keys, so the reasoning
    never reached the structured log. The wrapper must patch the converter so
    the extra key lands in additional_kwargs where _reasoning_of reads it."""
    from langchain_openai.chat_models import base as base_mod

    import daily_run

    daily_run._reset_reasoning_capture()
    daily_run._ensure_reasoning_capture()
    try:
        msg = base_mod._convert_dict_to_message(
            {"role": "assistant", "content": "hello",
             "reasoning": "deep chain of thought"})
        assert msg.additional_kwargs["reasoning_content"] == "deep chain of thought"
        assert base_mod._convert_dict_to_message._wrapped_original  # tagged
    finally:
        daily_run._reset_reasoning_capture()
    # Unwrapped, the converter drops the unknown key again (seam is ours).
    dropped = base_mod._convert_dict_to_message(
        {"role": "assistant", "content": "hello", "reasoning": "trace"})
    assert "reasoning_content" not in dropped.additional_kwargs


def test_reasoning_capture_flows_into_llm_end_event():
    """End to end through the real seam: converter keeps reasoning -> AIMessage
    -> on_llm_end -> event['reasoning'] (no SDK stash involved)."""
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_openai.chat_models import base as base_mod

    import daily_run
    import structured_log

    logger = structured_log.StructuredRunLogger(
        ticker="AAPL", out_dir="/tmp/structlog_reasoning_test")
    daily_run._reset_reasoning_capture()
    daily_run._ensure_reasoning_capture()
    try:
        msg = base_mod._convert_dict_to_message(
            {"role": "assistant", "content": "ok",
             "reasoning": "e2e thinking trace"})
        logger.on_llm_end(
            ChatResult(generations=[ChatGeneration(message=msg)]),
            run_id=__import__("uuid").uuid4())
        ev = json.loads(logger.path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert ev["reasoning"] == "e2e thinking trace"
    finally:
        daily_run._reset_reasoning_capture()


# --- portfolio-context injection (phantom-position fix) ----------------------


def _portfolio_snap(cash=9_999.31, max_positions=10, holdings=None):
    """Build a snapshot dict in the shape _fetch_portfolio_snapshot returns."""
    holdings = holdings or {}
    invested = sum(h["value"] for h in holdings.values() if h.get("value"))
    sectors = {}
    for _, h in holdings.items():
        if h.get("value") and h.get("sector"):
            sectors[h["sector"]] = sectors.get(h["sector"], 0.0) + h["value"]
    return {"cash": cash, "max_positions": max_positions, "holdings": holdings,
            "invested": invested, "sectors": sectors}


class _RecordingLLM:
    """Stub LLM: captures every prompt; no structured output (freetext path)."""

    def __init__(self):
        self.prompts = []

    def with_structured_output(self, *args, **kwargs):
        raise NotImplementedError

    def invoke(self, prompt, *args, **kwargs):
        from langchain_core.messages import AIMessage
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))
        return AIMessage(content="ok")


def _tail_state(ticker="COP", snap=None):
    """Minimal AgentState a decision-tail node can render without network."""
    import daily_run
    base_ctx = f"The instrument to analyze is `{ticker}`."
    if snap is not None:
        stance = daily_run._portfolio_stance_line(ticker, snap)
        if stance:
            base_ctx = f"{base_ctx}\n\n{stance}"
    return {
        "company_of_interest": ticker,
        "instrument_context": base_ctx,
        "investment_debate_state": {"history": "", "count": 0},
        "risk_debate_state": {"history": "", "count": 0,
                              "aggressive_history": "",
                              "conservative_history": "",
                              "neutral_history": "",
                              "current_aggressive_response": "",
                              "current_conservative_response": "",
                              "current_neutral_response": ""},
        "market_report": "market", "sentiment_report": "sentiment",
        "news_report": "news", "fundamentals_report": "fundamentals",
        "investment_plan": "plan", "trader_investment_plan": "trader",
        "past_context": "",
    }


def _install_portfolio_context(daily_run, cfg, snap):
    """Context manager: install the wrappers and keep the fake snapshot
    active so nodes invoked inside see it (wrappers read the module global
    at call time)."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        daily_run._reset_portfolio_context()
        daily_run._PORTFOLIO_PATCHED = False
        with patch("daily_run._portfolio_snapshot", return_value=snap):
            daily_run._ensure_portfolio_context(cfg)
            try:
                yield
            finally:
                daily_run._reset_portfolio_context()

    return _cm()


def test_portfolio_snapshot_builds_from_broker(cfg):
    """Shares + avg entry from the broker, marked to last close, sector-mixed."""
    import daily_run

    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({"AAPL": 10, "MSFT": 5},
                                                  2000.0)
    broker.get_position_details.return_value = {
        "AAPL": {"shares": 10, "avg_entry_price": 180.0},
        "MSFT": {"shares": 5, "avg_entry_price": None},
    }
    closes = {"AAPL": 190.0, "MSFT": 400.0}
    daily_run._reset_portfolio_context()
    with patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", side_effect=lambda t: closes.get(t)), \
         patch("tradingagents.agents.utils.agent_utils.resolve_instrument_identity",
               return_value={"sector": "Technology"}):
        snap = daily_run._portfolio_snapshot(cfg)
    daily_run._reset_portfolio_context()
    assert snap is not None
    assert snap["cash"] == 2000.0
    assert snap["invested"] == 1900.0 + 2000.0
    assert snap["holdings"]["AAPL"]["avg_entry_price"] == 180.0
    assert snap["holdings"]["AAPL"]["value"] == 1900.0
    assert snap["holdings"]["MSFT"]["avg_entry_price"] is None
    assert snap["sectors"]["Technology"] == 3900.0
    assert snap["max_positions"] == 10


def test_snapshot_fetches_on_freshly_booted_machine(cfg):
    """Regression: the empty-cache sentinel must not look like a fresh cache
    hit when monotonic() is near zero (machine booted < 10 min ago)."""
    import daily_run

    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({"AAPL": 10}, 2000.0)
    broker.get_position_details.return_value = {}
    closes = {"AAPL": 190.0}
    with patch("daily_run.time.monotonic", return_value=3.0), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", side_effect=lambda t: closes.get(t)), \
         patch("tradingagents.agents.utils.agent_utils.resolve_instrument_identity",
               return_value={"sector": "Technology"}):
        daily_run._reset_portfolio_context()
        snap = daily_run._portfolio_snapshot(cfg)
        assert snap is not None  # cached-None hit would fail here
        assert snap["holdings"]["AAPL"]["value"] == 1900.0
        # second call within TTL serves the cache
        snap2 = daily_run._portfolio_snapshot(cfg)
        assert snap2 is snap
        # after TTL the cache is refreshed, not served stale
        broker.get_positions_and_cash.return_value = ({"AAPL": 20}, 2000.0)
        with patch("daily_run.time.monotonic", return_value=900.0):
            snap3 = daily_run._portfolio_snapshot(cfg)
        assert snap3["holdings"]["AAPL"]["shares"] == 20
    daily_run._reset_portfolio_context()


def test_flat_book_stance_appended_to_resolved_context(cfg):
    """Flat book: every agent's context says 'no current position'."""
    import daily_run
    import tradingagents.graph.trading_graph as tg

    snap = _portfolio_snap(holdings={}, cash=9_999.31)
    with _install_portfolio_context(daily_run, cfg, snap), \
         patch("tradingagents.graph.trading_graph.resolve_instrument_identity",
               return_value={}):
        ctx = tg.TradingAgentsGraph.resolve_instrument_context(object(), "AAPL")
    assert "no current position in AAPL" in ctx
    assert "deciding whether to initiate" in ctx
    assert "existing position in AAPL are incorrect" in ctx


def test_held_stance_includes_cost_and_weight(cfg):
    """Held ticker: shares, avg cost, and book weight anchor add/trim talk."""
    import daily_run
    import tradingagents.graph.trading_graph as tg

    snap = _portfolio_snap(holdings={"AAPL": {"shares": 42,
                                              "avg_entry_price": 221.10,
                                              "value": 9500.0,
                                              "sector": "Technology"}},
                           cash=500.0)
    with _install_portfolio_context(daily_run, cfg, snap), \
         patch("tradingagents.graph.trading_graph.resolve_instrument_identity",
               return_value={}):
        ctx = tg.TradingAgentsGraph.resolve_instrument_context(object(), "AAPL")
    assert "holding 42 shares of AAPL" in ctx
    assert "avg cost $221.10" in ctx
    assert "95.0% of the book" in ctx  # 9500 of a 10k book


@pytest.mark.parametrize("factory_name", [
    "create_research_manager", "create_aggressive_debator",
    "create_neutral_debator", "create_conservative_debator",
    "create_portfolio_manager",
])
def test_book_shape_reaches_tail_node_prompt(cfg, factory_name):
    """The decision tail renders stance + shape in the actual prompt text."""
    import daily_run
    import tradingagents.graph.setup as setup_mod

    snap = _portfolio_snap(holdings={
        "PSX": {"shares": 10, "avg_entry_price": None, "value": 3000.0,
                "sector": "Energy"},
        "VLO": {"shares": 5, "avg_entry_price": None, "value": 2000.0,
                "sector": "Energy"},
        "AAPL": {"shares": 15, "avg_entry_price": None, "value": 4900.0,
                 "sector": "Technology"},
    }, cash=100.0)
    with _install_portfolio_context(daily_run, cfg, snap):
        original = getattr(setup_mod, factory_name)
        assert hasattr(original, "_wrapped_original")  # factory was wrapped
        llm = _RecordingLLM()
        state = _tail_state("COP", snap)
        node = original(llm)
        node(state)
        prompt = llm.prompts[-1]
    assert "no current position in COP" in prompt  # stance (flat for COP)
    assert "Current book (ground truth): 3/10 positions" in prompt
    assert "Energy 50% (PSX, VLO)" in prompt
    assert "never propose trades outside COP" in prompt


def test_evidence_stages_are_not_wrapped(cfg):
    """Stance/shape/recovery wrap the right layers and nothing else."""
    import daily_run
    import tradingagents.graph.setup as setup_mod

    daily_run._reset_analyst_report_recovery()
    daily_run._reset_portfolio_context()
    with _install_portfolio_context(daily_run, cfg, _portfolio_snap(holdings={})):
        daily_run._ANALYST_REPORT_RECOVERY_PATCHED = False
        daily_run._ensure_analyst_report_recovery()
        try:
            for name in ("create_sentiment_analyst",
                         "create_bull_researcher", "create_bear_researcher",
                         "create_trader"):
                assert not hasattr(getattr(setup_mod, name), "_wrapped_original"), name
            for name in ("create_market_analyst", "create_news_analyst",
                         "create_fundamentals_analyst"):
                assert hasattr(getattr(setup_mod, name), "_wrapped_original"), name
            for name in daily_run._TAIL_FACTORY_NAMES:
                assert hasattr(getattr(setup_mod, name), "_wrapped_original"), name
        finally:
            daily_run._reset_analyst_report_recovery()


def test_broker_failure_skips_injection(cfg):
    """No broker snapshot => no stance, no shape: never assert a wrong book."""
    import daily_run
    import tradingagents.graph.trading_graph as tg

    with patch("daily_run.create_broker",
               side_effect=RuntimeError("broker down")):
        daily_run._reset_portfolio_context()
        snap = daily_run._portfolio_snapshot(cfg)
        daily_run._reset_portfolio_context()
    assert snap is None
    assert daily_run._portfolio_stance_line("AAPL", None) == ""
    assert daily_run._portfolio_book_shape("AAPL", None) == ""

    with _install_portfolio_context(daily_run, cfg, None), \
         patch("tradingagents.graph.trading_graph.resolve_instrument_identity",
               return_value={}):
        ctx = tg.TradingAgentsGraph.resolve_instrument_context(object(), "AAPL")
    assert "Portfolio context" not in ctx


def test_run_analyze_includes_holdings(cfg):
    """Held positions must be analyzed so sells are evaluated."""
    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            return None, "**Rating**: Hold"

    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({"TSLA": 40}, 100_000.0)
    pool = [{"ticker": "NVDA", "score": 1.0}, {"ticker": "AAPL", "score": 0.5}]
    cfg["screener"] = {"candidate_slots": 2, "min_watchlist_size": 2,
                       "exclusion_days": 7}
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run.load_pool", return_value=pool):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg)
    assert set(payload["ratings"]) == {"TSLA", "NVDA", "AAPL"}
    broker.connect.assert_called_once()


def test_run_analyze_failure_is_isolated(cfg):
    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            if ticker == "AAPL":
                raise RuntimeError("boom")
            return None, "**Rating**: Hold"

    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.create_broker", return_value=broker):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg, tickers=["AAPL", "MSFT"])
    assert payload["ratings"] == {"MSFT": "Hold"}
    assert payload["failures"] == ["AAPL"]


def test_run_execute_kill_switch_blocks(cfg, tmp_path):
    (tmp_path / "DISABLE_TRADING").write_text("")
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.Path.exists", return_value=True):
        rc = run_execute(cfg)
    assert rc == 1


def test_run_execute_missing_ratings_fails_safe(cfg):
    with patch("daily_run.load_watchlist_config", return_value=cfg):
        rc = run_execute(cfg)
    assert rc == 1  # no ratings file -> no orders


def test_run_execute_places_orders_and_writes_log(cfg):
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = [{"ticker": "AAPL", "action": "BUY",
                                                "shares": 10, "filled": 10,
                                                "avg_price": 101.5}]
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    broker.place_market_orders.assert_called_once()
    import pathlib
    logs = list(pathlib.Path(cfg["results_dir"]).glob("executed_*.json"))
    assert len(logs) == 1


def test_run_execute_idempotent_second_call_skips(cfg):
    _ratings_file(cfg, {"AAPL": "Buy"})
    import pathlib
    pathlib.Path(cfg["results_dir"]).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(cfg["results_dir"]) / "executed_2026-08-31.json").write_text(
        json.dumps({"orders": []}), encoding="utf-8")
    broker = MagicMock()
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    broker.place_market_orders.assert_not_called()


def test_main_analyze_dispatch(cfg):
    with patch("daily_run.run_analyze", return_value={"ratings": {}}) as mock_run:
        rc = main(["--analyze", "--tickers", "AAPL,MSFT"])
    assert rc == 0
    mock_run.assert_called_once()


def test_run_analyze_parallelizes(cfg):
    """Ticker analyses run concurrently (thread pool), not sequentially."""
    import threading
    import time

    active = 0
    max_active = 0
    state_lock = threading.Lock()

    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.25)
            with state_lock:
                active -= 1
            return None, "**Rating**: Hold"

    cfg["analyze_max_workers"] = 4
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log:
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg, tickers=["A", "B", "C", "D", "E"])
    assert max_active >= 2  # at least two ran concurrently
    assert set(payload["ratings"]) == {"A", "B", "C", "D", "E"}
    assert payload["failures"] == []


def _buy_quota_graph(ratings_by_ticker):
    """Fake graph returning a preset rating per ticker (default Hold)."""
    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            rating = ratings_by_ticker.get(ticker, "Hold")
            return None, f"**Rating**: {rating}"

    return FakeTradingAgentsGraph


def _pool(n):
    return [{"ticker": chr(ord("A") + i), "score": 1.0 - i / 100} for i in range(n)]


def test_run_analyze_expands_until_buy_quota(cfg):
    """With min_buy_quota unmet after the base batch, more pool candidates are
    analyzed (in rank order, skipping already-analyzed) until the quota is hit."""
    cfg["screener"] = {"candidate_slots": 2, "min_watchlist_size": 1,
                       "exclusion_days": 7, "min_buy_quota": 2, "max_analyze": 6}
    ratings_by_ticker = {"A": "Hold", "B": "Hold", "C": "Buy", "D": "Buy"}
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    pool = _pool(8)
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", _buy_quota_graph(ratings_by_ticker)), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run.load_pool", return_value=pool), \
         patch("daily_run.load_regime", return_value="CALM"):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg)
    assert set(payload["ratings"]) == {"A", "B", "C", "D"}  # expanded 2 -> 4
    assert sum(1 for r in payload["ratings"].values() if r in {"Buy", "Overweight"}) == 2


def test_run_analyze_expansion_stops_at_max_analyze(cfg):
    """A quota that can never be met must stop at max_analyze, not run away."""
    cfg["screener"] = {"candidate_slots": 2, "min_watchlist_size": 1,
                       "exclusion_days": 7, "min_buy_quota": 5, "max_analyze": 4}
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    pool = _pool(8)
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", _buy_quota_graph({})), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run.load_pool", return_value=pool), \
         patch("daily_run.load_regime", return_value="CALM"):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg)
    assert len(payload["ratings"]) == 4  # capped at max_analyze
    assert sum(1 for r in payload["ratings"].values() if r in {"Buy", "Overweight"}) == 0


def test_run_analyze_no_expansion_when_quota_met_in_base(cfg):
    """Quota met by the base watchlist -> no extra tickers analyzed."""
    cfg["screener"] = {"candidate_slots": 2, "min_watchlist_size": 1,
                       "exclusion_days": 7, "min_buy_quota": 1, "max_analyze": 6}
    ratings_by_ticker = {"A": "Buy"}
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    pool = _pool(8)
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", _buy_quota_graph(ratings_by_ticker)), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run.load_pool", return_value=pool), \
         patch("daily_run.load_regime", return_value="CALM"):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg)
    assert set(payload["ratings"]) == {"A", "B"}  # base only, no expansion


def test_run_analyze_no_expansion_on_stress_regime(cfg):
    """STRESS pauses new buys -> the expansion loop must not burn LLM calls."""
    cfg["screener"] = {"candidate_slots": 2, "min_watchlist_size": 1,
                       "exclusion_days": 7, "min_buy_quota": 2, "max_analyze": 6}
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    pool = _pool(8)
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", _buy_quota_graph({})), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run.load_pool", return_value=pool), \
         patch("daily_run.load_regime", return_value="STRESS"):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg)
    assert set(payload["ratings"]) == {"A", "B"}  # base only under STRESS


def test_seconds_until_open():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from daily_run import _seconds_until_open
    ET = ZoneInfo("America/New_York")

    def at(h, m):
        return datetime(2026, 8, 31, h, m, tzinfo=ET)

    assert _seconds_until_open(at(9, 0)) == 1800.0
    assert _seconds_until_open(at(8, 30)) == 3600.0
    assert _seconds_until_open(at(9, 30)) == 0.0
    assert _seconds_until_open(at(14, 0)) == 0.0


def test_run_execute_waits_for_open_when_preopen(cfg):
    """Orders must be submitted AT the open, not polled-and-cancelled
    pre-open (a 60s fill poll before 09:30 would cancel the order)."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = [{"ticker": "AAPL", "action": "BUY",
                                                "shares": 10, "filled": 10,
                                                "avg_price": 101.5}]
    slept = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=1800.0), \
         patch("daily_run.time.sleep", side_effect=lambda s: slept.append(s)), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    assert slept == [1800.0]  # waited for the open before submitting
    broker.place_market_orders.assert_called_once()


def test_run_execute_skips_wait_in_dry_run(cfg):
    """--dry-run previews orders without waiting for the open."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []  # real broker dry-run returns reports list
    slept = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=1800.0), \
         patch("daily_run.time.sleep", side_effect=lambda s: slept.append(s)), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg, dry_run=True)
    assert rc == 0
    assert slept == []


def test_run_execute_cancels_stops_before_open_for_sells(cfg):
    """EL-class exit (2026-09-04): an Underweight on a held position sells at
    the open. The resting GTC stop must be cancelled BEFORE the open — with
    both orders live at the 09:30 auction, a gap through the stop could
    double-sell (stop + market sell) into an unintended short."""
    _ratings_file(cfg, {"EL": "Underweight"}, day="2026-09-04")
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({"EL": 8}, 8_324.0)
    broker.place_market_orders.return_value = [{"ticker": "EL", "action": "SELL",
                                                "shares": 8, "filled": 8,
                                                "avg_price": 101.5}]
    slept = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=1800.0), \
         patch("daily_run.time.sleep", side_effect=lambda s: slept.append(s)), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 9, 4)
        rc = run_execute(cfg)
    assert rc == 0
    # stop cancelled pre-open, and strictly before the market sell
    names = [c[0] for c in broker.method_calls]
    assert "cancel_stops_for" in names
    assert names.index("cancel_stops_for") < names.index("place_market_orders")
    assert broker.cancel_stops_for.call_args[0][0] == ["EL"]
    assert slept == [1800.0]  # still waited for the open before selling


def test_run_execute_does_not_cancel_stops_when_only_buys(cfg):
    _ratings_file(cfg, {"AAPL": "Buy"}, day="2026-09-04")
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = [{"ticker": "AAPL", "action": "BUY",
                                                "shares": 10, "filled": 10,
                                                "avg_price": 101.5}]
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 9, 4)
        rc = run_execute(cfg)
    assert rc == 0
    assert "cancel_stops_for" not in [c[0] for c in broker.method_calls]


def test_memory_lock_covers_all_write_paths():
    """Concurrent resolves also write the memory log (batch updates); the
    lock must wrap every read-modify-write method, not just store_decision."""
    import daily_run
    import tradingagents.agents.utils.memory as memory_mod

    original_store = memory_mod.TradingMemoryLog.store_decision
    original_batch = memory_mod.TradingMemoryLog.batch_update_with_outcomes
    original_single = memory_mod.TradingMemoryLog.update_with_outcome

    daily_run._MEMORY_PATCHED = False  # force re-patch in this process
    daily_run._ensure_memory_write_lock()

    assert memory_mod.TradingMemoryLog.store_decision is not original_store
    assert (memory_mod.TradingMemoryLog.batch_update_with_outcomes
            is not original_batch)
    assert (memory_mod.TradingMemoryLog.update_with_outcome
            is not original_single)


def test_run_execute_marks_before_submitting(cfg):
    """If the process dies between placing orders and writing the final log,
    a rerun must not double-execute: the mark exists before submission."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.side_effect = RuntimeError("crashed mid-submit")
    import pytest as _pytest
    with _pytest.raises(RuntimeError), \
         patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        run_execute(cfg)
    import pathlib
    marks = list(pathlib.Path(cfg["results_dir"]).glob("executed_*.json"))
    assert len(marks) == 1
    assert json.loads(marks[0].read_text())["status"] == "submitted"


def test_run_execute_dry_run_writes_no_idempotency_file(cfg):
    """A dry-run preview must not mark the day as executed."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=1800.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg, dry_run=True)
    assert rc == 0
    import pathlib
    assert not list(pathlib.Path(cfg["results_dir"]).glob("executed_*.json"))


def test_run_execute_caps_capital_by_actual_cash(cfg, caplog):
    """Configured capital must not exceed the account's real cash."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    cfg["capital"] = 1_000_000
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    orders = broker.place_market_orders.call_args[0][0]
    assert orders[0].shares == 150  # 100_000 cash cap / 10 positions x 1.5 (Buy) / 100.0
    assert any("cash" in r.message.lower() for r in caplog.records)


def test_run_execute_missing_last_close_warns_and_skips(cfg, caplog):
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=None), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    orders = broker.place_market_orders.call_args[0][0]
    assert orders == []  # no price -> no order (but warn, don't go silent)
    assert any("last close" in r.message.lower() for r in caplog.records)


def test_main_strips_ticker_whitespace():
    with patch("daily_run.run_analyze") as mock_run:
        rc = main(["--analyze", "--tickers", " AAPL , MSFT "])
    assert rc == 0
    mock_run.assert_called_once()
    assert mock_run.call_args[0][1] == ["AAPL", "MSFT"]


def _unwrap_reddit_fetch(fn):
    """Walk wrapper chains (paced_rss._wrapped_original) back to the real
    framework fetcher so tests leave the module pristine for later suites."""
    for _ in range(10):
        inner = getattr(fn, "_wrapped_original", None)
        if inner is None:
            return fn
        fn = inner
    return fn


def test_reddit_fetches_serialize_across_threads():
    """Parallel tickers must not burst Reddit's anonymous per-IP rate limit:
    all Reddit fetches across analyze workers serialize through one lock."""
    import threading
    import time

    import daily_run
    import tradingagents.dataflows.reddit as reddit_mod

    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def fake_rss(ticker, sub, limit, timeout):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with state_lock:
            active -= 1
        return []

    previous = reddit_mod._fetch_subreddit_rss  # real fetcher or earlier wrapper
    daily_run._REDDIT_MIN_INTERVAL = 0.0        # serialization test only
    daily_run._REDDIT_LAST_TS = 0.0             # clear pacing clock
    reddit_mod._fetch_subreddit_rss = fake_rss  # replace real fetcher first
    daily_run._REDDIT_PATCHED = False
    daily_run._ensure_reddit_pacing()           # wrapper now captures fake_rss
    try:
        def worker(i):
            reddit_mod._fetch_subreddit_rss(f"T{i}", "stocks", 5, 10)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert max_active == 1  # fully serialized
    finally:
        reddit_mod._fetch_subreddit_rss = _unwrap_reddit_fetch(previous)
        daily_run._REDDIT_PATCHED = False           # fully undo the patch
        daily_run._REDDIT_LAST_TS = 0.0
        daily_run._REDDIT_MIN_INTERVAL = 8.0


def test_reddit_requests_are_rate_limited():
    """The wrapper must pace requests globally (min interval), not just
    serialize them: Reddit's anonymous limit is ~10 req/min, and the
    framework's 1s inter-sub pacing alone sustains ~1 req/sec."""

    import daily_run
    import tradingagents.dataflows.reddit as reddit_mod

    sleeps = []

    def fake_rss(ticker, sub, limit, timeout):
        return []

    original_attr = reddit_mod._fetch_subreddit_rss
    reddit_mod._fetch_subreddit_rss = fake_rss
    daily_run._REDDIT_PATCHED = False
    daily_run._REDDIT_MIN_INTERVAL = 0.2  # speed up the test
    daily_run._REDDIT_LAST_TS = 0.0       # clear pacing clock from earlier tests
    try:
        daily_run._ensure_reddit_pacing()
        with patch("daily_run.time.sleep",
                   side_effect=lambda s: sleeps.append(s)):
            for i in range(5):
                reddit_mod._fetch_subreddit_rss(f"T{i}", "stocks", 5, 10)
        # 4 pauses between 5 requests, each ~ the min interval (first is free)
        assert len(sleeps) == 4, sleeps
        assert all(g >= 0.15 for g in sleeps), sleeps
    finally:
        reddit_mod._fetch_subreddit_rss = _unwrap_reddit_fetch(original_attr)
        daily_run._REDDIT_PATCHED = False      # fully undo the patch
        daily_run._REDDIT_LAST_TS = 0.0
        daily_run._REDDIT_MIN_INTERVAL = 8.0


def test_reddit_pacing_survives_internal_retry_recursion():
    """reddit.py's 429 retry re-invokes the module attribute (itself), which
    is our wrapper: the same thread re-enters the pacing lock. A plain Lock
    would deadlock; RLock must let the retry through."""

    import daily_run
    import tradingagents.dataflows.reddit as reddit_mod

    calls = []

    def recursive_fake(ticker, sub, limit, timeout, _retry=True):
        calls.append(_retry)
        if _retry:
            return reddit_mod._fetch_subreddit_rss(ticker, sub, limit, timeout,
                                                   _retry=False)
        return ["ok"]

    original_attr = reddit_mod._fetch_subreddit_rss
    reddit_mod._fetch_subreddit_rss = recursive_fake
    daily_run._REDDIT_PATCHED = False
    daily_run._REDDIT_MIN_INTERVAL = 0.0
    daily_run._REDDIT_LAST_TS = 0.0
    try:
        daily_run._ensure_reddit_pacing()
        result = reddit_mod._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        assert result == ["ok"]
        assert calls == [True, False]  # outer + internal retry via wrapper
    finally:
        reddit_mod._fetch_subreddit_rss = _unwrap_reddit_fetch(original_attr)
        daily_run._REDDIT_PATCHED = False
        daily_run._REDDIT_LAST_TS = 0.0
        daily_run._REDDIT_MIN_INTERVAL = 8.0


def test_reddit_oauth_swapped_when_creds_present(monkeypatch):
    import daily_run
    import reddit_auth
    import tradingagents.agents.analysts.sentiment_analyst as sa

    monkeypatch.setenv("REDDIT_CLIENT_ID", "c")
    monkeypatch.setenv("REDDIT_SECRET", "s")
    original = sa.fetch_reddit_posts
    daily_run._REDDIT_OAUTH_PATCHED = False
    daily_run._REDDIT_OAUTH_ACTIVE = False
    try:
        active = daily_run._ensure_reddit_oauth()
        assert active is True
        # swapped to the resilient OAuth wrapper (not the raw impl, not the original)
        assert sa.fetch_reddit_posts is not original
        assert sa.fetch_reddit_posts is not reddit_auth.fetch_reddit_posts
    finally:
        sa.fetch_reddit_posts = _unwrap_reddit_fetch(original)
        daily_run._REDDIT_OAUTH_PATCHED = False
        daily_run._REDDIT_OAUTH_ACTIVE = False


def test_reddit_resilient_wrapper_applied_without_creds(monkeypatch):
    """Even without OAuth creds, the RSS path gets the retry+cache wrapper —
    the agents are never left without Reddit data."""
    import daily_run
    import tradingagents.agents.analysts.sentiment_analyst as sa

    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_SECRET", raising=False)
    original = sa.fetch_reddit_posts
    daily_run._REDDIT_OAUTH_PATCHED = False
    daily_run._REDDIT_OAUTH_ACTIVE = False
    try:
        active = daily_run._ensure_reddit_oauth()
        assert active is False  # RSS path -> caller keeps pacing
        assert sa.fetch_reddit_posts is not original  # still wrapped
    finally:
        sa.fetch_reddit_posts = _unwrap_reddit_fetch(original)
        daily_run._REDDIT_OAUTH_PATCHED = False
        daily_run._REDDIT_OAUTH_ACTIVE = False


def test_stocktwits_resilient_wrapper_applied():
    """The sentiment analyst's StockTwits fetch is wrapped (retry+cache)."""
    import daily_run
    import tradingagents.agents.analysts.sentiment_analyst as sa

    original = sa.fetch_stocktwits_messages
    daily_run._STOCKTWITS_PATCHED = False
    try:
        daily_run._ensure_stocktwits_resilience()
        assert sa.fetch_stocktwits_messages is not original  # wrapped
        assert sa.fetch_stocktwits_messages._wrapped_original is original
    finally:
        sa.fetch_stocktwits_messages = original
        daily_run._STOCKTWITS_PATCHED = False


def test_reddit_archive_wrapper_applied():
    """The sentiment analyst's Reddit fetch is wrapped (archive-first)."""
    import daily_run
    import tradingagents.agents.analysts.sentiment_analyst as sa

    original = sa.fetch_reddit_posts
    daily_run._REDDIT_ARCHIVE_PATCHED = False
    try:
        daily_run._ensure_reddit_archive()
        assert sa.fetch_reddit_posts is not original  # wrapped
        assert sa.fetch_reddit_posts._wrapped_original is original
    finally:
        sa.fetch_reddit_posts = _unwrap_reddit_fetch(original)
        daily_run._REDDIT_ARCHIVE_PATCHED = False


def test_run_execute_stress_pauses_buys_but_exits(cfg, caplog):
    """Regime STRESS suppresses new BUY orders; rating exits still execute."""
    _ratings_file(cfg, {"AAPL": "Buy", "TSLA": "Sell"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({"TSLA": 40}, 100_000.0)
    broker.place_market_orders.return_value = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.load_regime", return_value="STRESS"), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    orders = broker.place_market_orders.call_args[0][0]
    assert [o.action for o in orders] == ["SELL"]
    assert all(o.ticker == "TSLA" for o in orders)
    assert any("STRESS" in r.message for r in caplog.records)


def test_openrouter_pin_injects_provider_body():
    """Pinned model slugs get OpenRouter provider routing via extra_body;
    unpinned models are untouched; non-OpenRouter providers never get the body."""
    import daily_run
    import tradingagents.llm_clients.openai_client as oc
    from tradingagents.llm_clients.openai_client import OpenAIClient

    original_attr = oc.OpenAIClient.get_llm
    daily_run._OPENROUTER_PINS_APPLIED = False
    daily_run._OPENROUTER_PINS = {"deepseek/deepseek-v4-flash-0731": "Relace",
                                  "deepseek/deepseek-v4-pro": "StreamLake"}
    try:
        daily_run._ensure_openrouter_pins(daily_run._OPENROUTER_PINS)

        pinned = OpenAIClient(model="deepseek/deepseek-v4-flash-0731",
                              provider="openrouter").get_llm()
        assert pinned.extra_body == {"provider": {"order": ["Relace"],
                                                  "allow_fallbacks": True}}

        pinned_pro = OpenAIClient(model="deepseek/deepseek-v4-pro",
                                  provider="openrouter").get_llm()
        assert pinned_pro.extra_body == {"provider": {"order": ["StreamLake"],
                                                      "allow_fallbacks": True}}

        native = OpenAIClient(model="deepseek/deepseek-v4-flash-0731",
                              provider="deepseek").get_llm()  # non-OpenRouter
        assert not getattr(native, "extra_body", None)
    finally:
        oc.OpenAIClient.get_llm = original_attr
        daily_run._OPENROUTER_PINS = {}
        daily_run._OPENROUTER_PINS_APPLIED = False
