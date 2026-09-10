"""binding_gate tests (hermetic): the automated morning gate that decides
whether PM execution binding may run for the day."""

import json

import pytest

from binding_gate import GATE_FAIL, GATE_PASS, evaluate, gate_path, run


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

    def test_empty_block_on_ow_held_is_maintain_not_failure(self, gate_cfg):
        """E2E finding (09-05 sandbox): a held ticker rated Overweight with
        explicit orders:[] is a deliberate maintain (13 HPE shares, no add
        at the current price) — NOT the silent-inaction failure class. Only
        empty blocks on NON-held buy-rated tickers fail the gate."""
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": {"orders": [],
                       "future_notes": "maintain; add only above 52.95"}})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={"HPE": 13}, last_close={"HPE": 52.0})
        assert result["verdict"] == GATE_PASS


class TestRun:
    """Chained entrypoint: analyze completion invokes the gate (2026-09-10
    race: a slow analyze overran the fixed 08:00 ET cron; the artifact stayed
    an empty FAIL and the day silently executed legacy)."""

    def test_run_snapshots_broker_and_writes_artifact(self, gate_cfg,
                                                      monkeypatch):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": _buy_block()})
        import broker as broker_mod
        import daily_run

        class FakeBroker:
            def __init__(self):
                self.connected = False

            def connect(self):
                self.connected = True

            def get_positions_and_cash(self):
                return {}, 10_000.0

            def disconnect(self):
                self.connected = False

        fake = FakeBroker()
        monkeypatch.setattr(broker_mod, "create_broker", lambda cfg: fake)
        monkeypatch.setattr(daily_run, "_last_close", lambda ticker: 54.25)

        result = run(gate_cfg, "2026-09-05")

        assert result["verdict"] == GATE_PASS
        assert fake.connected is False
        payload = json.loads(
            gate_path(gate_cfg["results_dir"], "2026-09-05").read_text())
        assert payload["verdict"] == GATE_PASS

    def test_run_broker_failure_writes_fail_artifact(self, gate_cfg,
                                                     monkeypatch):
        import broker as broker_mod

        class FakeBroker:
            def connect(self):
                raise RuntimeError("broker down")

            def disconnect(self):
                pass

        monkeypatch.setattr(broker_mod, "create_broker",
                            lambda cfg: FakeBroker())

        result = run(gate_cfg, "2026-09-05")

        assert result["verdict"] == GATE_FAIL
        assert any("broker snapshot" in r for r in result["reasons"])
        payload = json.loads(
            gate_path(gate_cfg["results_dir"], "2026-09-05").read_text())
        assert payload["verdict"] == GATE_FAIL
        assert payload["per_ticker"] == {}


class TestPerTickerGate:
    """Per-ticker gate status tests (Task A: fail-closed gate)."""

    def test_per_ticker_status_present_in_result(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": _buy_block()})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={"HPE": 54.25})
        assert "per_ticker" in result
        assert isinstance(result["per_ticker"], dict)

    def test_valid_block_gets_bind_status(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": _buy_block()})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={"HPE": 54.25})
        assert result["per_ticker"]["HPE"]["status"] == "bind"
        assert result["per_ticker"]["HPE"]["reason"] is None

    def test_invalid_block_gets_legacy_status(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": {
            "orders": [{"kind": "BUY", "shares": 5, "value_usd": 100.0}]}})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={"HPE": 54.25})
        assert result["per_ticker"]["HPE"]["status"] == "legacy"
        # The error message contains both field names
        assert "value_usd" in result["per_ticker"]["HPE"]["reason"]
        assert "shares" in result["per_ticker"]["HPE"]["reason"]

    def test_empty_on_unheld_buy_gets_legacy_status(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Buy"}, {"HPE": {"orders": []}})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={"HPE": 54.25})
        assert result["per_ticker"]["HPE"]["status"] == "legacy"
        assert "empty execution orders" in result["per_ticker"]["HPE"]["reason"]

    def test_engine_fallback_gets_legacy_status(self, gate_cfg):
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": _buy_block()})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={}, last_close={})  # no close -> fallback
        assert result["per_ticker"]["HPE"]["status"] == "legacy"
        assert "not honorable" in result["per_ticker"]["HPE"]["reason"]

    def test_mixed_tickers_bind_and_legacy(self, gate_cfg):
        """One bad ticker doesn't poison the day - good tickers bind."""
        _ratings_file(gate_cfg,
                      {"HPE": "Overweight", "DELL": "Buy", "EL": "Underweight"},
                      {"HPE": _buy_block(), "DELL": {"orders": []},
                       "EL": _sell_block()})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={"EL": 8},
                          last_close={"HPE": 54.25, "DELL": 120.0, "EL": 101.15})
        # HPE and EL are good -> bind
        assert result["per_ticker"]["HPE"]["status"] == "bind"
        assert result["per_ticker"]["EL"]["status"] == "bind"
        # DELL has empty orders on unheld Buy -> legacy
        assert result["per_ticker"]["DELL"]["status"] == "legacy"
        # Day verdict is still FAIL (has reasons), but per_ticker allows selective binding
        assert result["verdict"] == GATE_FAIL
        assert any("DELL" in r for r in result["reasons"])

    def test_empty_on_held_gets_bind_status(self, gate_cfg):
        """Empty orders on a held ticker = deliberate maintain -> bind."""
        _ratings_file(gate_cfg, {"HPE": "Overweight"}, {"HPE": {"orders": []}})
        result = evaluate(gate_cfg, gate_cfg["results_dir"], "2026-09-05",
                          holdings={"HPE": 13}, last_close={"HPE": 52.0})
        assert result["per_ticker"]["HPE"]["status"] == "bind"
        assert result["per_ticker"]["HPE"]["reason"] is None
