#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeakSignalCheck — 弱信号过滤

职责:
  过滤共识分数过低的信号

依赖:
  - ConfigCenter (读取 min_score 阈值)
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.weak_signal")


class WeakSignalCheck(RiskCheck):
    """弱信号过滤 — min_score 从 unified_config.yaml risk_limits.weak_signal_min_score 动态读取"""

    @property
    def name(self) -> str:
        return "weak_signal"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        # 从 ConfigCenter 动态读取阈值 (对齐 TrailingCooldownCheck/RiskRewardCheck 模式)
        from config.config_center import get_config_center
        min_score = get_config_center().get('risk_limits.weak_signal_min_score', 1.2)

        try:
            score_abs = abs(ctx.score)
        except Exception:
            # score 属性访问失败时放行，由外层 pipeline 的 except 兜底
            return CheckResult(passed=True, check_name=self.name)

        if score_abs < min_score:
            try:
                strategies = '+'.join(ctx.consensus_result.agree_strategies)
            except Exception:
                strategies = "unknown"
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"共识分数={ctx.score:.2f} < {min_score} (同向策略：{strategies})",
                reject_code="weak_signal",
            )

        return CheckResult(passed=True, check_name=self.name)
