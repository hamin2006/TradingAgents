# Daily Paper-Trading Signals System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an autonomous daily system that screens the S&P 500 weekly, analyzes a watchlist of IBKR holdings + top momentum candidates with the TradingAgents framework, and executes the resulting Buy/Sell ratings on an IBKR paper account at the 09:30 ET open.

**Architecture:** Five new repo-root modules (`config.py`, `decisions.py`, `screener.py`, `ibkr.py`, `daily_run.py`) plus `watchlist.yaml`; the `tradingagents/` framework package is used unmodified as a library. The pipeline is deterministic Python driven by three cron jobs (Sunday screen, weekday analyze, weekday execute); a Kiro Crew ops skill is a later read-only convenience layer, out of scope here.

**Tech Stack:** Python 3.12, TradingAgents v0.3.1 (LangGraph framework), yfinance, pandas, ib_async (IBKR), PyYAML, pytest. Host: Ubuntu 24.04.

## Global Constraints

- Do NOT modify anything under `tradingagents/` — the framework is consumed as a library.
- Python >= 3.10; repo runs `ruff` (line-length 100, rules E/W/F/I/B/UP/C4/SIM) and `pytest` (`testpaths=["tests"]`, markers `unit`/`integration`/`smoke`).
- All new tests must be hermetic: no network, no real LLM calls, no real broker. The repo's `tests/conftest.py` already installs placeholder API keys; mock `yfinance`, `requests`, `ib_async`, and `TradingAgentsGraph` where needed.
- Every module in `daily_run.py` must run on a host whose timezone is NOT America/New_York — all date logic uses `zoneinfo.ZoneInfo("America/New_York")`, never server-local or UTC time.
- Ratings vocabulary is the framework's 5-tier scale: `Buy, Overweight, Hold, Underweight, Sell` (see `tradingagents/agents/utils/rating.py`).
- Order safety is non-negotiable: kill switch (`DISABLE_TRADING` file), executed-log idempotency, max order value cap, entry protection cap.
- Default models: `llm_provider: openrouter`, `quick_think_llm: deepseek/deepseek-v4-flash`, `deep_think_llm: deepseek/deepseek-v4-pro`.

---

### Task 1: Config loader (`config.py`)

**Files:**
- Create: `config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `tradingagents.default_config.DEFAULT_CONFIG` (dict of framework defaults, incl. `results_dir`, `data_cache_dir`, `memory_log_path`, `llm_provider`, `deep_think_llm`, `quick_think_llm`).
- Produces:
  - `DEFAULT_WATCHLIST_PATH: Path` — `Path("watchlist.yaml")`
  - `load_watchlist_config(path: str | Path | None = None) -> dict` — returns the merged config dict (watchlist.yaml over DEFAULT_CONFIG); missing file returns a copy of DEFAULT_CONFIG.
  - `merge_over_default(base: dict, overrides: dict) -> dict` — one-level-deep merge: dict-valued keys in `overrides` are `.update()`-merged into the base value; scalar keys replace.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_config.py"""
import pytest
from config import load_watchlist_config, merge_over_default
from tradingagents.default_config import DEFAULT_CONFIG


def test_merge_scalar_replaces():
    merged = merge_over_default({"a": 1, "b": 2}, {"a": 99})
    assert merged["a"] == 99
    assert merged["b"] == 2


def test_merge_dict_is_one_level_deep():
    base = {"nested": {"x": 1, "y": 2}, "keep": True}
    merged = merge_over_default(base, {"nested": {"y": 20}})
    assert merged["nested"] == {"x": 1, "y": 20}
    assert merged["keep"] is True


def test_merge_does_not_mutate_base():
    base = {"nested": {"x": 1}}
    merged = merge_over_default(base, {"nested": {"y": 2}})
    assert base["nested"] == {"x": 1}
    assert "y" not in base["nested"]


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = load_watchlist_config(tmp_path / "nope.yaml")
    assert cfg["llm_provider"] == DEFAULT_CONFIG["llm_provider"]


def test_load_yaml_overrides_and_merges(tmp_path):
    yaml_path = tmp_path / "watchlist.yaml"
    yaml_path.write_text(
        "llm_provider: openrouter\n"
        "screener:\n"
        "  candidate_slots: 3\n"
        "  min_watchlist_size: 5\n"
    )
    cfg = load_watchlist_config(yaml_path)
    assert cfg["llm_provider"] == "openrouter"
    assert cfg["screener"]["candidate_slots"] == 3
    assert cfg["screener"]["pool_size"] == 50  # default kept from base
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 3: Write minimal implementation**

```python
"""config.py — load watchlist.yaml and merge it over the framework defaults."""

import copy
from pathlib import Path

import yaml

from tradingagents.default_config import DEFAULT_CONFIG

DEFAULT_WATCHLIST_PATH = Path("watchlist.yaml")

_KNOWN_KEYS = frozenset(DEFAULT_CONFIG) | frozenset(
    ["seed_watchlist", "capital", "max_positions", "max_order_value_cap",
     "screener", "ibkr", "trading_enabled"]
)


def merge_over_default(base: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(copy.deepcopy(value))
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_watchlist_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_WATCHLIST_PATH
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        overrides = yaml.safe_load(f) or {}
    unknown = set(overrides) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"Unknown watchlist.yaml keys: {sorted(unknown)}")
    return merge_over_default(DEFAULT_CONFIG, overrides)
```

(Add `PyYAML` is already a transitive dependency of the framework; no pyproject change needed.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: add watchlist.yaml config loader"
```

---

### Task 2: Decision engine (`decisions.py`)

**Files:**
- Create: `decisions.py`
- Test: `tests/test_decisions.py`

