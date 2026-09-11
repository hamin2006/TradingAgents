"""daily_run.py — daily pipeline orchestrator (watchlist assembly first)."""

import argparse
import contextlib
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
from decisions import BUY_RATINGS, apply_cash_budget, compute_orders
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


_MEMORY_REVIEW_PATCHED = False
_MEMORY_REVIEW_ORIGINALS: dict = {}


def _heal_empty_decision_entry(log, ticker: str, trade_date: str,
                               decision: str) -> None:
    """Drop a pending entry whose DECISION body is empty when this attempt
    has real text — the framework's pending-scan idempotency would otherwise
    keep the empty entry forever, swallowing a successful retry."""
    text = str(decision or "")
    path = getattr(log, "_log_path", None)
    if not text.strip() or path is None or not path.exists():
        return
    raw = path.read_text(encoding="utf-8")
    sep = log._SEPARATOR
    chunks = [c for c in raw.split(sep) if c.strip()]
    prefix = f"[{trade_date} | {ticker} |"
    kept = []
    healed = False
    for chunk in chunks:
        stripped = chunk.strip()
        first_line = stripped.splitlines()[0] if stripped else ""
        if (not healed and first_line.startswith(prefix)
                and first_line.rstrip().endswith("| pending]")):
            body = ""
            if "DECISION:\n" in chunk:
                body = chunk.split("DECISION:\n", 1)[1]
            if not body.strip():
                healed = True
                continue
        kept.append(chunk)
    if healed:
        path.write_text("".join(c + sep for c in kept), encoding="utf-8")


def _ensure_memory_review_tag() -> None:
    """Tag REVIEW days honestly in the memory log and let a retry heal an
    empty entry.

    The framework's store_decision uses legacy parse_rating (default Hold),
    so an all-provider-error day (empty decision -> REVIEW, no trade) was
    archived as a Hold-tier decision and polluted hit-rate analytics. Two
    idempotent runtime patches:
      1. memory_mod.parse_rating -> the explicit ``Rating:`` header only
         (fall back to REVIEW), matching the ratings file's F3 rule;
      2. a store_decision wrapper -> before appending, drop a pending entry
         whose DECISION body is empty when the incoming decision has text,
         so a successful retry replaces it.
    """
    global _MEMORY_REVIEW_PATCHED
    if _MEMORY_REVIEW_PATCHED:
        return
    import tradingagents.agents.utils.memory as memory_mod

    original_parse = memory_mod.parse_rating

    def parse_review(text, _default="Hold"):
        rating = _header_rating(text)
        return rating if rating else "REVIEW"

    memory_mod.parse_rating = parse_review

    original_store = memory_mod.TradingMemoryLog.store_decision

    def store_with_review(self, *args, **kwargs):
        ticker = kwargs.get("ticker", args[0] if args else "")
        trade_date = kwargs.get("trade_date", args[1] if len(args) > 1 else "")
        decision = kwargs.get("final_trade_decision",
                              args[2] if len(args) > 2 else "")
        with _MEMORY_WRITE_LOCK:
            _heal_empty_decision_entry(self, ticker, trade_date, decision)
            return original_store(self, *args, **kwargs)

    store_with_review._wrapped_original = original_store
    memory_mod.TradingMemoryLog.store_decision = store_with_review
    _MEMORY_REVIEW_ORIGINALS["parse_rating"] = original_parse
    _MEMORY_REVIEW_ORIGINALS["store_decision"] = original_store
    _MEMORY_REVIEW_PATCHED = True


def _reset_memory_review_tag() -> None:
    """Restore the framework memory seams (tests; safe anytime)."""
    global _MEMORY_REVIEW_PATCHED
    if not _MEMORY_REVIEW_PATCHED:
        return
    import tradingagents.agents.utils.memory as memory_mod

    memory_mod.parse_rating = _MEMORY_REVIEW_ORIGINALS["parse_rating"]
    memory_mod.TradingMemoryLog.store_decision = \
        _MEMORY_REVIEW_ORIGINALS["store_decision"]
    _MEMORY_REVIEW_ORIGINALS.clear()
    _MEMORY_REVIEW_PATCHED = False


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

_NEWS_DATING_PATCHED = False
_NEWS_DATING_ORIGINALS: dict[str, object] = {}


def _reset_news_dating() -> None:
    """Restore the news tool .funcs (tests; safe anytime).

    Restores to the recorded pre-install functions, so it is safe whether or
    not _ensure_news_logging wrapped on top afterwards.
    """
    global _NEWS_DATING_PATCHED
    if not _NEWS_DATING_PATCHED:
        return
    import tradingagents.agents.utils.news_data_tools as ndt

    ndt.get_news.func = _NEWS_DATING_ORIGINALS["get_news"]
    ndt.get_global_news.func = _NEWS_DATING_ORIGINALS["get_global_news"]
    _NEWS_DATING_ORIGINALS.clear()
    _NEWS_DATING_PATCHED = False


def _ensure_news_dating() -> None:
    """Make news tool outputs carry publication dates + the verified-snapshot
    anchor (2026-09-03 audit: the yfinance feed drops per-article pub_dates at
    render time, so the News Analyst could not date "REGN pulled back 4.8%" —
    an Aug-28 claim — against a $852.03 Sep-2 verified close).

    Replaces .func on the shared news Tool objects (news_data_tools module
    level; agent_utils re-exports the same instances, so the News Analyst
    ToolNode and the Sentiment Analyst's direct pre-fetch both pick it up).
    Install BEFORE _ensure_news_logging so the logging wrapper stays outermost
    and the sentiment pre-fetch keeps emitting fetch_end events.
    """
    global _NEWS_DATING_PATCHED
    if _NEWS_DATING_PATCHED:
        return
    import tradingagents.agents.utils.news_data_tools as ndt
    from news_dating import render_global_news, render_ticker_news

    _NEWS_DATING_ORIGINALS["get_news"] = ndt.get_news.func
    _NEWS_DATING_ORIGINALS["get_global_news"] = ndt.get_global_news.func
    render_ticker_news._wrapped_original = _NEWS_DATING_ORIGINALS["get_news"]
    render_global_news._wrapped_original = _NEWS_DATING_ORIGINALS["get_global_news"]
    ndt.get_news.func = render_ticker_news
    ndt.get_global_news.func = render_global_news
    _NEWS_DATING_PATCHED = True


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


# --- EDGAR fundamentals (companyfacts as-filed, config-gated) ---------------

_EDGAR_FUNDAMENTALS_PATCHED = False
_EDGAR_FUNDAMENTALS_ORIGINALS: dict[str, object] = {}


def _reset_edgar_fundamentals() -> None:
    """Restore the four fundamentals tool .funcs (tests; safe anytime)."""
    global _EDGAR_FUNDAMENTALS_PATCHED
    if not _EDGAR_FUNDAMENTALS_PATCHED:
        return
    import tradingagents.agents.utils.fundamental_data_tools as fdt

    for name, original in _EDGAR_FUNDAMENTALS_ORIGINALS.items():
        getattr(fdt, name).func = original
    _EDGAR_FUNDAMENTALS_ORIGINALS.clear()
    _EDGAR_FUNDAMENTALS_PATCHED = False


