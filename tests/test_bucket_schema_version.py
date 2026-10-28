"""E1 bucket schema/version and zero-behaviour boundary tests (2026-10-28)."""
from pathlib import Path

import pytest
import yaml

from app.playbook.instruction_generator import (
    _compute_volatility_based_sl_tp,
    _vol_bucket_sl_bounds,
)


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "playbook_horizon.yaml"


def _config():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_bucket_schema_versions_and_low_deprecation_are_explicit():
    cfg = _config()
    assert cfg["short_horizon"]["bucket_schema_version"] == "short_v1"
    assert cfg["long_horizon"]["bucket_schema_version"] == "long_v1"
    assert cfg["short_horizon"]["vol_buckets"]["low"]["deprecated_unreachable"] is True
    assert cfg["long_horizon"]["vol_buckets"]["low"]["deprecated_unreachable"] is True
    # E1 preserves the bucket; deletion would change fallback behaviour.
    assert "low" in cfg["short_horizon"]["vol_buckets"]
    assert "low" in cfg["long_horizon"]["vol_buckets"]


@pytest.mark.parametrize(
    "atr_pct,direction,expected",
    [
        (0.012, "BUY", {"bucket": "low", "sl_pct": 2.0, "tp_multiplier": 2.0}),
        (0.020, "BUY", {"bucket": "medium", "sl_pct": 3.0, "tp_multiplier": 1.8}),
        (0.032, "BUY", {"bucket": "medium", "sl_pct": 4.5, "tp_multiplier": 1.8}),
        (0.055, "BUY", {"bucket": "high", "sl_pct": 5.0, "tp_multiplier": 1.5}),
        (0.090, "BUY", {"bucket": "extreme", "sl_pct": 5.0, "tp_multiplier": 1.5}),
        (0.032, "SELL_SHORT", {"bucket": "medium", "sl_pct": 4.8, "tp_multiplier": 1.8}),
    ],
)
def test_short_bucket_outputs_are_unchanged(atr_pct, direction, expected):
    cfg = _config()["short_horizon"]
    result = _compute_volatility_based_sl_tp(atr_pct, direction, cfg, None)
    assert result == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize(
    "atr_pct,direction,expected",
    [
        (0.012, "BUY", {"bucket": "low", "sl_pct": 1.5, "tp_multiplier": 2.5}),
        (0.020, "BUY", {"bucket": "medium", "sl_pct": 3.0, "tp_multiplier": 2.0}),
        (0.032, "BUY", {"bucket": "medium", "sl_pct": 4.8, "tp_multiplier": 2.0}),
        (0.055, "BUY", {"bucket": "high", "sl_pct": 5.0, "tp_multiplier": 1.8}),
    ],
)
def test_long_bucket_outputs_are_unchanged(atr_pct, direction, expected):
    cfg = _config()["long_horizon"]
    result = _compute_volatility_based_sl_tp(atr_pct, direction, cfg, None)
    assert result == pytest.approx(expected, abs=1e-9)


def test_global_bounds_do_not_change_after_annotation():
    cfg = _config()
    assert _vol_bucket_sl_bounds(cfg["short_horizon"], None) == pytest.approx(
        (2.0, 5.0, 0.8)
    )
    assert _vol_bucket_sl_bounds(cfg["long_horizon"], None) == pytest.approx(
        (1.5, 5.0, 0.5)
    )
