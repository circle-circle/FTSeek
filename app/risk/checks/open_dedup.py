#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenDedupCheck — 开仓原子锁去重

职责:
  防止同一标的在短时间内重复开仓

依赖:
  - ICooldownProvider 或 subscriber._open_position_ts
"""

import logging
import time
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.open_dedup")


class OpenDedupCheck(RiskCheck):
    """开仓原子锁去重 — 防止同一标的在短时间内重复开仓"""

    @property
    def name(self) -> str:
        return "open_dedup"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        # 仅对开仓信号做去重检查
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        cooldown = deps.get("cooldown")

        if cooldown is None:
            # P0-8 加固：无 Provider → 警告通过（依赖 _on_consensus_inner 主去重）
            logger.warning(f"[开仓去重] Provider 未注入，依赖主路径去重：{ctx.code}")
            return CheckResult(passed=True, check_name=self.name)

        try:
            last_ts = cooldown.get_last_open_ts(ctx.code)

            if last_ts == 0:
                return CheckResult(passed=True, check_name=self.name)

            _open_cooldown_sec = 5.0  # 与 _on_consensus_inner 保持一致
            elapsed = time.time() - last_ts
            if elapsed < _open_cooldown_sec:
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=f"{ctx.code} {_open_cooldown_sec:.0f}s 内已开仓 (距上次 {elapsed:.1f}s)",
                    reject_code="open_dedup",
                )
        except Exception as e:
            # P0-8 加固：去重检查异常→拒绝（安全第一）
            logger.warning(f"[开仓去重] {ctx.code} 检查异常：{e}，拒绝开仓")
            return CheckResult(
                passed=False,
                check_name=self.name,
                reason=f"开仓去重检查异常：{e}",
                reject_code="open_dedup_error",
            )

        return CheckResult(passed=True, check_name=self.name)
