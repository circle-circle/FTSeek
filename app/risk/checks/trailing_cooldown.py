#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TrailingCooldownCheck — 追踪止盈冷却期检查

职责:
  追踪止盈后冷却期，防止短期内重新开仓

依赖:
  - IExitStateProvider 或 subscriber._trailing_exit_time
"""

import logging
import time
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.trailing_cooldown")


class TrailingCooldownCheck(RiskCheck):
    """追踪止盈冷却期检查"""

    @property
    def name(self) -> str:
        return "trailing_cooldown"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        exit_state = deps.get("exit_state")

        if exit_state is None:
            return CheckResult(passed=True, check_name=self.name)

        trailing_exit_ts = exit_state.get_trailing_exit_time(ctx.code)

        elapsed = time.time() - trailing_exit_ts
        # 2026-05-11: 从统一配置读取冷却时间
        try:
            from config.config_center import get_config_center
            TRAILING_COOLDOWN_SEC = get_config_center().get('risk_limits.trailing_cooldown_sec', 900)
        except Exception:
            TRAILING_COOLDOWN_SEC = 15 * 60  # 15 分钟兜底

        if elapsed < TRAILING_COOLDOWN_SEC:
            remain_min = (TRAILING_COOLDOWN_SEC - elapsed) / 60
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"追踪止盈后冷却中，剩余 {remain_min:.1f}min",
                reject_code="trailing_cooldown",
            )

        return CheckResult(passed=True, check_name=self.name)
