"""daily_run.py — daily pipeline orchestrator (watchlist assembly first)."""

import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from broker import create_broker
from config import load_watchlist_config
from decisions import BUY_RATINGS, compute_orders
from screener import load_pool, load_regime
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.dataflows.config import set_config
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def TODAY_ET() -> date:
    return datetime.now(ET).date()


def extract_rating(signal_text: str) -> str:
    # REVIEW is a visible no-op, NOT a Hold: parse_rating would silently
    # fabricate Hold for an unparseable decision (#1170 spirit). Consumers
    # (compute_orders) ignore REVIEW -- it trades nothing.
    if signal_text and str(signal_text).strip().upper() == "REVIEW":
        return "REVIEW"
    return parse_rating(signal_text)


class WatchlistShortError(Exception):
    pass


def _recently_touched(entry, today, exclusion_days):
    try:
        entry_date = date.fromisoformat(entry["date"])
    except (KeyError, ValueError, TypeError):
        return False
    return entry_date >= today - timedelta(days=exclusion_days)


def assemble_watchlist(holdings, pool, memory_entries, cfg, today):
    cfg = cfg or {}

    def scfg(key, default):
        # Top-level key wins; falls back to the nested `screener:` block
        # (watchlist.yaml nests these under `screener:`).
        return cfg.get(key, cfg.get("screener", {}).get(key, default))

    candidate_slots = int(scfg("candidate_slots", 3))
    exclusion_days = int(scfg("exclusion_days", 7))
    min_size = int(scfg("min_watchlist_size", 5))

    by_ticker = {e["ticker"]: e for e in memory_entries if e.get("ticker")}

    def eligible(ticker):
        # Excluded: held, or any memory entry within the exclusion window
        # (recent analysis = churn; a recent Sell/Underweight is covered by
        # the same rule per spec §5bis).
        if ticker in holdings:
            return False
        entry = by_ticker.get(ticker)
        if entry is None:
            return True
        return not _recently_touched(entry, today, exclusion_days)

    candidates = []
    for item in pool:
        if len(candidates) >= candidate_slots:
            break
        if item["ticker"] in {c["ticker"] for c in candidates}:
            continue
        if eligible(item["ticker"]):
            candidates.append({"ticker": item["ticker"]})

    watchlist = sorted(set(holdings) | {c["ticker"] for c in candidates})

    if len(watchlist) < min_size:
        for item in pool:
            if len(watchlist) >= min_size:
                break
            ticker = item["ticker"]
            if ticker in watchlist or not eligible(ticker):
                continue
            watchlist.append(ticker)
            watchlist.sort()

    if len(watchlist) < min_size:
        # Last resort: seed list (first run before any pool exists, or pool
        # exhausted). The min gate still applies afterwards.
        for ticker in (cfg.get("seed_watchlist") or []):
            if len(watchlist) >= min_size:
                break
            if ticker not in watchlist:
                watchlist.append(ticker)
        watchlist.sort()

    if len(watchlist) < min_size:
        raise WatchlistShortError(
            f"watchlist has {len(watchlist)} tickers; minimum is {min_size} "
            f"(pool exhausted, seed insufficient)")

    return watchlist


def _next_candidates(pool, holdings, memory_entries, cfg, today, skip, limit):
    """Next eligible pool candidates (rank order) not yet analyzed today.

    Mirrors assemble_watchlist's draw but for the buy-quota expansion loop:
    walks deeper into the ranked pool, skipping tickers already analyzed
    (``skip``), held, or inside the exclusion window.
    """
    cfg = cfg or {}
    exclusion_days = int(cfg.get("screener", {}).get("exclusion_days", 7))
    by_ticker = {e["ticker"]: e for e in memory_entries if e.get("ticker")}
    out = []
    for item in pool:
        if len(out) >= limit:
            break
        ticker = item["ticker"]
        if ticker in skip or ticker in holdings:
            continue
        entry = by_ticker.get(ticker)
        if entry is not None and _recently_touched(entry, today, exclusion_days):
            continue
        if ticker in out:
            continue
        out.append(ticker)
    return out


def _buy_count(ratings: dict[str, str]) -> int:
    return sum(1 for r in ratings.values() if r in BUY_RATINGS)


# --- orchestrator ---

DISABLE_TRADING_FILE = Path("DISABLE_TRADING")


def _today_str() -> str:
    return TODAY_ET().isoformat()


def _ratings_path(cfg: dict) -> Path:
    return Path(cfg["results_dir"]) / f"ratings_{_today_str()}.json"


def _executed_path(cfg: dict) -> Path:
    return Path(cfg["results_dir"]) / f"executed_{_today_str()}.json"


