"""news_dating.py — dated news rendering + verified-snapshot anchor header.

2026-09-03 audit finding: the yfinance news feed extracts a per-article
``pub_date`` (``_extract_article_data``) but drops it from the rendered string,
so the News Analyst cannot tell an Aug-28 article from a current one — REGN's
news leg quoted a "-4.8% pullback / $794.19" article against a $852.03 verified
close and nobody could date it. This module renders the same feed with:

- every article's publication date inline (``(source: X, published YYYY-MM-DD)``)
- a data-anchor header (last verified close + its date, memoized per ticker)
  telling the model to treat conflicting price claims as stale

Runtime tool.func replacement (daily_run._ensure_news_dating) makes the News
Analyst ToolNode and the Sentiment Analyst's direct pre-fetch both see it.
Fetch logic mirrors tradingagents/dataflows/yfinance_news.py exactly (window
filter, dedupe, canonical symbols, edge strings) — read-only reuse of the
framework's helpers; nothing under ``tradingagents/`` is modified.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import in_window
from tradingagents.dataflows.stockstats_utils import yf_retry
from tradingagents.dataflows.symbol_utils import normalize_symbol
from tradingagents.dataflows.yfinance_news import _extract_article_data

_ANCHOR_TTL_S = 600

_anchor_lock = threading.Lock()
_anchor_cache: dict[str, tuple[float, tuple[str, float]]] = {}

ANCHOR_NOTE = (
    "Article price claims conflicting with the anchor are stale — attribute "
    "them to their publication date; never present them as current."
)


def reset_anchor_cache() -> None:
    """Drop the memoized anchors (tests; also refreshes stale anchors)."""
    with _anchor_lock:
        _anchor_cache.clear()


def _fetch_anchor_yf(ticker: str) -> tuple[str, float] | None:
    """Last verified close and its trading date for ``ticker`` (best effort)."""
    hist = yf_retry(lambda: yf.Ticker(ticker).history(period="7d", interval="1d"))
    if hist is None or hist.empty:
        return None
    last = hist.iloc[-1]
    return str(last.name.date()), float(last["Close"])


def fetch_anchor(ticker: str) -> tuple[str, float] | None:
    """Memoized (600s TTL) last verified close for ``ticker``.

    Failures return None and are NOT cached, so a transient network error
    does not pin the day's news to an absent anchor.
    """
    now = time.monotonic()
    with _anchor_lock:
        hit = _anchor_cache.get(ticker)
        if hit is not None and now - hit[0] < _ANCHOR_TTL_S:
            return hit[1]
    try:
        value = _fetch_anchor_yf(ticker)
    except Exception:  # noqa: BLE001 - anchor is best-effort decoration
        return None
    if value is not None:
        with _anchor_lock:
            _anchor_cache[ticker] = (now, value)
    return value


def anchor_header(ticker: str) -> str:
    """The staleness-anchor paragraph, or "" when the anchor is unavailable."""
    anchor = fetch_anchor(ticker)
    if anchor is None:
        return ""
    date, close = anchor
    return (f"## Data anchor: last verified {ticker} close ${close:,.2f} "
            f"on {date}.\n{ANCHOR_NOTE}\n")


def fetch_articles(ticker: str, limit: int) -> list[dict]:
    """Raw article list for a canonical ticker (yfinance get_news)."""
    stock = yf.Ticker(ticker)
    return yf_retry(lambda: stock.get_news(count=limit))


def fetch_global_articles(queries: list[str], limit: int) -> list[dict]:
    """Search-based global news, deduped by title and capped at ``limit``."""
    all_news: list[dict] = []
    seen_titles: set[str] = set()
    for query in queries:
        search = yf_retry(lambda q=query: yf.Search(
            query=q, news_count=limit, enable_fuzzy_query=True))
        if not search.news:
            continue
        for article in search.news:
            if "content" in article:
                title = _extract_article_data(article)["title"]
            else:
                title = article.get("title", "")
            if title and title not in seen_titles:
                seen_titles.add(title)
                all_news.append(article)
        if len(all_news) >= limit:
            break
    return all_news


def _render_article(data: dict) -> str:
    when = ""
    if data.get("pub_date") is not None:
        when = f", published {data['pub_date'].strftime('%Y-%m-%d')}"
    block = f"### {data['title']} (source: {data['publisher']}{when})\n"
    if data.get("summary"):
        block += f"{data['summary']}\n"
    if data.get("link"):
        block += f"Link: {data['link']}\n"
    return block + "\n"


def render_ticker_news(ticker: str, start_date: str, end_date: str) -> str:
    """Dated ticker news mirroring get_news_yfinance's fetch, window, and edge
    strings — plus the publication dates and the anchor header."""
    try:
        canonical = normalize_symbol(ticker)
        resolved = "" if canonical == ticker else f" (resolved to {canonical})"
        news = fetch_articles(canonical, get_config()["news_article_limit"])
        if not news:
            return f"No news found for {ticker}{resolved}"
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        body = "".join(
            _render_article(_extract_article_data(a))
            for a in news
            if in_window(_extract_article_data(a)["pub_date"], start_dt, end_dt)
        )
        if not body:
            return (f"No news found for {ticker}{resolved} between "
                    f"{start_date} and {end_date}")
        try:
            anchor = anchor_header(ticker)
        except Exception:  # noqa: BLE001 - news must never die on anchor loss
            anchor = ""
        return (f"{anchor}## {ticker}{resolved} News, from "
                f"{start_date} to {end_date}:\n\n{body}")
    except Exception as exc:  # noqa: BLE001 - mirror upstream error strings
        return f"Error fetching news for {ticker}: {exc}"


def render_global_news(curr_date: str, look_back_days: int | None = None,
                       limit: int | None = None) -> str:
    """Dated global news mirroring get_global_news_yfinance (no ticker anchor:
    the feed is market-wide, not instrument-scoped)."""
    try:
        config = get_config()
        if look_back_days is None:
            look_back_days = config["global_news_lookback_days"]
        if limit is None:
            limit = config["global_news_article_limit"]
        all_news = fetch_global_articles(config["global_news_queries"], limit)
        if not all_news:
            return f"No global news found for {curr_date}"
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - timedelta(days=look_back_days)
        start_date = start_dt.strftime("%Y-%m-%d")
        body = "".join(
            _render_article(_extract_article_data(a))
            for a in all_news[:limit]
            if in_window(_extract_article_data(a)["pub_date"], start_dt, curr_dt)
        )
        if not body:
            return f"No global news found between {start_date} and {curr_date}"
        return (f"## Global Market News, from {start_date} to "
                f"{curr_date}:\n\n{body}")
    except Exception as exc:  # noqa: BLE001 - mirror upstream error strings
        return f"Error fetching global news: {exc}"
