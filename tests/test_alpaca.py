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
    ticks = iter([100.0, 100.0, 100.0, 100.0, 250.0])  # last tick exceeds deadline (100+120)
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0)])
    mock_client.cancel_order_by_id.assert_called_once_with("order-1")
    assert reports[0]["filled"] == 0


def test_fill_landing_in_grace_window_is_not_cancelled(broker):
    """A fill that lands after the main deadline but inside the grace
    requeries must NOT be cancelled — the 2026-09-04 class: EL SELL and the
    DASH/DXCM entries were cancelled at +60s just before their fills landed
    (paper-engine open-window latency), EL left naked after its exit
    pre-disarmed the stop."""
    b, mock_client, _ = broker
    submitted = MagicMock()
    submitted.id = "order-1"
    submitted.status = "new"
    mock_client.submit_order.return_value = submitted
    statuses = [
        MagicMock(status="new", filled_qty="", filled_avg_price=""),   # deadline polls
        MagicMock(status="new", filled_qty="", filled_avg_price=""),
        MagicMock(status="filled", filled_qty="8", filled_avg_price="103.91"),  # grace
    ]
    mock_client.get_order_by_id.side_effect = statuses
    ticks = iter([100.0, 100.0, 100.0, 250.0])  # exits main window after 3 polls
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders(
            [Order(ticker="EL", action="SELL", shares=8, reason="rating exit")])
    mock_client.cancel_order_by_id.assert_not_called()
    assert reports[0]["filled"] == 8
    assert reports[0]["avg_price"] == 103.91


def test_grace_fill_on_buy_attaches_stop(broker):
    """A BUY whose fill lands in the grace window still gets its GTC stop
    attached, sized to the filled qty."""
    b, mock_client, _ = broker
    submitted = MagicMock()
    submitted.id = "order-1"
    submitted.status = "new"
    mock_client.submit_order.return_value = submitted
    statuses = [
        MagicMock(status="new", filled_qty="", filled_avg_price=""),   # deadline poll
        MagicMock(status="partially_filled", filled_qty="3", filled_avg_price="141.59"),
        MagicMock(status="partially_filled", filled_qty="3", filled_avg_price="141.59"),
        MagicMock(status="partially_filled", filled_qty="3", filled_avg_price="141.59"),
    ]
    mock_client.get_order_by_id.side_effect = statuses
    ticks = iter([100.0, 100.0, 250.0])  # exits main window after 1 poll
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders(
            [Order(ticker="NOW", action="BUY", shares=5, reason="entry",
                   protection_price=152.87, stop_price=133.94)])
    mock_client.cancel_order_by_id.assert_called_once_with("order-1")  # shed 2-share remainder
    stop_reqs = [c[0][0] for c in mock_client.submit_order.call_args_list
                 if c[0][0].type.value == "stop"]
    assert len(stop_reqs) == 1
    assert stop_reqs[0].qty == 3
    assert stop_reqs[0].stop_price == 133.94
    assert reports[0]["filled"] == 3


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


def test_buy_bracket_attaches_stop_after_fill(broker):
    """Two-step entry: plain protection-capped limit first; only after the
    fill does the GTC stop-loss get submitted. (OTO-at-open inverts the pair
    in Alpaca's paper engine and never fills — verified live 2026-09-01.)"""
    b, mock_client, _ = broker
    entry = MagicMock()
    entry.id = "order-1"
    entry.status = "filled"
    entry.filled_qty = "10"
    entry.filled_avg_price = "101.5"
    stop = MagicMock()
    stop.id = "stop-1"
    stop.status = "new"
    mock_client.submit_order.side_effect = [entry, stop]
    mock_client.get_order_by_id.return_value = entry
    with patch("alpaca_broker.time.sleep"):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0, stop_price=92.0)])
    calls = mock_client.submit_order.call_args_list
    assert len(calls) == 2
    entry_req = calls[0][0][0]
    assert entry_req.symbol == "AAPL"
    assert entry_req.limit_price == 102.0
    assert getattr(entry_req, "order_class", None) is None       # plain entry
    assert getattr(entry_req, "stop_loss", None) is None         # no OTO leg
    stop_req = calls[1][0][0]
    assert stop_req.symbol == "AAPL"
    assert stop_req.side.value == "sell"
    assert stop_req.type.value == "stop"
    assert stop_req.stop_price == 92.0
    assert stop_req.time_in_force.value == "gtc"                 # 24/7 protection
    assert reports[0] == {"ticker": "AAPL", "action": "BUY", "shares": 10,
                          "filled": 10, "avg_price": 101.5}


