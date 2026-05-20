"""仓位限制检查 — 单股/总敞口/方向

P0-3 修复 (2026-04-29): 账户权益从 capital_safety / risk_engine 动态获取，
不再硬编码 $100K 默认值。无法获取权益时记录警告并放行（不基于错误数据拦截）。
"""

from app.risk.checks.base import RiskCheck, CheckResult
from typing import Dict, Any
import logging

logger = logging.getLogger("ftseek_us.risk.checks.position_limit")


def _resolve_equity(context: Dict[str, Any]) -> float:
    """从上下文中解析账户权益，优先 capital_safety → risk_engine → context，兜底 0"""
    # 1. capital_safety（最权威）
    risk_engine = context.get("risk_engine") if context else None
    if risk_engine:
        cs = getattr(risk_engine, "capital_safety", None)
        if cs and hasattr(cs, "_state") and cs._state.current_equity > 0:
            return float(cs._state.current_equity)
    # 2. context 显式传入
    eq = context.get("account_equity", 0) if context else 0
    if eq > 0:
        return float(eq)
    return 0.0


class PositionLimitCheck(RiskCheck):
    """检查仓位限制"""

    def __init__(self, max_per_symbol: float = 0.25, max_total: float = 0.80, max_per_direction: float = 0.50):
        self.max_per_symbol = max_per_symbol
        self.max_total = max_total
        self.max_per_direction = max_per_direction

    @property
    def name(self) -> str:
        return "PositionLimit"

    def check(self, signal, context: Dict[str, Any] = None) -> CheckResult:
        position_store = context.get("position_store") if context else None
        if not position_store:
            return self._pass(signal, "无持仓管理器，跳过仓位检查")

        try:
            positions = position_store.all()
            if not positions:
                return self._pass(signal, "无持仓")

            symbol = getattr(signal, 'symbol', '')
            direction = getattr(signal, 'direction', 'LONG')

            equity = _resolve_equity(context)
            if equity <= 0:
                logger.warning("[PositionLimit] 无法获取账户权益，跳过仓位检查（放行）")
                return self._pass(signal, "无法获取账户权益，跳过检查")

            # 单股检查
            symbol_positions = [p for p in positions if getattr(p, 'symbol', '') == symbol]
            if symbol_positions:
                total_symbol_size = sum(getattr(p, 'size', 0) * getattr(p, 'avg_entry_price', 0) for p in symbol_positions)
                if total_symbol_size / equity > self.max_per_symbol:
                    return self._reject(signal, f"单股敞口 {total_symbol_size/equity:.1%} > {self.max_per_symbol:.0%}")

            # 总敞口检查
            long_val = sum(getattr(p, 'size', 0) * getattr(p, 'avg_entry_price', 0) for p in positions if getattr(p, 'direction', '') == "LONG")
            short_val = sum(getattr(p, 'size', 0) * getattr(p, 'avg_entry_price', 0) for p in positions if getattr(p, 'direction', '') == "SHORT")
            total_exposure = (long_val + short_val) / equity
            if total_exposure > self.max_total:
                return self._reject(signal, f"总敞口 {total_exposure:.1%} > {self.max_total:.0%}")

            # 方向敞口
            dir_exposure = long_val / equity if direction == "LONG" else short_val / equity
            if dir_exposure > self.max_per_direction:
                return self._reject(signal, f"方向敞口 {dir_exposure:.1%} > {self.max_per_direction:.0%}")

            return self._pass(signal, f"long={long_val:.0f}, short={short_val:.0f}, equity={equity:.0f}")
        except Exception as e:
            logger.warning("[PositionLimit] 检查异常 (放行): %s", e)
            return self._pass(signal, f"检查异常: {e}")
