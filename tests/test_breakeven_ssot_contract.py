"""BE 武装阈值显式兜底与 horizon 现役值一致性契约（DEB-20261111 Batch A）。"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.playbook.config import get_playbook_config

ROOT = Path(__file__).resolve().parents[1]


def test_breakeven_at_pct_explicit_and_matches_runtime_default():
    config = yaml.safe_load((ROOT / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
    params = config["playbook"]["execution_parameters"]
    assert params["breakeven_at_pct"] == 0.4
    assert get_playbook_config().get("breakeven_at_pct") == 0.4
    horizon = yaml.safe_load((ROOT / "config" / "playbook_horizon.yaml").read_text(encoding="utf-8"))
    assert horizon["factor_exit_rules"]["default"]["breakeven_at_pct"] == params["breakeven_at_pct"]
    for rule_name, rule in horizon["factor_exit_rules"].items():
        if isinstance(rule, dict) and "breakeven_at_pct" in rule:
            assert rule["breakeven_at_pct"] == params["breakeven_at_pct"], rule_name


def test_be_cushion_geometry_has_headroom():
    cfg = get_playbook_config()
    arm = float(cfg.get("breakeven_at_pct"))
    fee = float(cfg.get("breakeven_fee_cushion_pct", 0.15))
    slip = float(cfg.get("breakeven_slip_cushion_pct", 0.0))
    assert fee + slip < arm