**Interfaces:**
- Consumes: nothing from this repo (framework rating strings only).
- Produces:
  - `@dataclass(frozen=True) class Order:` — fields `ticker: str`, `action: str` (`"BUY" | "SELL"`), `shares: int`, `reason: str`, `protection_price: float | None = None` (set on BUY orders).
  - `compute_orders(ratings: dict[str, str], holdings: dict[str, int], last_close: dict[str, float], capital: float, max_positions: int, max_order_value_cap: float | None = None, entry_protection_pct: float = 2.0) -> list[Order]`

Rules (from spec §5):
1. HOLDING + rating Sell/Underweight → SELL whole position (plain MKT, no protection price).
2. NOT holding + rating Buy/Overweight → BUY equal-weight slice as MKT-with-protection; `protection_price = round(last_close * (1 + entry_protection_pct / 100), 2)`. `slice = capital / max_positions`, `shares = int(slice / last_close)`; skip if `shares < 1`.
3. Everything else → no order.
Cap: total BUY value must not exceed `max_order_value_cap`; if it would, drop the largest-ticket buy and retry (repeat until under cap or no buys left). Skip tickers missing from `ratings` or `last_close`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_decisions.py"""
import pytest
from decisions import Order, compute_orders

RATINGS = {"AAPL": "Buy", "MSFT": "Hold", "NVDA": "Overweight", "TSLA": "Sell"}
HOLDINGS = {"TSLA": 40}
CLOSE = {"AAPL": 100.0, "MSFT": 200.0, "NVDA": 150.0, "TSLA": 250.0}


def test_sell_held_on_sell_rating():
    orders = compute_orders(RATINGS, HOLDINGS, CLOSE, capital=100_000, max_positions=10)
    sell = [o for o in orders if o.action == "SELL"]
    assert len(sell) == 1
    assert sell[0].ticker == "TSLA" and sell[0].shares == 40
    assert sell[0].protection_price is None


def test_buy_not_held_on_buy_rating_with_protection():
    orders = compute_orders(RATINGS, HOLDINGS, CLOSE, capital=100_000, max_positions=10)
    buys = {o.ticker: o for o in orders if o.action == "BUY"}
    assert set(buys) == {"AAPL", "NVDA"}
    assert buys["AAPL"].shares == 100  # 100_000 / 10 / 100.0
    assert buys["AAPL"].protection_price == 102.0  # +2%
    assert buys["AAPL"].reason == "entry"


def test_hold_and_held_buy_produce_no_orders():
    ratings = {"MSFT": "Hold", "AAPL": "Buy"}
    holdings = {"AAPL": 50}
    orders = compute_orders(ratings, holdings, CLOSE, capital=100_000, max_positions=10)
    assert orders == []


def test_underweight_held_is_sell():
    ratings = {"NVDA": "Underweight"}
    holdings = {"NVDA": 10}
    orders = compute_orders(ratings, holdings, CLOSE, capital=100_000, max_positions=10)
    assert orders[0].action == "SELL"


def test_shares_lt_1_skips_buy():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 300_000.0},
                            capital=100_000, max_positions=10)
    assert orders == []  # slice = 10_000 -> 0 shares


def test_max_order_value_cap_drops_largest_buy():
    ratings = {"AAPL": "Buy", "NVDA": "Buy"}
    orders = compute_orders(ratings, {}, CLOSE, capital=100_000, max_positions=10,
                            max_order_value_cap=12_000)
    # slice = 10_000 each; both fit under 12_000
    assert len([o for o in orders if o.action == "BUY"]) == 2
    orders = compute_orders(ratings, {}, CLOSE, capital=100_000, max_positions=10,
                            max_order_value_cap=10_500)
    # one of them must be dropped (slice value 10_000 each, but cap applies to total)
    assert len([o for o in orders if o.action == "BUY"]) == 1


def test_missing_rating_or_price_skipped():
    orders = compute_orders({"AAPL": "Buy"}, {}, {"AAPL": 100.0, "MSFT": 200.0},
                            capital=100_000, max_positions=10)
    assert all(o.ticker == "AAPL" for o in orders)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_decisions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'decisions'`

- [ ] **Step 3: Write minimal implementation**

```python
"""decisions.py — pure decision engine: ratings + holdings -> order list."""

from dataclasses import dataclass

SELL_RATINGS = {"Sell", "Underweight"}
BUY_RATINGS = {"Buy", "Overweight"}


@dataclass(frozen=True)
class Order:
    ticker: str
    action: str  # "BUY" | "SELL"
    shares: int
    reason: str
    protection_price: float | None = None


def compute_orders(ratings, holdings, last_close, capital, max_positions,
                   max_order_value_cap=None, entry_protection_pct=2.0):
    orders = []
    slice_value = capital / max_positions

    for ticker, shares in holdings.items():
        if ticker in ratings and ratings[ticker] in SELL_RATINGS:
            orders.append(Order(ticker=ticker, action="SELL", shares=int(shares),
                                reason="rating exit"))

    buys = []
    for ticker, rating in ratings.items():
        if ticker in holdings or rating not in BUY_RATINGS:
            continue
        price = last_close.get(ticker)
        if not price:
            continue
        shares = int(slice_value / price)
        if shares < 1:
            continue
        protection = round(price * (1 + entry_protection_pct / 100), 2)
        buys.append(Order(ticker=ticker, action="BUY", shares=shares,
                          reason="entry", protection_price=protection))

    if max_order_value_cap is not None:
        while True:
            total = sum(o.shares * last_close[o.ticker] for o in buys)
            if total <= max_order_value_cap or not buys:
                break
            buys.remove(max(buys, key=lambda o: o.shares * last_close[o.ticker]))

    return orders + buys
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_decisions.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add decisions.py tests/test_decisions.py
git commit -m "feat: add pure decision engine with entry protection cap"
```

---

### Task 3: Screener (`screener.py`)

**Files:**
- Create: `screener.py`
- Test: `tests/test_screener.py`

**Interfaces:**
- Consumes: `config.load_watchlist_config` (Task 1); `tradingagents.dataflows.config.set_config` (framework global config, for results dir).
- Produces:
  - `week_key(d: date) -> str` — ISO year-week, e.g. `"2026-35"`.
  - `fetch_universe(cfg: dict) -> list[str]` — S&P 500 symbols from Wikipedia table (cached in `results_dir/universe_sp500.json`, refreshed if older than 7 days; on fetch failure falls back to cache, then to `[]`).
  - `fetch_prices(universe: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]` — one batched `yf.download`, keyed by ticker.
  - `compute_raw_metrics(hist: pd.DataFrame) -> dict | None` — `{"ret_1m", "ret_3m", "ret_6m", "sma50_spread", "high_proximity", "avg_dollar_vol"}` or `None` if < 60 rows.
  - `score_universe(prices: dict[str, pd.DataFrame], min_dollar_vol: float = 10_000_000) -> list[dict]` — liquidity-filtered, z-score momentum composite, sorted desc: `[{"ticker": ..., "score": ...}, ...]`.
  - `build_pool(cfg: dict, limit: int | None = None) -> Path` — runs screen, persists ranked queue to `results_dir/pool_YYYY-WW.json`, returns path. `limit` truncates the universe for smoke tests.
  - `load_pool(cfg: dict) -> list[dict]` — most recent cached pool (any week), `[]` if none.
  - `main(argv: list[str] | None = None) -> int` — argparse `--screen`, `--universe-size N`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_screener.py"""
