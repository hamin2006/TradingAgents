"""stocktwits_resilience.py — retry-with-backoff + per-ticker cache for StockTwits.

The StockTwits public stream (``api.stocktwits.com/api/2/streams/symbol/..``)
intermittently 403s under parallel analyze workers — the same burst-throttle
failure class Reddit's RSS path has. This wrapper guarantees the sentiment
analyst always gets StockTwits data:

1. A failure placeholder (``<stocktwits unavailable: ...>``) triggers up to
   ``_MAX_RETRIES`` retries with exponential backoff + jitter.
2. If every attempt fails, the most recent successful block for that ticker is
   served from the per-ticker cache (stale data beats no data).
3. On success, the block is cached for the next failure.

A genuinely empty window (``<no StockTwits messages ...>``) is NOT a failure:
it is returned immediately and never cached.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_FAILURE_PREFIX = "<stocktwits unavailable"
_EMPTY_PREFIX = "<no StockTwits messages"

_MAX_RETRIES = 2       # retries after the initial attempt (exponential backoff)
_BASE_DELAY_S = 4.0    # 4s -> 8s between retries, plus jitter


def _cache_dir() -> Path:
    return Path(os.environ.get("STOCKTWITS_CACHE_DIR",
                               Path.home() / ".tradingagents" / "logs" / "stocktwits_cache"))


def _cache_path(ticker: str) -> Path:
    safe = "".join(c for c in ticker.lower() if c.isalnum()) or "ticker"
    return _cache_dir() / f"{safe}.json"


def _store_cache(ticker: str, block: str, date: str | None = None) -> None:
    try:
        path = _cache_path(ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "date": date or time.strftime("%Y-%m-%d"),
            "block": block,
        }), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - caching is best-effort
        logger.warning("could not write StockTwits cache for %s: %s", ticker, exc)


def _load_cache(ticker: str) -> dict | None:
    try:
        path = _cache_path(ticker)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # noqa: BLE001
        logger.warning("could not read StockTwits cache for %s: %s", ticker, exc)
        return None


def _is_failure(block: str) -> bool:
    return block.startswith(_FAILURE_PREFIX)


def make_resilient(impl):
    """Wrap a fetch_stocktwits_messages-style implementation with retry+cache.

    ``impl`` takes ``(ticker, **kwargs)`` and returns a plaintext block string
    (``<stocktwits unavailable: ...>`` on failure). Signature and output block
    format are drop-in identical to the framework's fetcher.
    """
    def resilient(ticker, **kwargs):
        block = impl(ticker, **kwargs)
        attempt = 0
        while _is_failure(block) and attempt < _MAX_RETRIES:
            attempt += 1
            delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, 2.0)
            logger.warning(
                "StockTwits fetch failed for %s (attempt %d/%d); "
                "backing off %.1fs then retrying",
                ticker, attempt, _MAX_RETRIES, delay,
            )
            time.sleep(delay)
            block = impl(ticker, **kwargs)

        if _is_failure(block):
            cached = _load_cache(ticker)
            if cached is not None:
                logger.warning("serving cached StockTwits block for %s from %s",
                               ticker, cached["date"])
                return (f"{cached['block']}\n\n"
                        f"(StockTwits messages cached from {cached['date']}; "
                        f"live fetch failed today)")
            return block
        if not block.startswith(_EMPTY_PREFIX):
            _store_cache(ticker, block)
        return block

    resilient._wrapped_original = impl  # tests unwrap to the underlying fetcher
    return resilient
