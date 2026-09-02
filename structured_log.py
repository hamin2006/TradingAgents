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
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

TRUNCATE_CHARS = 2000

_CACHE_DIR_ENV = "STRUCTURED_LOG_DIR"


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


def _truncate(text: str, limit: int = TRUNCATE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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
        self._llm_starts: dict[str, float] = {}
        self._started_at = time.monotonic()
        self._llm_calls = 0
        self._total_tokens = 0

    # -- helpers ------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat())
        event.setdefault("ticker", self.ticker)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def _agent_for(self, parent_run_id: UUID | None) -> str:
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
                            parent_run_id=None, **kwargs) -> None:
        self._llm_starts[str(run_id)] = time.monotonic()
        prompt = messages[0][0].content if messages and messages[0] else ""
        self._emit({"type": "llm_start", "agent": self._agent_for(parent_run_id),
                    "run_id": str(run_id),
                    "parent_run_id": str(parent_run_id) if parent_run_id else None,
                    "prompt": _truncate(str(prompt))})

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs) -> None:
        started = self._llm_starts.pop(str(run_id), time.monotonic())
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
                text = str(msg.content or "")
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
        self._emit({"type": "llm_end", "agent": self._agent_for(parent_run_id),
                    "run_id": str(run_id),
                    "parent_run_id": str(parent_run_id) if parent_run_id else None,
                    "model": model, "provider_used": provider,
                    "token_usage": usage,
                    "latency_s": round(time.monotonic() - started, 2),
                    "response": _truncate(text)})

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        started = self._llm_starts.pop(str(run_id), time.monotonic())
        self._emit({"type": "error", "agent": self._agent_for(parent_run_id),
                    "run_id": str(run_id),
                    "parent_run_id": str(parent_run_id) if parent_run_id else None,
                    "latency_s": round(time.monotonic() - started, 2),
                    "error": str(error)[:500]})

    # -- tool calls (reddit / stocktwits / news fetches) ----------------------

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                      **kwargs) -> None:
        name = serialized.get("name") if isinstance(serialized, dict) else "tool"
        self._tool_starts[str(run_id)] = {
            "tool": name,
            "input": _truncate(str(input_str), 1000),
        }
        self._emit({"type": "tool_start", "agent": self._agent_for(parent_run_id),
                    "run_id": str(run_id), "tool": name,
                    "tool_args": _truncate(str(input_str), 1000)})

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs) -> None:
        start = self._tool_starts.pop(str(run_id), {})
        metadata = kwargs.get("metadata") or {}
        self._emit({"type": "tool_end", "agent": self._agent_for(parent_run_id),
                    "run_id": str(run_id),
                    "tool": start.get("tool", "tool"),
                    "cache_hit": metadata.get("cache_hit"),
                    "retries": metadata.get("retries"),
                    "output_len": len(str(output))})

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs) -> None:
        self._emit({"type": "error", "agent": self._agent_for(parent_run_id),
                    "run_id": str(run_id), "error": str(error)[:500]})

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
