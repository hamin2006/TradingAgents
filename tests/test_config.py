"""tests/test_config.py"""
import pytest

from config import load_watchlist_config, merge_over_default
from tradingagents.default_config import DEFAULT_CONFIG


def test_merge_scalar_replaces():
    merged = merge_over_default({"a": 1, "b": 2}, {"a": 99})
    assert merged["a"] == 99
    assert merged["b"] == 2


def test_merge_dict_is_one_level_deep():
    base = {"nested": {"x": 1, "y": 2}, "keep": True}
    merged = merge_over_default(base, {"nested": {"y": 20}})
    assert merged["nested"] == {"x": 1, "y": 20}
    assert merged["keep"] is True


def test_merge_does_not_mutate_base():
    base = {"nested": {"x": 1}}
    merge_over_default(base, {"nested": {"y": 2}})
    assert base["nested"] == {"x": 1}
    assert "y" not in base["nested"]


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = load_watchlist_config(tmp_path / "nope.yaml")
    assert cfg["llm_provider"] == DEFAULT_CONFIG["llm_provider"]


def test_defaults_include_app_defaults(tmp_path):
    """App-level defaults live in config.py (the framework package is
    unmodifiable), so they must be present even with no yaml file."""
    cfg = load_watchlist_config(tmp_path / "nope.yaml")
    assert cfg["screener"]["pool_size"] == 50
    assert cfg["screener"]["min_watchlist_size"] == 5
    assert cfg["screener"]["entry_protection_pct"] == 2.0
    assert cfg["max_positions"] == 10
    assert cfg["capital"] == 100_000


def test_load_yaml_overrides_and_merges(tmp_path):
    yaml_path = tmp_path / "watchlist.yaml"
    yaml_path.write_text(
        "llm_provider: openrouter\n"
        "screener:\n"
        "  candidate_slots: 3\n"
        "  min_watchlist_size: 5\n"
    )
    cfg = load_watchlist_config(yaml_path)
    assert cfg["llm_provider"] == "openrouter"
    assert cfg["screener"]["candidate_slots"] == 3
    assert cfg["screener"]["pool_size"] == 50  # default kept from base


def test_unknown_yaml_key_rejected(tmp_path):
    yaml_path = tmp_path / "watchlist.yaml"
    yaml_path.write_text("nonsense_key: 1\n")
    with pytest.raises(ValueError):
        load_watchlist_config(yaml_path)
