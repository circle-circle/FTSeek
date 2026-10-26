# -*- coding: utf-8 -*-
"""今日决策摘要的作战计划口径 (2026-10-25 Dashboard UX).

旧计划冻结在 2026-07-31 后，targets 已被 stale 清空，但 position_actions 仍在
贡献 must_do，导致首屏同时出现「1 项必须执行」和「当前无需操作」。这里钉住：
计划一旦过期，所有摘要字段都必须归零，旧仓动作不得冒充今日待办。
"""
from app.web_dashboard.data_service import et_today
from app.web_dashboard.routes.dashboard import (
    _empty_plan_actions,
    _summarize_plan_actions,
)


def _plan(date: str) -> dict:
    return {
        "date": date,
        "market_regime": "NEUTRAL",
        "morning_brief": "test brief",
        "position_actions": [
            {"symbol": "US.BABA", "action": "REDUCE"},
            {"symbol": "US.SNDK", "action": "HOLD"},
        ],
        "targets": [
            {"symbol": "US.AMD", "confidence": 80},
            {"symbol": "US.RIVN", "confidence": 40},
            {"symbol": "US.META", "confidence": 90},
        ],
        "restricted": ["US.META"],
    }


def test_stale_plan_does_not_leak_position_actions_or_regime():
    actions = _summarize_plan_actions(_plan("2026-07-31"))
    assert actions == _empty_plan_actions()
    assert actions["must_do"] == 0
    assert actions["opportunities"] == 0
    assert actions["regime"] == "UNKNOWN"


def test_current_plan_keeps_existing_count_semantics():
    actions = _summarize_plan_actions(_plan(et_today()))
    assert actions == {
        "must_do": 1,
        "opportunities": 1,
        "regime": "NEUTRAL",
        "brief": "test brief",
    }


def test_non_dict_plan_falls_back_to_empty_actions():
    assert _summarize_plan_actions(None) == _empty_plan_actions()
