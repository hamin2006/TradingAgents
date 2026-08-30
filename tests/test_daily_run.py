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