def _last_close(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if len(hist) < 1:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None


# --- parallel analysis -------------------------------------------------------

_MEMORY_WRITE_LOCK = threading.RLock()  # re-entrant: wrapped methods may nest (double-patch in tests)
_MEMORY_PATCHED = False


def _ensure_memory_write_lock() -> None:
    """Serialize TradingMemoryLog writes from concurrent analyze workers.

    The framework's memory log has no internal locking; concurrent
    propagate() calls (parallel tickers) must not interleave appends into
    trading_memory.md nor race read-modify-rename outcome updates (a lost
    atomic replace). Patched once, lazily, from this module so the framework
    package itself stays untouched.
    """
    global _MEMORY_PATCHED
    if _MEMORY_PATCHED:
        return
    import tradingagents.agents.utils.memory as memory_mod

    wrapped_methods = [
        "store_decision",
        "update_with_outcome",
        "batch_update_with_outcomes",
    ]
    for name in wrapped_methods:
        original = getattr(memory_mod.TradingMemoryLog, name)

        def locked(self, *args, _orig=original, **kwargs):
            with _MEMORY_WRITE_LOCK:
                return _orig(self, *args, **kwargs)

        setattr(memory_mod.TradingMemoryLog, name, locked)
    _MEMORY_PATCHED = True


_REDDIT_LOCK = threading.RLock()  # re-entrant: reddit.py's own 429 retry re-invokes the module attr,
# which is our wrapper — a plain Lock would deadlock the same-thread re-entry.
_REDDIT_PATCHED = False
_REDDIT_MIN_INTERVAL = 8.0  # seconds between Reddit requests (anonymous ~10/min)
_REDDIT_LAST_TS = 0.0  # monotonic timestamp of the last request, guarded by _REDDIT_LOCK
_REDDIT_OAUTH_PATCHED = False
_REDDIT_OAUTH_ACTIVE = False
_STOCKTWITS_PATCHED = False
_REDDIT_ARCHIVE_PATCHED = False


def _ensure_stocktwits_resilience() -> None:
    """Wrap the sentiment analyst's StockTwits fetch with retry + per-ticker cache.

    The public StockTwits endpoint intermittently 403s under parallel analyze
    workers (burst throttling) — the same failure class Reddit's RSS path has.
    A retry-with-backoff + cache wrapper guarantees the analyst always gets
    StockTwits data. Framework untouched: the swap is lazy from this module.
    """
    global _STOCKTWITS_PATCHED
    if _STOCKTWITS_PATCHED:
        return
    import stocktwits_resilience
    import tradingagents.agents.analysts.sentiment_analyst as sa

    original = sa.fetch_stocktwits_messages
    sa.fetch_stocktwits_messages = stocktwits_resilience.make_resilient(original)
    _STOCKTWITS_PATCHED = True


def _ensure_reddit_archive() -> None:
    """Wrap the sentiment analyst's Reddit fetch archive-first (Arctic Shift).

    The anonymous RSS path loses subreddits to 429s under parallel workers;
    the keyless Arctic Shift archive gives complete 7-day coverage, cached
    per subreddit and filtered locally per ticker. Falls back to the existing
    resilient RSS path when the archive is unreachable. Framework untouched:
    the swap is lazy from this module.
    """
    global _REDDIT_ARCHIVE_PATCHED
    if _REDDIT_ARCHIVE_PATCHED:
        return
    import reddit_archive
    import tradingagents.agents.analysts.sentiment_analyst as sa

    original = sa.fetch_reddit_posts
    sa.fetch_reddit_posts = reddit_archive.make_archive_aware(original)
    _REDDIT_ARCHIVE_PATCHED = True


_GRAPH_TOOL_CALLBACKS_PATCHED = False


def _ensure_graph_tool_callbacks() -> None:
    """Inject the thread-local structured logger into graph-invoke callbacks.

    The framework's Propagator.get_graph_args accepts ``callbacks`` for tool
    execution tracking (propagation.py) but _run_graph never passes any —
    so ToolNode executions (FRED, stock data, news tools) emit nothing with
    constructor-bound callbacks alone (verified live). This patch makes every
    graph invocation pick up the current thread's structured logger, giving
    tool_start/tool_end events with per-analyst attribution.
    """
    global _GRAPH_TOOL_CALLBACKS_PATCHED
    if _GRAPH_TOOL_CALLBACKS_PATCHED:
        return
    import structured_log
    import tradingagents.graph.propagation as prop_mod

    original = prop_mod.Propagator.get_graph_args

    def with_structured_log(self, callbacks=None):
        args = original(self, callbacks=callbacks)
        active = structured_log.get_active_logger()
        if active is not None:
            config = args.setdefault("config", {})
            config["callbacks"] = list(config.get("callbacks") or []) + [active]
        return args

    with_structured_log._wrapped_original = original
    prop_mod.Propagator.get_graph_args = with_structured_log
    _GRAPH_TOOL_CALLBACKS_PATCHED = True


_NEWS_LOGGING_PATCHED = False


def _ensure_news_logging() -> None:
    """Wrap the sentiment analyst's direct get_news call for structured logs.

    The sentiment analyst pre-fetches news by calling ``get_news.func``
    directly (not through a LangGraph ToolNode), so it is invisible to the
    invoke-level callbacks. Wrap the tool's func to emit a fetch event into
    the thread-local structured log.
    """
    global _NEWS_LOGGING_PATCHED
    if _NEWS_LOGGING_PATCHED:
        return
    import structured_log
    import tradingagents.agents.analysts.sentiment_analyst as sa

    tool = sa.get_news
    original_func = tool.func

    def logged_news_func(*args, **kwargs):
        t0 = time.monotonic()
        try:
            out = original_func(*args, **kwargs)
            structured_log.emit_fetch(
                source="yahoo_news", agent="Sentiment Analyst", mode="live",
                latency_s=round(time.monotonic() - t0, 2),
                bytes=len(str(out or "")),
            )
            return out
        except Exception as exc:  # noqa: BLE001
            structured_log.emit_fetch(
                source="yahoo_news", agent="Sentiment Analyst", mode="placeholder",
                latency_s=round(time.monotonic() - t0, 2), error=str(exc)[:200],
            )
            raise

    logged_news_func._wrapped_original = original_func
    tool.func = logged_news_func
    _NEWS_LOGGING_PATCHED = True


_FRED_PATCHED = False


def _ensure_fred_aliases() -> None:
    """Close the FRED alias-discovery gap without touching the framework.

    The news analyst invents snake_case aliases (observed 2026-09-02:
    ``crude_oil_wti``) because the tool description discloses only a handful
    of examples and the framework passes any unmapped string to FRED verbatim
    as a raw series ID, which then 400s. Two runtime patches, both idempotent:

    1. Extend ``fred.MACRO_SERIES`` with the observed oil aliases (FRED's WTI
       spot series is ``DCOILWTICO``) so such requests resolve.
    2. Append the full alias map plus a "unlisted strings go to FRED verbatim"
       warning to the live ``get_macro_indicators`` tool description, so the
       model picks from the real map instead of inventing names.
    """
    global _FRED_PATCHED
    if _FRED_PATCHED:
        return
    import tradingagents.agents.utils.macro_data_tools as mdt
    import tradingagents.dataflows.fred as fred_mod

    for alias, series_id in _FRED_ALIAS_EXTENSIONS.items():
        fred_mod.MACRO_SERIES.setdefault(alias, series_id)

    tool = mdt.get_macro_indicators
    base = getattr(tool, "_wrapped_original_description", tool.description)
    tool._wrapped_original_description = base
    alias_list = ", ".join(sorted(fred_mod.MACRO_SERIES))
    tool.description = (
        f"{base}\n\nKnown friendly aliases (prefer these): {alias_list}.\n"
        "An indicator string NOT in that list is sent to FRED verbatim as a "
        "series ID and will error if no such series exists — do not invent "
        "aliases."
    )
    _FRED_PATCHED = True


# Aliases added on top of the framework's curated map. Every series ID was
# verified live against FRED's series endpoint (2026-09-02) — several plausible
# guesses were dead IDs (gold London fixing, ISM NAPM, JOLTS are all gone from
# FRED; the 3-month CMT is DGS3MO, not DGS3M), so nothing unverified lands here.
_FRED_ALIAS_EXTENSIONS = {
    # Energy (news analyst asked for oil on 2026-09-02; no alias existed)
    "crude_oil_wti": "DCOILWTICO",
    "wti": "DCOILWTICO",
    "crude_oil": "DCOILWTICO",
    "crude": "DCOILWTICO",
    "oil": "DCOILWTICO",
    "crude_oil_brent": "DCOILBRENTEU",
    "brent": "DCOILBRENTEU",
    "natural_gas": "DHHNGSP",
    "henry_hub": "DHHNGSP",
    # Treasury curve depth (map has 2y/10y/30y only)
    "3m_treasury": "DGS3MO",
    "5y_treasury": "DGS5",
    "10y_3m_spread": "T10Y3M",
    # Labor & housing follow-ups
    "hourly_earnings": "CES0500000003",
    "wage_growth": "CES0500000003",
    "case_shiller": "CSUSHPISA",
    "home_prices": "CSUSHPISA",
}


# --- Portfolio-context injection (phantom-position fix) -----------------------
#
# Every agent renders ``instrument_context`` from state at prompt time, but
# nothing ever told an agent whether the analyzed ticker is actually held --
# so the rating scale's holder verbs ("Hold: maintain current position") made
# agents fabricate positions (10 of 15 PM decisions referenced phantom
# holdings on a flat book, 2026-09-02). Two runtime patches:
#
#   Tier 1 (all agents): a stance line appended to the instrument context at
#     resolve time -- flat book => "deciding whether to initiate", held =>
#     shares/avg-cost/weight. Every agent embeds instrument_context, so the
#     seed (and the contradiction) reaches the earliest reports.
#   Tier 2 (decision tail only): the Research Manager, three risk debators,
#     and Portfolio Manager additionally see a precomputed book-shape block
#     (count/cash/sector mix by value) plus a no-cross-ticker-trades rule.
#     Analysts/researchers/trader never see it -- book noise is a
#     hallucination seed in evidence-gathering stages.
#
# Both tiers are keyed on a real broker snapshot; broker failure means NO
# injection (never assert a wrong book). Nothing under tradingagents/ changes.

_PORTFOLIO_SNAPSHOT_TTL_S = 600.0
# ts=None means "never fetched" -- a numeric 0.0 sentinel is wrong because
# monotonic() starts near 0 on a freshly booted machine, so `now - 0.0 < TTL`
# would treat an empty cache as a fresh cache hit of None.
_portfolio_cache: dict = {"ts": None, "snap": None}
_portfolio_lock = threading.Lock()
_PORTFOLIO_PATCHED = False
_PORTFOLIO_ORIGINALS: dict = {}

# The 5 decision-tail factories in tradingagents/graph/setup.py whose nodes
# render instrument_context at prompt time (research_manager.py:28,
# risk_mgmt/*:35, portfolio_manager.py:45).
_TAIL_FACTORY_NAMES = (
    "create_research_manager",
    "create_aggressive_debator",
    "create_neutral_debator",
    "create_conservative_debator",
    "create_portfolio_manager",
)


def _portfolio_snapshot(cfg: dict) -> dict | None:
    """Memoized real-account snapshot: cash, positions (shares, avg entry,
    mark value, sector), invested total, sector mix by value.

    ``None`` when the broker is unreachable -- callers then skip portfolio
    injection entirely. Retried after the TTL even after a failure so a
    transient broker blip at run start does not disable injection for a long
    analyze run.
    """
    with _portfolio_lock:
        ts = _portfolio_cache["ts"]
        if ts is not None and time.monotonic() - ts < _PORTFOLIO_SNAPSHOT_TTL_S:
            return _portfolio_cache["snap"]
    snap = _fetch_portfolio_snapshot(cfg)
    with _portfolio_lock:
        _portfolio_cache["ts"] = time.monotonic()
        _portfolio_cache["snap"] = snap
    return snap


def _fetch_portfolio_snapshot(cfg: dict) -> dict | None:
    try:
        broker = create_broker(cfg)
        broker.connect()
        try:
            holdings, cash = broker.get_positions_and_cash()
            details = {}
            getter = getattr(broker, "get_position_details", None)
            if getter is not None:
                try:
                    details = getter()
                except Exception:  # noqa: BLE001 - details are optional
                    logger.warning("portfolio snapshot: position details unavailable")
        finally:
            broker.disconnect()
    except Exception as exc:  # noqa: BLE001 - never block analysis on the book
        logger.warning("portfolio snapshot unavailable (%s); no stance/shape "
                       "injection this run", exc)
        return None

    from tradingagents.agents.utils.agent_utils import resolve_instrument_identity

    normalized: dict[str, dict] = {}
    invested = 0.0
    sector_value: dict[str, float] = {}
    for ticker, shares in (holdings or {}).items():
        price = _last_close(ticker)
        value = price * shares if price else None
        sector = "Unknown"
        try:
            identity = resolve_instrument_identity(ticker) or {}
            sector = identity.get("sector") or "Unknown"
        except Exception:  # noqa: BLE001 - sector enrichment is best-effort
            pass
        avg = None
        detail = (details or {}).get(ticker)
        if detail:
            avg = detail.get("avg_entry_price")
        normalized[ticker] = {"shares": int(shares),
                              "avg_entry_price": avg,
                              "value": value,
                              "sector": sector}
        if value is not None:
            invested += value
            sector_value[sector] = sector_value.get(sector, 0.0) + value
    return {"cash": float(cash or 0.0),
            "max_positions": int(cfg.get("max_positions", 10)),
            "holdings": normalized,
            "invested": invested,
            "sectors": sector_value}


def _portfolio_stance_line(ticker: str, snap: dict | None) -> str:
    """Ground truth for the analyzed ticker: initiate vs add/trim."""
    if not snap:
        return ""
    holding = snap["holdings"].get(ticker)
    if holding is None:
        return (
            f"Portfolio context (ground truth): no current position in "
            f"{ticker}. You are deciding whether to initiate. References to "
            f"an existing position in {ticker} are incorrect."
        )
    parts = [f"Portfolio context (ground truth): holding {holding['shares']} "
             f"shares of {ticker}"]
    if holding.get("avg_entry_price"):
        parts.append(f"at avg cost ${holding['avg_entry_price']:.2f}")
    if holding.get("value") is not None:
        total = snap["invested"] + snap["cash"]
        if total > 0:
            parts.append(f"({holding['value'] / total * 100:.1f}% of the book)")
    parts.append("Trim/add language must match this position.")
    return " ".join(parts)


def _portfolio_book_shape(ticker: str, snap: dict | None) -> str:
    """Precomputed book facts for the decision tail -- no raw lists, no
    arithmetic left to the model."""
    if not snap:
        return ""
    total = snap["invested"] + snap["cash"]
    invested_pct = snap["invested"] / total * 100 if total > 0 else 0.0
    mix = []
    for sector, value in sorted(snap["sectors"].items(),
                                key=lambda kv: -kv[1]):
        names = sorted(t for t, h in snap["holdings"].items()
                       if h.get("sector") == sector)
        pct = value / total * 100 if total > 0 else 0.0
        label = f"{sector} {pct:.0f}%"
        if names:
            label += f" ({', '.join(names)})"
        mix.append(label)
    sector_line = ("Sector mix by value: " + "; ".join(mix) if mix
                   else "Sector mix by value: none (flat book).")
    return (
        f"Current book (ground truth): {len(snap['holdings'])}/"
        f"{snap['max_positions']} positions, ${snap['invested']:,.0f} invested "
        f"({invested_pct:.0f}% of ${total:,.0f}), ${snap['cash']:,.0f} cash.\n"
        f"{sector_line}\n"
        f"Rule: never propose trades outside {ticker}; other holdings are "
        f"concentration/sizing context only."
    )


def _ensure_portfolio_context(cfg: dict) -> None:
    """Install the two-tier portfolio injection (idempotent, revertible).

    Tier 1 wraps TradingAgentsGraph.resolve_instrument_context -- the single
    seam propagate() uses (trading_graph.py:519) to seed the context every
    agent renders. Tier 2 wraps the 5 decision-tail factories resolved by
    tradingagents.graph.setup so only their nodes see the book shape.
    """
    global _PORTFOLIO_PATCHED
    if _PORTFOLIO_PATCHED:
        return
    import tradingagents.graph.setup as setup_mod
    import tradingagents.graph.trading_graph as tg_mod

    original_resolve = tg_mod.TradingAgentsGraph.resolve_instrument_context

    def resolve_with_stance(self, ticker: str, asset_type: str = "stock") -> str:
        base = original_resolve(self, ticker, asset_type)
        snap = _portfolio_snapshot(cfg)
        line = _portfolio_stance_line(ticker, snap) if snap else ""
        return f"{base}\n\n{line}".strip() if line else base

    resolve_with_stance._wrapped_original = original_resolve
    tg_mod.TradingAgentsGraph.resolve_instrument_context = resolve_with_stance

    def shape_factory(factory_name: str, original_factory):
        def wrapped_factory(llm):
            node = original_factory(llm)

            def node_with_shape(state):
                snap = _portfolio_snapshot(cfg)
                if snap:
                    ctx = state.get("instrument_context")
                    if isinstance(ctx, str) and ctx.strip():
                        block = _portfolio_book_shape(
                            state.get("company_of_interest") or "", snap)
                        if block:
                            state = {**state,
                                     "instrument_context": ctx + "\n\n" + block}
                return node(state)

            node_with_shape._wrapped_original = node
            return node_with_shape

        wrapped_factory._wrapped_original = original_factory
        return wrapped_factory

    for name in _TAIL_FACTORY_NAMES:
        original_factory = getattr(setup_mod, name)
        wrapped = shape_factory(name, original_factory)
        setattr(setup_mod, name, wrapped)
        _PORTFOLIO_ORIGINALS[name] = original_factory
    _PORTFOLIO_ORIGINALS["resolve_instrument_context"] = original_resolve
    _PORTFOLIO_PATCHED = True


def _reset_portfolio_context() -> None:
    """Restore the framework seams (tests; also safe to call at any time)."""
    global _PORTFOLIO_PATCHED
    if _PORTFOLIO_PATCHED:
        import tradingagents.graph.setup as setup_mod
        import tradingagents.graph.trading_graph as tg_mod

        for name in _TAIL_FACTORY_NAMES:
            if name in _PORTFOLIO_ORIGINALS:
                setattr(setup_mod, name, _PORTFOLIO_ORIGINALS[name])
        if "resolve_instrument_context" in _PORTFOLIO_ORIGINALS:
            tg_mod.TradingAgentsGraph.resolve_instrument_context = (
                _PORTFOLIO_ORIGINALS["resolve_instrument_context"])
        _PORTFOLIO_ORIGINALS.clear()
        _PORTFOLIO_PATCHED = False
    with _portfolio_lock:
        _portfolio_cache["ts"] = None
        _portfolio_cache["snap"] = None


# --- structured-output fallback visibility (F3) ------------------------------
#
# When an agent's structured-output invocation fails (schema rejection,
# malformed JSON, or a reasoning model answering in prose without calling
# the schema tool), the framework retries once as free text and only logs a
# warning on its own logger -- nothing ties the fallback to the ticker, and
# nothing downstream can tell a run fell back. A logging handler on that
# module logger routes each fallback into the per-ticker structured log.
# The rating guard (in _propagate_with_structured_log) then ensures a
# header-less fallback decision cannot silently pick a rating by prose-word
# guess.

_STRUCTURED_FALLBACK_HANDLER: logging.Handler | None = None


def _reset_structured_fallback_logging() -> None:
    """Detach the fallback handler (tests; safe to call anytime)."""
    global _STRUCTURED_FALLBACK_HANDLER
    if _STRUCTURED_FALLBACK_HANDLER is not None:
        import tradingagents.agents.utils.structured as structured_mod
        structured_mod.logger.removeHandler(_STRUCTURED_FALLBACK_HANDLER)
        _STRUCTURED_FALLBACK_HANDLER = None


def _ensure_structured_fallback_logging() -> None:
    """Route the framework's structured-fallback warnings into the log."""
    global _STRUCTURED_FALLBACK_HANDLER
    if _STRUCTURED_FALLBACK_HANDLER is not None:
        return
    import structured_log
    import tradingagents.agents.utils.structured as structured_mod

    class FallbackHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: A003
            try:
                message = record.getMessage()
                agent = str(record.args[0]) if record.args else "unknown"
                error = str(record.args[1]) if len(record.args) > 1 else message
                mode = ("retry"
                        if "structured-output invocation failed" in message
                        else "permanent")
                structured_log.emit_structured_fallback(
                    agent=agent, error=error, mode=mode)
            except Exception:  # noqa: BLE001 - logging must never throw
                pass

    _STRUCTURED_FALLBACK_HANDLER = FallbackHandler()
    _STRUCTURED_FALLBACK_HANDLER.setLevel(logging.WARNING)
    structured_mod.logger.addHandler(_STRUCTURED_FALLBACK_HANDLER)


# --- analyst report recovery (F7) --------------------------------------------
#
# The tool-loop analysts (market/news/fundamentals) capture their report only
# from the CURRENT message's content when it has zero tool calls
# (e.g. market_analyst.py:87-88). The router exits the loop only on a
# zero-tool-call message, so the normal path is fine -- but when the model's
# final message has EMPTY content, the report is empty and any substantive
# analysis the analyst wrote in earlier turns (messages that also carried
# tool calls) is lost downstream: the clear node wipes the history and
# bull/bear/debators never see it. Runtime fix: wrap the three analyst
# factories; when the returned report is empty, rebuild it from the
# analyst's own accumulated AIMessage content (tool results stay out).

_ANALYST_REPORT_RECOVERY_PATCHED = False
_ANALYST_REPORT_RECOVERY_ORIGINALS: dict = {}

_ANALYST_REPORT_KEYS = {
    "create_market_analyst": "market_report",
    "create_news_analyst": "news_report",
    "create_fundamentals_analyst": "fundamentals_report",
}


def _content_to_text(content) -> str:
    """Render a message payload to text (strings pass through; content-block
    lists join their text blocks)."""
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


def _ensure_analyst_report_recovery() -> None:
    """Wrap the 3 tool-loop analyst factories to rebuild empty reports from
    the analyst's own stranded message text (idempotent, revertible)."""
    global _ANALYST_REPORT_RECOVERY_PATCHED
    if _ANALYST_REPORT_RECOVERY_PATCHED:
        return
    import tradingagents.graph.setup as setup_mod

    for factory_name, report_key in _ANALYST_REPORT_KEYS.items():
        original_factory = getattr(setup_mod, factory_name)

        def wrapped_factory(llm, _key=report_key, _orig=original_factory):
            node = _orig(llm)

            def node_with_recovery(state):
                out = node(state)
                report = out.get(_key)
                if report is None or (isinstance(report, str)
                                      and not report.strip()):
                    texts = []
                    for msg in state.get("messages") or []:
                        if getattr(msg, "type", "") == "ai":
                            text = _content_to_text(getattr(msg, "content", None))
                            if text.strip():
                                texts.append(text.strip())
                    for msg in out.get("messages") or []:
                        text = _content_to_text(getattr(msg, "content", None))
                        if text.strip():
                            texts.append(text.strip())
                    if texts:
                        out = {**out, _key: "\n\n".join(texts)}
                return out

            node_with_recovery._wrapped_original = node
            return node_with_recovery

        wrapped_factory._wrapped_original = original_factory
        setattr(setup_mod, factory_name, wrapped_factory)
        _ANALYST_REPORT_RECOVERY_ORIGINALS[factory_name] = original_factory
    _ANALYST_REPORT_RECOVERY_PATCHED = True


def _reset_analyst_report_recovery() -> None:
    """Restore the analyst factory seams (tests; safe anytime)."""
    global _ANALYST_REPORT_RECOVERY_PATCHED
    if _ANALYST_REPORT_RECOVERY_PATCHED:
        import tradingagents.graph.setup as setup_mod

        for name, original in _ANALYST_REPORT_RECOVERY_ORIGINALS.items():
            setattr(setup_mod, name, original)
        _ANALYST_REPORT_RECOVERY_ORIGINALS.clear()
        _ANALYST_REPORT_RECOVERY_PATCHED = False


def _ensure_reddit_pacing() -> None:
    """Rate-limit Reddit fetches across parallel analyze workers.

    Parallel ticker analyses would otherwise burst Reddit's anonymous
    per-IP rate limit (~10 req/min): 4 workers x 3 subreddits interleaved
    trips 429s on every ticker. Serializing alone is insufficient — the
    framework's 1s inter-sub pacing still sustains ~1 req/sec — so this
    enforces a minimum interval between requests, held under the lock so
    concurrent workers queue instead of firing together. Framework package
    untouched: the patch is applied lazily from this module.
    """
    global _REDDIT_PATCHED, _REDDIT_LAST_TS
    if _REDDIT_PATCHED:
        return
    import tradingagents.dataflows.reddit as reddit_mod

    original = reddit_mod._fetch_subreddit_rss

    def paced_rss(*args, **kwargs):
        global _REDDIT_LAST_TS
        with _REDDIT_LOCK:
            elapsed = time.monotonic() - _REDDIT_LAST_TS
            if elapsed < _REDDIT_MIN_INTERVAL:
                time.sleep(_REDDIT_MIN_INTERVAL - elapsed)
            _REDDIT_LAST_TS = time.monotonic()
            return original(*args, **kwargs)

    paced_rss._wrapped_original = original  # tests unwrap to the real fetcher
    reddit_mod._fetch_subreddit_rss = paced_rss
    _REDDIT_PATCHED = True


# --- OpenRouter provider pinning ---------------------------------------------

_OPENROUTER_PINS: dict[str, str] = {}
_OPENROUTER_PINS_APPLIED = False


def _ensure_openrouter_pins(pins: dict[str, str] | None = None) -> None:
    """Pin OpenRouter routing per model slug (Relace, DeepSeek, ...).

    OpenRouter serves many slugs from multiple hosting providers and rotates
    between them by default; a pin orders the request to the named provider
    first (``allow_fallbacks=true`` — the pin is a preference, not a hard
    lock) by injecting OpenRouter's ``provider`` routing body through the
    OpenAI-compatible request. Framework untouched: the
    ``OpenAIClient.get_llm`` method is wrapped lazily from this module.
    """
    global _OPENROUTER_PINS, _OPENROUTER_PINS_APPLIED
    if _OPENROUTER_PINS_APPLIED:
        return
    _OPENROUTER_PINS = dict(pins or {})
    import tradingagents.llm_clients.openai_client as oc

    original = oc.OpenAIClient.get_llm

    def pinned_get_llm(self):
        llm = original(self)
        provider = _OPENROUTER_PINS.get(getattr(llm, "model_name", ""))
        if provider and getattr(self, "provider", "") == "openrouter":
            llm.extra_body = {**(getattr(llm, "extra_body", None) or {}),
                              "provider": {"order": [provider],
                                           "allow_fallbacks": True}}
            logger.info("OpenRouter pin: %s -> %s (fallbacks allowed)",
                        llm.model_name, provider)
        return llm

    pinned_get_llm._wrapped_original = original
    oc.OpenAIClient.get_llm = pinned_get_llm
    _OPENROUTER_PINS_APPLIED = True


def _ensure_reddit_oauth() -> bool:
    """Ensure the sentiment analyst always gets Reddit data.

    Swaps the sentiment analyst's fetch_reddit_posts to a resilient wrapper
    (retry-with-backoff + per-ticker cache) around either the OAuth fetcher
    (when REDDIT_CLIENT_ID / REDDIT_SECRET are set) or the framework's RSS
    path. Returns True when OAuth is active (100 QPM — the paced anonymous
    wrapper becomes unnecessary); False on the RSS path (caller keeps
    pacing to avoid 429 bursts).
    """
    global _REDDIT_OAUTH_PATCHED, _REDDIT_OAUTH_ACTIVE
    if _REDDIT_OAUTH_PATCHED:
        return _REDDIT_OAUTH_ACTIVE
    import reddit_auth

    # sentiment_analyst binds fetch_reddit_posts at import time; swap its
    # module global so the analyst's calls use our resilient wrapper.
    # Signature and output block format are drop-in identical.
    import tradingagents.agents.analysts.sentiment_analyst as sa

    original = sa.fetch_reddit_posts
    if reddit_auth.credentials_available():
        impl = reddit_auth.fetch_reddit_posts
        active = True
        logger.info("Reddit: using OAuth fetcher (100 QPM) with retry+cache")
    else:
        impl = original
        active = False
        logger.info("Reddit: using paced RSS path with retry+cache")
    sa.fetch_reddit_posts = reddit_auth.make_resilient(impl)
    _REDDIT_OAUTH_PATCHED = True
    _REDDIT_OAUTH_ACTIVE = active
    return active


def _analyze_one(ticker: str, today_str: str, cfg: dict):
    """Run the full framework pipeline for one ticker with one retry.

    Returns (ticker, rating, error). Runs inside a worker thread, so it must
    never raise: all failures are reported through the error slot.
    """
    import structured_log
    run_log = structured_log.StructuredRunLogger(ticker=ticker, today=today_str)
    try:
        rating = _propagate_with_structured_log(ticker, today_str, cfg, run_log)
        run_log.finish(rating=rating)
        return ticker, rating, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("analysis failed for %s: %s", ticker, exc)
        try:
            run_log.finish(rating=None)
            rating = _propagate_with_structured_log(ticker, today_str, cfg, run_log)
            run_log.finish(rating=rating)
            return ticker, rating, None
        except Exception as exc2:  # noqa: BLE001
            logger.error("retry also failed for %s: %s", ticker, exc2)
            run_log.finish(rating=None)
            return ticker, None, exc2


def _propagate_with_structured_log(ticker: str, today_str: str, cfg: dict,
                                   run_log) -> str:
    """Run the graph with ``run_log`` bound to this thread.

    The logger reaches the graph through the patched Propagator.get_graph_args
    (see _ensure_graph_tool_callbacks), which injects the thread-local logger
    into the invoke config — that is what makes ToolNode executions (FRED,
    stock data, news tools) emit events. Constructor callbacks alone never
    reach tools. Cleared in finally so parallel workers don't cross-talk.

    Rating safety (F3): propagate returns ``(state, signal)`` where the
    signal was parsed from the PM decision text by the framework's
    two-pass regex. Pass 1 (an explicit ``Rating:`` label) is trustworthy;
    pass 2 (first standalone 5-tier word anywhere in prose) is a guess that
    only ever fires when the PM fell back to free text without emitting a
    header. When the decision has no header we force REVIEW (a visible
    no-op) rather than let a prose word silently pick the rating.
    """
    import structured_log
    structured_log.set_active_logger(run_log)
    try:
        state, signal = TradingAgentsGraph(config=cfg).propagate(ticker, today_str)
        decision = (state.get("final_trade_decision")
                    if isinstance(state, dict) else None)
        if isinstance(decision, str) and decision.strip() \
                and not _header_rating(decision):
            logger.warning(
                "%s: PM decision has no explicit 'Rating:' header; framework "
                "signal %r came from a prose-word scan — forcing REVIEW "
                "(trades nothing) instead of trusting it", ticker, signal)
            structured_log.emit_structured_fallback(
                agent="Portfolio Manager",
                error=(f"header-less decision; framework signal {signal!r} "
                       "from prose-word scan; forced REVIEW"),
                mode="rating_guard")
            return "REVIEW"
        return extract_rating(signal)
    finally:
        structured_log.clear_active_logger()


def _header_rating(decision: str | None) -> str | None:
    """Explicit ``Rating:`` label only — never a prose-word guess.

    Reuses the framework's label regex and vocabulary (pinned version) but
    skips its pass-2 standalone-word scan, which misreads narrative sentences
    (e.g. "we should not sell into weakness" -> Sell).
    """
    if not decision:
        return None
    import unicodedata

    from tradingagents.agents.utils import rating as rating_mod

    norm = unicodedata.normalize("NFKC", str(decision))
    for line in norm.splitlines():
        match = rating_mod._RATING_LABEL_RE.search(line)
        if match and match.group(1).lower() in rating_mod._RATING_SET:
            return match.group(1).capitalize()
    return None


def run_analyze(cfg: dict, tickers: list[str] | None = None) -> dict:
    set_config(cfg)
    memory_log = TradingMemoryLog(cfg)
    ratings: dict[str, str] = {}
    failures: list[str] = []

    if tickers:
        watchlist = tickers
    else:
        holdings = set()
        broker = create_broker(cfg)
        try:
            broker.connect()
            holdings, _ = broker.get_positions_and_cash()
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not fetch holdings at analyze time (%s); "
                           "running candidates only — sells are blind today", exc)
        finally:
            broker.disconnect()
        pool = load_pool(cfg)
        watchlist = assemble_watchlist(holdings, pool,
                                       memory_log.load_entries(), cfg, TODAY_ET())

    _ensure_memory_write_lock()
    _ensure_openrouter_pins(cfg.get("openrouter_provider_pins"))
    if not _ensure_reddit_oauth():
        _ensure_reddit_pacing()
    _ensure_reddit_archive()
    _ensure_stocktwits_resilience()
    _ensure_graph_tool_callbacks()
    _ensure_news_logging()
    _ensure_fred_aliases()
    _ensure_structured_fallback_logging()
    _ensure_analyst_report_recovery()
    _ensure_portfolio_context(cfg)
    max_workers = max(1, int(cfg.get("analyze_max_workers", 4)))

    def record(result):
        ticker, rating, error = result
        if rating is not None:
            ratings[ticker] = rating
            logger.info("%s -> %s", ticker, rating)
        else:
            failures.append(ticker)

    def analyze_batch(tickers_batch):
        if max_workers <= 1 or len(tickers_batch) <= 1:
            for ticker in tickers_batch:
                record(_analyze_one(ticker, _today_str(), cfg))
        else:
            with ThreadPoolExecutor(max_workers=max_workers,
                                    thread_name_prefix="analyze") as pool:
                futures = [pool.submit(_analyze_one, t, _today_str(), cfg)
                           for t in tickers_batch]
                for future in as_completed(futures):
                    record(future.result())

    analyze_batch(watchlist)

    # Buy-quota expansion: if the base watchlist produced fewer agent-approved
    # buys than min_buy_quota (and the regime is not STRESS, which pauses new
    # buys anyway), keep analyzing deeper pool candidates — in rank order,
    # skipping held/excluded/already-analyzed — until the quota is met or
    # max_analyze tickers have been analyzed this run. Only the auto-watchlist
    # mode (explicit --tickers lists are fixed, e.g. smoke tests) expands.
    if tickers is None:
        scfg = cfg.get("screener", {}) or {}
        min_buy_quota = int(scfg.get("min_buy_quota", 0))
        max_analyze = int(scfg.get("max_analyze", 0)) or len(watchlist)
        candidate_slots = int(scfg.get("candidate_slots", 3))
        if min_buy_quota > 0 and load_regime(cfg) != "STRESS":
            while (_buy_count(ratings) < min_buy_quota
                   and len(ratings) + len(failures) < max_analyze):
                analyzed = set(ratings) | set(failures)
                more = _next_candidates(pool, holdings,
                                        memory_log.load_entries(), cfg,
                                        TODAY_ET(), analyzed, candidate_slots)
                if not more:
                    logger.info("buy quota %d unmet; pool exhausted "
                                "(have %d buys from %d tickers)",
                                min_buy_quota, _buy_count(ratings), len(ratings))
                    break
                logger.info("buy quota %d unmet (have %d); analyzing %d more: %s",
                            min_buy_quota, _buy_count(ratings), len(more), more)
                analyze_batch(more)

    payload = {"date": _today_str(),
               "ratings": dict(sorted(ratings.items())),
               "failures": sorted(failures)}
    path = _ratings_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("ratings written to %s", path)
    return payload