def _ensure_edgar_fundamentals(cfg: dict) -> None:
    """Swap the four fundamentals tools to EDGAR companyfacts when
    ``fundamentals_source: edgar`` (default yfinance = no-op).

    The renderers raise EdgarError on ingest failure; each wrapped func then
    falls back to the recorded yfinance original so fundamentals never go
    dark mid-batch.
    """
    global _EDGAR_FUNDAMENTALS_PATCHED
    if _EDGAR_FUNDAMENTALS_PATCHED or cfg.get("fundamentals_source") != "edgar":
        return
    import edgar
    import fundamentals_edgar
    import tradingagents.agents.utils.fundamental_data_tools as fdt

    _METHOD_TO_TOOL = {
        "get_fundamentals": ("payload_for", ("ticker", "curr_date")),
        "get_balance_sheet": ("statements_for", None),
        "get_cashflow": ("statements_for", None),
        "get_income_statement": ("statements_for", None),
    }
    for name in _METHOD_TO_TOOL:
        tool = getattr(fdt, name)
        original = tool.func
        _EDGAR_FUNDAMENTALS_ORIGINALS[name] = original

        def make_wrapped(tool_name, orig):
            def wrapped(*args, **kwargs):
                ticker = kwargs.get("ticker", args[0] if args else "")
                try:
                    if tool_name == "get_fundamentals":
                        curr_date = kwargs.get("curr_date",
                                               args[1] if len(args) > 1 else "")
                        return fundamentals_edgar.payload_for(ticker, curr_date)
                    freq = kwargs.get("freq", "quarterly")
                    curr_date = kwargs.get("curr_date")
                    return fundamentals_edgar.statements_for(
                        tool_name, ticker, freq, curr_date)
                except edgar.EdgarError as exc:
                    _report_edgar_fallback(tool_name, ticker, exc)
                    logger.warning("EDGAR fundamentals failed for %s (%s); "
                                   "falling back to yfinance", tool_name, ticker)
                    return orig(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    # The never-dark guarantee covers UNEXPECTED failures too
                    # (latent bugs, freak XBRL shapes raising KeyError deep in
                    # the render path) — an agent must never face a tool error
                    # with no fundamentals. Loud on purpose: the per-ticker
                    # fallback count in summary.json surfaces every case.
                    _report_edgar_fallback(tool_name, ticker, exc)
                    logger.error("EDGAR fundamentals UNEXPECTED failure for "
                                 "%s (%s) (%s: %s); falling back to yfinance",
                                 tool_name, ticker, type(exc).__name__, exc)
                    return orig(*args, **kwargs)
            wrapped._wrapped_original = orig
            return wrapped

        tool.func = make_wrapped(name, original)
    _EDGAR_FUNDAMENTALS_PATCHED = True


def _report_edgar_fallback(tool_name: str, ticker: str, exc: Exception) -> None:
    """Count/attribute an EDGAR→yfinance fallback to its ticker (best-effort).

    The structured logger is thread-local; bare runs (tests, smoke) have no
    active logger and the event is a no-op. Must never break the tool.
    """
    try:
        import structured_log
        structured_log.emit_edgar_fallback(
            tool=tool_name, ticker=str(ticker or ""),
            reason=f"{type(exc).__name__}: {exc}")
    except Exception:  # noqa: BLE001 — logging must never break the tool
        pass


# --- market tape + corporate events (context injection) ---------------------
#
# Wraps the SAME seam as the portfolio stance (resolve_instrument_context) so
# every agent sees one dated block: market tape (SPY/VIX/sector — the audit
# showed debates arguing regime-blind) and corporate events (Form 4s + 8-K —
# the framework's insider tool was never invoked and its vendor lags days).
# Chain AFTER _ensure_portfolio_context: each installer preserves the
# previously installed wrapper as its _wrapped_original.

_TAFE_PATCHED = False
_TAFE_ORIGINAL = None

_extras_memo: dict[str, str] = {}
_extras_lock = threading.RLock()


def _reset_extras_memo() -> None:
    with _extras_lock:
        _extras_memo.clear()


def _instrument_extras(ticker: str) -> str:
    """Composed, memoized context extras ("" on any failure)."""
    with _extras_lock:
        if ticker in _extras_memo:
            return _extras_memo[ticker]
    parts = []
    try:
        import corp_events
        import earnings_metrics
        import market_tape

        sector = None
        try:
            from tradingagents.agents.utils.agent_utils import resolve_instrument_identity
            sector = (resolve_instrument_identity(ticker) or {}).get("sector")
        except Exception:  # noqa: BLE001 - identity is optional context
            pass
        events = corp_events.events_block(ticker)
        tape = market_tape.tape_line(sector=sector)
        earnings = earnings_metrics.earnings_line(ticker)
        for block in (events, earnings, tape):
            if block:
                parts.append(block)
    except Exception:  # noqa: BLE001 - extras are decoration
        pass
    result = "\n\n".join(parts)
    with _extras_lock:
        _extras_memo[ticker] = result
    return result


def _reset_tape_and_events() -> None:
    """Restore the context seam (tests; safe anytime)."""
    global _TAFE_PATCHED, _TAFE_ORIGINAL
    if _TAFE_PATCHED and _TAFE_ORIGINAL is not None:
        import tradingagents.graph.trading_graph as tg_mod

        tg_mod.TradingAgentsGraph.resolve_instrument_context = _TAFE_ORIGINAL
    _TAFE_PATCHED = False
    _TAFE_ORIGINAL = None
    _reset_extras_memo()


def _ensure_tape_and_events() -> None:
    """Append market-tape and corporate-events blocks to the instrument
    context every agent receives (idempotent, revertible)."""
    global _TAFE_PATCHED, _TAFE_ORIGINAL
    if _TAFE_PATCHED:
        return
    import tradingagents.graph.trading_graph as tg_mod

    _TAFE_ORIGINAL = tg_mod.TradingAgentsGraph.resolve_instrument_context
    original = _TAFE_ORIGINAL

    def resolve_with_extras(self, ticker: str, asset_type: str = "stock") -> str:
        base = original(self, ticker, asset_type)
        if asset_type == "stock":
            extras = _instrument_extras(ticker)
            if extras:
                return f"{base}\n\n{extras}".strip()
        return base

    resolve_with_extras._wrapped_original = original



# --- Stop sweep (no-naked invariant) -----------------------------------------
#
# Any held ticker without a resting GTC stop gets one at last_close *
# (1 - stop_loss_pct/100). Covers fill-unknown races (cancel-vs-fill left
# a filled buy unstopped), manual trades, and any future hole. Must be
# failure-safe (broker down → skip with a warning, never block analyze)
# and idempotent (already-stopped tickers are left alone).

def _ensure_stop_sweep(cfg: dict) -> None:
    """Sweep holdings and attach missing GTC stops before the analyze batch.

    Failure-safe: broker errors skip the sweep with a warning; never block
    the analyze run. Idempotent: tickers with existing stops are left alone.
    """
    try:
        stop_loss_pct = float(cfg.get("stop_loss_pct", 8.0))
        broker = create_broker(cfg)
        try:
            broker.connect()
            holdings, _ = broker.get_positions_and_cash()
            if not holdings:
                return  # nothing to sweep

            # Query resting GTC stops
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                limit=100,  # should cover all open stops
            )
            open_orders = broker._client.get_orders(filter=req)
            stopped_tickers = {
                o.symbol for o in open_orders
                if o.type.value == "stop" and o.time_in_force.value == "gtc"
            }

            # For each held ticker without a stop, attach one
            for ticker, shares in holdings.items():
                if ticker in stopped_tickers:
                    continue
                # Get last close to calculate stop price
                last_close_px = _last_close(ticker)
                if last_close_px is None or last_close_px <= 0:
                    logger.warning(
                        "stop sweep: no last close for %s; skipping", ticker)
                    continue
                stop_px = round(last_close_px * (1 - stop_loss_pct / 100), 2)

                # Submit GTC stop
                from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
                from alpaca.trading.requests import StopOrderRequest
                stop_req = StopOrderRequest(
                    symbol=ticker,
                    qty=shares,
                    side=OrderSide.SELL,
                    type=OrderType.STOP,
                    stop_price=stop_px,
                    time_in_force=TimeInForce.GTC,
                    extended_hours=False,
                )
                broker._client.submit_order(stop_req)
                logger.info(
                    "stop sweep: attached GTC stop %.2f for %s (%d shares)",
                    stop_px, ticker, shares)
        finally:
            broker.disconnect()
    except Exception as exc:  # noqa: BLE001
        logger.warning("stop sweep failed: %s (continuing analyze)", exc)


