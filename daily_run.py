"""daily_run.py — daily pipeline orchestrator (watchlist assembly first)."""

import argparse
import json
import logging
import sys
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

    for ticker in watchlist:
        try:
            _, signal = TradingAgentsGraph(config=cfg).propagate(ticker, _today_str())
            ratings[ticker] = extract_rating(signal)
            logger.info("%s -> %s", ticker, ratings[ticker])
        except Exception as exc:  # noqa: BLE001
            logger.warning("analysis failed for %s: %s", ticker, exc)
            try:
                _, signal = TradingAgentsGraph(config=cfg).propagate(ticker, _today_str())
                ratings[ticker] = extract_rating(signal)
            except Exception as exc2:  # noqa: BLE001
                logger.error("retry also failed for %s: %s", ticker, exc2)
                failures.append(ticker)

    payload = {"date": _today_str(), "ratings": ratings, "failures": failures}
    path = _ratings_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("ratings written to %s", path)
    return payload


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

    payload = json.loads(ratings_path.read_text(encoding="utf-8"))
    broker = create_broker(cfg)
    try:
        broker.connect()
        holdings, cash = broker.get_positions_and_cash()
        last_close = {}
        for ticker in set(holdings) | set(payload["ratings"]):
            last_close[ticker] = _last_close(ticker) or 0.0
        orders = compute_orders(
            payload["ratings"], holdings, last_close,
            capital=float(cfg.get("capital", 100_000)),
            max_positions=int(cfg.get("max_positions", 10)),
            max_order_value_cap=cfg.get("max_order_value_cap"),
            entry_protection_pct=float(cfg.get("screener", {}).get(
                "entry_protection_pct", 2.0)))
        reports = broker.place_market_orders(orders, dry_run=dry_run)
        log = {"date": _today_str(), "dry_run": dry_run,
               "orders": [o.__dict__ for o in orders], "reports": reports}
        _executed_path(cfg).parent.mkdir(parents=True, exist_ok=True)
        _executed_path(cfg).write_text(json.dumps(log, indent=2), encoding="utf-8")
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
        tickers = args.tickers.split(",") if args.tickers else None
        run_analyze(cfg, tickers)
        return 0
    if args.execute:
        return run_execute(cfg, dry_run=args.dry_run)
    parser.error("pass --analyze, --execute, or --healthcheck")
    return 2


if __name__ == "__main__":
    sys.exit(main())