import json
from datetime import date

import pandas as pd
import pytest

from screener import (build_pool, compute_raw_metrics, fetch_prices,
                      fetch_universe, load_pool, score_universe, week_key)


def _hist(n=130, start_price=100.0, drift=0.0):
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=n, freq="B")
    vals = [start_price * (1 + drift) ** i for i in range(n)]
    df = pd.DataFrame({"Open": vals, "High": [v * 1.01 for v in vals],
                       "Low": [v * 0.99 for v in vals],
                       "Close": vals, "Volume": [2_000_000] * n},
                      index=idx)
    return df


def test_week_key():
    assert week_key(date(2026, 8, 30)) == "2026-35"


def test_compute_raw_metrics_uptrend():
    m = compute_raw_metrics(_hist(drift=0.001))
    assert m is not None
    assert m["ret_1m"] > 0 and m["sma50_spread"] > 0
    assert m["avg_dollar_vol"] == pytest.approx(200 * 2_000_000, rel=0.5)


def test_compute_raw_metrics_too_short():
    assert compute_raw_metrics(_hist(n=10)) is None


def test_score_universe_ranks_momentum_first():
    prices = {
        "WINNER": _hist(drift=0.003),
        "LOSER": _hist(drift=-0.003),
    }
    ranked = score_universe(prices)
    assert ranked[0]["ticker"] == "WINNER"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_score_universe_liquidity_filter():
    prices = {"LIQUID": _hist(), "THIN": _hist(n=130)}
    prices["THIN"]["Volume"] = [1_000] * 130  # ~$100k/day
    ranked = score_universe(prices, min_dollar_vol=10_000_000)
    assert all(r["ticker"] != "THIN" for r in ranked)


def test_build_and_load_pool_roundtrip(tmp_path, monkeypatch):
    import config as config_mod
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    monkeypatch.setattr(config_mod, "load_watchlist_config", lambda *a, **k: cfg)
    monkeypatch.setattr("screener.fetch_universe", lambda cfg: ["AAA", "BBB"])
    monkeypatch.setattr("screener.fetch_prices", lambda u, period="6mo": {
        "AAA": _hist(drift=0.002), "BBB": _hist(drift=-0.002)})

    path = build_pool(cfg)
    assert path.exists()
    pool = load_pool(cfg)
    assert pool[0]["ticker"] == "AAA"
    assert len(pool) == 2
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    assert "year_week" in payload and "pool" in payload


