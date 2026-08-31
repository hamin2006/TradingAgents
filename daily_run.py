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
from decisions import compute_orders
from screener import load_pool
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.dataflows.config import set_config
from tradingagents.graph.trading_graph import TradingAgentsGraph

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def TODAY_ET() -> date:
    return datetime.now(ET).date()


def extract_rating(signal_text: str) -> str:
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
    try:
        _, signal = TradingAgentsGraph(config=cfg).propagate(ticker, today_str)
        return ticker, extract_rating(signal), None
    except Exception as exc:  # noqa: BLE001
        logger.warning("analysis failed for %s: %s", ticker, exc)
        try:
            _, signal = TradingAgentsGraph(config=cfg).propagate(ticker, today_str)
            return ticker, extract_rating(signal), None
        except Exception as exc2:  # noqa: BLE001
            logger.error("retry also failed for %s: %s", ticker, exc2)
            return ticker, None, exc2


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
    if not _ensure_reddit_oauth():
        _ensure_reddit_pacing()
    max_workers = max(1, int(cfg.get("analyze_max_workers", 4)))

    def record(result):
        ticker, rating, error = result
        if rating is not None:
            ratings[ticker] = rating
            logger.info("%s -> %s", ticker, rating)
        else:
            failures.append(ticker)

    if max_workers <= 1 or len(watchlist) <= 1:
        for ticker in watchlist:
            record(_analyze_one(ticker, _today_str(), cfg))
    else:
        logger.info("analyzing %d tickers with %d workers", len(watchlist), max_workers)
        with ThreadPoolExecutor(max_workers=max_workers,
                                thread_name_prefix="analyze") as pool:
            futures = [pool.submit(_analyze_one, t, _today_str(), cfg)
                       for t in watchlist]
            for future in as_completed(futures):
                record(future.result())

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
            stop_loss_pct=float(cfg.get("stop_loss_pct", 8.0)))

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
