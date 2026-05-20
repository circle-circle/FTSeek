#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SymbolLossCooldownCheck — 连续亏损标的冷却

职责:
  标的连续亏损 2 次后 24h 禁止交易

依赖:
  - ICooldownProvider + IPositionQueryProvider
"""

import logging
import time
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.symbol_loss_cooldown")


class SymbolLossCooldownCheck(RiskCheck):
    """连续亏损标的冷却 — 标的连续亏损 2 次后 24h 禁止交易"""

    def __init__(self, consecutive_loss_limit: int = 2, cooldown_hours: int = 24):
        self.consecutive_loss_limit = consecutive_loss_limit
        self.cooldown_hours = cooldown_hours

    @property
    def name(self) -> str:
        return "symbol_loss_cooldown"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        cooldown = deps.get("cooldown")
        pos_query = deps.get("position_query")

        if cooldown is None:
            return CheckResult(passed=True, check_name=self.name)

        try:
            # 获取 position_manager._db
            pm = None
            if pos_query is not None and hasattr(pos_query, '_sub'):
                # adapter case: 从 subscriber 获取 pm
                pm = getattr(pos_query._sub, 'position_manager', None)
            elif pos_query is not None and hasattr(pos_query, 'get_all_positions'):
                # 尝试从 pos_query 获取 pm
                pm = getattr(pos_query, '_pm', None)

            if pm is None:
                return CheckResult(passed=True, check_name=self.name)

            db = getattr(pm, '_db', None)
            if db is None:
                return CheckResult(passed=True, check_name=self.name)

            cursor = db.cursor()
            cursor.execute(
                "SELECT pnl FROM trades WHERE code=? AND exit_price IS NOT NULL ORDER BY exit_time DESC LIMIT ?",
                (ctx.code, self.consecutive_loss_limit)
            )
            rows = cursor.fetchall()

            if len(rows) < self.consecutive_loss_limit:
                return CheckResult(passed=True, check_name=self.name)

            recent_pnls = [r[0] for r in rows]
            if all(pnl < 0 for pnl in recent_pnls):
                cooldown_until = cooldown.get_symbol_loss_cooldown_until(ctx.code)

                if time.time() < cooldown_until:
                    remaining_h = int((cooldown_until - time.time()) / 3600)
                    return CheckResult(
                        passed=False,
                        check_name=self.name,
                        reason=f"{ctx.code} 连续亏损冷却中：最近 {self.consecutive_loss_limit} 笔亏损 {recent_pnls}, 还需 {remaining_h}h",
                        reject_code="symbol_loss_cooldown",
                    )
                else:
                    # 写入冷却时间 (通过 adapter 不暴露写方法，依赖 subscriber 直接写)
                    # 这里只记录日志，实际写入由 data_subscriber 处理
                    logger.info(
                        f"[标的冷却] {ctx.code} 连续 {self.consecutive_loss_limit} 笔亏损 ({recent_pnls}), "
                        f"启动 {self.cooldown_hours}h 冷却"
                    )
        except Exception as e:
            logger.debug(f"[标的冷却] 检查异常：{e}")

        return CheckResult(passed=True, check_name=self.name)
