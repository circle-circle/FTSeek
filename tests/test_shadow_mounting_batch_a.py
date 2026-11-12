"""Batch A 影子挂载收口：按需结算项不再触发悬空观察。"""
from __future__ import annotations

from app.playbook.shadow_registry import REGISTRY, check_mounting


def test_batch_a_on_demand_items_are_declared():
    items = {item.id: item for item in REGISTRY}
    assert items["budget_p3_sizing"].eval_on_demand is True
    assert items["confidence_tier_gate"].eval_on_demand is True


def test_batch_a_items_no_longer_dangle():
    warnings = check_mounting()
    assert not any("budget_p3_sizing" in warning for warning in warnings)
    assert not any("confidence_tier_gate" in warning for warning in warnings)
