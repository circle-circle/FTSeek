"""
风控执行模块
负责风控措施的执行，包括止损、止盈、清仓等
"""
import logging
from typing import Dict, Optional, Any, List
from datetime import datetime

from app.risk.metrics import RiskConfig, RiskLimit, StopLossType

logger = logging.getLogger("ftseek.risk.execution")
logger.propagate = False


class StopLossExecutor:
    """止损执行器"""
    
    def __init__(self, config: Optional[RiskConfig] = None):
        """
        初始化止损执行器
        
        Args:
            config: 风控配置
        """
        self.config = config or RiskConfig()
    
    def check_stop_loss(self, position: Any, current_price: float) -> tuple:
        """
        检查是否触发止损
        
        Args:
            position: 持仓对象
            current_price: 当前价格
        
        Returns:
            (triggered: bool, reason: str)
        """
        if not position:
            return False, ""
        
        entry_price = getattr(position, 'entry_price', 0)
        stop_loss_price = getattr(position, 'stop_loss', 0)
        action = getattr(position, 'action', 'BUY')
        
        if entry_price <= 0:
            return False, ""
        
        # 多头止损
        if action == 'BUY':
            if stop_loss_price > 0 and current_price <= stop_loss_price:
                pnl_rate = (current_price - entry_price) / entry_price
                return True, f"止损触发 (盈亏={pnl_rate:.2%})"
        
        # 空头止损
        elif action == 'SELL':
            if stop_loss_price > 0 and current_price >= stop_loss_price:
                pnl_rate = (entry_price - current_price) / entry_price
                return True, f"止损触发 (盈亏={pnl_rate:.2%})"
        
        return False, ""
    
    def update_trailing_stop(self, position: Any, current_price: float,
                            trailing_stop_cache: Dict) -> None:
        """
        更新追踪止损
        
        Args:
            position: 持仓对象
            current_price: 当前价格
            trailing_stop_cache: 追踪止损缓存
        """
        if not position:
            return
        
        code = getattr(position, 'code', '')
        entry_price = getattr(position, 'entry_price', 0)
        action = getattr(position, 'action', 'BUY')
        
        if entry_price <= 0:
            return
        
        # 计算盈利
        if action == 'BUY':
            pnl_rate = (current_price - entry_price) / entry_price
        else:
            pnl_rate = (entry_price - current_price) / entry_price
        
        # 盈利超过 2% 时激活追踪止损
        if pnl_rate >= 0.02:
            # 获取 ATR 百分比 (简化实现)
            atr_pct = 0.02
            trail_atr_mult = 2.0
            trail_gap = current_price * atr_pct * trail_atr_mult
            
            if action == 'BUY':
                new_trail = current_price - trail_gap * 1.5
                old_trail = trailing_stop_cache.get(code, 0.0)
                if new_trail > old_trail:
                    trailing_stop_cache[code] = new_trail
                    logger.debug(f"[追踪止损] {code} 上移至 {new_trail:.3f}")
            else:
                new_trail = current_price + trail_gap * 1.5
                old_trail = trailing_stop_cache.get(code, float('inf'))
                if new_trail < old_trail:
                    trailing_stop_cache[code] = new_trail
                    logger.debug(f"[追踪止损 - 空头] {code} 下移至 {new_trail:.3f}")


class TakeProfitExecutor:
    """止盈执行器"""
    
    def __init__(self, config: Optional[RiskConfig] = None):
        """
        初始化止盈执行器
        
        Args:
            config: 风控配置
        """
        self.config = config or RiskConfig()
    
    def check_take_profit(self, position: Any, current_price: float) -> tuple:
        """
        检查是否触发止盈
        
        Args:
            position: 持仓对象
            current_price: 当前价格
        
        Returns:
            (triggered: bool, reason: str)
        """
        if not position:
            return False, ""
        
        entry_price = getattr(position, 'entry_price', 0)
        take_profit_price = getattr(position, 'take_profit', 0)
        action = getattr(position, 'action', 'BUY')
        
        if entry_price <= 0:
            return False, ""
        
        # 多头止盈
        if action == 'BUY':
            if take_profit_price > 0 and current_price >= take_profit_price:
                pnl_rate = (current_price - entry_price) / entry_price
                return True, f"止盈触发 (盈亏={pnl_rate:.2%})"
        
        # 空头止盈
        elif action == 'SELL':
            if take_profit_price > 0 and current_price <= take_profit_price:
                pnl_rate = (entry_price - current_price) / entry_price
                return True, f"止盈触发 (盈亏={pnl_rate:.2%})"
        
        return False, ""
    
    def check_partial_take_profit(self, position: Any, current_price: float,
                                 peak_price: float) -> float:
        """
        检查是否触发分批止盈
        
        Args:
            position: 持仓对象
            current_price: 当前价格
            peak_price: 峰值价格
        
        Returns:
            平仓比例 (0 表示不触发)
        """
        if not position:
            return 0.0
        
        entry_price = getattr(position, 'entry_price', 0)
        action = getattr(position, 'action', 'BUY')
        
        if entry_price <= 0:
            return 0.0
        
        # 计算盈利
        if action == 'BUY':
            pnl_rate = (current_price - entry_price) / entry_price
            peak_pnl_rate = (peak_price - entry_price) / entry_price
        else:
            pnl_rate = (entry_price - current_price) / entry_price
            peak_pnl_rate = (entry_price - peak_price) / entry_price
        
        # 从峰值回撤超过 50% 时触发分批止盈
        if peak_pnl_rate > 0.03 and pnl_rate < peak_pnl_rate * 0.5:
            return 0.6  # 平仓 60%
        
        # 盈利超过 5% 时触发分批止盈
        if pnl_rate > 0.05:
            return 0.5  # 平仓 50%
        
        return 0.0


