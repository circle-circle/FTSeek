#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DailyTradeLimitCheck — 每日最大交易次数限制

职责:
  防止过度交易降低胜率

依赖:
  - IPositionQueryProvider 或 subscriber.position_manager
"""

import logging
from datetime import date
from typing import Dict

from app.risk.risk_pipeline import RiskCheck, SignalContext, CheckResult
from app.strategy.base import SignalAction

logger = logging.getLogger("ftseek.risk.checks.daily_trade_limit")


class DailyTradeLimitCheck(RiskCheck):
    """每日最大交易次数限制"""

    @property
    def name(self) -> str:
        return "daily_trade_limit"

    def check(self, ctx: SignalContext, deps: Dict) -> CheckResult:
        if ctx.action not in (SignalAction.BUY, SignalAction.SELL):
            return CheckResult(passed=True, check_name=self.name)

        try:
            # 从配置读取每日最大交易次数 (默认 4 次)
            from config.config_center import get_config_center
            cc = get_config_center()
            max_daily_trades = cc.get('risk_limits.max_daily_trades', 4)

            # 统计今日已开仓次数 (从 positions.db 查询)
            today_str = str(date.today())
            today_opened = 0
            try:
                import sqlite3
                db_path = '/opt/ftseek/FTSeek/data/paper_positions.db'
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM positions WHERE entry_time LIKE ?",
                    (f"{today_str}%",)
                )
                today_opened = cursor.fetchone()[0]
                conn.close()
            except Exception:
                # 降级：从 IPositionQueryProvider / subscriber 的 position_manager 统计
                pos_query = deps.get("position_query")
                pm = None
                if pos_query is not None and hasattr(pos_query, 'get_all_positions'):
                    pm = pos_query
                if pm:
                    try:
                        open_positions = pm.get_all_positions()
                        today_opened = len([p for p in open_positions
                                          if getattr(p, 'entry_time', '') and p.entry_time[:10] == today_str])
                    except Exception:
                        pass

            if today_opened >= max_daily_trades:
                return CheckResult(
                    passed=False,
                    check_name=self.name,
                    reason=f"每日交易次数已达上限：今日已开仓 {today_opened}/{max_daily_trades} 笔",
                    reject_code="daily_trade_limit",
                )
        except Exception as e:
            # 2026-05-19: 每日交易限制异常必须记录，防止静默绕过
            logger.warning(f"[每日交易限制] 检查异常，降级放行：{e}")

        return CheckResult(passed=True, check_name=self.name)