def test_load_pool_missing_returns_empty(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["results_dir"] = str(tmp_path)
    assert load_pool(cfg) == []


def test_fetch_prices_batched(monkeypatch):
    import screener
    captured = {}

    class FakeFrame(pd.DataFrame):
        pass

    def fake_download(tickers, **kwargs):
        captured["tickers"] = tickers
        return FakeFrame()
    monkeypatch.setattr(screener.yf, "download", fake_download)
    prices = fetch_prices(["AAA", "BBB"])
    assert captured["tickers"] == "AAA BBB"
    assert isinstance(prices, dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_screener.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'screener'`

- [ ] **Step 3: Write minimal implementation**

```python
"""screener.py — weekly S&P 500 momentum screen producing the candidate pool."""

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from config import load_watchlist_config
from tradingagents.dataflows.config import set_config

logger = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
UNIVERSE_CACHE_TTL_DAYS = 7
MIN_ROWS = 60


def week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-{iso[1]:02d}"


def _results_dir(cfg: dict) -> Path:
    return Path(cfg["results_dir"])


def _universe_cache_path(cfg: dict) -> Path:
    return _results_dir(cfg) / "universe_sp500.json"


def fetch_universe(cfg: dict) -> list[str]:
    path = _universe_cache_path(cfg)
    if path.exists():
        age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
        if age < timedelta(days=UNIVERSE_CACHE_TTL_DAYS):
            return json.loads(path.read_text(encoding="utf-8"))
    try:
        tables = pd.read_html(WIKI_URL)
        symbols = sorted(tables[0]["Symbol"].astype(str).str.strip().tolist())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(symbols), encoding="utf-8")
        return symbols
    except Exception as exc:  # noqa: BLE001 - fail open to cache
        logger.warning("universe fetch failed (%s); using cached list", exc)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return []


def fetch_prices(universe: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    if not universe:
        return {}
    frame = yf.download(" ".join(universe), period=period, group_by="ticker",
                        auto_adjust=True, threads=False, progress=False)
    prices: dict[str, pd.DataFrame] = {}
    for ticker in universe:
        try:
            col = frame[ticker]
            if isinstance(col.columns, pd.MultiIndex):
                col.columns = col.columns.get_level_values(0)
            if len(col.dropna(how="all")) >= MIN_ROWS:
                prices[ticker] = col.dropna(how="all")
        except KeyError:
            continue
    return prices


def compute_raw_metrics(hist: pd.DataFrame) -> dict | None:
    close = hist["Close"].dropna()
    if len(close) < MIN_ROWS:
        return None
    last = close.iloc[-1]
    n = len(close)
    ret = lambda days: last / close.iloc[-1 - days] - 1 if n > days else None  # noqa: E731
    sma50 = close.rolling(50).mean().iloc[-1]
    high = close.max()
    avg_dollar_vol = float((hist["Close"] * hist["Volume"]).tail(20).mean())
    return {
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
        "sma50_spread": last / sma50 - 1,
        "high_proximity": float(last / high),
        "avg_dollar_vol": avg_dollar_vol,
    }


def _zscore(values: pd.Series) -> pd.Series:
    s = pd.Series(values)
    return (s - s.mean()) / s.std()


def score_universe(prices: dict[str, pd.DataFrame],
                   min_dollar_vol: float = 10_000_000) -> list[dict]:
    metrics = {t: m for t, m in ((t, compute_raw_metrics(h)) for t, h in prices.items())
               if m is not None}
    rows = {t: m for t, m in metrics.items() if m["avg_dollar_vol"] >= min_dollar_vol}
    if not rows:
        return []
    frame = pd.DataFrame(rows).T
    score = sum(_zscore(frame[col]).fillna(0.0) for col in
                ("ret_1m", "ret_3m", "ret_6m", "sma50_spread", "high_proximity"))
    ranked = score.sort_values(ascending=False)
    return [{"ticker": t, "score": round(float(s), 4)} for t, s in ranked.items()]


def build_pool(cfg: dict, limit: int | None = None) -> Path:
    universe = fetch_universe(cfg)
    if limit:
        universe = universe[:limit]
    prices = fetch_prices(universe)
    ranked = score_universe(prices)
    path = _results_dir(cfg) / f"pool_{week_key(date.today())}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"year_week": week_key(date.today()),
                                "built_at": datetime.now().isoformat(),
                                "pool": ranked}, indent=2), encoding="utf-8")
    logger.info("pool written to %s with %d tickers", path, len(ranked))
    return path


def load_pool(cfg: dict) -> list[dict]:
    pool_files = sorted(_results_dir(cfg).glob("pool_*.json"))
    if not pool_files:
        return []
    payload = json.loads(pool_files[-1].read_text(encoding="utf-8"))
    return payload.get("pool", [])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Weekly S&P 500 momentum screen")
    parser.add_argument("--screen", action="store_true", help="run the screen and write the pool")
    parser.add_argument("--universe-size", type=int, default=0,
                        help="limit universe for smoke tests (0 = full)")
    args = parser.parse_args(argv)
    if not args.screen:
        parser.error("nothing to do; pass --screen")
    cfg = load_watchlist_config()
    set_config(cfg)
    build_pool(cfg, limit=args.universe_size or None)
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_screener.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add screener.py tests/test_screener.py
git commit -m "feat: add weekly S&P 500 momentum screener"
```

---

### Task 4: Watchlist assembly (`daily_run.py` part 1)

**Files:**
- Create: `daily_run.py` (watchlist assembly + rating extraction only — orchestrator comes in Task 6)
- Test: `tests/test_watchlist.py`

**Interfaces:**
- Consumes: pool format from Task 3 (`load_pool` → `[{"ticker": ..., "score": ...}]`); memory entries from the framework (`TradingMemoryLog(cfg).load_entries()` → list of dicts with keys `date` ("YYYY-MM-DD"), `ticker`, `rating`, `pending`, ...); `parse_rating` from `tradingagents.agents.utils.rating`.
- Produces:
  - `extract_rating(signal_text: str) -> str` — thin wrapper returning `parse_rating(signal_text)`.
  - `class WatchlistShortError(Exception)`
  - `assemble_watchlist(holdings: set[str], pool: list[dict], memory_entries: list[dict], cfg: dict, today: date) -> list[str]`
  - `TODAY_ET() -> date` — `datetime.now(ZoneInfo("America/New_York")).date()`.

Assembly rules (spec §5bis): draw candidates from pool in score order, skipping (a) held tickers, (b) tickers with any memory entry dated >= `today - exclusion_days`, (c) tickers with a Sell/Underweight memory entry dated >= `today - exclusion_days`. Take up to `candidate_slots`. `watchlist = sorted(set(holdings) | set(candidates))`. If len < `min_watchlist_size`, top up with further pool members (same exclusions) until the minimum; if the pool is exhausted and the gate still fails, raise `WatchlistShortError`. If `pool` is empty, return `cfg["seed_watchlist"]`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_watchlist.py"""
from datetime import date, timedelta

import pytest

from daily_run import TODAY_ET, WatchlistShortError, assemble_watchlist, extract_rating

POOL = [{"ticker": "NVDA", "score": 3.0}, {"ticker": "AAPL", "score": 2.5},
        {"ticker": "AMD", "score": 2.0}, {"ticker": "MSFT", "score": 1.5},
        {"ticker": "GOOGL", "score": 1.0}, {"ticker": "META", "score": 0.5}]
TODAY = date(2026, 8, 31)  # Monday


def _entry(ticker, days_ago, rating="Hold"):
    return {"ticker": ticker, "rating": rating,
            "date": (TODAY - timedelta(days=days_ago)).isoformat()}


def test_extract_rating():
    assert extract_rating("**Rating**: Buy\n\nExecutive Summary: ...") == "Buy"
    assert extract_rating("no rating word here") == "Hold"


def test_holdings_always_included():
    got = assemble_watchlist({"TSLA"}, POOL, [], {}, TODAY)
    assert "TSLA" in got


def test_top_candidates_taken():
    got = assemble_watchlist(set(), POOL, [], {"candidate_slots": 2}, TODAY)
    assert got[:2] == sorted(["NVDA", "AAPL"])


def test_recently_analyzed_excluded():
    entries = [_entry("NVDA", 1)]
    got = assemble_watchlist(set(), POOL, entries, {"candidate_slots": 3}, TODAY)
    assert "NVDA" not in got
    assert "AAPL" in got


def test_recent_sell_rating_excluded():
    entries = [_entry("NVDA", 2, rating="Sell")]
    got = assemble_watchlist(set(), POOL, entries, {"candidate_slots": 3}, TODAY)
    assert "NVDA" not in got


def test_old_entries_do_not_exclude():
    entries = [_entry("NVDA", 10)]
    got = assemble_watchlist(set(), POOL, entries, {"candidate_slots": 3,
                                                    "exclusion_days": 7}, TODAY)
    assert "NVDA" in got


def test_min_size_topup():
    got = assemble_watchlist(set(), POOL, [],
                             {"candidate_slots": 1, "min_watchlist_size": 5}, TODAY)
    assert len(got) >= 5


def test_min_size_gate_fails_loudly():
    with pytest.raises(WatchlistShortError):
        assemble_watchlist(set(), [{"ticker": "NVDA", "score": 1.0}], [],
                           {"candidate_slots": 1, "min_watchlist_size": 5}, TODAY)


def test_empty_pool_uses_seed():
    got = assemble_watchlist(set(), [], [],
                             {"seed_watchlist": ["AAPL", "MSFT"],
                              "min_watchlist_size": 2}, TODAY)
    assert got == ["AAPL", "MSFT"]


def test_today_et_is_date():
    assert isinstance(TODAY_ET(), date)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_watchlist.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'daily_run'`

