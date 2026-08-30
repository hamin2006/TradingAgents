"""config.py — load watchlist.yaml and merge it over the framework defaults."""

import copy
from pathlib import Path

import yaml

from tradingagents.default_config import DEFAULT_CONFIG

DEFAULT_WATCHLIST_PATH = Path("watchlist.yaml")

_KNOWN_KEYS = frozenset(DEFAULT_CONFIG) | frozenset(
    ["seed_watchlist", "capital", "max_positions", "max_order_value_cap",
     "screener", "ibkr", "alpaca", "broker", "trading_enabled",
     "analyze_max_workers"]
)

# App-level defaults for keys the framework does not know about. These live
# here (not in tradingagents/default_config.py — that package is unmodifiable)
# so the config always carries them, exactly like the framework's own defaults.
APP_DEFAULTS = {
    "seed_watchlist": [],
    "capital": 100_000,
    "max_positions": 10,
    "max_order_value_cap": None,
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
        "exclusion_days": 7,
        "entry_protection_pct": 2.0,
    },
    # Alpaca: secrets come from ALPACA_API_KEY / ALPACA_SECRET_KEY env vars,
    # never from yaml. paper=True is the safe default; flip only with intent.
    "alpaca": {"paper": True},
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


def load_watchlist_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_WATCHLIST_PATH
    base = merge_over_default(DEFAULT_CONFIG, APP_DEFAULTS)
    if not path.exists():
        return base
    with open(path, encoding="utf-8") as f:
        overrides = yaml.safe_load(f) or {}
    unknown = set(overrides) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"Unknown watchlist.yaml keys: {sorted(unknown)}")
    return merge_over_default(base, overrides)
