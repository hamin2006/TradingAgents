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
    files = list(__import__("pathlib").Path(cfg["results_dir"]).glob("ratings_*.json"))
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
         patch("daily_run.create_broker", return_value=broker), \
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
         patch("daily_run.create_broker", return_value=broker):
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
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
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
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
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


def test_run_analyze_parallelizes(cfg):
    """Ticker analyses run concurrently (thread pool), not sequentially."""
    import threading
    import time

    active = 0
    max_active = 0
    state_lock = threading.Lock()

    class FakeTradingAgentsGraph:
        def __init__(self, **kwargs):
            pass

        def propagate(self, ticker, date, asset_type="stock"):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.25)
            with state_lock:
                active -= 1
            return None, "**Rating**: Hold"

    cfg["analyze_max_workers"] = 4
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.TradingAgentsGraph", FakeTradingAgentsGraph), \
         patch("daily_run.TradingMemoryLog") as mock_log:
        mock_log.return_value.load_entries.return_value = []
        payload = run_analyze(cfg, tickers=["A", "B", "C", "D", "E"])
    assert max_active >= 2  # at least two ran concurrently
    assert set(payload["ratings"]) == {"A", "B", "C", "D", "E"}
    assert payload["failures"] == []


def test_seconds_until_open():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from daily_run import _seconds_until_open
    ET = ZoneInfo("America/New_York")

    def at(h, m):
        return datetime(2026, 8, 31, h, m, tzinfo=ET)

    assert _seconds_until_open(at(9, 0)) == 1800.0
    assert _seconds_until_open(at(8, 30)) == 3600.0
    assert _seconds_until_open(at(9, 30)) == 0.0
    assert _seconds_until_open(at(14, 0)) == 0.0


def test_run_execute_waits_for_open_when_preopen(cfg):
    """Orders must be submitted AT the open, not polled-and-cancelled
    pre-open (a 60s fill poll before 09:30 would cancel the order)."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = [{"ticker": "AAPL", "action": "BUY",
                                                "shares": 10, "filled": 10,
                                                "avg_price": 101.5}]
    slept = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=1800.0), \
         patch("daily_run.time.sleep", side_effect=lambda s: slept.append(s)), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    assert slept == [1800.0]  # waited for the open before submitting
    broker.place_market_orders.assert_called_once()


def test_run_execute_skips_wait_in_dry_run(cfg):
    """--dry-run previews orders without waiting for the open."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []  # real broker dry-run returns reports list
    slept = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=1800.0), \
         patch("daily_run.time.sleep", side_effect=lambda s: slept.append(s)), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg, dry_run=True)
    assert rc == 0
    assert slept == []


def test_memory_lock_covers_all_write_paths():
    """Concurrent resolves also write the memory log (batch updates); the
    lock must wrap every read-modify-write method, not just store_decision."""
    import daily_run
    import tradingagents.agents.utils.memory as memory_mod

    original_store = memory_mod.TradingMemoryLog.store_decision
    original_batch = memory_mod.TradingMemoryLog.batch_update_with_outcomes
    original_single = memory_mod.TradingMemoryLog.update_with_outcome

    daily_run._MEMORY_PATCHED = False  # force re-patch in this process
    daily_run._ensure_memory_write_lock()

    assert memory_mod.TradingMemoryLog.store_decision is not original_store
    assert (memory_mod.TradingMemoryLog.batch_update_with_outcomes
            is not original_batch)
    assert (memory_mod.TradingMemoryLog.update_with_outcome
            is not original_single)


def test_run_execute_marks_before_submitting(cfg):
    """If the process dies between placing orders and writing the final log,
    a rerun must not double-execute: the mark exists before submission."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.side_effect = RuntimeError("crashed mid-submit")
    import pytest as _pytest
    with _pytest.raises(RuntimeError), \
         patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        run_execute(cfg)
    import pathlib
    marks = list(pathlib.Path(cfg["results_dir"]).glob("executed_*.json"))
    assert len(marks) == 1
    assert json.loads(marks[0].read_text())["status"] == "submitted"


def test_run_execute_dry_run_writes_no_idempotency_file(cfg):
    """A dry-run preview must not mark the day as executed."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=1800.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg, dry_run=True)
    assert rc == 0
    import pathlib
    assert not list(pathlib.Path(cfg["results_dir"]).glob("executed_*.json"))


