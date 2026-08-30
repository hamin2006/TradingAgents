"""ibkr.py — thin wrapper over ib_async for the daily execution pass."""

import contextlib
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
            with contextlib.suppress(Exception):
                self._ib.disconnect()
