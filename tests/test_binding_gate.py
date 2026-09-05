"""binding_gate tests (hermetic): the automated morning gate that decides
whether PM execution binding may run for the day."""

import json

import pytest

from binding_gate import GATE_FAIL, GATE_PASS, evaluate, gate_path


def _ratings_file(cfg, ratings, execution=None, day="2026-09-05",
                  schema_version=2, failures=None):
    import pathlib
    payload = {"date": day, "ratings": ratings, "failures": failures or []}
    if execution is not None:
        payload["schema_version"] = schema_version
        payload["execution"] = execution
    path = pathlib.Path(cfg["results_dir"]) / f"ratings_{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _buy_block(value_usd=200.0):
    return {"orders": [{"kind": "BUY", "value_usd": value_usd}]}


def _sell_block(shares=2, limit_px=100.5, stop_px=95.6):
    return {"orders": [{"kind": "SELL", "shares": shares,
                        "limit_px": limit_px, "stop_px": stop_px}]}


@pytest.fixture
def cfg(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG
    c = DEFAULT_CONFIG.copy()
    c["results_dir"] = str(tmp_path / "results")
    c["data_cache_dir"] = str(tmp_path / "cache")
    c["memory_log_path"] = str(tmp_path / "memory" / "trading_memory.md")
    return c


@pytest.fixture
def gate_cfg(cfg):
    cfg["pm_execution"] = True
    return cfg


class TestEvaluate:
    def test_pass_with_valid_blocks(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": _buy_block()})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={"HPE": 54.25})
        assert result["verdict"] == GATE_PASS
        assert result["reasons"] == []

    def test_missing_ratings_file_fails(self, gate_cfg):
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={})
        assert result["verdict"] == GATE_FAIL
        assert any("ratings" in r for r in result["reasons"])

    def test_v1_ratings_no_blocks_fails(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, day="2026-09-05",
                      schema_version=1, execution={})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={})
        assert result["verdict"] == GATE_FAIL

    def test_invalid_block_fails(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": {
            "orders": [{"kind": "BUY", "shares": 5, "value_usd": 100.0}]}})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={"HPE": 54.25})
        assert result["verdict"] == GATE_FAIL
        assert any("invalid" in r for r in result["reasons"])

    def test_empty_block_on_buy_rated_ticker_fails(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": {"orders": []}})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={"HPE": 54.25})
        assert result["verdict"] == GATE_FAIL
        assert any("empty" in r.lower() or "no order" in r.lower()
                   for r in result["reasons"])

    def test_engine_fallback_fails(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": _buy_block()})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={})  # no close -> fallback
        assert result["verdict"] == GATE_FAIL

    def test_partial_sell_block_passes(self, gate_cfg):
        _ratings_file(gate_cfg, {"EL": "Underweight"},
                      {"EL": _sell_block()})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={"EL": 8}, last_close={"EL": 101.15})
        assert result["verdict"] == GATE_PASS

    def test_empty_block_on_sell_rated_held_is_explicit_hold(self, gate_cfg):
        _ratings_file(gate_cfg, {"EL": "Underweight"},
                      {"EL": {"orders": []}})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={"EL": 8}, last_close={"EL": 101.15})
        assert result["verdict"] == GATE_PASS

    def test_gate_file_written(self, gate_cfg, tmp_path):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": _buy_block()})
        evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                 holdings={}, last_close={"HPE": 54.25})
        path = gate_path(gate_cfg["results_dir"], "2026-09-05")
        payload = json.loads(path.read_text())
        assert payload["verdict"] == GATE_PASS
        assert payload["date"] == "2026-09-05"


class TestPath:
    def test_gate_path_lives_in_results_dir(self, gate_cfg):
        assert gate_path(gate_cfg["results_dir"], "2026-09-05").name == \
            "binding_gate_2026-09-05.json"