def test_run_execute_caps_capital_by_actual_cash(cfg, caplog):
    """Configured capital must not exceed the account's real cash."""
    _ratings_file(cfg, {"AAPL": "Buy"})
    cfg["capital"] = 1_000_000
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=100.0), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    orders = broker.place_market_orders.call_args[0][0]
    assert orders[0].shares == 150  # 100_000 cash cap / 10 positions x 1.5 (Buy) / 100.0
    assert any("cash" in r.message.lower() for r in caplog.records)


def test_run_execute_missing_last_close_warns_and_skips(cfg, caplog):
    _ratings_file(cfg, {"AAPL": "Buy"})
    broker = MagicMock()
    broker.get_positions_and_cash.return_value = ({}, 100_000.0)
    broker.place_market_orders.return_value = []
    with patch("daily_run.load_watchlist_config", return_value=cfg), \
         patch("daily_run.create_broker", return_value=broker), \
         patch("daily_run._last_close", return_value=None), \
         patch("daily_run._seconds_until_open", return_value=0.0), \
         patch("daily_run.TODAY_ET") as mock_today:
        mock_today.return_value = __import__("datetime").date(2026, 8, 31)
        rc = run_execute(cfg)
    assert rc == 0
    orders = broker.place_market_orders.call_args[0][0]
    assert orders == []  # no price -> no order (but warn, don't go silent)
    assert any("last close" in r.message.lower() for r in caplog.records)


def test_main_strips_ticker_whitespace():
    with patch("daily_run.run_analyze") as mock_run:
        rc = main(["--analyze", "--tickers", " AAPL , MSFT "])
    assert rc == 0
    mock_run.assert_called_once()
    assert mock_run.call_args[0][1] == ["AAPL", "MSFT"]


def _unwrap_reddit_fetch(fn):
    """Walk wrapper chains (paced_rss._wrapped_original) back to the real
    framework fetcher so tests leave the module pristine for later suites."""
    for _ in range(10):
        inner = getattr(fn, "_wrapped_original", None)
        if inner is None:
            return fn
        fn = inner
    return fn


def test_reddit_fetches_serialize_across_threads():
    """Parallel tickers must not burst Reddit's anonymous per-IP rate limit:
    all Reddit fetches across analyze workers serialize through one lock."""
    import threading
    import time

    import daily_run
    import tradingagents.dataflows.reddit as reddit_mod

    active = 0
    max_active = 0
    state_lock = threading.Lock()

    def fake_rss(ticker, sub, limit, timeout):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with state_lock:
            active -= 1
        return []

    previous = reddit_mod._fetch_subreddit_rss  # real fetcher or earlier wrapper
    daily_run._REDDIT_MIN_INTERVAL = 0.0        # serialization test only
    daily_run._REDDIT_LAST_TS = 0.0             # clear pacing clock
    reddit_mod._fetch_subreddit_rss = fake_rss  # replace real fetcher first
    daily_run._REDDIT_PATCHED = False
    daily_run._ensure_reddit_pacing()           # wrapper now captures fake_rss
    try:
        def worker(i):
            reddit_mod._fetch_subreddit_rss(f"T{i}", "stocks", 5, 10)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert max_active == 1  # fully serialized
    finally:
        reddit_mod._fetch_subreddit_rss = _unwrap_reddit_fetch(previous)
        daily_run._REDDIT_PATCHED = False           # fully undo the patch
        daily_run._REDDIT_LAST_TS = 0.0
        daily_run._REDDIT_MIN_INTERVAL = 8.0


