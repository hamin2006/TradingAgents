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
import json
import logging
import os
import time
from datetime import timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    OrderType,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
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

# Fallback bound when a submit failure does not carry the held_for_orders
# shape (an unrelated error, e.g. a network blip) — retry a small fixed
# number of times rather than burning the full deadline on a race that
# isn't the one this budget targets.
_UNRECOGNIZED_ERROR_ATTEMPTS = 3
_RETRY_POLL_INTERVAL_S = 2


def _held_for_orders(exc: Exception) -> int | None:
    """Parse Alpaca's held_for_orders count from a rejection payload.

    A just-cancelled or just-filled order can leave shares reserved in
    Alpaca's held_for_orders accounting for a few seconds (DXCM
    2026-09-09, recurring worse on HPQ/AMD/DELL/VLO/ZBRA 2026-09-15) —
    the message is JSON-shaped with the exact reservation count. Returns
    None when the shape is not recognized (a different failure entirely).
    """
    try:
        payload = json.loads(str(exc))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "held_for_orders" not in payload:
        return None
    try:
        return int(payload["held_for_orders"])
    except (TypeError, ValueError):
        return None


class _NothingToProtect(Exception):
    """Sentinel: the position emptied between retries — stop immediately,
    submitting nothing (would otherwise risk shorting the account)."""


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
        self._cfg = cfg
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
        self._naked_remainder_tickers: set[str] = set()
        first_reports, retryable = self._place_batch(orders)
        results = {id(o): r for o, r in zip(orders, first_reports, strict=False)}
        if retryable:
            logger.warning("retrying %d unfilled order(s) once: %s",
                           len(retryable),
                           ", ".join(o.ticker for o in retryable))
            # The retry round is ALWAYS final: an order still unfilled after
            # it must not be re-anchored-then-resubmitted (stop + live order
            # could double-sell) nor left naked (its stop was disarmed).
            second_reports, _ = self._place_batch(retryable, final_round=True)
            for o, r in zip(retryable, second_reports, strict=False):
                results[id(o)] = r

        self._resweep_naked_sell_remainders(orders)
        return [results[id(o)] for o in orders]

    def _resweep_naked_sell_remainders(self, orders: list[Order]) -> None:
        """One last-resort protection pass after the whole batch resolves.

        2026-09-14/15: HPQ exhausted the remainder-stop retry deadline and
        stayed naked for hours (recoverable only by next morning's stop
        sweep or a manual fix). 2026-09-17: RVTY's fresh BUY-entry stop
        attach hit the identical class of same-symbol collision (a wash-
        trade rejection from a second, still-open BUY tranche on the same
        ticker) and is now tracked the same way. Other orders in the SAME
        batch cancelling or filling around the same time are a plausible
        contributor to the
        held_for_orders contention that caused the exhaustion — checking
        again after the whole batch has settled, rather than only inside
        each order's own finalize step, gives the race more real-world time
        to clear before falling back to tomorrow's sweep.

        Scope is deliberately narrow: only tickers where ``_place_batch``
        itself already decided protection was owed (passed every existing
        guard — not fill_unknown, not a pending retry) AND the in-batch
        attach attempt failed. This never re-derives protection intent from
        `stop_price` alone, so it can never attach a stop the batch's own
        fill_unknown / pending-retry guards deliberately withheld (double-
        sell risk).

        Failure-safe: a broker error here is logged and swallowed, never
        raised out of place_market_orders (the batch's own reports are
        unaffected).
        """
        naked = getattr(self, "_naked_remainder_tickers", set())
        if not naked:
            return
        try:
            resting = self.get_resting_protection()
        except Exception as exc:  # noqa: BLE001 — leave it for tomorrow's sweep
            logger.warning("resweep skipped (protection snapshot: %s)", exc)
            return
        stop_by_ticker = {o.ticker: o.stop_price for o in orders
                          if o.stop_price is not None}
        for ticker in sorted(naked):
            if resting.get(ticker):
                continue  # already protected (a later attempt succeeded)
            remain = self._position_qty(ticker)
            if remain < 1:
                continue  # flat; nothing to protect
            stop_price = stop_by_ticker.get(ticker)
            if stop_price is None:
                continue  # no anchor level on record; nothing to resweep with
            logger.warning("%s: still unprotected after the batch — "
                           "one resweep attempt", ticker)
            if self._submit_remainder_stop(ticker, remain, stop_price):
                logger.info("resweep: re-anchored GTC stop for %s", ticker)

    def _place_batch(self, orders: list[Order],
                     final_round: bool = False) -> tuple[list[dict], list[Order]]:
        """Submit + poll + finalize one batch of orders (no retries).

        Returns (reports in input order, orders to retry once): an order is
        retryable only when it was cancelled UNFILLED — never on gap-down
        undos (deliberate), partial sheds (kept what filled), submit
        failures (hard rejections), or failed cancels (fill status unknown,
        a resubmit could double the position).
        """
        reports = []
        retryable = []
        if not hasattr(self, "_naked_remainder_tickers"):
            self._naked_remainder_tickers = set()  # direct _place_batch use

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
            elif o.action == "SELL" and o.protection_price:
                # PM execution: SELL with a floor limit — fills only if the
                # auction print is at/above the floor; else day-expiry no-fill.
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
                # Hard submission rejection: no order exists and none is
                # retried — but a SELL's stop was already disarmed pre-open
                # and the position is intact. The real position query guards
                # the case where the sell actually landed despite the
                # exception (flat -> nothing to protect).
                if o.action == "SELL" and o.stop_price is not None:
                    if self._submit_remainder_stop(o.ticker, o.shares,
                                                   o.stop_price):
                        logger.error(
                            "submit failed for %s (%s); re-anchored GTC stop "
                            "%.2f for the intact position",
                            o.ticker, exc, o.stop_price)
                    else:
                        self._naked_remainder_tickers.add(o.ticker)
                reports.append({"ticker": o.ticker, "action": o.action,
                                "shares": o.shares, "filled": 0, "avg_price": 0.0})
                continue
            status = final[submitted.id]
            filled = _filled_qty(status)
            avg_price = _filled_avg(status)
            cancelled_dead = False   # definitively dead this round
            fill_unknown = False     # cancel raced a possible late fill
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
                        cancelled_dead = True
                        if not final_round:
                            retryable.append(o)
                    except Exception as cancel_exc:  # noqa: BLE001
                        with contextlib.suppress(Exception):
                            status = self._client.get_order_by_id(submitted.id)
                        filled = _filled_qty(status)
                        avg_price = _filled_avg(status)
                        if filled == 0:
                            fill_unknown = True
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
                        # partial fill must not over-size the stop). Routed
                        # through the shared held_for_orders-aware retry:
                        # 2026-09-17 live, RVTY's stop attach was rejected
                        # "potential wash trade detected... opposite side
                        # limit order exists" because a second, still-open
                        # same-symbol BUY tranche was resting concurrently in
                        # the same batch — the old unguarded single submit
                        # left the fresh position naked for the rest of the
                        # day with no resweep coverage (the outer per-order
                        # exception handler swallowed it silently).
                        stop_qty = filled
                        stop_symbol = o.ticker
                        stop_price = o.stop_price

                        def submit_stop(symbol=stop_symbol, qty=stop_qty,
                                        price=stop_price):
                            self._client.submit_order(StopOrderRequest(
                                symbol=symbol, qty=qty, side=OrderSide.SELL,
                                type=OrderType.STOP, stop_price=price,
                                time_in_force=TimeInForce.GTC, extended_hours=False,
                            ))
                            return True

                        result = self._retry_until_available(
                            stop_symbol, submit_stop,
                            self._remainder_retry_deadline_s())
                        if result:
                            logger.info("attached GTC stop %s for %s (%d shares)",
                                        o.stop_price, o.ticker, filled)
                        else:
                            logger.error(
                                "entry stop for %s NOT attached after retries "
                                "— position unprotected", o.ticker)
                            self._naked_remainder_tickers.add(o.ticker)

                # Sell-resume completion: one bounded resume of the
                # remainder so the PM's stated quantity is actually
                # completed (exits are never paused). Gated to orders that
                # are DEFINITIVELY settled: never after a fill_unknown
                # cancel (the original may still fill — resuming would
                # double-sell), first-round partials here, final-round
                # deaths after their retry resolved. Floor-limit sells are
                # never resumed as market (the PM's price intent governs;
                # the re-anchor below protects the remainder). The real
                # position query clamps the resume size (never shorts).
                if (o.action == "SELL" and o.protection_price is None
                        and not fill_unknown and filled < o.shares
                        and (filled > 0 or final_round)):
                    real_qty = self._position_qty(o.ticker)
                    resume_qty = min(o.shares - filled, real_qty)
                    if resume_qty < 1:
                        logger.info(
                            "sell resume for %s skipped: position %d does "
                            "not cover the remaining %d", o.ticker,
                            real_qty, o.shares - filled)
                    else:
                        try:
                            resume_order = Order(ticker=o.ticker,
                                                 action="SELL",
                                                 shares=resume_qty,
                                                 reason="sell-resume")
                            submitted_resume = self._client.submit_order(
                                MarketOrderRequest(
                                    symbol=o.ticker, qty=resume_qty,
                                    side=OrderSide.SELL,
                                    type=OrderType.MARKET,
                                    time_in_force=TimeInForce.DAY,
                                    extended_hours=False,
                                ))
                            resume_status = self._poll_all_concurrently(
                                [(resume_order, submitted_resume)])
                            resume_filled = _filled_qty(
                                resume_status[submitted_resume.id])
                            resume_avg = _filled_avg(
                                resume_status[submitted_resume.id])
                            if resume_filled > 0:
                                orig_qty = filled
                                filled += resume_filled
                                avg_price = (
                                    float(resume_avg) if orig_qty == 0
                                    else round((orig_qty * avg_price
                                                + resume_filled * resume_avg)
                                               / filled, 4))
                                logger.info(
                                    "sell resume filled %d for %s @ %.2f "
                                    "(total %d/%d)", resume_filled,
                                    o.ticker, resume_avg, filled, o.shares)
                            else:
                                logger.warning(
                                    "sell resume for %s not filled; %d of "
                                    "%d still held", o.ticker,
                                    o.shares - filled, o.shares)
                        except Exception as exc:  # noqa: BLE001
                            logger.error("sell resume failed for %s: %s",
                                         o.ticker, exc)

                # Sell protection: a partial-sell remainder (or a sell that
                # never filled and is now definitively dead) must be
                # re-anchored — the pre-open disarm already cancelled the
                # original stop, so skipping this would leave the position
                # naked between runs. Covers PM partials AND legacy full
                # exits (daily_run anchors every sell's remainder level).
                # Never attach while a retry is pending (stop + live retry
                # could double-sell) or when the fill status is unknown
                # (the order may still fill today).
                if (o.action == "SELL" and not fill_unknown
                        and (filled > 0 or (cancelled_dead and final_round))):
                    # Leftover-stop cleanup (full exits keep their belt-and-
                    # braces cleanup; never cancels the fresh stop below).
                    self._cancel_open_stops(o.ticker)
                    if o.stop_price is not None:
                        if self._submit_remainder_stop(o.ticker, o.shares,
                                                        o.stop_price):
                            logger.info(
                                "re-anchored GTC stop %s for %s remainder",
                                o.stop_price, o.ticker)
                        else:
                            self._naked_remainder_tickers.add(o.ticker)
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

    def _position_qty(self, symbol: str) -> int:
        """Current share count for a symbol (0 when flat/unknown)."""
        try:
            for pos in self._client.get_all_positions():
                if pos.symbol == symbol:
                    return int(pos.qty)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not fetch position qty for %s: %s",
                           symbol, exc)
        return 0

    @staticmethod
    def _order_value(order, name: str) -> str:
        """Normalized Alpaca enum/string field for real models and test fakes."""
        value = getattr(order, name, None)
        value = getattr(value, "value", value)
        return str(value or "").lower()

    @staticmethod
    def _order_float(order, name: str) -> float:
        try:
            return float(getattr(order, name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _order_qty(order) -> int:
        try:
            return int(float(getattr(order, "qty", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def get_resting_protection(self) -> dict[str, list[dict]]:
        """Nested open-order snapshot of standalone stops and OCO parents.

        Alpaca's flat query omits an OCO's held STOP child.  Consumers use
        this single nested snapshot for sweep, disarm, and post-fill OCO
        reconciliation so that one position is never protected twice.
        """
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True,
                                   limit=500)
        protection: dict[str, list[dict]] = {}
        for order in self._client.get_orders(request):
            kind = self._order_value(order, "order_class")
            order_type = self._order_value(order, "type")
            symbol = str(getattr(order, "symbol", "") or "")
            if not symbol:
                continue
            if kind == "oco":
                leg = next((candidate for candidate in
                            (getattr(order, "legs", None) or [])
                            if self._order_value(candidate, "type") == "stop"),
                           None)
                if leg is None:
                    logger.warning("open OCO %s for %s has no stop leg",
                                   getattr(order, "id", "?"), symbol)
                    continue
                target = self._order_float(order, "limit_price")
                stop = self._order_float(leg, "stop_price")
                qty = self._order_qty(order) or self._order_qty(leg)
                if not target or not stop or qty < 1:
                    logger.warning("open OCO %s for %s has invalid protection data",
                                   getattr(order, "id", "?"), symbol)
                    continue
                protection.setdefault(symbol, []).append({
                    "kind": "oco", "order_id": str(order.id), "qty": qty,
                    "target_price": target, "stop_price": stop,
                })
            elif order_type == "stop":
                stop = self._order_float(order, "stop_price")
                qty = self._order_qty(order)
                if stop and qty > 0:
                    protection.setdefault(symbol, []).append({
                        "kind": "stop", "order_id": str(order.id), "qty": qty,
                        "stop_price": stop,
                    })
        return protection

    def cancel_protection(self, order_id: str) -> None:
        """Cancel one standalone stop or OCO parent (parent cascades legs)."""
        self._client.cancel_order_by_id(order_id)

    def cancel_protection_for(self, tickers: list[str]) -> dict[str, list[dict]]:
        """Disarm protection for open-window exits and return stop anchors."""
        cancelled: dict[str, list[dict]] = {}
        resting = self.get_resting_protection()
        for ticker in tickers:
            for protection in resting.get(ticker, []):
                try:
                    self.cancel_protection(protection["order_id"])
                except Exception as exc:  # noqa: BLE001 — do not claim disarm
                    logger.warning("could not cancel %s protection %s: %s",
                                   ticker, protection["order_id"], exc)
                    continue
                logger.info("cancelled %s protection %s for %s",
                            protection["kind"], protection["order_id"], ticker)
                cancelled.setdefault(ticker, []).append({
                    "stop_price": protection["stop_price"],
                    "qty": protection["qty"],
                })
        return cancelled

    def _retry_until_available(self, symbol: str, submit_fn, deadline_s: float):
        """Retry a submit callable through Alpaca's held_for_orders race.

        2026-09-15: HPQ/AMD/DELL/VLO/ZBRA all hit held_for_orders rejections
        in one run; the old fixed 3-attempt/6s budget exhausted on HPQ three
        separate times, leaving it naked for hours. This retries against a
        wall-clock deadline instead of a fixed count — the rejection payload
        already carries the exact reservation count, so a recognized race
        keeps retrying as long as time remains, closing the same-day gap
        the old budget could not. An unrecognized error (not this race)
        falls back to a small fixed number of attempts instead of burning
        the full deadline on an unrelated failure.

        Returns the submit_fn result, or None if the deadline/fallback
        budget is exhausted. Never raises for a recognized or unrecognized
        submit failure; the caller decides how to log/react.
        """
        deadline: float | None = None
        attempt = 0
        while True:
            attempt += 1
            try:
                return submit_fn()
            except _NothingToProtect:
                raise  # propagate immediately, never retried
            except Exception as exc:  # noqa: BLE001 — race-classified below
                held = _held_for_orders(exc)
                if held is None:
                    logger.warning("%s attempt %d failed (unrecognized): %s",
                                   symbol, attempt, exc)
                    if attempt >= _UNRECOGNIZED_ERROR_ATTEMPTS:
                        return None
                    time.sleep(_RETRY_POLL_INTERVAL_S)
                    continue
                logger.warning("%s attempt %d failed (held_for_orders=%d): %s",
                               symbol, attempt, held, exc)
                # Deadline clock starts on the first failure, not before the
                # first attempt — an immediately-successful submit (the
                # common case) never touches the clock.
                if deadline is None:
                    deadline = time.monotonic() + deadline_s
                if time.monotonic() >= deadline:
                    return None
                time.sleep(_RETRY_POLL_INTERVAL_S)

    def place_stop(self, symbol: str, qty: int, stop_px: float) -> bool:
        """Place a GTC stop with the existing position-lag retry guard."""
        return self._submit_remainder_stop(symbol, qty, stop_px)

    def place_oco(self, symbol: str, qty: int, stop_px: float,
                  target_px: float) -> str:
        """Place a GTC sell OCO, sized from the live position on each retry."""
        def submit():
            remain = self._position_qty(symbol)
            if remain < 1:
                raise _NothingToProtect(symbol)
            submitted = self._client.submit_order(LimitOrderRequest(
                symbol=symbol, qty=remain, side=OrderSide.SELL,
                type=OrderType.LIMIT, limit_price=target_px,
                order_class=OrderClass.OCO,
                take_profit=TakeProfitRequest(limit_price=target_px),
                stop_loss=StopLossRequest(stop_price=stop_px),
                time_in_force=TimeInForce.GTC, extended_hours=False,
            ))
            return str(submitted.id)

        try:
            result = self._retry_until_available(
                symbol, submit, self._remainder_retry_deadline_s())
        except _NothingToProtect:
            raise RuntimeError(f"no position remains to protect for {symbol}") from None
        if result is None:
            raise RuntimeError(f"OCO for {symbol} failed after retries")
        return result

    def _remainder_retry_deadline_s(self) -> float:
        """Wall-clock budget for the held_for_orders retry (config
        remainder_protection_retry_s, default 30s — see spec
        2026-09-15-remainder-protection-retry-hardening-design.md)."""
        return float(self._cfg.get("remainder_protection_retry_s", 30.0))

    def _submit_remainder_stop(self, symbol: str, qty: int,
                               stop_price: float) -> bool:
        """Submit the remainder GTC stop with a held_for_orders-aware retry.

        A just-cancelled sell can leave shares in Alpaca's held_for_orders
        accounting for some seconds (DXCM 2026-09-09; recurring worse on
        HPQ 2026-09-14/15, exhausting the old fixed 3-attempt/6s budget and
        leaving the position naked for hours) — and the position can change
        between tries. Re-query the position per attempt and size to it;
        retry through the shared deadline-based helper. Returns True when a
        stop is resting.
        """
        def submit():
            remain = self._position_qty(symbol)
            if remain < 1:
                raise _NothingToProtect(symbol)
            self._client.submit_order(StopOrderRequest(
                symbol=symbol, qty=remain, side=OrderSide.SELL,
                type=OrderType.STOP, stop_price=stop_price,
                time_in_force=TimeInForce.GTC, extended_hours=False,
            ))
            return True

        try:
            result = self._retry_until_available(
                symbol, submit, self._remainder_retry_deadline_s())
        except _NothingToProtect:
            return False  # nothing left to protect
        if result is None:
            logger.error("remainder stop for %s NOT placed after retries — "
                         "position unprotected", symbol)
            return False
        return True

    def cancel_stops_for(self, tickers: list[str]) -> dict[str, list[dict]]:
        """Backward-compatible name for the nested-aware exit disarm."""
        return self.cancel_protection_for(tickers)

    def _cancel_open_stops(self, symbol: str) -> list[dict]:
        """Backward-compatible private alias for nested-aware cleanup."""
        return self._cancel_open_protection(symbol)

    def _cancel_open_protection(self, symbol: str) -> list[dict]:
        """Cancel an open standalone stop or OCO parent for one symbol."""
        return self.cancel_protection_for([symbol]).get(symbol, [])

    def disconnect(self) -> None:
        pass  # stateless REST client; nothing to tear down

    def get_filled_stop_orders(self, since, until) -> list[dict]:
        """Broker-side stop fills with ``since <= filled_at < until``.

        Resting GTC stops are not engine orders, so execute-outcome rows
        cannot see their fills (ZBRA 2026-09-10: the stop sold the last
        share while the card said "1 remain"). The submitted window is
        widened because a stop can rest for weeks before it fires — the
        client-side filter is on FILL time, not submission time.
        Best-effort: returns [] on any error.
        """
        fills: list[dict] = []
        try:
            request = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, limit=500,
                after=since - timedelta(days=180))
            for order in self._client.get_orders(request):
                if order.type != "stop" or order.status != "filled":
                    continue
                filled_at = getattr(order, "filled_at", None)
                if filled_at is None or not (since <= filled_at < until):
                    continue
                try:
                    qty = int(float(order.qty))
                    avg_price = float(order.filled_avg_price)
                except (TypeError, ValueError):
                    continue
                fills.append({"symbol": order.symbol,
                              "side": str(getattr(order, "side", "sell")),
                              "qty": qty, "avg_price": avg_price,
                              "filled_at": filled_at.isoformat()})
        except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
            logger.warning("could not fetch filled stop orders: %s", exc)
        return fills

    def get_filled_exit_orders(self, since, until) -> list[dict]:
        """Nested broker exits, classified as a stop or OCO take-profit fill."""
        fills: list[dict] = []
        try:
            request = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, limit=500,
                after=since - timedelta(days=180), nested=True)

            def collect(order) -> None:
                order_type = self._order_value(order, "type")
                order_class = self._order_value(order, "order_class")
                status = self._order_value(order, "status")
                if order_type == "stop":
                    kind = "STOP"
                elif order_type == "limit" and order_class == "oco":
                    kind = "TP"
                else:
                    kind = None
                filled_at = getattr(order, "filled_at", None)
                if kind and status == "filled" and filled_at is not None \
                        and since <= filled_at < until:
                    qty = self._order_qty(order)
                    avg_price = self._order_float(order, "filled_avg_price")
                    symbol = str(getattr(order, "symbol", "") or "")
                    if symbol and qty > 0 and avg_price > 0:
                        fills.append({"symbol": symbol, "kind": kind,
                                      "qty": qty, "avg_price": avg_price,
                                      "filled_at": filled_at.isoformat()})
                for leg in getattr(order, "legs", None) or []:
                    collect(leg)

            for order in self._client.get_orders(request):
                collect(order)
        except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
            logger.warning("could not fetch filled exit orders: %s", exc)
        return fills
