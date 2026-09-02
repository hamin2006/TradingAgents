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

    def test_prompt_truncated_to_limit(self, logger_fx):
        logger_fx.on_chat_model_start({}, [[_fake_prompt("x" * 5000)]], run_id=_rid())
        ev = logger_fx._read_all()[-1]
        assert ev["type"] == "llm_start"
        assert len(ev["prompt"]) == structured_log.TRUNCATE_CHARS
        assert ev["prompt"].endswith("…")

    def test_llm_error_recorded(self, logger_fx):
        logger_fx.on_llm_error(RuntimeError("boom"), run_id=_rid())
        ev = logger_fx._read_all()[-1]
        assert ev["type"] == "error"
        assert "boom" in ev["error"]


class TestToolEvents:
    def test_tool_start_end_with_cache_hit_and_retries(self, logger_fx):
        logger_fx.on_tool_start({"name": "fetch_reddit_posts"}, {"ticker": "AAPL"},
                                run_id=_rid())
        logger_fx.on_tool_end("block text", run_id=_rid(),
                              metadata={"cache_hit": True, "retries": 2})
        events = logger_fx._read_all()
        assert events[-2]["type"] == "tool_start"
        assert events[-2]["tool"] == "fetch_reddit_posts"
        assert events[-1]["type"] == "tool_end"
        assert events[-1]["cache_hit"] is True
        assert events[-1]["retries"] == 2


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


def _fake_llm_result(usage=None):
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
    msg = AIMessage(
        content="OK",
        usage_metadata=complete,
        response_metadata={
            "model_provider": "Relace",
            "model_name": "deepseek/deepseek-v4-flash-0731",
        },
    )
    return ChatResult(generations=[ChatGeneration(message=msg)])