- [ ] **Step 3: Write minimal implementation** (create `daily_run.py` with only these pieces; the orchestrator lands in Task 6)

```python
"""daily_run.py — daily pipeline orchestrator (watchlist assembly first)."""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tradingagents.agents.utils.rating import parse_rating

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
```

Note: `seed_watchlist` is a top-level `watchlist.yaml` key (not under `screener:`), matching the shipped yaml in Task 7.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_watchlist.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add daily_run.py tests/test_watchlist.py
git commit -m "feat: add watchlist assembly and rating extraction"
```

---

### Task 5: IBKR broker layer (`ibkr.py`)

**Files:**
- Create: `ibkr.py`
- Test: `tests/test_ibkr.py`

**Interfaces:**
- Consumes: `decisions.Order` (Task 2).
- Produces:
  - `class IBKRBroker:` with `__init__(self, cfg: dict)`, `connect(self) -> None`, `get_positions_and_cash(self) -> tuple[dict[str, int], float]`, `place_market_orders(self, orders: list[Order], dry_run: bool = False) -> list[dict]`, `disconnect(self) -> None`.
  - Connection uses `ib_async` (`IB()`), host/port/clientId from `cfg["ibkr"]` (defaults `127.0.0.1`, `7497`, `1`).
  - BUY orders: `MarketOrder("BUY", shares)` with `order.auxPrice = protection_price` (MKT with LMT protection). SELL orders: plain `MarketOrder("SELL", shares)`.
  - `place_market_orders` returns fill reports `[{"ticker", "action", "shares", "filled", "avg_price"}]`; `dry_run=True` returns the same list with `filled=0`, `avg_price=0.0` without touching the client.
  - Fill timeout 60 s per order → cancel + log + record `filled=False`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_ibkr.py"""
from unittest.mock import MagicMock, patch

import pytest

from decisions import Order
from ibkr import IBKRBroker


@pytest.fixture
def broker():
    cfg = {"ibkr": {"host": "127.0.0.1", "port": 7497, "client_id": 1}}
    with patch("ibkr.IB") as mock_ib_cls:
        mock_ib = MagicMock()
        mock_ib_cls.return_value = mock_ib
        b = IBKRBroker(cfg)
        b._ib = mock_ib
        yield b, mock_ib


def test_connect_retries_then_raises(broker):
    b, mock_ib = broker
    mock_ib.connect.side_effect = [ConnectionError, ConnectionError]
    b._connect_opts = {"retries": 2, "sleep_s": 0}
    with pytest.raises(ConnectionError):
        b.connect()
    assert mock_ib.connect.call_count == 2


def test_get_positions_and_cash(broker):
    b, mock_ib = broker
    pos = MagicMock()
    pos.contract.symbol = "AAPL"
    pos.position = 10
    mock_ib.positions.return_value = [pos]
    mock_ib.accountSummary.return_value = []
    with patch("ibkr.time.sleep"):
        holdings, cash = b.get_positions_and_cash()
    assert holdings == {"AAPL": 10}
    assert isinstance(cash, float)


def test_place_market_orders_buy_has_aux_price(broker):
    b, mock_ib = broker
    mock_ib.qualifyContracts.return_value = []
    mock_ib.reqMktData.return_value = None
    mock_ib.trades = []
    with patch("ibkr.time.sleep"):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0)], dry_run=False)
    assert reports[0]["ticker"] == "AAPL"
    submitted = mock_ib.placeOrder.call_args[0][1]
    assert submitted.action == "BUY"
    assert submitted.totalQuantity == 10
    assert submitted.auxPrice == 102.0


def test_place_market_orders_dry_run_touches_nothing(broker):
    b, mock_ib = broker
    reports = b.place_market_orders(
        [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
               protection_price=102.0)], dry_run=True)
    assert reports[0]["filled"] == 0
    mock_ib.placeOrder.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ibkr.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ibkr'`

