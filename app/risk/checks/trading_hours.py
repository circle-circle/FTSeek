#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingHoursCheck — 交易时段检查

职责:
  1. 检查是否在交易时段
  2. 检查是否在高质量交易时段 (可选)

依赖:
  - ITradingHoursProvider 或 subscriber._is_trading_hours
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.trading_hours")


class TradingHoursCheck(RiskCheck):
    """交易时段检查"""

    @property
    def name(self) -> str:
        return "trading_hours"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        hours = deps.get("trading_hours")

        if hours is None:
            return CheckResult(passed=True, check_name=self.name)

        is_trading = hours.is_trading_hours()
        is_hq = hours.is_high_quality_trading_hours()

        if not is_trading:
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason="非交易时间",
                reject_code="non_trading_hours",
            )

        if not is_hq:
            # 2026-05-16: 噪音时段不再直接拒绝，改为高分信号放行 + 低分降权
            # 根因：score=3.56 的最高分信号被 trading_hours 拦截 (15:45-16:00)
            # 逻辑：高分信号 (>2.5) 在噪音时段仍可开仓，低分信号 (<1.5) 拒绝
            score_abs = abs(ctx.score)
            if score_abs >= 2.5:
                # 高分信号：放行但记录警告
                logger.debug(
                    f"[风控Pipeline] [{ctx.trace_id}] {ctx.code} 噪音时段但 score={score_abs:.2f}>=2.5, 降级放行"
                )
                return CheckResult(passed=True, check_name=self.name)
            elif score_abs >= 1.5:
                # 中等分数：放行但注入折扣
                logger.debug(
                    f"[风控Pipeline] [{ctx.trace_id}] {ctx.code} 噪音时段但 score={score_abs:.2f}>=1.5, 注入质量折扣"
                )
                return CheckResult(
                    passed=True,
                    check_name=self.name,
                    context_updates={"noise_hours_discount": 0.8},
                )
            else:
                # 低分信号：噪音时段拒绝
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=f"开/收盘噪音时段且分数={score_abs:.2f}<1.5（信号质量差）",
                    reject_code="noise_hours",
                )

        return CheckResult(passed=True, check_name=self.name)
