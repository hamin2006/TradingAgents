"""tests/test_alpaca.py — AlpacaBroker tests with a mocked alpaca-py client."""

from unittest.mock import MagicMock, patch

import pytest
from alpaca.trading.requests import MarketOrderRequest

from alpaca_broker import AlpacaBroker
from decisions import Order


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    cfg = {"alpaca": {"paper": True}}
    with patch("alpaca_broker.TradingClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        b = AlpacaBroker(cfg)
        b._client = mock_client
        yield b, mock_client, mock_cls


def test_connect_constructs_paper_client(broker):
    b, mock_client, mock_cls = broker
    b.connect()
    mock_cls.assert_called_once_with("test-key", "test-secret", paper=True)
    mock_client.get_account.assert_called_once()


def test_connect_missing_key_raises(broker, monkeypatch):
    b, _, _ = broker
    monkeypatch.delenv("ALPACA_API_KEY")
    with pytest.raises(ConnectionError):
        b.connect()


def test_connect_bad_key_raises(broker):
    b, mock_client, _ = broker
    mock_client.get_account.side_effect = Exception("invalid key")
    with pytest.raises(ConnectionError):
        b.connect()


def test_get_positions_and_cash(broker):
    b, mock_client, _ = broker
    pos = MagicMock()
    pos.symbol = "AAPL"
    pos.qty = "10"
    mock_client.get_all_positions.return_value = [pos]
    mock_client.get_account.return_value = MagicMock(cash="12345.67")
    holdings, cash = b.get_positions_and_cash()
    assert holdings == {"AAPL": 10}
    assert cash == pytest.approx(12345.67)


def test_place_buy_uses_limit_at_protection_price(broker):
    b, mock_client, _ = broker
    submitted = MagicMock()
    submitted.id = "order-1"
    submitted.status = "filled"
    submitted.filled_qty = "10"
    submitted.filled_avg_price = "101.5"
    mock_client.submit_order.return_value = submitted
    mock_client.get_order_by_id.return_value = submitted
    with patch("alpaca_broker.time.sleep"):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0)])
    req = mock_client.submit_order.call_args[0][0]
    assert req.symbol == "AAPL"
    assert req.side.value == "buy"
    assert req.limit_price == 102.0
    assert reports[0] == {"ticker": "AAPL", "action": "BUY", "shares": 10,
                          "filled": 10, "avg_price": 101.5}


def test_place_sell_uses_market_order(broker):
    b, mock_client, _ = broker
    submitted = MagicMock()
    submitted.id = "order-1"
    submitted.status = "filled"
    submitted.filled_qty = "40"
    submitted.filled_avg_price = "245.0"
    mock_client.submit_order.return_value = submitted
    mock_client.get_order_by_id.return_value = submitted
    with patch("alpaca_broker.time.sleep"):
        reports = b.place_market_orders(
            [Order(ticker="TSLA", action="SELL", shares=40, reason="rating exit")])
    req = mock_client.submit_order.call_args[0][0]
    assert isinstance(req, MarketOrderRequest)
    assert req.side.value == "sell"
    assert req.type.value == "market"
    assert reports[0]["filled"] == 40


def test_unfilled_order_cancelled_after_timeout(broker):
    b, mock_client, _ = broker
    submitted = MagicMock()
    submitted.id = "order-1"
    submitted.status = "new"
    mock_client.submit_order.return_value = submitted
    mock_client.get_order_by_id.return_value = submitted
    ticks = iter([100.0, 100.0, 100.0, 161.0])  # last tick exceeds deadline (100+60)
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0)])
    mock_client.cancel_order_by_id.assert_called_once_with("order-1")
    assert reports[0]["filled"] == 0


def test_dry_run_touches_nothing(broker):
    b, mock_client, _ = broker
    reports = b.place_market_orders(
        [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
               protection_price=102.0)], dry_run=True)
    assert reports[0]["filled"] == 0
    mock_client.submit_order.assert_not_called()


def test_disconnect_is_noop(broker):
    b, _, _ = broker
    b.disconnect()  # must not raise


def test_live_mode_hard_rejected(monkeypatch):
    """paper=False would trade real money — must be refused outright."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    b = AlpacaBroker({"alpaca": {"paper": False}})
    with pytest.raises(ConnectionError):
        b.connect()


def test_buy_bracket_includes_stop_leg(broker):
    b, mock_client, _ = broker
    submitted = MagicMock()
    submitted.id = "order-1"
    submitted.status = "filled"
    submitted.filled_qty = "10"
    submitted.filled_avg_price = "101.5"
    mock_client.submit_order.return_value = submitted
    mock_client.get_order_by_id.return_value = submitted
    with patch("alpaca_broker.time.sleep"):
        b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0, stop_price=92.0)])
    req = mock_client.submit_order.call_args[0][0]
    assert req.order_class.value == "oto"
    assert req.stop_loss.stop_price == 92.0


def test_sell_cancels_open_stops_for_symbol(broker):
    b, mock_client, _ = broker
    open_stop = MagicMock()
    open_stop.symbol = "TSLA"
    open_stop.type = "stop"
    open_stop.id = "stop-1"
    mock_client.get_orders.return_value = [open_stop]
    submitted = MagicMock()
    submitted.id = "order-2"
    submitted.status = "filled"
    submitted.filled_qty = "40"
    submitted.filled_avg_price = "245.0"
    mock_client.submit_order.return_value = submitted
    mock_client.get_order_by_id.return_value = submitted
    with patch("alpaca_broker.time.sleep"):
        b.place_market_orders(
            [Order(ticker="TSLA", action="SELL", shares=40, reason="rating exit")])
    mock_client.cancel_order_by_id.assert_called_with("stop-1")
