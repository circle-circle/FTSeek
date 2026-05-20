#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LateSessionGateCheck — 尾盘门禁

职责:
  15:30 后禁止开新仓 (防止尾盘拥堵)

依赖:
  - pytz (时区处理)
"""

import logging
from datetime import datetime
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.late_session")


class LateSessionGateCheck(RiskCheck):
    """尾盘门禁 — 15:00 后禁止开新仓"""

    def __init__(self, cutoff_time: str = "15:30"):
        self.cutoff_time = cutoff_time

    @property
    def name(self) -> str:
        return "late_session_gate"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        try:
            import pytz
            from datetime import time as dtime
            now_hkt = datetime.now(pytz.timezone("Asia/Hong_Kong"))

            hour, minute = map(int, self.cutoff_time.split(":"))
            if now_hkt.time() >= dtime(hour, minute):
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=f"尾盘门禁：{now_hkt.strftime('%H:%M')} HKT >= {self.cutoff_time}，禁止开新仓",
                    reject_code="late_session_gate",
                )
        except Exception as e:
            # 2026-05-19: 尾盘门禁异常必须记录，防止静默绕过
            logger.warning(f"[尾盘门禁] 检查异常，降级放行：{e}")

        return CheckResult(passed=True, check_name=self.name)