- [ ] **Step 3: Write minimal implementation**

```python
"""ibkr.py — thin wrapper over ib_async for the daily execution pass."""

import logging
import time

from ib_async import IB, MarketOrder, Stock

from decisions import Order

logger = logging.getLogger(__name__)

FILL_TIMEOUT_S = 60


class IBKRBroker:
    def __init__(self, cfg: dict):
        ibkr_cfg = cfg.get("ibkr", {})
        self.host = ibkr_cfg.get("host", "127.0.0.1")
        self.port = int(ibkr_cfg.get("port", 7497))
        self.client_id = int(ibkr_cfg.get("client_id", 1))
        self._connect_opts = {"retries": 3, "sleep_s": 5}
        self._ib = None

    def connect(self) -> None:
        ib = IB()
        last_error = None
        for attempt in range(self._connect_opts["retries"]):
            try:
                ib.connect(self.host, self.port, clientId=self.client_id,
                           readonly=False)
                self._ib = ib
                logger.info("connected to IBKR Gateway on %s:%s", self.host, self.port)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("IBKR connect attempt %d failed: %s", attempt + 1, exc)
                time.sleep(self._connect_opts["sleep_s"])
        raise ConnectionError(f"IBKR unreachable after retries: {last_error}")

    def get_positions_and_cash(self) -> tuple[dict[str, int], float]:
        holdings: dict[str, int] = {}
        for pos in self._ib.positions():
            symbol = pos.contract.symbol
            if pos.position:
                holdings[symbol] = int(pos.position)
        cash = 0.0
        for item in self._ib.accountSummary():
            if item.tag == "TotalCashValue":
                try:
                    cash = float(item.value)
                except ValueError:
                    cash = 0.0
        return holdings, cash

    def place_market_orders(self, orders: list[Order], dry_run: bool = False) -> list[dict]:
        reports = []
        if dry_run:
            for o in orders:
                logger.info("DRY-RUN %s %s %d shares (protection %s)",
                            o.action, o.ticker, o.shares, o.protection_price)
                reports.append({"ticker": o.ticker, "action": o.action,
                                "shares": o.shares, "filled": 0, "avg_price": 0.0})
            return reports

        for o in orders:
            contract = Stock(o.ticker, "SMART", "USD")
            order = MarketOrder(o.action, o.shares)
            if o.action == "BUY" and o.protection_price:
                order.auxPrice = o.protection_price  # MKT with LMT protection
            trade = self._ib.placeOrder(contract, order)
            filled = 0
            avg_price = 0.0
            try:
                for _ in range(FILL_TIMEOUT_S * 2):
                    if trade.isDone():
                        break
                    time.sleep(0.5)
                if trade.fills():
                    filled = sum(f.execution.shares for f in trade.fills())
                    avg_price = (sum(f.execution.price * f.execution.shares
                                     for f in trade.fills()) / filled) if filled else 0.0
                else:
                    self._ib.cancelOrder(order)
                    logger.warning("order for %s not filled in %ds; cancelled",
                                   o.ticker, FILL_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                logger.error("order handling failed for %s: %s", o.ticker, exc)
            reports.append({"ticker": o.ticker, "action": o.action,
                            "shares": o.shares, "filled": filled,
                            "avg_price": round(float(avg_price), 4)})
        return reports

    def disconnect(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ibkr.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add ibkr.py tests/test_ibkr.py
git commit -m "feat: add IBKR broker wrapper with protection-capped market orders"
```

---

### Task 6: Orchestrator (`daily_run.py` part 2)

**Files:**
- Modify: `daily_run.py` (append orchestrator + CLI)
- Test: `tests/test_daily_run.py`