def _ensure_tape_and_events() -> None:
    """Append market-tape and corporate-events blocks to the instrument
    context every agent receives (idempotent, revertible)."""
    global _TAFE_PATCHED, _TAFE_ORIGINAL
    if _TAFE_PATCHED:
        return
    import tradingagents.graph.trading_graph as tg_mod

    _TAFE_ORIGINAL = tg_mod.TradingAgentsGraph.resolve_instrument_context
    original = _TAFE_ORIGINAL

    def resolve_with_extras(self, ticker: str, asset_type: str = "stock") -> str:
        base = original(self, ticker, asset_type)
        if asset_type == "stock":
            extras = _instrument_extras(ticker)
            if extras:
                return f"{base}\n\n{extras}".strip()
        return base

    resolve_with_extras._wrapped_original = original
    tg_mod.TradingAgentsGraph.resolve_instrument_context = resolve_with_extras
    _TAFE_PATCHED = True


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


# --- reasoning capture (langchain drops OpenRouter message.reasoning) --------
#
# OpenRouter returns the model's thinking in ``message.reasoning``; the openai
# SDK (3.x) keeps it as an extra field (extra="allow", so it survives
# ``model_dump``). Two layers then drop it before the structured log:
#   1. langchain_openai 1.6 reaches the SDK via
#      ``chat.completions.with_raw_response.parse()`` — never
#      ``Completions.create`` (the original SDK-patch seam never fired;
#      verified live 2026-09-03: 0/300+ llm_end events had reasoning).
#   2. ``_convert_dict_to_message`` copies only known keys into the AIMessage.
# Fix: patch the converter so a ``reasoning``/``reasoning_content`` extra lands
# in ``additional_kwargs["reasoning_content"]`` — the channel ``_reasoning_of``
# already reads in structured_log's llm_end handler. No thread stash needed:
# the AIMessage carries the reasoning straight into the callback.

_REASONING_CAPTURE_PATCHED = False
_REASONING_CAPTURE_ORIGINAL = None


def _reset_reasoning_capture() -> None:
    """Restore the converter seam (tests; safe anytime)."""
    global _REASONING_CAPTURE_PATCHED, _REASONING_CAPTURE_ORIGINAL
    if _REASONING_CAPTURE_PATCHED and _REASONING_CAPTURE_ORIGINAL is not None:
        from langchain_openai.chat_models import base as base_mod

        base_mod._convert_dict_to_message = _REASONING_CAPTURE_ORIGINAL
    _REASONING_CAPTURE_PATCHED = False
    _REASONING_CAPTURE_ORIGINAL = None


def _ensure_reasoning_capture() -> None:
    """Keep OpenRouter reasoning on the AIMessage for the log handler."""
    global _REASONING_CAPTURE_PATCHED, _REASONING_CAPTURE_ORIGINAL
    if _REASONING_CAPTURE_PATCHED:
        return
    from langchain_openai.chat_models import base as base_mod

    original = base_mod._convert_dict_to_message
    _REASONING_CAPTURE_ORIGINAL = original

    def convert_with_reasoning(_dict):
        msg = original(_dict)
        if getattr(msg, "type", None) == "ai" and isinstance(_dict, dict):
            for key in ("reasoning", "reasoning_content", "thinking"):
                value = _dict.get(key)
                if value:
                    msg.additional_kwargs["reasoning_content"] = (
                        value if isinstance(value, str) else str(value))
                    break
        return msg

    convert_with_reasoning._wrapped_original = original
    base_mod._convert_dict_to_message = convert_with_reasoning
    _REASONING_CAPTURE_PATCHED = True


def _reset_analyst_report_recovery() -> None:
    """Restore the analyst factory seams (tests; safe anytime)."""
    global _ANALYST_REPORT_RECOVERY_PATCHED
    if _ANALYST_REPORT_RECOVERY_PATCHED:
        _reset_analyst_tool_budget()  # budget wraps recovery; reset it first
        import tradingagents.graph.setup as setup_mod

        for name, original in _ANALYST_REPORT_RECOVERY_ORIGINALS.items():
            setattr(setup_mod, name, original)
        _ANALYST_REPORT_RECOVERY_ORIGINALS.clear()
        _ANALYST_REPORT_RECOVERY_PATCHED = False


# --- analyst tool-round budget (runaway tool-loop guard) ---------------------
#
# deepseek-v4-flash can get stuck re-issuing the same tool calls (live
# 2026-09-10: CRL/IQV News Analysts burned 29 tool rounds / ~150 successful
# but redundant calls and 1.7-2.0M tokens; the analyze overran its gate
# window and the day silently ran legacy). The framework has no loop bound —
# ConditionalLogic routes back to the ToolNode until the model stops asking.
# Two runtime layers bound it:
#   1. node wrapper: at `max_analyst_tool_rounds` the analyst call gets a
#      final-answer notice appended, so the model reports with the evidence
#      it already has instead of another tool round;
#   2. conditional wrapper: one round past the cap, the phase is forced to
#      its clear node (report recovery rebuilds any empty report from
#      stranded AIMessage text).
# Worst case per analyst: cap + 1 LLM calls.

_ANALYST_TOOL_BUDGET_PATCHED = False
_ANALYST_TOOL_BUDGET_FACTORY_ORIGINALS: dict = {}
_ANALYST_TOOL_BUDGET_CONDITIONAL_ORIGINALS: dict = {}

_ANALYST_TOOL_FACTORIES = (
    "create_market_analyst", "create_sentiment_analyst",
    "create_news_analyst", "create_fundamentals_analyst",
)

_ANALYST_TOOL_CLEAR_LABELS = {
    "should_continue_market": "Msg Clear Market",
    "should_continue_social": "Msg Clear Sentiment",
    "should_continue_news": "Msg Clear News",
    "should_continue_fundamentals": "Msg Clear Fundamentals",
}

_TOOL_BUDGET_NOTICE = (
    "TOOL BUDGET EXHAUSTED for this analysis. Do NOT call any more tools. "
    "Write your final report now using the evidence already gathered; if a "
    "data source was unavailable, state that limitation in the report."
)


def _count_tool_rounds(messages) -> int:
    """AI messages carrying tool calls in this analyst phase.

    Clear nodes delete the previous phase's messages, so the state seen by an
    analyst node holds exactly its own placeholder + tool-loop messages.
    """
    return sum(1 for m in (messages or [])
               if getattr(m, "type", "") == "ai"
               and getattr(m, "tool_calls", None))