def test_reddit_requests_are_rate_limited():
    """The wrapper must pace requests globally (min interval), not just
    serialize them: Reddit's anonymous limit is ~10 req/min, and the
    framework's 1s inter-sub pacing alone sustains ~1 req/sec."""

    import daily_run
    import tradingagents.dataflows.reddit as reddit_mod

    sleeps = []

    def fake_rss(ticker, sub, limit, timeout):
        return []

    original_attr = reddit_mod._fetch_subreddit_rss
    reddit_mod._fetch_subreddit_rss = fake_rss
    daily_run._REDDIT_PATCHED = False
    daily_run._REDDIT_MIN_INTERVAL = 0.2  # speed up the test
    daily_run._REDDIT_LAST_TS = 0.0       # clear pacing clock from earlier tests
    try:
        daily_run._ensure_reddit_pacing()
        with patch("daily_run.time.sleep",
                   side_effect=lambda s: sleeps.append(s)):
            for i in range(5):
                reddit_mod._fetch_subreddit_rss(f"T{i}", "stocks", 5, 10)
        # 4 pauses between 5 requests, each ~ the min interval (first is free)
        assert len(sleeps) == 4, sleeps
        assert all(g >= 0.15 for g in sleeps), sleeps
    finally:
        reddit_mod._fetch_subreddit_rss = _unwrap_reddit_fetch(original_attr)
        daily_run._REDDIT_PATCHED = False      # fully undo the patch
        daily_run._REDDIT_LAST_TS = 0.0
        daily_run._REDDIT_MIN_INTERVAL = 8.0


def test_reddit_pacing_survives_internal_retry_recursion():
    """reddit.py's 429 retry re-invokes the module attribute (itself), which
    is our wrapper: the same thread re-enters the pacing lock. A plain Lock
    would deadlock; RLock must let the retry through."""

    import daily_run
    import tradingagents.dataflows.reddit as reddit_mod

    calls = []

    def recursive_fake(ticker, sub, limit, timeout, _retry=True):
        calls.append(_retry)
        if _retry:
            return reddit_mod._fetch_subreddit_rss(ticker, sub, limit, timeout,
                                                   _retry=False)
        return ["ok"]

    original_attr = reddit_mod._fetch_subreddit_rss
    reddit_mod._fetch_subreddit_rss = recursive_fake
    daily_run._REDDIT_PATCHED = False
    daily_run._REDDIT_MIN_INTERVAL = 0.0
    daily_run._REDDIT_LAST_TS = 0.0
    try:
        daily_run._ensure_reddit_pacing()
        result = reddit_mod._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        assert result == ["ok"]
        assert calls == [True, False]  # outer + internal retry via wrapper
    finally:
        reddit_mod._fetch_subreddit_rss = _unwrap_reddit_fetch(original_attr)
        daily_run._REDDIT_PATCHED = False
        daily_run._REDDIT_LAST_TS = 0.0
        daily_run._REDDIT_MIN_INTERVAL = 8.0


def test_reddit_oauth_swapped_when_creds_present(monkeypatch):
    import daily_run
    import reddit_auth
    import tradingagents.agents.analysts.sentiment_analyst as sa

    monkeypatch.setenv("REDDIT_CLIENT_ID", "c")
    monkeypatch.setenv("REDDIT_SECRET", "s")
    original = sa.fetch_reddit_posts
    daily_run._REDDIT_OAUTH_PATCHED = False
    daily_run._REDDIT_OAUTH_ACTIVE = False
    try:
        active = daily_run._ensure_reddit_oauth()
        assert active is True
        # swapped to the resilient OAuth wrapper (not the raw impl, not the original)
        assert sa.fetch_reddit_posts is not original
        assert sa.fetch_reddit_posts is not reddit_auth.fetch_reddit_posts
    finally:
        sa.fetch_reddit_posts = _unwrap_reddit_fetch(original)
        daily_run._REDDIT_OAUTH_PATCHED = False
        daily_run._REDDIT_OAUTH_ACTIVE = False


def test_reddit_resilient_wrapper_applied_without_creds(monkeypatch):
    """Even without OAuth creds, the RSS path gets the retry+cache wrapper —
    the agents are never left without Reddit data."""
    import daily_run
    import tradingagents.agents.analysts.sentiment_analyst as sa

    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_SECRET", raising=False)
    original = sa.fetch_reddit_posts
    daily_run._REDDIT_OAUTH_PATCHED = False
    daily_run._REDDIT_OAUTH_ACTIVE = False
    try:
        active = daily_run._ensure_reddit_oauth()
        assert active is False  # RSS path -> caller keeps pacing
        assert sa.fetch_reddit_posts is not original  # still wrapped
    finally:
        sa.fetch_reddit_posts = _unwrap_reddit_fetch(original)
        daily_run._REDDIT_OAUTH_PATCHED = False
        daily_run._REDDIT_OAUTH_ACTIVE = False
