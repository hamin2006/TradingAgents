"""structured_log.py — per-ticker structured JSONL logging for the analyze run.

The multi-agent pipeline is otherwise a black box: cron pipes WARNING+ text
into a shared file, INFO is dropped entirely, and there is no record of which
LLM calls happened, which agent made them, what the providers charged, or
where the time went. This module records every LLM turn, tool call, and chain
boundary as one JSON object per line:

    ~/.tradingagents/logs/structured/{date}/{ticker}.jsonl

plus a per-ticker ``summary.json`` written by :meth:`finish`.

The date subdirectory follows the pipeline's America/New_York convention:
daily_run passes ``today`` (the ET trade date) explicitly, so logs land under
the same date as the ratings file.

Capture mechanism: a ``BaseCallbackHandler`` passed through the framework's
native ``callbacks=[...]`` constructor argument (TradingAgentsGraph injects
it into every LLM), so nothing under ``tradingagents/`` is modified. The
handler keys off ``parent_run_id`` to attribute LLM calls to their agent
chain (e.g. "Sentiment Analyst", "Portfolio Manager") via on_chain_start.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

_CACHE_DIR_ENV = "STRUCTURED_LOG_DIR"

# LangGraph names the per-analyst ToolNodes tools_<analyst>; attribute their
# executions to the analyst (verified live: tools_market/tools_news/...).
_ANALYST_BY_TOOLS_NODE = {
    "tools_market": "Market Analyst",
    "tools_social": "Sentiment Analyst",
    "tools_news": "News Analyst",
    "tools_fundamentals": "Fundamentals Analyst",
}

# Thread-local "active logger": _analyze_one sets this in each worker thread so
# module-level wrappers (reddit/stocktwits/news fetches) can emit fetch events
# into the right ticker's JSONL without being passed the logger explicitly.
_thread_local = threading.local()


def _out_dir(base: str | None, today: str | None = None) -> Path:
    if base:
        return Path(base)  # explicit dir = complete target (tests, overrides)
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (Path(os.environ.get(_CACHE_DIR_ENV,
                                Path.home() / ".tradingagents" / "logs" / "structured"))
            / today)


def _git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5,
                             cwd=Path(__file__).parent)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - best-effort
        return None


def _message_text(content) -> str:
    """Render an LLM message payload to text: plain strings pass through,
    content-block lists (text / reasoning / tool blocks) are joined."""
    if content is None:
        return ""
    if not isinstance(content, list):
        return str(content)
    bits = []
    for block in content:
        if isinstance(block, dict):
            bits.append(str(block.get("text", "") or block))
        else:
            bits.append(str(block))
    return "\n".join(b for b in bits if b)


def _dump_messages(messages) -> str:
    """Full human-readable dump of the message list sent to the model.

    Debugging requires the exact context a call saw, so nothing is truncated:
    every message role and its full content (plus any tool calls) is included.
    """
    if not messages:
        return ""
    msgs = messages[0] if isinstance(messages[0], list) else messages
    parts = []
    for msg in msgs:
        label = getattr(msg, "type", None) or type(msg).__name__
        line = f"[{label}] {_message_text(getattr(msg, 'content', None))}"
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            line += f"\n  tool_calls: {json.dumps(tool_calls, default=str)}"
        parts.append(line)
    return "\n".join(parts)


def _reasoning_of(msg) -> str:
    """Extract model thinking from whatever channel the provider used.

    DeepSeek-style models send reasoning in ``reasoning_content`` (an
    extra OpenAI-compatible field surfaced in additional_kwargs); some
    providers inline ``reasoning``/``thinking`` blocks in content instead.
    """
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        bits = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("reasoning", "thinking"):
                bits.append(_message_text(block))
        if bits:
            return "\n".join(bits)
    ak = getattr(msg, "additional_kwargs", None) or {}
    rm = getattr(msg, "response_metadata", None) or {}
    for key in ("reasoning_content", "reasoning"):
        if ak.get(key):
            return _message_text(ak.get(key))
        if rm.get(key):
            return _message_text(rm.get(key))
    return ""


def set_active_logger(run_log: StructuredRunLogger | None) -> None:
    """Bind a logger to the current thread (see _analyze_one)."""
    _thread_local.run_log = run_log


def get_active_logger() -> StructuredRunLogger | None:
    return getattr(_thread_local, "run_log", None)


def clear_active_logger() -> None:
    _thread_local.run_log = None


def stash_reasoning(text: str | None) -> None:
    """Park the model's reasoning text for the llm_end handler on this thread.

    OpenRouter returns ``message.reasoning`` in the SDK message's
    ``model_extra``; LangChain forwards only typed extras (``refusal``) and
    drops ``model_extra``, so the reasoning never reaches the callback
    handler. The daily_run SDK-layer wrapper stashes it here; on_llm_end
    picks it up in the same synchronous call stack.
    """
    _thread_local.reasoning = text or ""


def _pop_reasoning() -> str:
    value = getattr(_thread_local, "reasoning", "") or ""
    _thread_local.reasoning = None
    return value


def emit_fetch(*, source: str, agent: str, mode: str,
               retries: int = 0, latency_s: float = 0.0, bytes: int = 0,
               error: str | None = None) -> None:
    """Emit a fetch_end event into the current thread's structured log.

    Called from the module-level resilience wrappers (stocktwits_resilience,
    reddit_archive, reddit_auth, the news wrapper) which have no reference to
    the per-ticker logger. No-op outside an analyze run (hermetic tests, bare
    calls).
    """
    run_log = get_active_logger()
    if run_log is None:
        return
    run_log.emit_fetch(source=source, agent=agent, mode=mode,
                       retries=retries, latency_s=latency_s,
                       bytes=bytes, error=error)


def emit_structured_fallback(*, agent: str, error: str,
                             mode: str = "retry") -> None:
    """Emit a structured_fallback event into the current thread's log.

    Records when an agent's structured-output invocation failed and the
    framework retried as free text (F3 observability), and when the rating
    guard forces REVIEW on a header-less decision. No-op outside an analyze
    run.
    """
    run_log = get_active_logger()
    if run_log is None:
        return
    run_log.emit_structured_fallback(agent=agent, error=error, mode=mode)


class StructuredRunLogger(BaseCallbackHandler):
    """LangChain callback handler writing one JSONL event per pipeline action."""

    def __init__(self, ticker: str, out_dir: str | None = None,
                 today: str | None = None, git_sha: str | None = None):
        self.ticker = ticker
        self.path = _out_dir(out_dir, today) / f"{ticker}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.git_sha = git_sha or _git_sha()
        self._chain_names: dict[str, str] = {}
        self._tool_starts: dict[str, dict] = {}
        self._llm_starts: dict[str, dict] = {}
        self._started_at = time.monotonic()
        self._llm_calls = 0
        self._total_tokens = 0

    # -- helpers ------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        event.setdefault("ticker", self.ticker)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def emit_fetch(self, *, source: str, agent: str, mode: str,
                   retries: int = 0, latency_s: float = 0.0, bytes: int = 0,
                   error: str | None = None) -> None:
        self._emit({"type": "fetch_end", "agent": agent, "source": source,
                    "mode": mode, "retries": retries, "latency_s": latency_s,
                    "bytes": bytes, "error": error})

    def emit_structured_fallback(self, *, agent: str, error: str,
                                 mode: str = "retry") -> None:
        self._emit({"type": "structured_fallback", "agent": agent,
                    "error": error, "mode": mode})

    def _agent_for(self, parent_run_id: UUID | None,
                   metadata: dict | None = None) -> str:
        # LangGraph tags LLM starts with the node name in metadata; tool runs
        # carry the ToolNode name (tools_market etc.) which we map to analysts.
        if metadata:
            node = metadata.get("langgraph_node")
            if node in _ANALYST_BY_TOOLS_NODE:
                return _ANALYST_BY_TOOLS_NODE[node]
            if node:
                return node
        if parent_run_id is None:
            return "unknown"
        return self._chain_names.get(str(parent_run_id), "unknown")

    # -- chains (agent/stage attribution) -----------------------------------

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       tags=None, metadata=None, **kwargs) -> None:
        name = serialized.get("name") if isinstance(serialized, dict) else None
        if name:
            self._chain_names[str(run_id)] = name
            self._emit({"type": "chain_start", "agent": name,
                        "run_id": str(run_id),
                        "parent_run_id": str(parent_run_id) if parent_run_id else None})

    def on_chain_end(self, outputs, *, run_id, **kwargs) -> None:
        agent = self._chain_names.get(str(run_id), "unknown")
        self._emit({"type": "chain_end", "agent": agent, "run_id": str(run_id)})

    def on_chain_error(self, error, *, run_id, **kwargs) -> None:
        agent = self._chain_names.get(str(run_id), "unknown")
        self._emit({"type": "error", "agent": agent, "run_id": str(run_id),
                    "error": str(error)[:500]})

    # -- LLM turns -----------------------------------------------------------

    def on_chat_model_start(self, serialized, messages, *, run_id,
                            parent_run_id=None, tags=None, metadata=None,
                            **kwargs) -> None:
        self._llm_starts[str(run_id)] = {
            "t0": time.monotonic(),
            "agent": self._agent_for(parent_run_id, metadata),
        }
        self._emit({"type": "llm_start",
                "agent": self._agent_for(parent_run_id, metadata),
                "run_id": str(run_id),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "prompt": _dump_messages(messages)})

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs) -> None:
        started_info = self._llm_starts.pop(str(run_id), None)
        started = started_info["t0"] if started_info else time.monotonic()
        agent = started_info["agent"] if started_info else self._agent_for(parent_run_id)
        usage = {}
        provider = None
        model = None
        text = ""
        # LLMResult.generations is list[list[ChatGeneration]] (choices x seqs);
        # ChatResult.generations is a flat list. Handle both.
        gen = None
        for outer in response.generations:
            if outer:
                gen = outer[0] if isinstance(outer, list) else outer
                break
        if gen is not None:
            msg = getattr(gen, "message", None)
            if msg is not None:
                text = _message_text(msg.content)
                um = getattr(msg, "usage_metadata", None) or {}
                usage = {
                    "input": um.get("input_tokens", 0),
                    "output": um.get("output_tokens", 0),
                    "cache_read": (um.get("input_token_details") or {}).get("cache_read", 0),
                }
                rm = getattr(msg, "response_metadata", None) or {}
                provider = rm.get("model_provider")
                model = rm.get("model_name")
            else:
                text = str(gen.text or "")
        self._llm_calls += 1
        self._total_tokens += usage.get("input", 0) + usage.get("output", 0)
        reasoning = (_reasoning_of(msg) if gen is not None
                     and getattr(gen, "message", None) is not None else "")
        if not reasoning:
            reasoning = _pop_reasoning()  # SDK-layer stash (LangChain drops it)
        self._emit({"type": "llm_end", "agent": agent,
                    "run_id": str(run_id),
                    "parent_run_id": str(parent_run_id) if parent_run_id else None,
                    "model": model, "provider_used": provider,
                    "token_usage": usage,
                    "latency_s": round(time.monotonic() - started, 2),
                    "finish_reason": rm.get("finish_reason"),
                    "reasoning": reasoning,
                    "response": text})

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        started_info = self._llm_starts.pop(str(run_id), None)
        started = started_info["t0"] if started_info else time.monotonic()
        agent = started_info["agent"] if started_info else self._agent_for(parent_run_id)
        _pop_reasoning()  # discard any stashed reasoning for the failed call
        self._emit({"type": "error", "agent": agent,
                    "run_id": str(run_id),
                    "parent_run_id": str(parent_run_id) if parent_run_id else None,
                    "latency_s": round(time.monotonic() - started, 2),
                    "error": str(error)})

    # -- tool calls (LangGraph ToolNodes: fred / stock data / news / ...) ----

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                      metadata=None, **kwargs) -> None:
        name = serialized.get("name") if isinstance(serialized, dict) else "tool"
        agent = self._agent_for(parent_run_id, metadata)
        self._tool_starts[str(run_id)] = {
            "tool": name,
            "agent": agent,
            "t0": time.monotonic(),
        }
        self._emit({"type": "tool_start", "agent": agent,
                    "run_id": str(run_id), "tool": name,
                    "tool_args": str(input_str)})

    def on_tool_end(self, output, *, run_id, parent_run_id=None, metadata=None,
                    **kwargs) -> None:
        start = self._tool_starts.pop(str(run_id), {})
        agent = start.get("agent") or self._agent_for(parent_run_id, metadata)
        self._emit({"type": "tool_end", "agent": agent,
                    "run_id": str(run_id),
                    "tool": start.get("tool", "tool"),
                    "latency_s": round(time.monotonic() - start.get("t0", time.monotonic()), 2),
                    "output_len": len(str(output))})

    def on_tool_error(self, error, *, run_id, parent_run_id=None, metadata=None,
                      **kwargs) -> None:
        start = self._tool_starts.pop(str(run_id), {})
        agent = start.get("agent") or self._agent_for(parent_run_id, metadata)
        self._emit({"type": "error", "agent": agent,
                    "run_id": str(run_id), "tool": start.get("tool", "tool"),
                    "error": str(error)[:500]})

    # -- run summary ----------------------------------------------------------

    def finish(self, rating: str | None = None) -> None:
        wall = round(time.monotonic() - self._started_at, 1)
        self._emit({"type": "run_end", "agent": "run",
                    "rating": rating,
                    "git_sha": self.git_sha,
                    "total_llm_calls": self._llm_calls,
                    "total_tokens": self._total_tokens,
                    "wall_clock_s": wall})
        summary_path = self.path.parent / "summary.json"
        summary: dict = {}
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                summary = {}
        summary[self.ticker] = {
            "rating": rating,
            "llm_calls": self._llm_calls,
            "total_tokens": self._total_tokens,
            "wall_clock_s": wall,
            "git_sha": self.git_sha,
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