def test_unfilled_entry_does_not_attach_stop(broker):
    """If the entry never fills (gap beyond the cap / timeout), no stop-loss
    must be submitted — a stop for a position that does not exist would be
    rejected and logged as noise."""
    b, mock_client, _ = broker
    entry = MagicMock()
    entry.id = "order-1"
    entry.status = "new"
    mock_client.submit_order.return_value = entry
    mock_client.get_order_by_id.return_value = entry
    ticks = iter([100.0, 100.0, 100.0, 250.0])  # last tick exceeds deadline (100+120)
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0, stop_price=92.0)])
    assert mock_client.submit_order.call_count == 1  # entry only, no stop
    mock_client.cancel_order_by_id.assert_called_once_with("order-1")
    assert reports[0]["filled"] == 0


def test_gap_down_fill_below_stop_is_undone(broker):
    """A fill at or below the stop level (last close x 0.92) means the stock
    gapped through the stop at the open. The position would be dead on
    arrival (stop fires immediately at a loss), so the entry is undone with
    an immediate market sell and no stop is attached."""
    b, mock_client, _ = broker
    entry = MagicMock()
    entry.id = "order-1"
    entry.status = "filled"
    entry.filled_qty = "10"
    entry.filled_avg_price = "90.0"   # below stop 92.0: gapped through
    mock_client.submit_order.return_value = entry
    mock_client.get_order_by_id.return_value = entry
    with patch("alpaca_broker.time.sleep"):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0, stop_price=92.0)])
    calls = mock_client.submit_order.call_args_list
    assert len(calls) == 2            # entry + undo sell; NO stop order
    undo = calls[1][0][0]
    assert isinstance(undo, MarketOrderRequest)
    assert undo.side.value == "sell"
    assert undo.qty == 10
    assert not any("stop" in str(c[0][0].type.value) for c in calls)
    assert reports[0]["filled"] == 0  # position undone, nothing held


def test_gap_down_undo_failure_still_attaches_no_stop(broker):
    """If the undo sell itself fails, no stop is attached either — the
    position is left naked and logged loudly rather than being sold at the
    stop with a guaranteed loss."""
    b, mock_client, _ = broker
    entry = MagicMock()
    entry.id = "order-1"
    entry.status = "filled"
    entry.filled_qty = "10"
    entry.filled_avg_price = "90.0"
    mock_client.submit_order.side_effect = [entry, Exception("sell failed")]
    mock_client.get_order_by_id.return_value = entry
    with patch("alpaca_broker.time.sleep"):
        reports = b.place_market_orders(
            [Order(ticker="AAPL", action="BUY", shares=10, reason="entry",
                   protection_price=102.0, stop_price=92.0)])
    assert mock_client.submit_order.call_count == 2  # no stop attempted
    assert reports[0]["filled"] == 0


def test_partial_fill_counts_and_stops_filled_qty(broker):
    """2026-09-03 live: EL filled 8/9 in 25s but stayed `partially_filled`;
    the old poll (counting only a `filled` status) saw filled 0, cancelled at
    the deadline, and never attached the stop — 8 shares left naked. A
    partial fill must count, shed the unfilled remainder, and size the stop
    to what is actually held."""
    b, mock_client, _ = broker
    entry = MagicMock()
    entry.id = "order-1"
    entry.status = "partially_filled"
    entry.filled_qty = "8"
    entry.filled_avg_price = "102.0"
    stop = MagicMock()
    stop.id = "stop-1"
    stop.status = "new"
    mock_client.submit_order.side_effect = [entry, stop]
    mock_client.get_order_by_id.return_value = entry
    ticks = iter([100.0, 100.0, 100.0, 250.0])  # partial persists to deadline
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders(
            [Order(ticker="EL", action="BUY", shares=9, reason="entry",
                   protection_price=106.21, stop_price=93.06)])
    mock_client.cancel_order_by_id.assert_called_once_with("order-1")  # remainder
    calls = mock_client.submit_order.call_args_list
    assert len(calls) == 2            # entry + stop
    stop_req = calls[1][0][0]
    assert stop_req.qty == 8          # stop sizes to the held shares
    assert stop_req.side.value == "sell"
    assert stop_req.type.value == "stop"
    assert stop_req.stop_price == 93.06
    assert reports[0] == {"ticker": "EL", "action": "BUY", "shares": 9,
                          "filled": 8, "avg_price": 102.0}