**Interfaces:**
- Consumes: `load_watchlist_config` (T1), `compute_orders`/`Order` (T2), `load_pool` (T3), `TODAY_ET`/`assemble_watchlist` (T4), `IBKRBroker` (T5), framework `TradingAgentsGraph` + `TradingMemoryLog` + `set_config`.
- Produces:
  - `run_analyze(cfg: dict, tickers: list[str] | None = None) -> dict` — fetches IBKR holdings (held positions are always analyzed, so sells are evaluated), assembles watchlist (or uses explicit tickers), runs `TradingAgentsGraph(config=cfg).propagate(ticker, TODAY_ET())` per ticker with one retry, extracts ratings, persists `results_dir/ratings_YYYY-MM-DD.json` `{"date", "ratings": {ticker: rating}, "failures": [...]}`, returns the payload. On broker connection failure at analyze time, logs a warning and proceeds with candidates only.
  - `_last_close(ticker: str) -> float | None` — last daily close via `yfinance.Ticker(ticker).history(period="5d")`; `None` on failure. (The framework's `get_stock_data` returns a formatted string, not a frame — use yfinance directly here, same as the framework's own `_fetch_returns`.)
  - `run_execute(cfg: dict, dry_run: bool = False) -> int` — kill-switch check (`DISABLE_TRADING` file at repo root or `trading_enabled: false` → log + return 1); loads today's ratings JSON (missing → return 1); connects broker; fetches holdings+cash; computes orders; idempotency check (`results_dir/executed_YYYY-MM-DD.json` exists → skip); places orders; writes executed log; returns 0.
  - `main(argv: list[str] | None = None) -> int` — argparse `--analyze`, `--execute`, `--healthcheck`, `--dry-run`, `--tickers A,B` (for analyze only).
  - `healthcheck(cfg: dict) -> bool` — tries broker connect, returns True/False (used by the 06:50 cron guard).

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_daily_run.py"""
import json
from unittest.mock import MagicMock, patch

import pytest

from daily_run import main, run_analyze, run_execute


@pytest.fixture
def cfg(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    c = DEFAULT_CONFIG.copy()
    c["results_dir"] = str(tmp_path / "results")
    c["data_cache_dir"] = str(tmp_path / "cache")
    c["memory_log_path"] = str(tmp_path / "memory" / "trading_memory.md")
    return c


def _ratings_file(cfg, ratings, failures=None, day="2026-08-31"):
    import daily_run
    payload = {"date": day, "ratings": ratings, "failures": failures or []}
    path = daily_run.Path(cfg["results_dir"]) / f"ratings_{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_run_analyze_extracts_ratings_and_writes_json(cfg):
    fake_graph = MagicMock()
    fake_graph.propagate.return_value = (None, "**Rating**: Buy")

    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            return fake_graph.propagate(ticker)

    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log:
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg, tickers=["AAPL", "MSFT"])
    assert payload["ratings"] == {"AAPL": "Buy", "MSFT": "Buy"}
    files = [p for p in __import__("pathlib").Path(cfg["results_dir"]).glob("ratings_*.json")]
    assert len(files) == 1
    assert json.loads(files[0].read_text())["ratings"]["AAPL"] == "Buy"


def test_run_analyze_includes_holdings(cfg):
    """Held positions must be analyzed so sells are evaluated."""
    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            return None, "**Rating**: Hold"

    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({"TSLA": 40}, 100_000.0)
    pool = [{"ticker": "NVDA", "score": 1.0}, {"ticker": "AAPL", "score": 0.5}]
    cfg["screener"] = {"candidate_slots": 2, "min_watchlist_size": 2,
                       "exclusion_days": 7}
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.IBKRBroker", return_value=broker), \
         patch("daily_run.load_pool", return_value=pool):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg)
    assert set(payload["ratings"]) == {"TSLA", "NVDA", "AAPL"}
    broker.connect.assert_called_once()


def test_run_analyze_failure_is_isolated(cfg):
    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            if ticker == "AAPL":
                raise RuntimeError("boom")
            return None, "**Rating**: Hold"

    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log, \
         patch("daily_run.IBKRBroker", return_value=broker):
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg, tickers=["AAPL", "MSFT"])
    assert payload["ratings"] == {"MSFT": "Hold"}
    assert payload["failures"] == ["AAPL"]


def test_run_execute_kill_switch_blocks(cfg, tmp_path):
    (tmp_path / "DISABLE_TRADING").write_text("")
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.Path.exists", return_value=True):
        rc = run_execute(cfg)
    assert rc == 1


def test_run_execute_missing_ratings_fails_safe(cfg):
    with patch("daily_run.load_watchlist_config", return_value=cfg):
        rc = run_execute(cfg)
    assert rc == 1  # no ratings file -> no orders


def test_run_execute_places_orders_and_writes_log(cfg):
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = [{"ticker": "AAPL", "action": "BUY",
                                                "shares": 10, "filled": 10,
                                                "avg_price": 101.5}]
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.IBKRBroker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    broker.place_market_orders.assert_called_once()
    import pathlib
    logs = list(pathlib.Path(cfg["results_dir"]).glob("executed_*.json"))
    assert len(logs) == 1


def test_run_execute_idempotent_second_call_skips(cfg):
    _ratings_file(cfg, {"AAPL": "Buy"})
    import pathlib
    pathlib.Path(cfg["results_dir"]).mkdir(parents=True, exist_ok=True)
    (pathlib.Path(cfg["results_dir"]) / "executed_2026-08-31.json").write_text(
        json.dumps({"orders": []}), encoding="utf-8")
    broker = MagicMock()
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.IBKRBroker", return_value=broker), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    broker.place_market_orders.assert_not_called()


def test_main_analyze_dispatch(cfg):
    with patch("daily_run.run_analyze", return_value={"ratings": {}}) as mock_run:
        rc = main(["--analyze", "--tickers", "AAPL,MSFT"])
    assert rc == 0
    mock_run.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daily_run.py -v`
Expected: FAIL — `AttributeError: module 'daily_run' has no attribute 'run_analyze'`

- [ ] **Step 3: Write minimal implementation** (append to `daily_run.py`)

```python
# --- orchestrator ---

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from config import load_watchlist_config
from decisions import compute_orders
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import set_config

import yfinance as yf

from ibkr import IBKRBroker
from screener import load_pool
from tradingagents.graph.trading_graph import TradingAgentsGraph

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
        broker = IBKRBroker(cfg)
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
    broker = IBKRBroker(cfg)
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
    broker = IBKRBroker(cfg)
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
    parser.add_argument("--healthcheck", action="store_true", help="check IBKR reachability")
    parser.add_argument("--dry-run", action="store_true", help="print orders without placing")
    parser.add_argument("--tickers", default=None, help="comma-separated tickers (analyze)")
    args = parser.parse_args(argv)

    cfg = load_watchlist_config()
    set_config(cfg)

    if args.healthcheck:
        ok = healthcheck(cfg)
        print("IBKR reachable" if ok else "IBKR UNREACHABLE")
        return 0 if ok else 1
    if args.analyze:
        tickers = args.tickers.split(",") if args.tickers else None
        run_analyze(cfg, tickers)
        return 0
    if args.execute:
        return run_execute(cfg, dry_run=args.dry_run)
    parser.error("pass --analyze, --execute, or --healthcheck")
    return 2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_daily_run.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add daily_run.py tests/test_daily_run.py
git commit -m "feat: add daily orchestrator with analyze/execute/healthcheck"
```

---

### Task 7: `watchlist.yaml` + setup doc + final verification

**Files:**
- Create: `watchlist.yaml`, `SETUP.md`
- Test: extend `tests/test_config.py` (validate the shipped yaml loads)

**Interfaces:**
- Consumes: all prior tasks.
- Produces: the operational artifact set for the Ubuntu 24.04 host.

- [ ] **Step 1: Write the failing test (shipped yaml must load and merge)**

```python
"""tests/test_config.py (append)"""

def test_shipped_watchlist_yaml_loads():
    from config import load_watchlist_config, DEFAULT_WATCHLIST_PATH
    cfg = load_watchlist_config(DEFAULT_WATCHLIST_PATH)
    assert cfg["llm_provider"] == "openrouter"
    assert cfg["quick_think_llm"].startswith("deepseek/")
    assert cfg["deep_think_llm"].startswith("deepseek/")
    assert cfg["screener"]["min_watchlist_size"] == 5
    assert cfg["screener"]["pool_size"] == 50
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_shipped_watchlist_yaml_loads -v`
Expected: FAIL — `FileNotFoundError: watchlist.yaml`

- [ ] **Step 3: Create `watchlist.yaml`**

```yaml
# Daily paper-trading configuration. Merge layer over tradingagents/default_config.py.
seed_watchlist: [AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA]   # used until the first screen

llm_provider: openrouter
quick_think_llm: deepseek/deepseek-v4-flash
deep_think_llm: deepseek/deepseek-v4-pro
output_language: English

capital: 100000            # paper capital
max_positions: 10          # equal-weight denominator
max_order_value_cap: 15000 # per-day total buy cap

screener:
  universe: sp500
  pool_size: 50            # ranked queue depth the daily draw may use
  candidate_slots: 3
  min_watchlist_size: 5    # production gate (test: 1)
  exclusion_days: 7
  entry_protection_pct: 2.0

ibkr:
  host: 127.0.0.1
  port: 7497               # paper trading port
  client_id: 1

trading_enabled: true      # false = analysis-only; DISABLE_TRADING file also works
```

- [ ] **Step 4: Create `SETUP.md`** (Ubuntu 24.04 host setup — operational doc)

```markdown
# Daily Paper-Trading Setup (Ubuntu 24.04)

## One-time install
1. `sudo apt install -y python3.12 python3.12-venv cron`
2. `git clone <repo> /opt/tradingagents && cd /opt/tradingagents`
3. `python3.12 -m venv .venv && .venv/bin/pip install . ib_async pyyaml`
4. Install IB Gateway (paper), enable API access, paper account login on port 7497.
5. Create `.env` with `OPENROUTER_API_KEY` and `FRED_API_KEY` (dotenv is loaded by the framework).
6. Copy `watchlist.yaml` into place; verify `trading_enabled: true`.

## Cron (CRON_TZ avoids DST bugs)
Run `crontab -e` and add:
```cron
CRON_TZ=America/New_York
50 6 * * 1-5  /opt/tradingagents/.venv/bin/python /opt/tradingagents/daily_run.py --healthcheck >> /opt/tradingagents/logs/health.log 2>&1
0 18 * * 0    /opt/tradingagents/.venv/bin/python /opt/tradingagents/screener.py --screen >> /opt/tradingagents/logs/screener.log 2>&1
0 7 * * 1-5   /opt/tradingagents/.venv/bin/python /opt/tradingagents/daily_run.py --analyze >> /opt/tradingagents/logs/cron.log 2>&1
0 9 * * 1-5   /opt/tradingagents/.venv/bin/python /opt/tradingagents/daily_run.py --execute >> /opt/tradingagents/logs/orders.log 2>&1
```

## Kill switch
`touch /opt/tradingagents/DISABLE_TRADING`   # analysis runs, no orders
`rm /opt/tradingagents/DISABLE_TRADING`      # re-enable

## Smoke test (before trusting cron)
1. `cd /opt/tradingagents && .venv/bin/python screener.py --screen`   # full weekly screen
2. `.venv/bin/python daily_run.py --analyze --tickers AAPL`           # one-ticker analysis
3. `.venv/bin/python daily_run.py --execute --dry-run`                # prints orders, places none
4. `.venv/bin/python daily_run.py --healthcheck`                      # IBKR reachable
5. Watch `logs/` for a full week before enabling real orders.

## Artifacts
- `~/.tradingagents/logs/ratings_YYYY-MM-DD.json` — morning ratings
- `~/.tradingagents/logs/executed_YYYY-MM-DD.json` — order log (idempotency guard)
- `~/.tradingagents/logs/pool_YYYY-WW.json` — weekly candidate pool
- `~/.tradingagents/memory/trading_memory.md` — framework decision memory
```

- [ ] **Step 5: Run full test suite**

Run: `pytest -q`
Expected: all tests pass (new + existing framework tests).

Run: `ruff check config.py decisions.py screener.py ibkr.py daily_run.py tests/`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add watchlist.yaml SETUP.md tests/test_config.py
git commit -m "feat: add watchlist.yaml, setup doc, and shipped-config test"
```

---

## Self-review notes

- Spec §2/§5bis (curation, min-5 gate, exclusions, seed fallback): Task 4 + Task 7.
- Spec §5 (decision engine, protection cap, sizing, order cap): Task 2.
- Spec §4 (screener, pool ranked queue, liquidity filter, weekly cache): Task 3.
- Spec §9/§10/§11 (IBKR setup, cron, health guard, host setup): Task 5 + Task 7.
- Spec §12 (kill switch, idempotency, fill timeout, fail-safe execution): Task 5 + Task 6.
- Spec §13 (unit + broker-mock + integration smoke): tasks' unit tests + SETUP.md smoke steps.
- Spec §7 (DeepSeek V4 Flash/Pro defaults): Task 7 yaml.
- Out of scope honored: no `tradingagents/` edits, no email, no stop-loss/target orders, no LLM screening overlay.
