"""
PositionManager - 持仓管理模块

功能:
- 检查当前持仓
- 根据持仓状态生成操作建议
- 动态调整推送按钮
"""
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
import time

logger = logging.getLogger("ftseek.v4.position_manager")
logger.propagate = False  # 防止日志重复


@dataclass
class Position:
    """持仓数据"""
    code: str
    direction: str  # BUY/SELL
    quantity: int
    entry_price: float
    current_price: float
    pnl: float = 0.0
    pnl_percent: float = 0.0
    
    def __post_init__(self):
        # 计算盈亏
        if self.direction == 'BUY':
            self.pnl = (self.current_price - self.entry_price) * self.quantity
            self.pnl_percent = (self.current_price - self.entry_price) / self.entry_price
        else:  # SELL
            self.pnl = (self.entry_price - self.current_price) * self.quantity
            self.pnl_percent = (self.entry_price - self.current_price) / self.entry_price
    
    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "direction": self.direction,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "pnl": self.pnl,
            "pnl_percent": self.pnl_percent,
        }


class PositionManager:
    """持仓管理器"""
    
    def __init__(self):
        self.positions: Dict[str, Position] = {}
        self.logger = logging.getLogger("ftseek.v4.position_manager")
logger.propagate = False  # 防止日志重复
    
    def add_position(self, code: str, direction: str, quantity: int, 
                     entry_price: float, current_price: float):
        """添加/更新持仓"""
        if code in self.positions:
            # 已有持仓，更新
            pos = self.positions[code]
            if pos.direction == direction:
                # 同方向，加仓
                total_qty = pos.quantity + quantity
                avg_price = (pos.entry_price * pos.quantity + entry_price * quantity) / total_qty
                pos.quantity = total_qty
                pos.entry_price = avg_price
                pos.current_price = current_price
                self.logger.info(f"{code} 加仓：{quantity}股，均价：{avg_price:.2f}")
            else:
                # 反方向，平仓或反向开仓
                if quantity >= pos.quantity:
                    # 平仓并可能反向开仓
                    remaining = quantity - pos.quantity
                    del self.positions[code]
                    if remaining > 0:
                        self.add_position(code, direction, remaining, entry_price, current_price)
                    self.logger.info(f"{code} 平仓并可能反向开仓")
                else:
                    # 部分平仓
                    pos.quantity -= quantity
                    self.logger.info(f"{code} 部分平仓：{quantity}股")
        else:
            # 新开仓
            self.positions[code] = Position(
                code=code,
                direction=direction,
                quantity=quantity,
                entry_price=entry_price,
                current_price=current_price
            )
            self.logger.info(f"{code} 新开仓：{direction} {quantity}股 @ {entry_price:.2f}")
    
    def get_position(self, code: str) -> Optional[Position]:
        """获取持仓"""
        return self.positions.get(code)
    
    def has_position(self, code: str) -> bool:
        """检查是否有持仓"""
        return code in self.positions
    
    def get_position_direction(self, code: str) -> Optional[str]:
        """获取持仓方向"""
        pos = self.positions.get(code)
        return pos.direction if pos else None
    
    def get_position_quantity(self, code: str) -> int:
        """获取持仓数量"""
        pos = self.positions.get(code)
        return pos.quantity if pos else 0
    
    def close_position(self, code: str, quantity: int, current_price: float):
        """平仓"""
        if code in self.positions:
            pos = self.positions[code]
            if quantity >= pos.quantity:
                pnl = pos.pnl
                del self.positions[code]
                self.logger.info(f"{code} 全部平仓，盈亏：{pnl:.2f}")
            else:
                pos.quantity -= quantity
                self.logger.info(f"{code} 部分平仓：{quantity}股")
    
    def get_all_positions(self) -> List[Position]:
        """获取所有持仓"""
        return list(self.positions.values())
    
    def get_recommendation(self, code: str, signal_direction: str) -> Dict[str, Any]:
        """
        根据持仓状态获取操作建议
        
        Args:
            code: 股票代码
            signal_direction: 信号方向 (BUY/SELL)
            
        Returns:
            建议字典 {action, buttons, message}
        """
        pos = self.get_position(code)
        
        if not pos:
            # 无持仓，建议开仓
            return {
                "action": "OPEN",
                "buttons": [
                    {"title": "✅ 确认开仓", "decision": "confirm"},
                    {"title": "❌ 忽略", "decision": "ignore"}
                ],
                "message": "无持仓，可开新仓"
            }
        
        # 有持仓
        if pos.direction == signal_direction:
            # 同方向信号，建议加仓
            return {
                "action": "ADD",
                "buttons": [
                    {"title": "✅ 确认加仓", "decision": "confirm"},
                    {"title": "❌ 忽略", "decision": "ignore"}
                ],
                "message": f"已有{pos.direction}持仓{pos.quantity}股，可加仓"
            }
        else:
            # 反方向信号，建议平仓
            return {
                "action": "CLOSE",
                "buttons": [
                    {"title": "💰 确认平仓", "decision": "confirm"},
                    {"title": "❌ 忽略", "decision": "ignore"}
                ],
                "message": f"已有{pos.direction}持仓{pos.quantity}股，建议平仓"
            }
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "positions": [pos.to_dict() for pos in self.positions.values()],
            "total_count": len(self.positions),
        }


# 全局实例
position_manager: Optional[PositionManager] = None


def get_position_manager() -> PositionManager:
    """获取单例"""
    global position_manager
    if position_manager is None:
        position_manager = PositionManager()
    return position_manager
