#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MinHoldTimeCheck — 最小持仓时间保护

职责:
  防止过早平仓 (反向信号需等待最小持仓时间)

依赖:
  - IPositionQueryProvider 或 subscriber.position_manager
"""

import logging
from datetime import datetime
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult

logger = logging.getLogger("ftseek.risk.checks.min_hold_time")


class MinHoldTimeCheck(RiskCheck):
    """最小持仓时间保护"""

    @property
    def name(self) -> str:
        return "min_hold_time"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.trade_type != 'CLOSE':
            return CheckResult(passed=True, check_name=self.name)

        pos_query = deps.get("position_query")

        if pos_query is None:
            return CheckResult(passed=True, check_name=self.name)

        position = pos_query.get_position(ctx.code)

        if position is None:
            return CheckResult(passed=True, check_name=self.name)

        # 2026-05-11: 从统一配置读取最小持仓时间
        try:
            from config.config_center import get_config_center
            MIN_HOLD_MINUTES = get_config_center().get('risk_limits.min_hold_minutes', 30)
        except Exception:
            MIN_HOLD_MINUTES = 30
        try:
            entry_time_str = getattr(position, 'entry_time', None)
            if entry_time_str:
                if isinstance(entry_time_str, str):
                    et = datetime.fromisoformat(entry_time_str)
                else:
                    et = entry_time_str
                hold_minutes = ((datetime.now() - et).total_seconds()) / 60
                if hold_minutes < MIN_HOLD_MINUTES:
                    pnl_rate = (ctx.consensus_result.price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0
                    try:
                        from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                        stop_pct = abs(_dem.get_stop_loss())
                    except Exception:
                        stop_pct = 0.03
                    if pnl_rate > -stop_pct:
                        return CheckResult(
                            passed=False,
                            check_name=self.name,
                            reason=f"持仓仅 {hold_minutes:.1f}min < {MIN_HOLD_MINUTES}min，拒绝反向信号平仓",
                            reject_code="min_hold_time",
                        )
        except Exception as _e:
            logger.debug(f"[最小持仓保护] 检查异常：{_e}")

        return CheckResult(passed=True, check_name=self.name)