def _reset_analyst_tool_budget() -> None:
    """Restore the analyst factories + conditional seams (tests; safe)."""
    global _ANALYST_TOOL_BUDGET_PATCHED
    if not _ANALYST_TOOL_BUDGET_PATCHED:
        return
    import tradingagents.graph.conditional_logic as cl_mod
    import tradingagents.graph.setup as setup_mod

    for name, original in _ANALYST_TOOL_BUDGET_FACTORY_ORIGINALS.items():
        setattr(setup_mod, name, original)
    for name, original in _ANALYST_TOOL_BUDGET_CONDITIONAL_ORIGINALS.items():
        setattr(cl_mod.ConditionalLogic, name, original)
    _ANALYST_TOOL_BUDGET_FACTORY_ORIGINALS.clear()
    _ANALYST_TOOL_BUDGET_CONDITIONAL_ORIGINALS.clear()
    _ANALYST_TOOL_BUDGET_PATCHED = False


def _ensure_analyst_tool_budget(cfg: dict) -> None:
    """Cap tool-calling rounds per analyst (0 disables; idempotent)."""
    global _ANALYST_TOOL_BUDGET_PATCHED
    if _ANALYST_TOOL_BUDGET_PATCHED:
        return
    cap = int((cfg or {}).get("max_analyst_tool_rounds", 8))
    if cap <= 0:
        return
    from langchain_core.messages import HumanMessage

    import tradingagents.graph.conditional_logic as cl_mod
    import tradingagents.graph.setup as setup_mod

    for factory_name in _ANALYST_TOOL_FACTORIES:
        original_factory = getattr(setup_mod, factory_name)

        def wrapped_factory(llm, _orig=original_factory, _cap=cap):
            node = _orig(llm)

            def node_with_budget(state):
                if _count_tool_rounds(state.get("messages")) >= _cap:
                    messages = list(state.get("messages") or [])
                    messages.append(HumanMessage(content=_TOOL_BUDGET_NOTICE))
                    state = {**state, "messages": messages}
                return node(state)

            node_with_budget._wrapped_original = node
            return node_with_budget

        wrapped_factory._wrapped_original = original_factory
        setattr(setup_mod, factory_name, wrapped_factory)
        _ANALYST_TOOL_BUDGET_FACTORY_ORIGINALS[factory_name] = original_factory

    for method_name, clear_label in _ANALYST_TOOL_CLEAR_LABELS.items():
        original = getattr(cl_mod.ConditionalLogic, method_name)

        def capped(self, state, _orig=original, _label=clear_label, _cap=cap):
            out = _orig(self, state)
            if (isinstance(out, str) and out.startswith("tools_")
                    and _count_tool_rounds(state.get("messages")) > _cap):
                return _label
            return out

        capped._wrapped_original = original
        setattr(cl_mod.ConditionalLogic, method_name, capped)
        _ANALYST_TOOL_BUDGET_CONDITIONAL_ORIGINALS[method_name] = original

    _ANALYST_TOOL_BUDGET_PATCHED = True


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


# --- PM execution intent + dated decision cards (spec 2026-09-04) ---------
# The PM's structured PortfolioDecision gains an `execution` block (today's
# open-window orders; long-term intent rides the dated decision cards). The
# schema swap repoints the framework's PortfolioDecision global (schemas AND
# the portfolio_manager import binding) before any graph build so the bound
# structured-output schema carries `execution`. The capture wrapper stashes
# the parsed PM decision per worker thread; the analyze runner writes the
# per-ticker card + execution events from it. Injection appends dated cards
# to the PM-only past_context.

_PM_SCHEMA_PATCHED = False
_PM_CONTRACT_DISCLOSURE = ""
_PM_ORIGINAL_DECISION: type | None = None  # framework class captured pre-swap
_PM_CAPTURE = threading.local()
_PM_CARDS_ENABLED = False  # mirrors cfg execution_intent (decision cards + events)
_CARDS_MAX_AGE_DAYS = 21
_CARDS_FLIP_MAX = 3
_CARD_INJECTION_PATCHED = False
_CARD_INJECTION_ROOT: Path | None = None
_EXECUTION_MAP: dict = {}  # ticker -> ratings-v2 execution block (worker-filled)


def _import_module(name: str):
    import importlib
    return importlib.import_module(name)


def _stash_pm_capture(decision: dict | None) -> None:
    _PM_CAPTURE.decision = decision


def _pop_pm_capture() -> dict | None:
    decision = getattr(_PM_CAPTURE, "decision", None)
    _PM_CAPTURE.decision = None
    return decision


def _clear_pm_capture() -> None:
    _PM_CAPTURE.decision = None


