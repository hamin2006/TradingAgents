"""Hermetic tests for structured_log.py (no network, no LLM)."""

from __future__ import annotations

import json
import uuid

import pytest

import structured_log


def _rid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def logger_fx(tmp_path):
    return structured_log.StructuredRunLogger(ticker="AAPL", out_dir=str(tmp_path))


def _read_all(self):
    import json as _json
    lines = self.path.read_text(encoding="utf-8").strip().splitlines()
    return [_json.loads(line) for line in lines]


structured_log.StructuredRunLogger._read_all = _read_all


class TestAttribution:
    def test_chain_start_maps_name_to_run_id(self, logger_fx):
        rid = _rid()
        logger_fx.on_chain_start({"name": "Sentiment Analyst"}, {}, run_id=uuid.UUID(rid))
        assert logger_fx._chain_names[rid] == "Sentiment Analyst"

    def test_llm_end_uses_parent_chain_name(self, logger_fx):
        chain_id = _rid()
        logger_fx.on_chain_start({"name": "Portfolio Manager"}, {},
                                 run_id=uuid.UUID(chain_id))
        logger_fx.on_llm_end(_fake_llm_result(), run_id=_rid(),
                             parent_run_id=uuid.UUID(chain_id))
        events = logger_fx._read_all()
        assert events[-1]["agent"] == "Portfolio Manager"

    def test_llm_without_chain_gets_unknown_agent(self, logger_fx):
        logger_fx.on_llm_end(_fake_llm_result(), run_id=_rid(), parent_run_id=None)
        assert logger_fx._read_all()[-1]["agent"] == "unknown"

    def test_langgraph_node_metadata_names_the_agent(self, logger_fx):
        """LangGraph tags every LLM start with metadata['langgraph_node'];
        that must win over the parent-run-id mapping."""
        rid = _rid()
        logger_fx.on_chat_model_start(
            {}, [[_fake_prompt("hello")]], run_id=uuid.UUID(rid),
            metadata={"langgraph_node": "Sentiment Analyst"})
        logger_fx.on_llm_end(_fake_llm_result(), run_id=uuid.UUID(rid))
        events = logger_fx._read_all()
        assert events[-2]["agent"] == "Sentiment Analyst"  # llm_start
        assert events[-1]["agent"] == "Sentiment Analyst"  # llm_end (stashed)


class TestLLMEvents:
    def test_llm_end_records_usage_and_provider(self, logger_fx):
        logger_fx.on_llm_end(_fake_llm_result(usage={"input_tokens": 100,
                                                     "output_tokens": 50,
                                                     "input_token_details": {"cache_read": 40}}),
                             run_id=_rid())
        ev = logger_fx._read_all()[-1]
        assert ev["type"] == "llm_end"
        assert ev["token_usage"]["input"] == 100
        assert ev["token_usage"]["output"] == 50
        assert ev["token_usage"]["cache_read"] == 40
        assert ev["provider_used"] == "Relace"
        assert ev["model"] == "deepseek/deepseek-v4-flash-0731"
        assert ev["response"].startswith("OK")
        assert ev["latency_s"] >= 0

    def test_prompt_not_truncated_full_context(self, logger_fx):
        """Debugging needs the exact context the model saw: no truncation."""
        logger_fx.on_chat_model_start({}, [[_fake_prompt("x" * 5000)]], run_id=_rid())
        ev = logger_fx._read_all()[-1]
        assert ev["type"] == "llm_start"
        assert "x" * 5000 in ev["prompt"]
        assert "[P]" in ev["prompt"]  # message role labelled

    def test_llm_end_captures_reasoning(self, logger_fx):
        logger_fx.on_llm_end(_fake_llm_result(reasoning="deep thinking trace"),
                             run_id=_rid())
        ev = logger_fx._read_all()[-1]
        assert ev["reasoning"] == "deep thinking trace"

    def test_response_full_not_truncated(self, logger_fx):
        long_text = "R" * 30000
        logger_fx.on_llm_end(_fake_llm_result(text=long_text), run_id=_rid())
        ev = logger_fx._read_all()[-1]
        assert ev["response"] == long_text

    def test_tool_args_full_not_truncated(self, logger_fx):
        args = {"ticker": "COP", "start_date": "x" * 3000}
        logger_fx.on_tool_start({"name": "get_news"}, args, run_id=uuid.UUID(_rid()))
        ev = logger_fx._read_all()[-1]
        assert ev["tool_args"] == str(args)

    def test_llm_error_recorded(self, logger_fx):
        logger_fx.on_llm_error(RuntimeError("boom"), run_id=_rid())
        ev = logger_fx._read_all()[-1]
        assert ev["type"] == "error"
        assert "boom" in ev["error"]


class TestToolEvents:
    def test_tool_start_end_records_tool_and_latency(self, logger_fx):
        rid = _rid()
        logger_fx.on_tool_start({"name": "get_macro_indicators"}, {"ticker": "AAPL"},
                                run_id=uuid.UUID(rid))
        logger_fx.on_tool_end("block text", run_id=uuid.UUID(rid))
        events = logger_fx._read_all()
        assert events[-2]["type"] == "tool_start"
        assert events[-2]["tool"] == "get_macro_indicators"
        assert events[-1]["type"] == "tool_end"
        assert events[-1]["tool"] == "get_macro_indicators"
        assert events[-1]["latency_s"] >= 0
        assert events[-1]["output_len"] == len("block text")


