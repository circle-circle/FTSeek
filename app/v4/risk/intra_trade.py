"""
IntraTradeRisk - 交易中风控模块

功能:
- 实时盈亏监控
- 移动止损触发
- 异常波动检测
- 仓位动态调整
"""
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime

logger = logging.getLogger("ftseek.v4.intra_trade_risk")


@dataclass
class RiskAlert:
    """风险警报"""
    type: str  # LARGE_LOSS, PROFIT_TARGET, STOP_LOSS, ABNORMAL_VOLATILITY
    level: str  # WARNING, CRITICAL
    message: str
    code: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "level": self.level,
            "message": self.message,
            "code": self.code,
            "value": self.value,
            "threshold": self.threshold,
            "timestamp": datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S"),
        }


@dataclass
class PositionData:
    """仓位数据"""
    code: str
    entry_price: float
    quantity: int
    direction: str  # BUY or SELL
    current_price: float = 0.0
    entry_time: float = field(default_factory=time.time)
    stop_loss_price: Optional[float] = None
    trailing_stop_price: Optional[float] = None
    profit_target_price: Optional[float] = None


class VolatilityDetector:
    """波动检测器"""
    
    def __init__(self, window_size: int = 20, threshold_multiplier: float = 2.0):
        self.window_size = window_size
        self.threshold_multiplier = threshold_multiplier
        self.price_history: deque = deque(maxlen=window_size)
        self.baseline_volatility: Optional[float] = None
    
    def add_price(self, price: float):
        """添加价格数据"""
        self.price_history.append(price)
        
        # 如果有足够数据，计算基准波动率
        if len(self.price_history) == self.window_size and self.baseline_volatility is None:
            self.baseline_volatility = self._calculate_std()
    
    def _calculate_std(self) -> float:
        """计算标准差"""
        if len(self.price_history) < 2:
            return 0.0
        
        prices = list(self.price_history)
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return variance ** 0.5
    
    def calculate_volatility(self) -> float:
        """计算当前波动率"""
        return self._calculate_std()
    
    def is_abnormal_volatility(self) -> bool:
        """检测异常波动"""
        if len(self.price_history) < self.window_size:
            return False
        
        if self.baseline_volatility is None or self.baseline_volatility == 0:
            return False
        
        current_volatility = self.calculate_volatility()
        return current_volatility > (self.baseline_volatility * self.threshold_multiplier)
    
    def reset(self):
        """重置检测器"""
        self.price_history.clear()
        self.baseline_volatility = None


class PositionMonitor:
    """仓位监控器"""
    
    def __init__(self):
        self.positions: Dict[str, PositionData] = {}
        self.logger = logging.getLogger("ftseek.v4.position_monitor")
    
    def add_position(self, code: str, position_data: Dict[str, Any]):
        """添加仓位"""
        self.positions[code] = PositionData(
            code=code,
            entry_price=position_data["entry_price"],
            quantity=position_data["quantity"],
            direction=position_data["direction"],
            stop_loss_price=position_data.get("stop_loss_price"),
            profit_target_price=position_data.get("profit_target_price"),
        )
        self.logger.info(f"添加仓位监控：{code} @ {position_data['entry_price']}")
    
    def remove_position(self, code: str):
        """移除仓位"""
        if code in self.positions:
            del self.positions[code]
            self.logger.info(f"移除仓位监控：{code}")
    
    def update_price(self, code: str, price: float):
        """更新价格"""
        if code in self.positions:
            self.positions[code].current_price = price
    
    def get_position_pnl(self, code: str) -> Dict[str, Any]:
        """获取仓位盈亏"""
        if code not in self.positions:
            return {"error": "Position not found"}
        
        pos = self.positions[code]
        if pos.direction == "BUY":
            pnl_amount = (pos.current_price - pos.entry_price) * pos.quantity
            pnl_percent = (pos.current_price - pos.entry_price) / pos.entry_price
        else:  # SELL
            pnl_amount = (pos.entry_price - pos.current_price) * pos.quantity
            pnl_percent = (pos.entry_price - pos.current_price) / pos.entry_price
        
        return {
            "code": code,
            "unrealized_pnl": pnl_amount,
            "unrealized_pnl_percent": pnl_percent,
            "current_price": pos.current_price,
            "entry_price": pos.entry_price,
            "quantity": pos.quantity,
            "direction": pos.direction,
        }
    
    def get_all_positions_pnl(self) -> Dict[str, Dict[str, Any]]:
        """获取所有仓位盈亏"""
        return {code: self.get_position_pnl(code) for code in self.positions}
    
    def get_total_pnl(self) -> Dict[str, float]:
        """获取总盈亏"""
        total_amount = 0.0
        total_percent_weighted = 0.0
        total_value = 0.0
        
        for code in self.positions:
            pnl = self.get_position_pnl(code)
            if "error" not in pnl:
                position_value = pnl["current_price"] * pnl["quantity"]
                total_amount += pnl["unrealized_pnl"]
                total_value += position_value
                total_percent_weighted += pnl["unrealized_pnl_percent"] * position_value
        
        total_percent = (total_percent_weighted / total_value) if total_value > 0 else 0.0
        
        return {
            "total_pnl_amount": total_amount,
            "total_pnl_percent": total_percent,
            "total_value": total_value,
        }