def _seconds_until_open(now: datetime | None = None) -> float:
    """Seconds until the 09:30 ET regular-session open (0 if already open)."""
    now = now or datetime.now(ET)
    open_today = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ET)
    if now < open_today:
        return (open_today - now).total_seconds()
    return 0.0


def run_execute(cfg: dict, dry_run: bool = False) -> int:
    if DISABLE_TRADING_FILE.exists() or not cfg.get("trading_enabled", True):
        logger.warning("trading disabled (kill switch); no orders placed")
        return 1
    ratings_path = _ratings_path(cfg)
    if not ratings_path.exists():
        logger.error("no ratings file for today (%s); refusing to execute", ratings_path)
        return 1
    if _executed_path(cfg).exists():
        logger.info("orders already executed today; skipping")
        return 0

    # Orders are submitted AT the open and then polled for fills (60s). A fill
    # can't happen before 09:30 ET, so a pre-open run must wait: submitting
    # early and polling would time out and cancel a perfectly valid order.
    # Dry-runs never wait (they're previews).
    wait = _seconds_until_open()
    if wait > 0 and not dry_run:
        logger.info("market opens in %.0fs; waiting before placing orders", wait)
        time.sleep(wait)

    payload = json.loads(ratings_path.read_text(encoding="utf-8"))
    broker = create_broker(cfg)
    try:
        broker.connect()
        holdings, cash = broker.get_positions_and_cash()
        last_close = {}
        for ticker in set(holdings) | set(payload["ratings"]):
            price = _last_close(ticker)
            if price is None:
                logger.warning("no last close for %s; skipping any order for it", ticker)
            last_close[ticker] = price or 0.0

        # Never size against configured capital the account cannot cover;
        # a rejected order is a silent no-op, a capped size is visible.
        capital = float(cfg.get("capital", 100_000))
        if cash < capital:
            logger.warning("account cash (%.2f) below configured capital (%.2f); "
                           "sizing against cash", cash, capital)
            capital = cash

        orders = compute_orders(
            payload["ratings"], holdings, last_close,
            capital=capital,
            max_positions=int(cfg.get("max_positions", 10)),
            max_order_value_cap=cfg.get("max_order_value_cap"),
            entry_protection_pct=float(cfg.get("screener", {}).get(
                "entry_protection_pct", 2.0)),
            stop_loss_pct=float(cfg.get("stop_loss_pct", 8.0)),
            conviction_weights=cfg.get("conviction_weights"))

        # Regime gate (execute side): STRESS suppresses new BUY orders —
        # rating-based exits still execute. Mirrors the pool-side pause.
        if load_regime(cfg) == "STRESS":
            buys = [o for o in orders if o.action == "BUY"]
            if buys:
                logger.warning("regime STRESS: suppressing %d new buy order(s); "
                               "exit orders only", len(buys))
            orders = [o for o in orders if o.action == "SELL"]

        # Two-phase execution log: write the "submitted" mark BEFORE placing
        # orders so a crash mid-submit can never double-execute on rerun
        # (the idempotency check at the top sees the mark). Dry-runs never
        # write it: a preview must not block the day's real execution.
        log_path = _executed_path(cfg)
        if not dry_run:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps({
                "date": _today_str(), "dry_run": False, "status": "submitted",
                "orders": [o.__dict__ for o in orders], "reports": [],
            }, indent=2), encoding="utf-8")

        reports = broker.place_market_orders(orders, dry_run=dry_run)
        log = {"date": _today_str(), "dry_run": dry_run, "status": "completed",
               "orders": [o.__dict__ for o in orders], "reports": reports}
        if not dry_run:
            log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
        return 0
    finally:
        broker.disconnect()


def healthcheck(cfg: dict) -> bool:
    broker = create_broker(cfg)
    try:
        broker.connect()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        broker.disconnect()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily trading pipeline")
    parser.add_argument("--analyze", action="store_true", help="morning analysis pass")
    parser.add_argument("--execute", action="store_true", help="open-time execution pass")
    parser.add_argument("--healthcheck", action="store_true", help="check broker reachability")
    parser.add_argument("--dry-run", action="store_true", help="print orders without placing")
    parser.add_argument("--tickers", default=None, help="comma-separated tickers (analyze)")
    args = parser.parse_args(argv)

    cfg = load_watchlist_config()
    set_config(cfg)

    if args.healthcheck:
        ok = healthcheck(cfg)
        print(f"broker ({cfg.get('broker', 'alpaca')}) reachable" if ok
              else f"broker ({cfg.get('broker', 'alpaca')}) UNREACHABLE")
        return 0 if ok else 1
    if args.analyze:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] \
            if args.tickers else None
        run_analyze(cfg, tickers)
        return 0
    if args.execute:
        return run_execute(cfg, dry_run=args.dry_run)
    parser.error("pass --analyze, --execute, or --healthcheck")
    return 2


if __name__ == "__main__":
    sys.exit(main())