def test_late_fill_caught_by_grace_check(broker):
    """2026-09-03 live: REGN filled at +59s — the last poll missed it and the
    cancel raced the fill (order left filled with no stop attached, log said
    0). The post-deadline grace query must see the late fill, skip the
    cancel, and attach the stop."""
    b, mock_client, _ = broker
    entry = MagicMock()
    entry.id = "order-1"
    entry.status = "new"
    late = MagicMock()
    late.id = "order-1"
    late.status = "filled"
    late.filled_qty = "1"
    late.filled_avg_price = "859.24"
    stop = MagicMock()
    stop.id = "stop-1"
    stop.status = "new"
    mock_client.submit_order.side_effect = [entry, stop]
    mock_client.get_order_by_id.side_effect = [entry, entry, entry, late]
    # deadline computation eats one monotonic tick, so 5 ticks = 3 polls + exit
    ticks = iter([100.0, 100.0, 100.0, 100.0, 250.0])
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders(
            [Order(ticker="REGN", action="BUY", shares=1, reason="entry",
                   protection_price=894.63, stop_price=783.87)])
    mock_client.cancel_order_by_id.assert_not_called()
    calls = mock_client.submit_order.call_args_list
    assert len(calls) == 2            # entry + stop; no cancel in between
    stop_req = calls[1][0][0]
    assert stop_req.qty == 1
    assert stop_req.stop_price == 783.87
    assert reports[0] == {"ticker": "REGN", "action": "BUY", "shares": 1,
                          "filled": 1, "avg_price": 859.24}


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


def test_cancel_stops_for_only_requests_listed_symbols(broker):
    """The pre-open exit guard: resting GTC stops on SELL-bound symbols must
    be cancelled BEFORE the open (a stop and a same-size market sell both
    live at the 09:30 auction could double-sell into a short). Other
    symbols' stops are untouched."""
    b, mock_client, _ = broker
    el_stop = MagicMock()
    el_stop.symbol = "EL"
    el_stop.type = "stop"
    el_stop.id = "stop-el"
    regn_stop = MagicMock()
    regn_stop.symbol = "REGN"
    regn_stop.type = "stop"
    regn_stop.id = "stop-regn"
    mock_client.get_orders.return_value = [el_stop, regn_stop]
    b.cancel_stops_for(["EL"])
    cancelled = [c.args[0] for c in mock_client.cancel_order_by_id.call_args_list]
    assert cancelled == ["stop-el"]


def test_multi_order_submitted_then_polled_concurrently(broker):
    """2026-09-04: sequential submit+poll per order let each full poll window
    delay the next — NOW was submitted 4.5 minutes after EL. All orders must
    be submitted FIRST, then polled round-robin in the same rounds, so wall
    time tracks the slowest fill, not the sum."""
    b, mock_client, _ = broker
    order_a = MagicMock()
    order_a.id = "ord-A"
    order_a.status = "new"
    order_b = MagicMock()
    order_b.id = "ord-B"
    order_b.status = "new"
    mock_client.submit_order.side_effect = [order_a, order_b]
    new_a = MagicMock(status="new", filled_qty="", filled_avg_price="")
    filled_a = MagicMock(status="filled", filled_qty="3",
                         filled_avg_price="214.85")
    new_b = MagicMock(status="new", filled_qty="", filled_avg_price="")
    seq = {"ord-A": [new_a, new_a, filled_a],
           "ord-B": [new_b, new_b, new_b, new_b, new_b, new_b]}
    mock_client.get_order_by_id.side_effect = lambda oid: seq[oid].pop(0)
    ticks = iter([100.0, 100.0, 100.0, 250.0])  # deadline 220; 2 main rounds
    with patch("alpaca_broker.time.sleep"), \
         patch("alpaca_broker.time.monotonic", side_effect=lambda: next(ticks)):
        reports = b.place_market_orders([
            Order(ticker="DASH", action="BUY", shares=3, reason="entry",
                  protection_price=233.1, stop_price=204.24),
            Order(ticker="DXCM", action="BUY", shares=9, reason="entry",
                  protection_price=94.2, stop_price=82.53)])

    # Both entries submitted before any polling began.
    submit_calls = [c[0][0].symbol for c in mock_client.submit_order.call_args_list]
    assert submit_calls[:2] == ["DASH", "DXCM"]

    # Round-robin: both order ids fetched within the same first round.
    poll_ids = [c.args[0] for c in mock_client.get_order_by_id.call_args_list]
    assert poll_ids[0] in ("ord-A", "ord-B")
    assert set(poll_ids[:2]) == {"ord-A", "ord-B"}

    # A filled at the grace requery -> kept + stop attached. B never filled
    # -> cancelled once (the grace requeries all ran before any cancel).
    stop_reqs = [c[0][0] for c in mock_client.submit_order.call_args_list
                 if c[0][0].type.value == "stop"]
    assert len(stop_reqs) == 1
    assert stop_reqs[0].qty == 3
    assert stop_reqs[0].stop_price == 204.24
    mock_client.cancel_order_by_id.assert_called_once_with("ord-B")
    assert reports[0] == {"ticker": "DASH", "action": "BUY", "shares": 3,
                          "filled": 3, "avg_price": 214.85}
    assert reports[1]["filled"] == 0
