"""alpaca.py — broker backend backed by Alpaca's paper-trading API (alpaca-py).

Same interface as IBKRBroker (connect / get_positions_and_cash /
place_market_orders / disconnect) so daily_run.py stays broker-agnostic via
the factory in broker.py.

Execution semantics:
- Orders are placed for the regular session (extended_hours=False), so a
  market order submitted before 09:30 ET queues for the 09:30 open — the
  same behavior as the IBKR path.
- BUY orders carry a protection cap (limit order at protection_price): if
  the open gaps beyond the cap the order stays unfilled and is cancelled
  after the fill timeout — never overpaid, mirroring IBKR's MKT+auxPrice.
- Stop-losses attach TWO-STEP, not as an OTO bracket: Alpaca's paper engine
  inverts the OTO leg creation at the open (stop leg lands before the limit,
  no parent linkage) so the entry never activates — verified live 2026-09-01
  (IT and CRWD both unfilled). Submitting the plain capped entry first and
  the GTC stop only after the fill avoids the broken path entirely; the
  unprotected window is the poll interval (<=5s).
- SELL orders are plain market orders (clean exit, no cap).

Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY env vars (secrets never live
in watchlist.yaml). ``cfg["alpaca"]["paper"]`` defaults to True.
"""

import contextlib
import logging
import os
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from decisions import Order

logger = logging.getLogger(__name__)

FILL_TIMEOUT_S = 120
POLL_INTERVAL_S = 5
# The paper engine queues open-window orders and fills them with 30-70s
# latency (verified live 2026-09-03 REGN at +59s; 2026-09-04 EL/DASH/DXCM
# cancelled at +60s just before their fills landed). After the deadline,
# requery FILL_GRACE_REQUERIES more times, FILL_GRACE_INTERVAL_S apart,
# before giving up and cancelling — a cancel must never race a fill.
FILL_GRACE_REQUERIES = 3
FILL_GRACE_INTERVAL_S = 10


def _filled_qty(status) -> int:
    """Order.filled_qty is a str ('' until fills land). Only str values are
    real (test fakes without the attribute auto-create MagicMock children,
    whose __int__ lies and returns 1)."""
    val = getattr(status, "filled_qty", None)
    if not isinstance(val, str) or not val.strip():
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _filled_avg(status) -> float:
    val = getattr(status, "filled_avg_price", None)
    if not isinstance(val, str) or not val.strip():
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