def _ensure_pm_execution_schema(cfg: dict) -> None:
    """Repoint the framework's PortfolioDecision to our execution-bearing
    subclass (both import sites) and wrap the structured invocation so the
    parsed PM decision is captured. Also append the execution contract
    disclosure to the PM prompt template. Only when ``execution_intent``
    is on; off = today's behavior byte-identical. Idempotent; ``_reset_*``
    restores.
    """
    global _PM_SCHEMA_PATCHED, _PM_CARDS_ENABLED, _PM_ORIGINAL_DECISION
    _PM_CARDS_ENABLED = bool(cfg.get("execution_intent", False))
    _CARDS_MAX_AGE_DAYS = int(cfg.get("card_max_age_days", 21))
    _CARDS_FLIP_MAX = int(cfg.get("card_flip_inject_max", 3))
    if _PM_SCHEMA_PATCHED:
        return
    _PM_SCHEMA_PATCHED = True
    if not _PM_CARDS_ENABLED:
        return
    import tradingagents.agents.managers.portfolio_manager as pm_agents_mod
    import tradingagents.agents.schemas as schemas_mod
    import tradingagents.agents.utils.structured as structured_mod
    from pm_execution import ExecutionPortfolioDecision

    _PM_ORIGINAL_DECISION = schemas_mod.PortfolioDecision
    # Both import sites: the schema module's own global and the binding the
    # portfolio manager factory captured at module import time.
    schemas_mod.PortfolioDecision = ExecutionPortfolioDecision
    pm_agents_mod.PortfolioDecision = ExecutionPortfolioDecision

    # Execution contract disclosure: appended to the PM prompt by the
    # capture wrapper below (append-at-seam — the node itself is never
    # copied, so upstream prompt changes cannot drift out of sync).
    global _PM_CONTRACT_DISCLOSURE
    _PM_CONTRACT_DISCLOSURE = """

---

**Execution Contract** (your `execution` block binds when enabled):
- You hold positions per the "Portfolio context (ground truth)" block above.
- Every ticker you rate MUST carry an `execution` block with an `orders` list.
- `orders: []` on a ticker you HOLD is valid (a deliberate maintain decision).
- `orders: []` on a ticker you DON'T hold but rate Buy/Overweight is an ENGINE FAILURE: either size an entry (shares OR value_usd) or reconsider the rating. The gate marks such blocks legacy (that ticker will not bind today).
- Full exit: `shares` equal to the held quantity. Partial trim: fewer shares (or `fraction_held`); set `stop_px` for the remainder.
- Entries are whole-share only: `value_usd` below one share's price sizes to ZERO shares and the gate marks the ticker legacy. For a ticker whose price exceeds your intended dollar amount, size in `shares` (minimum 1); no fractionals.
- `limit_px` on a SELL is a floor (day-expiry if never reached); buys get a +2% protection ceiling automatically.
- Orders are day-expiry and fill at/after the 09:30 ET open; `stop_px` becomes a broker-side GTC stop protecting fills and remainders.
"""

    original = structured_mod.invoke_structured_or_freetext

    def with_capture(structured_llm, plain_llm, prompt, render, agent_name,
                     _orig=original):
        if agent_name != "Portfolio Manager" or structured_llm is None:
            return _orig(structured_llm, plain_llm, prompt, render, agent_name)
        # Execution contract disclosure: appended once per PM call, never
        # accumulated (the prompt arrives fresh from the framework node).
        prompt = prompt + _PM_CONTRACT_DISCLOSURE
        try:
            result = structured_llm.invoke(prompt)
        except Exception:  # noqa: BLE001 — original owns the free-text retry
            return _orig(structured_llm, plain_llm, prompt, render, agent_name)
        if result is None:
            return _orig(structured_llm, plain_llm, prompt, render, agent_name)
        try:
            _stash_pm_capture(result.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 — capture must never break the PM
            _stash_pm_capture(None)
        return render(result)

    with_capture._wrapped_original = original  # type: ignore[attr-defined]
    structured_mod.invoke_structured_or_freetext = with_capture
    # The framework agents import the function at module load, so each
    # consumer binds its own copy; repoint every binding or the wrapper
    # never fires (the E2E replay caught this: the schema carried
    # `execution` but no capture happened through the PM's own import).
    for mod in (pm_agents_mod,
                _import_module("tradingagents.agents.managers.research_manager"),
                _import_module("tradingagents.agents.trader.trader"),
                _import_module("tradingagents.agents.analysts.sentiment_analyst")):
        if getattr(mod, "invoke_structured_or_freetext", None) is original:
            mod.invoke_structured_or_freetext = with_capture


def _reset_pm_execution_schema() -> None:
    """Restore the original class + invocation for tests / later processes."""
    global _PM_SCHEMA_PATCHED, _PM_CARDS_ENABLED, _PM_ORIGINAL_DECISION
    import tradingagents.agents.managers.portfolio_manager as pm_agents_mod
    import tradingagents.agents.schemas as schemas_mod
    import tradingagents.agents.utils.structured as structured_mod

    current = getattr(structured_mod.invoke_structured_or_freetext,
                      "_wrapped_original", None)
    if current is not None:
        for mod in (structured_mod, pm_agents_mod,
                    _import_module("tradingagents.agents.managers.research_manager"),
                    _import_module("tradingagents.agents.trader.trader"),
                    _import_module("tradingagents.agents.analysts.sentiment_analyst")):
            mod.invoke_structured_or_freetext = current
    original = _PM_ORIGINAL_DECISION
    if original is not None:
        schemas_mod.PortfolioDecision = original
        pm_agents_mod.PortfolioDecision = original
    _PM_SCHEMA_PATCHED = False
    _PM_CARDS_ENABLED = False
    _clear_pm_capture()
    _EXECUTION_MAP.clear()


def _write_decision_card(ticker: str, today_str: str, cfg: dict,
                         rating: str, run_log=None) -> dict | None:
    """Persist the ticker's dated decision card + emit execution events.

    Reads the thread-local captured PM decision (pop). With no structured
    decision (free-text fallback) no card is written — the memory log keeps
    the prose — but an ``execution_intent: absent`` event still fires so the
    compliance stream is complete. Failure-safe: artifacts never break the
    analysis pass.

    ``run_log`` (the per-ticker StructuredRunLogger) is passed explicitly
    because this runs AFTER propagate cleared the thread-local logger —
    module-level emitters would silently no-op (E2E 09-05 finding).
    """
    if not cfg.get("execution_intent", False):
        return None
    import decision_cards
    import structured_log

    def emit(name: str, **kw) -> None:
        if run_log is not None:
            getattr(run_log, name)(**kw)
        else:
            getattr(structured_log, name)(**kw)

    decision = _pop_pm_capture()
    if decision is None:
        emit("emit_execution_intent", status="absent")
        emit("emit_decision_card", mode="absent")
        return None
    try:
        old = decision_cards.latest_card(cfg["results_dir"], ticker)
    except Exception:  # noqa: BLE001
        old = None
    if old and old.get("date") == today_str:
        # Already recorded today (analyze retry re-ran a successful pass):
        # never double-append, never re-emit flip events.
        return old
    status = "present_valid"
    execution = decision.get("execution")
    n_orders = 0
    if execution is not None:
        from pm_execution import EXECUTION_VALID, extract_execution
        status, intent, reason = extract_execution(decision)
        n_orders = len(intent.orders) if status == EXECUTION_VALID else 0
    emit("emit_execution_intent", status=status, n_orders=n_orders)
    if execution is not None:
        _EXECUTION_MAP[ticker] = {"status": status, "block": execution}
    try:
        old = decision_cards.latest_card(cfg["results_dir"], ticker)
        if old and old.get("rating") != rating:
            emit("emit_rating_flip", card_date=old.get("date", ""),
                 old_rating=old.get("rating", ""), new_rating=rating)
        card = {
            "date": today_str,
            "ticker": ticker,
            "rating": rating,
            "ref_close": _card_reference_close(ticker, cfg),
            "schema_version": decision_cards.CARD_SCHEMA_VERSION,
            "executive_summary": decision.get("executive_summary"),
            "investment_thesis": decision.get("investment_thesis"),
            "execution": execution,
        }
        decision_cards.append_card(cfg["results_dir"], card)
        emit("emit_decision_card", mode="injected")
        return card
    except Exception:  # noqa: BLE001 — a bad card must never fail analysis
        logger.warning("decision-card write failed for %s: %s", ticker,
                       "card failure", exc_info=True)
        return None


_CARD_CLOSE_CACHE: dict = {}


def _card_reference_close(ticker: str, cfg: dict) -> float | None:
    """Memoized prior-session close for the card (best-effort, never fatal)."""
    if ticker in _CARD_CLOSE_CACHE:
        return _CARD_CLOSE_CACHE[ticker]
    close = _last_close(ticker)
    _CARD_CLOSE_CACHE[ticker] = close
    return close


def _ensure_decision_card_injection(cfg: dict) -> None:
    """Append fresh dated cards to the PM-only past_context.

    ``get_past_context`` feeds only the Portfolio Manager prompt, so cards
    reach exactly the agent that must confirm-or-refute. Stable ratings ->
    latest card; a flip between the two latest fresh cards -> the last
    ``card_flip_inject_max`` cards. Historical runs (as_of set) anchor
    freshness at ``as_of`` so a backtest can't see future cards.
    """
    global _CARD_INJECTION_PATCHED, _CARD_INJECTION_ROOT
    if _CARD_INJECTION_PATCHED:
        return
    if not cfg.get("execution_intent", False):
        return  # never install while off
    _CARD_INJECTION_PATCHED = True
    _CARD_INJECTION_ROOT = Path(cfg["results_dir"])
    import tradingagents.agents.utils.memory as memory_mod

    original = memory_mod.TradingMemoryLog.get_past_context

    def with_cards(self, ticker, n_same=5, n_cross=3, as_of=None,
                   _orig=original):
        ctx = _orig(self, ticker, n_same=n_same, n_cross=n_cross, as_of=as_of)
        root = _CARD_INJECTION_ROOT
        if root is None:
            return ctx
        import decision_cards
        try:
            fresh = decision_cards.fresh_cards(
                root, ticker, _CARDS_MAX_AGE_DAYS, as_of=as_of)
            picked = decision_cards.select_cards_for_injection(
                fresh, _CARDS_FLIP_MAX)
            outcomes = decision_cards.load_outcomes(root, ticker)
            block = decision_cards.render_prior_decisions(
                ticker, picked, outcomes=outcomes)
        except Exception:  # noqa: BLE001 — cards must never break the prompt
            return ctx
        if not block:
            import structured_log
            structured_log.emit_decision_card(mode="absent")
            return ctx
        import structured_log
        structured_log.emit_decision_card(mode="injected", n_cards=len(picked))
        return f"{ctx}\n\n{block}" if ctx else block

    with_cards._wrapped_original = original  # type: ignore[attr-defined]
    memory_mod.TradingMemoryLog.get_past_context = with_cards


def _reset_decision_card_injection() -> None:
    """Restore the original get_past_context for tests."""
    global _CARD_INJECTION_PATCHED, _CARD_INJECTION_ROOT
    import tradingagents.agents.utils.memory as memory_mod

    current = getattr(memory_mod.TradingMemoryLog.get_past_context,
                      "_wrapped_original", None)
    if current is not None:
        memory_mod.TradingMemoryLog.get_past_context = current
    _CARD_INJECTION_PATCHED = False
    _CARD_INJECTION_ROOT = None


class _EmptyDecisionError(Exception):
    """PM decision text is empty (all providers errored) — retryable."""


def _analyze_one(ticker: str, today_str: str, cfg: dict):
    """Run the full framework pipeline for one ticker with one retry.

    Returns (ticker, rating, error). Runs inside a worker thread, so it must
    never raise: all failures are reported through the error slot.
    """
    import structured_log
    _clear_pm_capture()  # threads are pooled: never leak a prior ticker's PM
    run_log = structured_log.StructuredRunLogger(ticker=ticker, today=today_str)

    def attempt():
        _clear_pm_capture()
        rating = _propagate_with_structured_log(ticker, today_str, cfg, run_log)
        _write_decision_card(ticker, today_str, cfg, rating, run_log=run_log)
        run_log.finish(rating=rating)
        return ticker, rating, None

    try:
        return attempt()
    except _EmptyDecisionError as exc:
        # Provider bad window emptied the decision (REVIEW) — unlike other
        # failures it raised nothing before. Retry once; MSFT-class windows
        # clear within a minute. A second failure stays the visible REVIEW
        # no-op rather than a silent Hold or a fabricated rating.
        logger.warning("empty PM decision for %s (%s); retrying once",
                       ticker, exc)
        try:
            run_log.finish(rating=None)
            return attempt()
        except Exception as exc2:  # noqa: BLE001
            logger.error("retry also empty for %s: %s; leaving REVIEW (no-op)",
                         ticker, exc2)
            run_log.finish(rating="REVIEW")
            return ticker, "REVIEW", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("analysis failed for %s: %s", ticker, exc)
        try:
            run_log.finish(rating=None)
            return attempt()
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
        rating = extract_rating(signal)
        if rating == "REVIEW" and not (decision or "").strip():
            # All structured attempts and the free-text fallback came back
            # empty (provider bad window). Unlike other failures nothing
            # raised, so without this the day silently no-op'd (PSX 9/10).
            logger.warning("%s: empty PM decision; scheduling one retry",
                           ticker)
            structured_log.emit_structured_fallback(
                agent="Portfolio Manager",
                error="empty final decision; retry scheduled",
                mode="review_retry")
            raise _EmptyDecisionError(f"{ticker}: empty PM decision")
        return rating
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
    _ensure_memory_review_tag()  # REVIEW tags + retry-heal for empty decisions
    _ensure_openrouter_pins(cfg.get("openrouter_provider_pins"))
    if not _ensure_reddit_oauth():
        _ensure_reddit_pacing()
    _ensure_reddit_archive()
    _ensure_stocktwits_resilience()
    _ensure_graph_tool_callbacks()
    _ensure_news_dating()  # before news logging: logging must stay outermost
    _ensure_news_logging()
    _ensure_fred_aliases()
    _ensure_structured_fallback_logging()
    _ensure_analyst_report_recovery()
    _ensure_analyst_tool_budget(cfg)
    _ensure_reasoning_capture()
    _ensure_portfolio_context(cfg)
    _ensure_edgar_fundamentals(cfg)
    _ensure_tape_and_events()
    _ensure_stop_sweep(cfg)  # No-naked invariant: before analyze batch starts
    if cfg.get("execution_intent", False):
        _reconcile_broker_outcomes(cfg)  # broker-side fills -> yesterday's cards
    if cfg.get("execution_intent", False):
        _EXECUTION_MAP.clear()
        _ensure_pm_execution_schema(cfg)
        _ensure_decision_card_injection(cfg)
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
    if _EXECUTION_MAP:
        payload["execution"] = {t: v["block"] for t, v in
                                sorted(_EXECUTION_MAP.items())
                                if v.get("status") == "present_valid"
                                and v.get("block") is not None}
        payload["schema_version"] = 2
    path = _ratings_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("ratings written to %s", path)
    if cfg.get("pm_execution", False):
        _run_binding_gate(cfg, payload["date"])
    return payload


def _seconds_until_open(now: datetime | None = None) -> float:
    """Seconds until the 09:30 ET regular-session open (0 if already open)."""
    now = now or datetime.now(ET)
    open_today = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ET)
    if now < open_today:
        return (open_today - now).total_seconds()
    return 0.0