class BreakevenExecutor:
    """保本止损执行器"""
    
    def __init__(self, config: Optional[RiskConfig] = None):
        """
        初始化保本止损执行器
        
        Args:
            config: 风控配置
        """
        self.config = config or RiskConfig()
    
    def check_breakeven(self, position: Any, current_price: float) -> bool:
        """
        检查是否触发保本止损
        
        Args:
            position: 持仓对象
            current_price: 当前价格
        
        Returns:
            True: 触发保本止损
            False: 未触发
        """
        if not position:
            return False
        
        entry_price = getattr(position, 'entry_price', 0)
        action = getattr(position, 'action', 'BUY')
        
        if entry_price <= 0:
            return False
        
        # 计算盈利
        if action == 'BUY':
            pnl_rate = (current_price - entry_price) / entry_price
        else:
            pnl_rate = (entry_price - current_price) / entry_price
        
        # 盈利超过 2% 后，如果回落到成本价附近，触发保本止损
        if pnl_rate < 0.005 and pnl_rate > -0.005:
            return True
        
        return False
    
    def update_breakeven_stop(self, position: Any, current_price: float) -> Optional[float]:
        """
        更新保本止损价
        
        Args:
            position: 持仓对象
            current_price: 当前价格
        
        Returns:
            新的止损价 (如果更新)
        """
        if not position:
            return None
        
        entry_price = getattr(position, 'entry_price', 0)
        action = getattr(position, 'action', 'BUY')
        
        if entry_price <= 0:
            return None
        
        # 计算盈利
        if action == 'BUY':
            pnl_rate = (current_price - entry_price) / entry_price
        else:
            pnl_rate = (entry_price - current_price) / entry_price
        
        # 盈利超过 2% 时，将止损上移到成本价
        if pnl_rate >= 0.02:
            return entry_price
        
        return None


class PositionSizer:
    """仓位计算器"""
    
    def __init__(self, config: Optional[RiskConfig] = None):
        """
        初始化仓位计算器
        
        Args:
            config: 风控配置
        """
        self.config = config or RiskConfig()
    
    def calculate_position_size(self, total_capital: float, current_price: float,
                               stop_loss_pct: float, risk_per_trade: float = 0.02) -> int:
        """
        计算仓位大小
        
        Args:
            total_capital: 总资金
            current_price: 当前价格
            stop_loss_pct: 止损百分比
            risk_per_trade: 每笔交易风险
        
        Returns:
            仓位数量 (股)
        """
        if total_capital <= 0 or current_price <= 0 or stop_loss_pct <= 0:
            return 0
        
        # 风险金额 = 总资金 × 每笔风险
        risk_amount = total_capital * risk_per_trade
        
        # 每股风险 = 当前价格 × 止损百分比
        risk_per_share = current_price * stop_loss_pct
        
        # 仓位数量 = 风险金额 / 每股风险
        quantity = int(risk_amount / risk_per_share)
        
        # 限制在最大仓位内
        max_quantity = int(total_capital * self.config.max_single_position / current_price)
        quantity = min(quantity, max_quantity)
        
        # 确保为 100 的整数倍 (港股整手)
        quantity = (quantity // 100) * 100
        
        return max(100, quantity)  # 最小 100 股


class EmergencyLiquidator:
    """紧急清仓器"""
    
    def __init__(self, config: Optional[RiskConfig] = None):
        """
        初始化紧急清仓器
        
        Args:
            config: 风控配置
        """
        self.config = config or RiskConfig()
    
    def should_liquidate_all(self, metrics: Dict) -> tuple:
        """
        检查是否应该全部清仓
        
        Args:
            metrics: 风险指标字典
        
        Returns:
            (should_liquidate: bool, reason: str)
        """
        # 检查最大回撤
        max_drawdown = metrics.get('max_drawdown', 0)
        if max_drawdown > self.config.max_drawdown:
            return True, f"回撤超限：{max_drawdown:.1%} > {self.config.max_drawdown:.1%}"
        
        # 检查日亏损
        daily_loss_pct = metrics.get('daily_loss_pct', 0)
        if daily_loss_pct > self.config.max_daily_loss:
            return True, f"日亏损超限：{daily_loss_pct:.1%} > {self.config.max_daily_loss:.1%}"
        
        # 检查连亏次数
        consecutive_losses = metrics.get('consecutive_losses', 0)
        if consecutive_losses >= self.config.consecutive_loss_limit:
            return True, f"连续亏损超限：{consecutive_losses} >= {self.config.consecutive_loss_limit}"
        
        return False, ""


# 工厂函数
def create_stop_loss_executor(config: Optional[RiskConfig] = None) -> StopLossExecutor:
    """创建止损执行器"""
    return StopLossExecutor(config)


def create_take_profit_executor(config: Optional[RiskConfig] = None) -> TakeProfitExecutor:
    """创建止盈执行器"""
    return TakeProfitExecutor(config)


def create_breakeven_executor(config: Optional[RiskConfig] = None) -> BreakevenExecutor:
    """创建保本止损执行器"""
    return BreakevenExecutor(config)


def create_position_sizer(config: Optional[RiskConfig] = None) -> PositionSizer:
    """创建仓位计算器"""
    return PositionSizer(config)


def create_emergency_liquidator(config: Optional[RiskConfig] = None) -> EmergencyLiquidator:
    """创建紧急清仓器"""
    return EmergencyLiquidator(config)