class AlpacaBroker:
    def __init__(self, cfg: dict):
        alpaca_cfg = cfg.get("alpaca", {})
        self.paper = bool(alpaca_cfg.get("paper", True))
        self._client = None

    def _credentials(self) -> tuple[str, str]:
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise ConnectionError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY not set; add them to .env "
                "(paper keys: https://alpaca.markets -> Paper Trading -> API Keys)"
            )
        return api_key, secret_key

    def connect(self) -> None:
        if not self.paper:
            raise ConnectionError(
                "refusing to connect: alpaca.paper is false and this system "
                "only ever trades paper. Set alpaca.paper: true in watchlist.yaml."
            )
        try:
            api_key, secret_key = self._credentials()
            client = TradingClient(api_key, secret_key, paper=self.paper)
            client.get_account()  # validates the credentials
            self._client = client
            logger.info("connected to Alpaca (paper=%s)", self.paper)
        except ConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001 - any auth/network failure
            raise ConnectionError(f"Alpaca connection failed: {exc}") from exc

    def get_positions_and_cash(self) -> tuple[dict[str, int], float]:
        holdings: dict[str, int] = {}
        for pos in self._client.get_all_positions():
            qty = int(pos.qty)
            if qty:
                holdings[pos.symbol] = qty
        cash = 0.0
        try:
            cash = float(self._client.get_account().cash)
        except (TypeError, ValueError):
            cash = 0.0
        return holdings, cash

    def get_position_details(self) -> dict[str, dict]:
        """Per-position share counts and average entry prices.

        Optional interface addition (the base broker contract only requires
        ``get_positions_and_cash``): the portfolio-context injection uses avg
        entry cost to ground trim/add language, and gracefully degrades to
        shares-only when a backend does not provide it.
        """
        details: dict[str, dict] = {}
        for pos in self._client.get_all_positions():
            qty = int(pos.qty)
            if not qty:
                continue
            try:
                avg = (float(pos.avg_entry_price)
                       if getattr(pos, "avg_entry_price", None) else None)
            except (TypeError, ValueError):
                avg = None
            details[pos.symbol] = {"shares": qty, "avg_entry_price": avg}
        return details

    def place_market_orders(self, orders: list[Order], dry_run: bool = False) -> list[dict]:
        reports = []
        if dry_run:
            for o in orders:
                logger.info("DRY-RUN %s %s %d shares (protection %s)",
                            o.action, o.ticker, o.shares, o.protection_price)
                reports.append({"ticker": o.ticker, "action": o.action,
                                "shares": o.shares, "filled": 0, "avg_price": 0.0})
            return reports

        # Batch 1: submit all + concurrent poll + finalize. Orders cancelled
        # unfilled (filled==0, cancel SUCCEEDED — the order is provably dead)
        # get exactly ONE resubmission round: re-submitting the same cap
        # limit is self-guarding (it only fills while the price is inside
        # the limit), so the retry catches paper-engine latency-cancels and
        # cap-edge fades without ever chasing a gap beyond the protection.
        first_reports, retryable = self._place_batch(orders)
        results = {id(o): r for o, r in zip(orders, first_reports)}
        if retryable:
            logger.warning("retrying %d unfilled order(s) once: %s",
                           len(retryable),
                           ", ".join(o.ticker for o in retryable))
            second_reports, _ = self._place_batch(retryable)
            for o, r in zip(retryable, second_reports):
                results[id(o)] = r
        return [results[id(o)] for o in orders]

    def _place_batch(self, orders: list[Order]) -> tuple[list[dict], list[Order]]:
        """Submit + poll + finalize one batch of orders (no retries).

        Returns (reports in input order, orders to retry once): an order is
        retryable only when it was cancelled UNFILLED — never on gap-down
        undos (deliberate), partial sheds (kept what filled), submit
        failures (hard rejections), or failed cancels (fill status unknown,
        a resubmit could double the position).
        """
        reports = []
        retryable = []

        # Phase 1: submit EVERY order before polling any. Sequential
        # submit+poll per order let each full poll window delay the next
        # order (live 2026-09-04: NOW was submitted 4.5 minutes after EL);
        # submitting together queues them all at the open simultaneously.
        submissions = []  # (order, submitted | None, submit_error | None)
        for o in orders:
            side = OrderSide.BUY if o.action == "BUY" else OrderSide.SELL
            if o.action == "BUY" and o.protection_price:
                # Plain protection-capped limit entry — NO OTO bracket (the
                # paper engine inverts the pair at the open; see module doc).
                request = LimitOrderRequest(
                    symbol=o.ticker, qty=o.shares, side=side,
                    type=OrderType.LIMIT, limit_price=o.protection_price,
                    time_in_force=TimeInForce.DAY, extended_hours=False,
                )
            else:
                request = MarketOrderRequest(
                    symbol=o.ticker, qty=o.shares, side=side,
                    type=OrderType.MARKET,
                    time_in_force=TimeInForce.DAY, extended_hours=False,
                )
            try:
                submitted = self._client.submit_order(request)
                submissions.append((o, submitted, None))
            except Exception as exc:  # noqa: BLE001
                logger.error("order submission failed for %s: %s", o.ticker, exc)
                submissions.append((o, None, exc))

        # Phase 2: poll every outstanding order in one shared round-robin
        # loop. Wall time tracks the SLOWEST fill (paper engine: 30-70s),
        # not the sum of all fills.
        final = self._poll_all_concurrently(
            [(o, s) for o, s, _ in submissions if s is not None])

        # Phase 3: finalize each order (cancel/shed/gap-down/stop) in the
        # original order so reports keep the caller's sequence.
        for o, submitted, exc in submissions:
            if submitted is None:
                reports.append({"ticker": o.ticker, "action": o.action,
                                "shares": o.shares, "filled": 0, "avg_price": 0.0})
                continue
            status = final[submitted.id]
            filled = _filled_qty(status)
            avg_price = _filled_avg(status)
            try:
                if filled == 0:
                    # Only reached after the main window + grace requeries.
                    # A cancel exception means the cancel raced a late fill
                    # (the 2026-09-03/04 failure class): requery before
                    # giving up, so a filled order is never reported at 0
                    # and left without its stop.
                    try:
                        self._client.cancel_order_by_id(submitted.id)
                        logger.warning("order for %s not filled in %ds; cancelled",
                                       o.ticker, FILL_TIMEOUT_S)
                        retryable.append(o)
                    except Exception as cancel_exc:  # noqa: BLE001
                        with contextlib.suppress(Exception):
                            status = self._client.get_order_by_id(submitted.id)
                        filled = _filled_qty(status)
                        avg_price = _filled_avg(status)
                        if filled == 0:
                            logger.error("cancel failed for %s: %s "
                                         "(fill status unknown)", o.ticker, cancel_exc)
                else:
                    if filled < o.shares and status.status != "filled":
                        # Partial fill: shed the unfilled remainder so holdings
                        # and stop qty match (the DAY limit would otherwise
                        # keep working beyond the poll window as an unstopped
                        # add-on).
                        self._client.cancel_order_by_id(submitted.id)
                        logger.info("partial fill %d/%d for %s; remainder "
                                    "cancelled", filled, o.shares, o.ticker)
                    if o.action == "BUY" and o.stop_price and avg_price <= o.stop_price:
                        # Gap-down guard: the fill at/below the stop level (last
                        # close x 0.92) means the stock gapped through the stop at
                        # the open — the position would be dead on arrival (stop
                        # fires immediately at a guaranteed loss). Undo the entry
                        # with an immediate market sell; never attach the stop.
                        logger.warning(
                            "gap-down entry for %s: filled %.2f at/below stop %.2f; "
                            "undoing the position", o.ticker, avg_price, o.stop_price)
                        try:
                            undo = MarketOrderRequest(
                                symbol=o.ticker, qty=filled, side=OrderSide.SELL,
                                type=OrderType.MARKET,
                                time_in_force=TimeInForce.DAY, extended_hours=False,
                            )
                            self._client.submit_order(undo)
                            filled = 0
                        except Exception as exc:  # noqa: BLE001 - position left naked; log loudly
                            logger.error("gap-down undo SELL failed for %s: %s "
                                         "(position left without a stop!)", o.ticker, exc)
                            filled = 0
                    elif o.action == "BUY" and o.stop_price and filled > 0:
                        # Two-step: attach the GTC stop-loss only once the entry
                        # filled, so the position is protected 24/7 between runs.
                        # Sized to the FILLED qty — never the intended qty (a
                        # partial fill must not over-size the stop).
                        stop_request = StopOrderRequest(
                            symbol=o.ticker, qty=filled, side=OrderSide.SELL,
                            type=OrderType.STOP, stop_price=o.stop_price,
                            time_in_force=TimeInForce.GTC, extended_hours=False,
                        )
                        self._client.submit_order(stop_request)
                        logger.info("attached GTC stop %s for %s (%d shares)",
                                    o.stop_price, o.ticker, filled)
                    elif o.action == "SELL" and filled > 0:
                        self._cancel_open_stops(o.ticker)
            except Exception as exc:  # noqa: BLE001
                logger.error("order handling failed for %s: %s", o.ticker, exc)
            reports.append({"ticker": o.ticker, "action": o.action,
                            "shares": o.shares, "filled": filled,
                            "avg_price": round(float(avg_price), 4)})
        return reports, retryable

    def _poll_all_concurrently(self, submissions) -> dict[str, object]:
        """Poll every outstanding order together, round-robin, until each
        fills or the main window + grace requeries elapse.

        Submission order skew is sub-second, so one shared deadline from
        right after the last submit gives every order its full window.
        Wall time ~= latency of the slowest fill (~60-150s for the whole
        batch), not the sum. Never cancels here: returns the last known
        status per order id; the caller cancels/sheds/attaches afterwards.
        Per-round fetch errors (429 throttling etc.) keep the last known
        status and try again next round — Alpaca does not publish fixed
        limits; the contract is 429 + X-RateLimit headers, and sandbox
        limits are lower than production (worst case here: <=10 orders /
        5s round ≈ 120 req/min).
        """
        if not submissions:
            return {}
        by_id = {s.id: (o, s, s) for o, s in submissions}
        deadline = time.monotonic() + FILL_TIMEOUT_S

        def outstanding() -> dict:
            return {oid: v for oid, v in by_id.items()
                    if not (v[2].status == "filled"
                            or _filled_qty(v[2]) >= v[0].shares)}

        while outstanding() and time.monotonic() < deadline:
            for oid, (o, s, _last) in outstanding().items():
                try:
                    by_id[oid] = (o, s, self._client.get_order_by_id(oid))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fill poll failed for %s (%s); retrying next round",
                                   o.ticker, exc)
            time.sleep(POLL_INTERVAL_S)

        for _ in range(FILL_GRACE_REQUERIES):
            pend = outstanding()
            if not pend:
                break
            time.sleep(FILL_GRACE_INTERVAL_S)
            for oid, (o, s, _last) in pend.items():
                try:
                    by_id[oid] = (o, s, self._client.get_order_by_id(oid))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("grace fill poll failed for %s (%s)",
                                   o.ticker, exc)

        return {oid: last for oid, (_o, _s, last) in by_id.items()}

    def get_current_price(self, ticker: str) -> float | None:
        """Latest trade price, extended-hours sessions included (tripwire).

        A pre-market quote is the market's own aggregation of overnight
        events (news, guidance cuts, CEO deaths) long before any article
        reaches our feeds — run_execute's tripwire compares it against the
        reference close used for the morning's orders.
        """
        try:
            trade = self._client.get_last_trade(ticker)
            return float(trade.price)
        except Exception:  # noqa: BLE001 - a quote is best-effort, never blocking
            return None

    def cancel_stops_for(self, tickers: list[str]) -> None:
        """Cancel resting GTC stops for symbols being sold (exit guard).

        Called by the execute pass BEFORE the market opens: if a rating exit
        sells at the open while its stop is still resting, a gap through the
        stop level could fill BOTH orders at the auction (stop + market
        sell) and double-sell the position into an unintended short.
        """
        for ticker in tickers:
            self._cancel_open_stops(ticker)

    def _cancel_open_stops(self, symbol: str) -> None:
        """Cancel leftover stop orders for a symbol after a rating exit."""
        try:
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            for order in self._client.get_orders(request):
                if (order.symbol == symbol and order.type == "stop"):
                    self._client.cancel_order_by_id(order.id)
                    logger.info("cancelled leftover stop %s for %s", order.id, symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not cancel open stops for %s: %s", symbol, exc)

    def disconnect(self) -> None:
        pass  # stateless REST client; nothing to tear down
