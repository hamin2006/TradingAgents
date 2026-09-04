"""config.py — load watchlist.yaml and merge it over the framework defaults."""

import copy
from pathlib import Path

import yaml

from tradingagents.default_config import DEFAULT_CONFIG

DEFAULT_WATCHLIST_PATH = Path("watchlist.yaml")

_KNOWN_KEYS = frozenset(DEFAULT_CONFIG) | frozenset(
    ["seed_watchlist", "capital", "max_positions", "max_order_value_cap",
     "screener", "ibkr", "alpaca", "broker", "trading_enabled",
     "analyze_max_workers", "stop_loss_pct", "conviction_weights",
     "openrouter_provider_pins", "fundamentals_source"]
)

# App-level defaults for keys the framework does not know about. These live
# here (not in tradingagents/default_config.py — that package is unmodifiable)
# so the config always carries them, exactly like the framework's own defaults.
APP_DEFAULTS = {
    "seed_watchlist": [],
    "capital": 100_000,
    "max_positions": 10,
    "max_order_value_cap": None,
    # Broker-side stop-loss attached to every buy (GTC, % below last close).
    # Enforced 24/7 by the broker between daily runs. 0 disables.
    "stop_loss_pct": 8.0,
    # Conviction-scaled sizing: slice multiplier per rating (base = capital /
    # max_positions). A Buy gets 1.5x an Overweight's exposure.
    "conviction_weights": {"Buy": 1.5, "Overweight": 1.0},
    # OpenRouter provider pinning: model slug -> provider name. Injects the
    # provider routing body (allow_fallbacks=false) into every request for
    # that model. Empty = OpenRouter's default routing.
    "openrouter_provider_pins": {},
    "trading_enabled": True,
    "broker": "alpaca",  # active backend: "alpaca" (default) or "ibkr"
    # Parallel ticker analyses in the 07:00 pass. The pipeline is IO-bound on
    # LLM calls, so threads scale ~linearly; keep this modest to respect
    # provider rate limits (4 = ~10 min per 10 tickers vs ~1.5h sequential).
    "analyze_max_workers": 4,
    "screener": {
        "universe": "sp500",
        "pool_size": 50,
        "candidate_slots": 3,
        "min_watchlist_size": 5,
        "exclusion_days": 3,
        "entry_protection_pct": 2.0,
        # Regime gate (5y backtest: the only defense that survived the 2022
        # crash in-sample). STRESS pauses new buys; WARN drops the 1m tail.
        "regime_gate": True,
        # Buy-quota expansion: if the base watchlist yields fewer agent-approved
        # buys (Buy/Overweight) than min_buy_quota, keep analyzing deeper pool
        # candidates until the quota is met or max_analyze tickers were analyzed
        # this run. 0 disables expansion (max_analyze 0 = no cap beyond the base
        # watchlist). Skipped entirely under STRESS (buys paused anyway).
        "min_buy_quota": 0,
        "max_analyze": 0,
    },
    # Alpaca: secrets come from ALPACA_API_KEY / ALPACA_SECRET_KEY env vars,
    # never from yaml. paper=True is the safe default; flip only with intent.
    "alpaca": {"paper": True},
    # Fundamentals source: "edgar" serves statements/metrics from SEC
    # companyfacts (as-filed, point-in-time) with consensus kept from Yahoo;
    # "yfinance" is the upstream vendor payload. EDGAR failures fall back to
    # the yfinance path automatically, so a flip is safe.
    "fundamentals_source": "yfinance",
    # IBKR (kept for the flip to a paper Gateway): host/port of the local
    # Gateway, not account credentials.
    "ibkr": {"host": "127.0.0.1", "port": 7497, "client_id": 1},
}


def merge_over_default(base: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(copy.deepcopy(value))
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _reject_duplicate_keys(node, path=""):
    """Recursively reject YAML mapping nodes with duplicate keys.

    PyYAML silently keeps the last duplicate (last-wins), which once shadowed
    a working OpenRouter provider pin with a broken one. Loud failure beats a
    silent override of a safety-relevant setting.
    """
    if not isinstance(node, yaml.MappingNode):
        return
    seen = set()
    for key_node, value_node in node.value:
        key = key_node.value
        full = f"{path}.{key}" if path else key
        if key in seen:
            raise ValueError(f"Duplicate key in watchlist.yaml: {full!r}")
        seen.add(key)
        _reject_duplicate_keys(value_node, full)


def load_watchlist_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_WATCHLIST_PATH
    base = merge_over_default(DEFAULT_CONFIG, APP_DEFAULTS)
    if not path.exists():
        return base
    with open(path, encoding="utf-8") as f:
        text = f.read()
    _reject_duplicate_keys(yaml.compose(text))
    overrides = yaml.safe_load(text) or {}
    unknown = set(overrides) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"Unknown watchlist.yaml keys: {sorted(unknown)}")
    return merge_over_default(base, overrides)
