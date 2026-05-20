#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ConcentrationCheck — 持仓集中度检查

职责:
  1. 检查持仓数量是否超过上限
  2. 检查相关性折扣

依赖:
  - IPositionQueryProvider 或 subscriber.position_manager
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.concentration")


class ConcentrationCheck(RiskCheck):
    """持仓集中度检查"""

    @property
    def name(self) -> str:
        return "concentration"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        pos_query = deps.get("position_query")

        if pos_query is None:
            return CheckResult(passed=True, check_name=self.name)

        concentration_discount = 1.0
        try:
            open_positions = pos_query.get_all_positions()
            open_codes = {p.code for p in open_positions if p.code != ctx.code}
            num_open = len(open_codes)

            # 2026-05-13 胜率提升修复：最大并发持仓限制
            # 根因：近 30 天数据显示单日 8-12 笔交易全部亏损，过度分散降低胜率
            from config.config_center import get_config_center
            max_concurrent = get_config_center().get('risk_limits.max_positions', 3)
            if num_open >= max_concurrent:
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=f"持仓过于分散：已有 {num_open} 只持仓 (上限 {max_concurrent})，拒绝新开仓",
                    reject_code="max_concurrent_positions",
                )

            if num_open >= 2:
                concentration_discount = 0.7
                logger.info(
                    f"[相关性控制] {ctx.code} 当前已有 {num_open} 个其他标的持仓 "
                    f"({', '.join(sorted(open_codes))})，注入分散折扣 0.7x"
                )
        except Exception as _e:
            logger.debug(f"[相关性控制] 检查异常：{_e}")
            # 安全策略：无法确认持仓数时，保守施加分散折扣
            concentration_discount = 0.7

        return CheckResult(
            passed=True,
            check_name=self.name,
            context_updates={"concentration_discount": concentration_discount},
        )