def _anchor_sell_remainders(orders: list, holdings: dict, cancelled: dict,
                            last_close: dict, stop_loss_pct: float) -> None:
    """Anchor every sell's remainder stop so a held position can never sit
    naked between runs.

    Any sell on a held ticker whose stop was disarmed pre-open must carry a
    re-anchor level: the broker attaches a GTC stop for the still-held
    shares when the sell partially fills (remainder shed) or dies unfilled
    on its final round. Legacy full exits (shares == holdings) need this
    exactly as much as PM partial sells — HPE 2026-09-08: 1/13 filled,
    remainder shed, no anchor -> 12 shares naked. Level: the ORIGINAL
    cancelled stop, else the standard -stop_loss_pct% stop. PM blocks with
    explicit stops pass through untouched; with neither level source the
    order passes through (logged loudly) rather than guessing.
    """
    from dataclasses import replace as replace_order
    for i, o in enumerate(orders):
        if (o.action != "SELL" or o.stop_price is not None
                or holdings.get(o.ticker, 0) <= 0):
            continue
        stops = cancelled.get(o.ticker) or []
        if stops and stops[0].get("stop_price"):
            logger.info("%s: sell remainder anchored at original stop %.2f",
                        o.ticker, stops[0]["stop_price"])
            orders[i] = replace_order(o, stop_price=stops[0]["stop_price"])
        elif last_close.get(o.ticker):
            default = round(last_close[o.ticker] * (1 - stop_loss_pct / 100), 2)
            logger.warning("%s: sell has no original stop on record; "
                           "anchoring remainder at the standard stop %.2f",
                           o.ticker, default)
            orders[i] = replace_order(o, stop_price=default)
        else:
            logger.error("%s: sell remainder has no anchor source (no "
                         "cancelled stop, no last close) — the position "
                         "will be unprotected if the sell does not fully "
                         "fill", o.ticker)


