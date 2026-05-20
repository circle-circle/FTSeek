#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QualityCheck — 质量管理器检查 + 仓位调节

职责:
  1. 检查标的质量等级 (JUNK 过滤)
  2. 根据质量等级调整仓位 (A:1.3 / B:1.0 / C:0.7)

依赖:
  - IQualityProvider 或 subscriber._check_quality_and_valuation
"""

import logging
from typing import Dict

from app.strategy.base import SignalAction
from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.quality")


class QualityCheck(RiskCheck):
    """质量管理器检查 + 仓位调节"""

    @property
    def name(self) -> str:
        return "quality"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        # 优先使用 IQualityProvider, 回退 subscriber (Shadow)
        quality = deps.get("quality")
        subscriber = deps.get("subscriber")

        scorer = None
        if quality is not None:
            scorer = quality.get_quality_scorer()
        elif subscriber:
            scorer = getattr(subscriber, 'quality_scorer', None)

        if not scorer:
            return CheckResult(passed=True, check_name=self.name)

        if ctx.action != SignalAction.BUY:
            return CheckResult(passed=True, check_name=self.name)

        qr = None
        if quality is not None:
            qr = quality.check_quality(ctx.code, ctx.consensus_result.price)
        elif subscriber and hasattr(subscriber, '_check_quality_and_valuation'):
            qr = subscriber._check_quality_and_valuation(ctx.code, ctx.consensus_result.price)

        if qr is None or qr.get('reject'):
            reason = qr.get('reason', '未知原因') if qr else '检查失败'
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"质量检查拒绝：{reason}",
                reject_code="quality_rejected",
            )

        # 质量等级影响仓位
        quality_class = qr.get('quality_class', 'CLASS_C')
        if quality_class == 'CLASS_A':
            quality_boost = 1.3
        elif quality_class == 'CLASS_B':
            quality_boost = 1.0
        else:
            quality_boost = 0.7

        return CheckResult(
            passed=True,
            check_name=self.name,
            context_updates={"quality_boost": quality_boost},
        )