class TestRunSummary:
    def test_finish_writes_summary_and_rating(self, logger_fx):
        logger_fx.on_llm_end(_fake_llm_result(usage={"input_tokens": 100,
                                                     "output_tokens": 50}),
                             run_id=_rid())
        logger_fx.finish(rating="Hold")
        events = logger_fx._read_all()
        assert events[-1]["type"] == "run_end"
        assert events[-1]["rating"] == "Hold"
        assert events[-1]["total_llm_calls"] == 1
        assert events[-1]["total_tokens"] == 150
        assert events[-1]["git_sha"]

    def test_writes_jsonl_per_ticker(self, logger_fx, tmp_path):
        logger_fx.on_llm_end(_fake_llm_result(), run_id=_rid())
        path = tmp_path / "AAPL.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["ticker"] == "AAPL"

    def test_summary_file_aggregates(self, logger_fx, tmp_path):
        logger_fx.finish(rating="Buy")
        summary_path = tmp_path / "summary.json"
        assert summary_path.exists()
        d = json.loads(summary_path.read_text(encoding="utf-8"))
        assert d["AAPL"]["rating"] == "Buy"


def _fake_prompt(text="hi"):
    class P:
        def __init__(self, content):
            self.content = content

        def dict(self):
            return {"content": self.content}

    return P(text)


class TestActiveLogger:
    def test_set_active_logger_routes_emit_fetch(self, logger_fx):
        structured_log.set_active_logger(logger_fx)
        try:
            structured_log.emit_fetch(source="stocktwits", agent="Sentiment Analyst",
                                      mode="live", retries=1, latency_s=0.2, bytes=108)
        finally:
            structured_log.clear_active_logger()
        ev = logger_fx._read_all()[-1]
        assert ev["type"] == "fetch_end"
        assert ev["source"] == "stocktwits"
        assert ev["agent"] == "Sentiment Analyst"
        assert ev["mode"] == "live"
        assert ev["retries"] == 1
        assert ev["bytes"] == 108

    def test_emit_fetch_noop_without_active_logger(self, tmp_path):
        structured_log.clear_active_logger()
        structured_log.emit_fetch(source="stocktwits", agent="x", mode="live")
        assert not (tmp_path / "AAPL.jsonl").exists()

    def test_emit_fetch_thread_local_isolation(self, tmp_path):
        """Parallel workers must not cross-talk: the active logger is per-thread."""
        import threading

        lg_a = structured_log.StructuredRunLogger(ticker="A", out_dir=str(tmp_path))
        lg_b = structured_log.StructuredRunLogger(ticker="B", out_dir=str(tmp_path))
        seen = {}

        def worker(name, lg):
            structured_log.set_active_logger(lg)
            try:
                structured_log.emit_fetch(source="reddit", agent="Sentiment Analyst",
                                          mode="cache")
                seen[name] = lg._read_all()[-1]["ticker"]
            finally:
                structured_log.clear_active_logger()

        ta = threading.Thread(target=worker, args=("a", lg_a))
        tb = threading.Thread(target=worker, args=("b", lg_b))
        ta.start()
        tb.start()
        ta.join()
        tb.join()
        assert seen == {"a": "A", "b": "B"}

    def test_emit_structured_fallback_event(self, logger_fx):
        """F3: a structured-output fallback is recorded per ticker with cause."""
        structured_log.set_active_logger(logger_fx)
        try:
            structured_log.emit_structured_fallback(
                agent="Portfolio Manager",
                error="structured output returned no parsed result",
                mode="retry")
        finally:
            structured_log.clear_active_logger()
        ev = logger_fx._read_all()[-1]
        assert ev["type"] == "structured_fallback"
        assert ev["agent"] == "Portfolio Manager"
        assert ev["error"] == "structured output returned no parsed result"
        assert ev["mode"] == "retry"
        assert ev["ticker"] == "AAPL"


class TestToolAttribution:
    def test_tools_node_maps_to_analyst(self, logger_fx):
        rid = _rid()
        logger_fx.on_tool_start({"name": "get_macro_indicators"}, "cpi",
                                run_id=uuid.UUID(rid),
                                metadata={"langgraph_node": "tools_news"})
        logger_fx.on_tool_end("data", run_id=uuid.UUID(rid),
                              metadata={"langgraph_node": "tools_news"})
        events = logger_fx._read_all()
        assert events[-2]["agent"] == "News Analyst"   # tool_start
        assert events[-2]["tool"] == "get_macro_indicators"
        assert events[-1]["agent"] == "News Analyst"   # tool_end

    def test_unknown_tools_node_agent(self, logger_fx):
        rid = _rid()
        logger_fx.on_tool_start({"name": "some_tool"}, "x", run_id=uuid.UUID(rid),
                                metadata={"langgraph_node": "tools_unknown"})
        ev = logger_fx._read_all()[-1]
        assert ev["agent"] == "tools_unknown"  # unmapped node kept raw


# tool node -> analyst mapping used for attribution
_ANALYST_BY_TOOL_NODE = {
    "tools_market": "Market Analyst",
    "tools_social": "Sentiment Analyst",
    "tools_news": "News Analyst",
    "tools_fundamentals": "Fundamentals Analyst",
}


def _fake_llm_result(usage=None, reasoning=None, text="OK"):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    if usage is None:
        usage = {"input_tokens": 6, "output_tokens": 10,
                 "input_token_details": {"cache_read": 0}}
    complete = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        "input_token_details": {"cache_read": (usage.get("input_token_details") or {}).get("cache_read", 0)},
        "output_token_details": {"reasoning": 0},
    }
    kwargs = {}
    if reasoning:
        kwargs["additional_kwargs"] = {"reasoning_content": reasoning}
    msg = AIMessage(
        content=text,
        usage_metadata=complete,
        response_metadata={
            "model_provider": "Relace",
            "model_name": "deepseek/deepseek-v4-flash-0731",
        },
        **kwargs,
    )
    return ChatResult(generations=[ChatGeneration(message=msg)])
