"""盘前计划门禁检查 — 仅允许执行计划内信号"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any
import logging

logger = logging.getLogger("ftseek_us.risk.checks.battle_plan")


class BattlePlanCheck(RiskCheck):
    """
    盘前计划门禁
    
    仅允许执行以下信号:
    1. 计划内标的 (daily_battle_plan_us.json 中的 targets)
    2. 方向与计划一致
    3. 已持仓标的的加减仓
    """
    
    @property
    def name(self) -> str:
        return "BattlePlan"
    
    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        # 获取盘前计划管理器 (单例, 可直接实例化)
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        plan_mgr = context.get("battle_plan_manager") if context else None
        if not plan_mgr:
            plan_mgr = USBattlePlanManager()
            if not plan_mgr._loaded:
                plan_mgr.load(plan_mgr.plan_path)
        
        symbol = getattr(signal, 'symbol', '')
        direction = getattr(signal, 'direction', 'LONG')
        
        # P0 修复 (2026-05-20): HALT_TRADING 检测必须在已持仓 bypass 之前
        # 否则已持仓标的的加减仓会绕过 HALT 指令
        if hasattr(plan_mgr, 'is_trading_halted'):
            halted, halt_reason = plan_mgr.is_trading_halted()
            if halted:
                return self._reject(signal, halt_reason)
        
        # 已持仓标的: 允许加减仓 (HALT 通过后)
        position_store = context.get("position_store") if context else None
        if position_store and position_store.has(symbol):
            return self._pass(signal, f"已持仓标的 {symbol}，允许操作")
        
        # 检查盘前计划 (使用 check_trade 方法, 包含 restricted 列表检查)
        if hasattr(plan_mgr, 'check_trade'):
            allowed, reason = plan_mgr.check_trade(symbol, direction)
            if not allowed:
                return self._reject(signal, reason)
            return self._pass(signal, f"BattlePlan check passed: {reason}")
        
        return self._pass(signal, f"计划内标的 {symbol}")
