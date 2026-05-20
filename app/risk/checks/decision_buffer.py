#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DecisionBufferCheck — 决策缓冲层审批检查

职责:
  检查信号是否通过决策缓冲层审批

依赖:
  - app.decision_buffer.DecisionBuffer
"""

import logging
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.decision_buffer")


class DecisionBufferCheck(RiskCheck):
    """决策缓冲层审批检查"""

    @property
    def name(self) -> str:
        return "decision_buffer"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        try:
            from app.decision_buffer import get_decision_buffer, DecisionStatus
            decision_buffer = get_decision_buffer()
            decision = decision_buffer.process_signal(ctx.consensus_result)

            if decision.status != DecisionStatus.APPROVED:
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=decision.rejection_reason,
                    reject_code="decision_buffer_rejected",
                )
            return CheckResult(passed=True, check_name=self.name)
        except Exception as e:
            # 2026-05-19: 决策缓冲层异常降级记录，不阻断（后续还有风控检查）
            logger.warning(f"[决策缓冲层] 检查异常，降级放行：{e}")
            return CheckResult(passed=True, check_name=self.name)