def _read_gate_artifact(cfg: dict) -> dict | None:
    """Today's binding-gate artifact (verdict/reasons), or None.

    Read for the execution-outcome record regardless of the ``pm_execution``
    switch, so the card history shows WHY binding was on/off each day.
    """
    from binding_gate import gate_path
    try:
        return json.loads(gate_path(cfg["results_dir"],
                                    _today_str()).read_text(
                                        encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/invalid gate is not fatal
        return None


def _run_binding_gate(cfg: dict, date_str: str | None = None) -> None:
    """Run the automated binding gate as analyze completes (chained).

    The gate must never race the analyze: on 2026-09-10 a 4h run overran the
    fixed 08:00 ET gate cron, the artifact stayed an empty FAIL ("no ratings
    file"), and execute silently ran the whole day legacy. Chaining removes
    the schedule dependency entirely. Failures here are never fatal —
    execute reads the missing artifact and falls back to legacy.
    """
    try:
        import binding_gate
        result = binding_gate.run(cfg, date_str)
        logger.info("binding gate: %s (%d block(s) valid)",
                    result.get("verdict"),
                    (result.get("counts") or {}).get("valid", 0))
    except Exception as exc:  # noqa: BLE001 — execute fails closed without it
        logger.warning("binding gate failed to run (%s); execute will fall "
                       "back to legacy", exc)


def _reconcile_broker_outcomes(cfg: dict, today: str | None = None) -> int:
    """Correct the latest executed day's outcome rows with broker truth.

    Broker-side GTC stop fills are not engine orders, so the execute pass
    cannot see them — and they can fire after the outcomes are written
    (ZBRA 2026-09-10: the card said "1 remain" while the stop had already
    sold the last share, feeding a stale share count into future PM
    prompts). Runs at analyze start, before any PM prompt is built, and
    appends a corrected outcome event (the renderer uses the latest event
    per date). Idempotent and fail-safe: broker trouble leaves the
    execute-time snapshot untouched.
    """
    from decision_cards import append_outcome, load_outcomes

    if _seconds_until_open() <= 0:
        logger.info("outcome reconcile skipped: regular session already open "
                    "(positions are only yesterday-close truth pre-open)")
        return 0
    today = today or _today_str()
    results = Path(cfg["results_dir"])
    dates = []
    for path in results.glob("executed_*.json"):
        date_part = path.stem[len("executed_"):]
        try:
            datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        if date_part < today:
            dates.append(date_part)
    if not dates:
        return 0
    date_str = max(dates)

    try:
        payload = json.loads(
            (results / f"ratings_{date_str}.json").read_text(encoding="utf-8"))
        tickers = sorted(payload.get("ratings", {}))
    except (OSError, ValueError):
        logger.warning("outcome reconcile: no ratings for %s; skipping",
                       date_str)
        return 0

    positions: dict = {}
    fills_by_symbol: dict[str, list[dict]] = {}
    broker = None
    try:
        broker = create_broker(cfg)
        broker.connect()
        positions, _cash = broker.get_positions_and_cash()
        getter = getattr(broker, "get_filled_stop_orders", None)
        if callable(getter):
            start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ET)
            for fill in getter(start, start + timedelta(days=1)):
                fills_by_symbol.setdefault(fill["symbol"], []).append(fill)
    except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
        logger.warning("outcome reconcile skipped (broker: %s); cards keep "
                       "the execute-time snapshot", exc)
        return 0
    finally:
        if broker is not None:
            with contextlib.suppress(Exception):  # disconnect noise is not fatal
                broker.disconnect()

    corrected = 0
    for ticker in tickers:
        events = [e for e in load_outcomes(results, ticker)
                  if e.get("date") == date_str]
        if not events:
            continue
        current = events[-1]
        if current.get("reconciled"):
            continue
        true_remaining = int(positions.get(ticker, 0) or 0)
        fills = fills_by_symbol.get(ticker, [])
        if true_remaining == current.get("remaining") and not fills:
            continue
        updated = dict(current)
        updated["remaining"] = true_remaining
        updated["reconciled"] = True
        updated["reconciled_on"] = today
        actual = list(current.get("actual") or [])
        for fill in fills:
            actual.append({"action": "SELL(STOP)", "shares": fill["qty"],
                           "filled": fill["qty"],
                           "avg_price": fill["avg_price"],
                           "source": "broker-stop"})
        updated["actual"] = actual
        notes = [str(current["note"])] if current.get("note") else []
        notes.extend(
            f"broker-side stop filled {f['qty']} {ticker} "
            f"@ ${f['avg_price']:.2f}" for f in fills)
        if true_remaining != current.get("remaining") and not fills:
            notes.append(
                f"book reconciled from broker position ({true_remaining} held)")
        if notes:
            updated["note"] = "; ".join(notes)
        append_outcome(results, updated)
        corrected += 1
    if corrected:
        logger.info("outcome reconcile: corrected %d ticker(s) for %s",
                    corrected, date_str)
    return corrected


def _execution_outcomes(payload: dict, orders: list, reports: list,
                        holdings: dict, gate: dict | None,
                        bound_tickers: set[str]) -> dict[str, dict]:
    """Reconcile intent vs reality, per analyzed ticker.

    For every ticker in the ratings payload (each has a decision card),
    record what the PM asked for (normalized execution block, or None when
    absent/invalid), what the engine actually ran (orders + fills), where
    the book stands afterwards (remaining shares), and the re-anchor level
    carried for protection. Written as ``execution_outcome`` events so the
    card history reads 'decided X -> executed Y -> book is Z'.
    """
    from pm_execution import EXECUTION_VALID, extract_execution

    by_ticker: dict[str, list] = {}
    for report in reports:
        by_ticker.setdefault(report.get("ticker"), []).append(report)
    sells_by_ticker: dict[str, list] = {}
    for o in orders:
        sells_by_ticker.setdefault(o.ticker, []).append(o)

    outcomes: dict[str, dict] = {}
    for ticker, rating in payload.get("ratings", {}).items():
        block = (payload.get("execution") or {}).get(ticker)
        if block is not None:
            status, intent, _reason = extract_execution(
                {"execution": block})
            pm_orders = ([[o.kind, o.shares] for o in intent.orders]
                         if status == EXECUTION_VALID and intent else None)
        else:
            pm_orders = None
        actual = [{"action": r.get("action"), "shares": r.get("shares"),
                   "filled": r.get("filled"), "avg_price": r.get("avg_price")}
                  for r in by_ticker.get(ticker, [])]
        delta = sum((a["filled"] or 0) * (1 if a["action"] == "BUY" else -1)
                    for a in actual)
        stops = [o.stop_price for o in sells_by_ticker.get(ticker, [])
                 if o.stop_price is not None]
        outcomes[ticker] = {
            "date": _today_str(),
            "ticker": ticker,
            "rating": rating,
            "binding_active": ticker in bound_tickers,
            "gate_verdict": (gate or {}).get("verdict"),
            "gate_reasons": (gate or {}).get("reasons") or [],
            "pm_orders": pm_orders,
            "actual": actual,
            "remaining": int(holdings.get(ticker, 0)) + delta,
            "stop_anchored": stops[0] if stops else None,
        }
    return outcomes


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
    # Dry-runs never wait (they're previews). The wait happens AFTER orders
    # are computed so exits can disarm their resting stops pre-open.

    payload = json.loads(ratings_path.read_text(encoding="utf-8"))

    gate_info = _read_gate_artifact(cfg)
    # PM execution binding is fail-closed PER TICKER: it requires the config
    # switch AND the automated morning gate artifact (binding_gate.py, runs
    # post-analyze). The artifact's per_ticker map decides which tickers
    # bind ("bind") and which fall back to the legacy tier path ("legacy" or
    # absent) — one bad block must not discard the day's good ones. No gate
    # artifact at all = nothing binds (no human reviews the morning batch,
    # so binding must never run ungated). The day-level verdict stays in the
    # artifact for observability only.
    binding_enabled = bool(cfg.get("pm_execution", False))
    if binding_enabled and gate_info is None:
        logger.warning("no binding gate artifact for today; PM execution "
                       "binding NOT active (legacy path)")
        binding_enabled = False
    elif binding_enabled:
        failures = (gate_info or {}).get("reasons") or []
        if failures:
            logger.warning("binding gate day verdict %s with %d ticker "
                           "reason(s); per-ticker statuses govern",
                           gate_info.get("verdict"), len(failures))
        else:
            logger.info("binding gate PASS — PM execution binding active today")

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

        # Risk-budget sizing (spec 2026-09-11): the base is real portfolio
        # equity (cash + holdings at the reference close) — config `capital`
        # is documentation only. Whole shares, min-1 at the whole-share
        # boundary when the 1-share weight fits the risk ceiling.
        orders = compute_orders(
            payload["ratings"], holdings, last_close,
            cash=cash,
            max_positions=int(cfg.get("max_positions", 10)),
            max_order_value_cap=cfg.get("max_order_value_cap"),
            entry_protection_pct=float(cfg.get("screener", {}).get(
                "entry_protection_pct", 2.0)),
            stop_loss_pct=float(cfg.get("stop_loss_pct", 8.0)),
            conviction_weights=cfg.get("conviction_weights"),
            risk_budget_pct=float(cfg.get("risk_budget_pct", 1.2)))

        # PM execution binding (phase 2): per-ticker execution blocks from
        # the ratings file (schema_version 2) replace the legacy tier orders
        # for their tickers. Per-ticker gate status determines which tickers
        # bind: gate says "bind" + valid block -> binding orders; gate says
        # "legacy" OR invalid/absent block -> legacy compute_orders path.
        per_ticker_gate = (gate_info or {}).get("per_ticker", {})
        bound_tickers: set[str] = set()
        if binding_enabled and isinstance(
                payload.get("execution"), dict):
            from decisions import orders_from_execution as orders_from_block
            from pm_execution import EXECUTION_VALID, extract_execution

            for ticker in sorted(payload["execution"]):
                # Per-ticker gate status governs (fail-closed on absence)
                ticker_gate = per_ticker_gate.get(ticker, {})
                if ticker_gate.get("status") != "bind":
                    gate_reason = ticker_gate.get("reason", "not in gate")
                    logger.info("%s: gate says legacy (%s)", ticker, gate_reason)
                    continue

                status, intent, reason = extract_execution(
                    {"execution": payload["execution"][ticker]})
                if status != EXECUTION_VALID:
                    logger.warning("%s: invalid execution block (%s); "
                                   "legacy path", ticker, reason)
                    continue
                block_orders, clamps = orders_from_block(
                    intent, ticker=ticker, holdings=holdings,
                    last_close=last_close,
                    entry_protection_pct=float(cfg.get("screener", {}).get(
                        "entry_protection_pct", 2.0)),
                    stop_loss_pct=float(cfg.get("stop_loss_pct", 8.0)),
                    stop_px_band_pct=tuple(cfg.get("stop_px_band_pct",
                                                   [3.0, 25.0])),
                    min_order_value_usd=float(
                        cfg.get("min_order_value_usd", 50.0)))
                for clamp in clamps:
                    logger.warning("PM execution clamp: %s", clamp)
                if block_orders is None:
                    logger.warning("%s: execution block not honorable; "
                                   "legacy path", ticker)
                    continue
                orders = [o for o in orders if o.ticker != ticker]
                orders.extend(block_orders)
                bound_tickers.add(ticker)

        # Cash pass (risk-budget sizing spec 2026-09-11): every buy — PM
        # explicit intents first, then legacy buys by conviction — must fit
        # the account's real cash. Deterministic all-or-nothing: an order is
        # skipped, never shaved. Replaces the old per-order cash clamp.
        orders, skipped_buys = apply_cash_budget(
            orders, cash, last_close, payload["ratings"])
        for o in skipped_buys:
            logger.warning("%s: %s buy (%d sh) skipped — does not fit the "
                           "available cash (%.2f)", o.ticker, o.reason,
                           o.shares, cash)

        # Overnight-move tripwire: an event between the analysis cutoff and
        # the open (CEO death, disaster, guidance cut) shows up in the
        # pre-market quote before it reaches any article feed we parse. A
        # buy into a material gap-down catches a falling knife the debate
        # never saw — pause that ticker's order and log it for review.
        # Rating exits are never paused (selling at a gap-down open IS the
        # intended exit). 0 disables.
        tripwire_pct = float(cfg.get("tripwire_gap_pct", 0.0))
        paused = []
        if tripwire_pct > 0 and hasattr(broker, "get_current_price"):
            kept = []
            for o in orders:
                if o.action != "BUY":
                    kept.append(o)
                    continue
                ref = last_close.get(o.ticker)
                live = broker.get_current_price(o.ticker)
                if not ref or live is None:
                    kept.append(o)  # no quote: existing caps still protect
                    continue
                move_pct = (live - ref) / ref * 100
                if move_pct <= -tripwire_pct:
                    logger.warning(
                        "TRIPWIRE: %s pre-market %.2f is %.1f%% below the "
                        "reference close %.2f — material overnight move; "
                        "pausing today's buy", o.ticker, live, move_pct, ref)
                    paused.append({"ticker": o.ticker, "reference": ref,
                                   "pre_market": live,
                                   "move_pct": round(move_pct, 2)})
                    continue
                kept.append(o)
            if paused:
                orders = kept

        # Regime gate (execute side): STRESS suppresses new BUY orders —
        # rating-based exits still execute. Mirrors the pool-side pause.
        if load_regime(cfg) == "STRESS":
            buys = [o for o in orders if o.action == "BUY"]
            if buys:
                logger.warning("regime STRESS: suppressing %d new buy order(s); "
                               "exit orders only", len(buys))
            orders = [o for o in orders if o.action == "SELL"]

        # Exit guard: disarm resting GTC stops on SELL-bound symbols BEFORE
        # the open. With both the stop and the market sell live at the 09:30
        # auction, a gap through the stop level could double-sell the
        # position into an unintended short (EL-class exit, 2026-09-04).
        sells = [o.ticker for o in orders if o.action == "SELL"]
        cancelled = {}
        if sells and hasattr(broker, "cancel_stops_for") and not dry_run:
            logger.info("cancelling stops before the open for exit(s): %s",
                        ", ".join(sells))
            try:
                cancelled = broker.cancel_stops_for(sells)
            except Exception as exc:  # noqa: BLE001
                logger.warning("stop disarm failed for %s: %s", sells, exc)
            if not isinstance(cancelled, dict):
                cancelled = {}
        # Sell remainder anchoring: EVERY sell on a held ticker (legacy full
        # exits and PM partials alike) carries a re-anchor level, so the
        # broker can protect still-held shares after a partial fill or a
        # final-round death — the stop was disarmed pre-open and must never
        # stay disarmed while shares remain.
        _anchor_sell_remainders(
            orders, holdings, cancelled, last_close,
            float(cfg.get("stop_loss_pct", 8.0)))

        wait = _seconds_until_open()
        if wait > 0 and not dry_run:
            logger.info("market opens in %.0fs; waiting before placing orders", wait)
            time.sleep(wait)

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
                "paused": paused,
            }, indent=2), encoding="utf-8")

        reports = broker.place_market_orders(orders, dry_run=dry_run)
        log = {"date": _today_str(), "dry_run": dry_run, "status": "completed",
               "orders": [o.__dict__ for o in orders], "reports": reports,
               "paused": paused}
        if not dry_run:
            log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
            # Decision-card outcome events: intent + truth in one history.
            # Never fatal — execution already happened.
            try:
                import decision_cards
                outcomes = _execution_outcomes(
                    payload, orders, reports, holdings, gate_info,
                    bound_tickers)
                for outcome in outcomes.values():
                    decision_cards.append_outcome(cfg["results_dir"], outcome)
            except Exception as exc:  # noqa: BLE001
                logger.warning("execution-outcome write failed: %s", exc)
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
