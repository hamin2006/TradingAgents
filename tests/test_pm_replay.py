"""pm_replay tests (hermetic): replay a pre-binding day's real PM payloads
through the binding engine (extractor + orders_from_execution) and compare
against what the legacy engine actually executed."""

import json

import pytest

from pm_replay import (
    derive_closes,
    infer_holdings,
    load_legacy_orders,
    replay_day,
)


def _ratings(dirpath, date_str, ratings):
    p = dirpath / f"ratings_{date_str}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": date_str, "ratings": ratings,
                             "failures": []}))
    return p


def _executed(dirpath, date_str, orders):
    p = dirpath / f"executed_{date_str}.json"
    p.write_text(json.dumps({"date": date_str, "orders": orders,
                             "paused": []}))
    return p


def _pm_event(args):
    return {"type": "llm_end", "agent": "Portfolio Manager",
            "tool_calls": [{"name": "PortfolioDecision", "args": args}]}


def _structured(dirpath, date_str, ticker, args):
    p = dirpath / "structured" / date_str / f"{ticker}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_pm_event(args)))
    return p


def _legacy_order(ticker, action, shares, stop=None, protection=None):
    o = {"ticker": ticker, "action": action, "shares": shares}
    if stop:
        o["stop_price"] = stop
    if protection:
        o["protection_price"] = protection
    return o


@pytest.fixture
def logs(tmp_path):
    root = tmp_path / "logs"
    root.mkdir()
    return root


class TestArtifacts:
    def test_derive_closes_from_legacy_buy_stops(self, logs):
        _executed(logs, "2026-09-04", [
            _legacy_order("HPE", "BUY", 13, stop=49.91),
            _legacy_order("EL", "SELL", 8),
        ])
        closes = derive_closes(logs, "2026-09-04", stop_loss_pct=8.0)
        assert abs(closes["HPE"] - 49.91 / 0.92) < 0.01
        assert "EL" not in closes

    def test_infer_holdings_from_sells(self, logs):
        _executed(logs, "2026-09-04", [
            _legacy_order("EL", "SELL", 8),
            _legacy_order("HPE", "BUY", 13),
        ])
        assert infer_holdings(logs, "2026-09-04") == {"EL": 8}

    def test_legacy_orders_by_ticker(self, logs):
        _executed(logs, "2026-09-04", [
            _legacy_order("EL", "SELL", 8),
            _legacy_order("HPE", "BUY", 13),
        ])
        orders = load_legacy_orders(logs, "2026-09-04")
        assert orders["EL"] == [{"ticker": "EL", "action": "SELL",
                                 "shares": 8}]


class TestReplayDay:
    def _day(self, logs):
        _ratings(logs, "2026-09-04", {"HPE": "Overweight", "EL": "Underweight"})
        _structured(logs, "2026-09-04", "HPE", {
            "rating": "Overweight",
            "executive_summary": "2% starter ~$200",
            "execution": {"orders": [
                {"kind": "BUY", "value_usd": 200.0, "stop_px": 45.7,
                 "cap_value_usd": 500.0}]}})
        _structured(logs, "2026-09-04", "EL", {
            "rating": "Underweight",
            "executive_summary": "trim 2 of 8",
            "execution": {"orders": [
                {"kind": "SELL", "shares": 2, "limit_px": 100.5,
                 "stop_px": 95.6}]}})
        _executed(logs, "2026-09-04", [
            _legacy_order("HPE", "BUY", 13, stop=49.91),
            _legacy_order("EL", "SELL", 8),
        ])

    def test_replays_blocks_and_reports(self, logs):
        self._day(logs)
        rows = replay_day(logs, "2026-09-04")
        by_ticker = {r["ticker"]: r for r in rows}
        hpe = by_ticker["HPE"]
        assert hpe["status"] == "present_valid"
        assert hpe["orders"] == [("BUY", 3)]          # $200 / ~54.25
        assert hpe["legacy"] == [("BUY", 13)]
        el = by_ticker["EL"]
        assert el["orders"] == [("SELL", 2)]
        assert el["legacy"] == [("SELL", 8)]

    def test_freetext_day_absent_no_orders(self, logs):
        _ratings(logs, "2026-09-04", {"REGN": "Buy"})
        _executed(logs, "2026-09-04", [])
        rows = replay_day(logs, "2026-09-04")
        assert rows == []

    def test_status_counts_roll_up(self, logs):
        self._day(logs)
        counts = {}
        replay_day(logs, "2026-09-04", counts=counts)
        assert counts.get("present_valid") == 2
        assert counts.get("absent", 0) == 0
