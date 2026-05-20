#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CircuitBreakerCheck — 熔断器检查

职责:
  1. 检查是否处于熔断状态 (手动/自动)
  2. 返回仓位倍率 (position_multiplier)

依赖:
  - ICircuitBreakerProvider 或 subscriber.circuit_breaker
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.circuit_breaker")


class CircuitBreakerCheck(RiskCheck):
    """熔断器检查"""

    @property
    def name(self) -> str:
        return "circuit_breaker"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        cb_provider = deps.get("circuit_breaker")

        if cb_provider is None:
            return CheckResult(passed=True, check_name=self.name)

        # 1. 原有熔断器检查 (暂停状态)
        is_paused = cb_provider.is_paused()

        if is_paused:
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason="熔断器已触发，暂停交易",
                reject_code="circuit_breaker",
            )

        # 2. 智能熔断守卫 — 获取 position_multiplier (2026-05-11 修复)
        position_multiplier = 1.0
        try:
            equity = cb_provider.get_current_equity()

            # check() -> (allowed, reason, position_multiplier)
            allowed, reason, position_multiplier = cb_provider.check(
                current_equity=equity,
                signal_confidence=ctx.confidence,
            )

            if not allowed:
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=reason,
                    reject_code="circuit_breaker",
                )
        except Exception as e:
            # 2026-05-19: fail-close — 熔断器异常时拒绝交易，而非降级放行
            # 原因：熔断器是防止回撤螺旋的最后防线，异常放行 = 无保护
            logger.error(f"[CircuitBreakerCheck] 智能熔断检查异常，拒绝交易：{e}")
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"熔断检查异常，安全拒绝：{e}",
                reject_code="circuit_breaker_error",
            )

        if position_multiplier < 1.0:
            logger.info(f"[风控Pipeline] 智能熔断仓位调整：{position_multiplier:.0%}")
            return CheckResult(
                passed=True,
                check_name=self.name,
                context_updates={"position_multiplier": position_multiplier},
            )

        return CheckResult(passed=True, check_name=self.name)
