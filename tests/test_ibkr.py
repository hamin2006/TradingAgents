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
