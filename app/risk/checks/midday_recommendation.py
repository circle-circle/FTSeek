#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiddayRecommendationCheck — 午间 LLM 推荐检查

职责:
  检查午间作战计划推荐标的，禁止与推荐方向相反的开仓

依赖:
  - BattlePlanManager (读取午间推荐)
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.midday_recommendation")


class MiddayRecommendationCheck(RiskCheck):
    """午间 LLM 推荐检查 — 盘前计划的重要补充"""

    @property
    def name(self) -> str:
        return "midday_recommendation"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        try:
            from app.pipeline.battle_plan import BattlePlanManager
            bpm = BattlePlanManager()
            rec = bpm.get_midday_recommendation(ctx.code)
        except Exception as e:
            # 2026-05-19: 午间推荐异常记录，辅助排查
            logger.debug(f"[午间推荐] 检查异常：{e}")
            return CheckResult(passed=True, check_name=self.name)

        if not rec:
            return CheckResult(passed=True, check_name=self.name)

        rec_upper = rec.upper()

        if rec_upper == "CLOSE":
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"午间 LLM 推荐 {ctx.code} 为 CLOSE (清仓)，禁止新开仓",
                reject_code="midday_close",
            )

        if rec_upper == "REDUCE":
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"午间 LLM 推荐 {ctx.code} 为 REDUCE (减仓)，禁止新增仓位",
                reject_code="midday_reduce",
            )

        # HOLD / ADD: 放行
        return CheckResult(passed=True, check_name=self.name)
