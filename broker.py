"""broker.py — factory selecting the active broker backend.

``cfg["broker"]`` picks the backend: ``"alpaca"`` (default — instant paper
account, no local daemon) or ``"ibkr"`` (IB Gateway, kept for when the
paper Gateway is set up). Both backends implement the same interface
(connect / get_positions_and_cash / place_market_orders / disconnect), so
swapping is a one-line config change.
"""

import logging

logger = logging.getLogger(__name__)


def create_broker(cfg: dict):
    """Return a broker instance for the configured backend (Alpaca default)."""
    backend = (cfg.get("broker") or "alpaca").strip().lower()
    if backend == "ibkr":
        from ibkr import IBKRBroker
        return IBKRBroker(cfg)
    if backend == "alpaca":
        from alpaca_broker import AlpacaBroker
        return AlpacaBroker(cfg)
    raise ValueError(f"Unknown broker backend: {backend!r} (use 'alpaca' or 'ibkr')")
