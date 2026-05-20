#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SignalCooldownCheck — 信号冷却检查

职责:
  检查是否处于信号推送冷却期 (防止同一标的短时间内重复推送)

依赖:
  - ICooldownProvider 或 subscriber._open_position_ts
"""

import logging
import time
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.signal_cooldown")


class SignalCooldownCheck(RiskCheck):
    """信号冷却检查"""

    @property
    def name(self) -> str:
        return "signal_cooldown"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        cooldown = deps.get("cooldown")

        if cooldown is None:
            return CheckResult(passed=True, check_name=self.name)

        try:
            last_ts = cooldown.get_last_open_ts(ctx.code)

            if last_ts == 0:
                return CheckResult(passed=True, check_name=self.name)

            # 2026-05-16: 30min→10min，释放 38 次被误拦的高分信号
            min_hold_sec = 600
            elapsed = time.time() - last_ts
            if elapsed < min_hold_sec:
                remaining_min = int((min_hold_sec - elapsed) / 60)
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=f"{ctx.code} 冷却中：距上次开仓 {elapsed/60:.0f}min < {min_hold_sec/60:.0f}min (还需 {remaining_min}min)",
                    reject_code="signal_cooldown",
                )
        except Exception as e:
            logger.debug(f"[信号冷却] 检查异常：{e}")

        return CheckResult(passed=True, check_name=self.name)
