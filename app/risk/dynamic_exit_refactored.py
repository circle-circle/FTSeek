"""
动态退出管理器 - 重构版本 (复杂函数优化)

优化内容:
1. check_exit_priority 函数从 135 行拆分为 10 个小函数
2. 每个函数<30 行，职责单一
3. 添加类型注解和文档字符串
"""
from typing import Optional, Dict, Any
from datetime import datetime
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """退出决策"""
    should_exit: bool
    reason: str = ""
    priority: int = 0
    close_ratio: float = 1.0
    is_stop_loss: bool = False


class ExitPriorityChecker:
    """退出优先级检查器"""
    
    def __init__(self, manager):
        """
        初始化检查器
        
        Args:
            manager: DynamicExitManager 实例
        """
        self.manager = manager
    
    def check(
        self,
        code: str,
        entry_price: float,
        current_price: float,
        peak_price: float,
        entry_time,
        atr_value: float,
        direction: str,
    ) -> ExitDecision:
        """
        组合式退出优先级检查 (主入口)
        
        按优先级依次判断各退出条件
        """
        if not self._validate_prices(entry_price, current_price):
            return ExitDecision(should_exit=False)
        
        pnl_pct = self._calculate_pnl(entry_price, current_price, direction)
        peak_pnl = self._calculate_peak_pnl(entry_price, peak_price, direction)
        
        # 按优先级检查退出条件
        checks = [
            (1, lambda: self._check_atr_stop(pnl_pct, atr_value, entry_price)),
            (2, lambda: self._check_structural_stop(pnl_pct)),
            (3, lambda: self._check_time_stop(entry_time)),
            (4, lambda: self._check_volatility_stop(pnl_pct, peak_price, current_price, atr_value, entry_price)),
            (5, lambda: self._check_trailing_stop(pnl_pct, peak_pnl, atr_value, entry_price)),
        ]
        
        for priority, check_func in checks:
            decision = check_func()
            if decision and decision.should_exit:
                decision.priority = priority
                return decision
        
        # 止盈检查 (优先级 6+)
        profit_decision = self._check_profit_exit(pnl_pct)
        if profit_decision:
            return profit_decision
        
        return ExitDecision(should_exit=False)
    
    def _validate_prices(self, entry_price: float, current_price: float) -> bool:
        """验证价格有效性"""
        if entry_price <= 0 or current_price <= 0:
            return False
        return True
    
    def _calculate_pnl(self, entry_price: float, current_price: float, direction: str) -> float:
        """计算盈亏百分比"""
        pnl_pct = (current_price - entry_price) / entry_price
        if direction == 'SELL':
            pnl_pct = -pnl_pct
        return pnl_pct
    
    def _calculate_peak_pnl(self, entry_price: float, peak_price: float, direction: str) -> float:
        """计算峰值盈亏"""
        if direction == 'BUY':
            return (peak_price - entry_price) / entry_price
        else:
            return (entry_price - peak_price) / entry_price
    
    def _check_atr_stop(
        self, 
        pnl_pct: float, 
        atr_value: float, 
        entry_price: float
    ) -> Optional[ExitDecision]:
        """1. ATR 硬止损检查"""
        if not (atr_value and atr_value > 0):
            return None
        
        atr_stop_dist = 1.5 * atr_value / entry_price
        if pnl_pct <= -atr_stop_dist:
            return ExitDecision(
                should_exit=True,
                reason=f"ATR 硬止损 (1.5x): 亏损={pnl_pct:.1%}",
                close_ratio=1.0,
                is_stop_loss=True
            )
        return None
    
    def _check_structural_stop(self, pnl_pct: float) -> Optional[ExitDecision]:
        """2. 结构止损检查"""
        struct_stop = abs(self.manager.get_stop_loss())
        if pnl_pct <= -struct_stop:
            return ExitDecision(
                should_exit=True,
                reason=f"结构止损：亏损={pnl_pct:.1%}",
                close_ratio=1.0,
                is_stop_loss=True
            )
        return None
    
    def _check_time_stop(self, entry_time) -> Optional[ExitDecision]:
        """3. 时间止损检查"""
        time_params = self.manager.get_time_stop_params()
        if not time_params.get('enabled', True) or not entry_time:
            return None
        
        try:
            if isinstance(entry_time, str):
                et = datetime.fromisoformat(entry_time)
            else:
                et = entry_time
            
            holding_hours = (datetime.now() - et).total_seconds() / 3600
            max_hours = time_params.get('max_holding_hours', 72)
            
            if holding_hours >= max_hours:
                return ExitDecision(
                    should_exit=True,
                    reason=f"时间止损：持仓{holding_hours:.1f}h >= {max_hours}h",
                    close_ratio=1.0,
                    is_stop_loss=False
                )
        except Exception:
            pass
        
        return None
    
    def _check_volatility_stop(
        self,
        pnl_pct: float,
        peak_price: float,
        current_price: float,
        atr_value: float,
        entry_price: float
    ) -> Optional[ExitDecision]:
        """4. 波动止损检查"""
        if not (atr_value and atr_value > 0):
            return None
        
        intraday_range = abs(peak_price - current_price) / entry_price
        atr_ratio = atr_value / entry_price
        
        if intraday_range > 3 * atr_ratio and pnl_pct < 0:
            return ExitDecision(
                should_exit=True,
                reason=f"波动止损：区间={intraday_range:.1%} > 3×ATR",
                close_ratio=1.0,
                is_stop_loss=True
            )
        return None
    
    def _check_trailing_stop(
        self,
        pnl_pct: float,
        peak_pnl: float,
        atr_value: float,
        entry_price: float
    ) -> Optional[ExitDecision]:
        """5. 移动止损检查"""
        trail_params = self.manager.get_trailing_stop_params()
        if not trail_params.get('enabled', True):
            return None
        
        trigger = trail_params.get('trigger_profit', 0.08)
        trail_pct = trail_params.get('trail_percent', 0.04)
        
        # ATR 移动止损优先，其次用百分比
        if atr_value and atr_value > 0:
            trail_dist = 2 * atr_value / entry_price
        else:
            trail_dist = trail_pct
        
        if pnl_pct >= trigger:
            drawdown_from_peak = peak_pnl - pnl_pct
            if drawdown_from_peak >= trail_dist:
                return ExitDecision(
                    should_exit=True,
                    reason=f"移动止损：从峰值回撤={drawdown_from_peak:.1%} >= {trail_dist:.1%}",
                    close_ratio=1.0,
                    is_stop_loss=False
                )
        
        return None
    
    def _check_profit_exit(self, pnl_pct: float) -> Optional[ExitDecision]:
        """6-8. 分批止盈 & 固定止盈检查"""
        # 分批止盈
        partial_levels = self.manager.get_partial_take_profit_levels()
        if partial_levels:
            for idx, level in enumerate(sorted(partial_levels, key=lambda x: x['target'])):
                target = level['target']
                ratio = level['ratio']
                if pnl_pct >= target:
                    return ExitDecision(
                        should_exit=True,
                        reason=f"分批止盈第{idx+1}目标：+{target:.1%}",
                        priority=6 + idx,
                        close_ratio=ratio,
                        is_stop_loss=False
                    )
        
        # 固定止盈（全平）
        take_profit = self.manager.get_take_profit()
        if pnl_pct >= take_profit:
            return ExitDecision(
                should_exit=True,
                reason=f"固定止盈：+{pnl_pct:.1%}",
                priority=8,
                close_ratio=1.0,
                is_stop_loss=False
            )
        
        return None


# 使用示例
def create_exit_checker(manager) -> ExitPriorityChecker:
    """创建退出检查器实例"""
    return ExitPriorityChecker(manager)