class IntraTradeRisk:
    """交易中风控核心类"""
    
    def __init__(
        self,
        trailing_stop_pct: float = 0.03,
        profit_lock_pct: float = 0.05,
        profit_target_pct: float = 0.10,
        large_loss_threshold: float = -0.05,
        abnormal_volatility_threshold: float = 2.0,
    ):
        """
        初始化交易中风控
        
        Args:
            trailing_stop_pct: 移动止损百分比 (默认 3%)
            profit_lock_pct: 利润锁定百分比 (默认 5%)
            profit_target_pct: 止盈目标百分比 (默认 10%)
            large_loss_threshold: 大额亏损阈值 (默认 -5%)
            abnormal_volatility_threshold: 异常波动倍数阈值 (默认 2.0)
        """
        self.trailing_stop_pct = trailing_stop_pct
        self.profit_lock_pct = profit_lock_pct
        self.profit_target_pct = profit_target_pct
        self.large_loss_threshold = large_loss_threshold
        self.abnormal_volatility_threshold = abnormal_volatility_threshold
        
        # 仓位信息
        self.entry_price: Optional[float] = None
        self.current_price: Optional[float] = None
        self.quantity: int = 0
        self.direction: Optional[str] = None  # BUY or SELL
        
        # 止损止盈
        self.initial_stop_loss: Optional[float] = None
        self.trailing_stop_price: Optional[float] = None
        self.profit_target_price: Optional[float] = None
        self.highest_price: Optional[float] = None
        self.lowest_price: Optional[float] = None
        
        # 警报
        self.alerts: List[RiskAlert] = []
        
        # 波动检测器
        self.volatility_detector = VolatilityDetector(
            threshold_multiplier=abnormal_volatility_threshold
        )
        
        # 仓位监控器
        self.position_monitor = PositionMonitor()
        
        self.logger = logging.getLogger("ftseek.v4.intra_trade_risk")
    
    def set_entry_price(self, price: float, direction: str = "BUY"):
        """设置入场价格"""
        self.entry_price = price
        self.current_price = price
        self.direction = direction
        self.highest_price = price
        self.lowest_price = price
        
        # 计算初始止损
        if direction == "BUY":
            self.initial_stop_loss = price * (1 - self.trailing_stop_pct)
            self.profit_target_price = price * (1 + self.profit_target_pct)
        else:  # SELL
            self.initial_stop_loss = price * (1 + self.trailing_stop_pct)
            self.profit_target_price = price * (1 - self.profit_target_pct)
        
        self.trailing_stop_price = self.initial_stop_loss
        
        self.logger.info(
            f"设置入场价格：{price} ({direction}), 初始止损={self.initial_stop_loss}, "
            f"止盈={self.profit_target_price}"
        )
    
    def set_quantity(self, quantity: int):
        """设置仓位数量"""
        self.quantity = quantity
    
    def update_price(self, price: float):
        """更新当前价格"""
        self.current_price = price
        self.volatility_detector.add_price(price)
        
        # 更新最高/最低价
        if self.highest_price is None or price > self.highest_price:
            self.highest_price = price
        if self.lowest_price is None or price < self.lowest_price:
            self.lowest_price = price
        
        # 更新移动止损
        self._update_trailing_stop()
        
        # 检查警报
        self._check_alerts()
    
    def _update_trailing_stop(self):
        """更新移动止损"""
        if self.entry_price is None or self.current_price is None:
            return
        
        if self.direction == "BUY":
            # 多头：价格上涨时止损上移，下跌时保持
            if self.highest_price and self.highest_price > self.entry_price:
                # 计算锁定利润的止损
                locked_profit_stop = self.highest_price * (1 - self.trailing_stop_pct)
                
                # 如果已经有利润，应用利润锁定
                if self.profit_lock_pct > 0:
                    profit = (self.highest_price - self.entry_price) / self.entry_price
                    if profit >= self.profit_lock_pct:
                        # 锁定至少 profit_lock_pct 的利润
                        min_stop = self.entry_price * (1 + self.profit_lock_pct)
                        locked_profit_stop = max(locked_profit_stop, min_stop)
                
                # 只上移，不下移
                if self.trailing_stop_price is None or locked_profit_stop > self.trailing_stop_price:
                    self.trailing_stop_price = locked_profit_stop
        else:  # SELL
            # 空头：价格下跌时止损下移，上涨时保持
            if self.lowest_price and self.lowest_price < self.entry_price:
                locked_profit_stop = self.lowest_price * (1 + self.trailing_stop_pct)
                
                if self.profit_lock_pct > 0:
                    profit = (self.entry_price - self.lowest_price) / self.entry_price
                    if profit >= self.profit_lock_pct:
                        min_stop = self.entry_price * (1 - self.profit_lock_pct)
                        locked_profit_stop = min(locked_profit_stop, min_stop)
                
                if self.trailing_stop_price is None or locked_profit_stop < self.trailing_stop_price:
                    self.trailing_stop_price = locked_profit_stop
    
    def _check_alerts(self):
        """检查并生成警报"""
        if self.entry_price is None or self.current_price is None:
            return
        
        # 计算盈亏
        if self.direction == "BUY":
            pnl_percent = (self.current_price - self.entry_price) / self.entry_price
        else:
            pnl_percent = (self.entry_price - self.current_price) / self.entry_price
        
        # 大额亏损警报
        if pnl_percent <= self.large_loss_threshold:
            self._add_alert(
                type="LARGE_LOSS",
                level="WARNING",
                message=f"大额亏损预警：{pnl_percent:.1%}",
                value=pnl_percent,
                threshold=self.large_loss_threshold,
            )
        
        # 止盈警报
        if pnl_percent >= self.profit_target_pct:
            self._add_alert(
                type="PROFIT_TARGET",
                level="INFO",
                message=f"达到止盈目标：{pnl_percent:.1%}",
                value=pnl_percent,
                threshold=self.profit_target_pct,
            )
        
        # 异常波动警报
        if self.volatility_detector.is_abnormal_volatility():
            self._add_alert(
                type="ABNORMAL_VOLATILITY",
                level="WARNING",
                message="检测到异常波动",
                threshold=self.abnormal_volatility_threshold,
            )
    
    def _add_alert(
        self,
        type: str,
        level: str,
        message: str,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
    ):
        """添加警报"""
        # 避免重复警报
        for alert in self.alerts:
            if alert.type == type and time.time() - alert.timestamp < 60:  # 1 分钟内不重复
                return
        
        alert = RiskAlert(
            type=type,
            level=level,
            message=message,
            value=value,
            threshold=threshold,
        )
        self.alerts.append(alert)
        self.logger.warning(f"风险警报：{type} - {message}")
    
    def should_stop_loss(self) -> bool:
        """检查是否应该止损"""
        if self.trailing_stop_price is None or self.current_price is None:
            return False
        
        if self.direction == "BUY":
            return self.current_price <= self.trailing_stop_price
        else:  # SELL
            return self.current_price >= self.trailing_stop_price
    
    def get_unrealized_pnl(self) -> Dict[str, Any]:
        """获取未实现盈亏"""
        if self.entry_price is None or self.current_price is None:
            return {"error": "No position"}
        
        if self.direction == "BUY":
            pnl_amount = (self.current_price - self.entry_price) * self.quantity
            pnl_percent = (self.current_price - self.entry_price) / self.entry_price
        else:  # SELL
            pnl_amount = (self.entry_price - self.current_price) * self.quantity
            pnl_percent = (self.entry_price - self.current_price) / self.entry_price
        
        return {
            "pnl_amount": pnl_amount,
            "pnl_percent": pnl_percent,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "quantity": self.quantity,
            "direction": self.direction,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price,
        }
    
    def get_alerts(self) -> List[Dict[str, Any]]:
        """获取警报列表"""
        return [alert.to_dict() for alert in self.alerts]
    
    def clear_alerts(self):
        """清除警报"""
        self.alerts.clear()
    
    def get_status(self) -> Dict[str, Any]:
        """获取风控状态"""
        pnl = self.get_unrealized_pnl()
        
        return {
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "direction": self.direction,
            "quantity": self.quantity,
            "initial_stop_loss": self.initial_stop_loss,
            "trailing_stop_price": self.trailing_stop_price,
            "profit_target_price": self.profit_target_price,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price,
            "pnl": pnl,
            "should_stop_loss": self.should_stop_loss(),
            "alerts_count": len(self.alerts),
            "abnormal_volatility": self.volatility_detector.is_abnormal_volatility(),
        }
    
    def reset(self):
        """重置风控状态"""
        self.entry_price = None
        self.current_price = None
        self.quantity = 0
        self.direction = None
        self.initial_stop_loss = None
        self.trailing_stop_price = None
        self.profit_target_price = None
        self.highest_price = None
        self.lowest_price = None
        self.alerts.clear()
        self.volatility_detector.reset()


# 全局实例 (可选)
intra_trade_risk: Optional[IntraTradeRisk] = None


def get_intra_trade_risk() -> IntraTradeRisk:
    """获取 IntraTradeRisk 单例"""
    global intra_trade_risk
    if intra_trade_risk is None:
        intra_trade_risk = IntraTradeRisk()
    return intra_trade_risk
