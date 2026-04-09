"""
订单状态追踪模块

功能:
- 订单状态管理 (已提交/部分成交/完全成交/已取消)
- 订单生命周期追踪
- 成交统计分析
"""
import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime
import json

logger = logging.getLogger("ftseek.trader.order_tracker")
logger.propagate = False  # 防止日志重复


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"          # 已提交，待成交
    PARTIALLY_FILLED = "partial" # 部分成交
    FILLED = "filled"            # 完全成交
    CANCELLED = "cancelled"      # 已取消
    REJECTED = "rejected"        # 已拒绝
    EXPIRED = "expired"          # 已过期


@dataclass
class Order:
    """订单数据"""
    order_id: str
    stock_code: str
    side: str  # "BUY" or "SELL"
    quantity: int
    price: float
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    filled_at: Optional[str] = None
    cancel_reason: str = ""
    fills: List[Dict] = field(default_factory=list)  # 成交明细
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at
    
    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "stock_code": self.stock_code,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price": self.avg_fill_price,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "filled_at": self.filled_at,
            "cancel_reason": self.cancel_reason,
            "fills": self.fills,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Order':
        data['status'] = OrderStatus(data['status'])
        return cls(**data)


class OrderTracker:
    """订单追踪器"""
    
    def __init__(self):
        self.orders: Dict[str, Order] = {}
        self.stock_orders: Dict[str, List[str]] = {}  # stock_code -> [order_ids]
        logger.info("订单追踪器已初始化")
    
    def create_order(self, 
                     stock_code: str,
                     side: str,
                     quantity: int,
                     price: float,
                     order_id: str = None) -> Order:
        """创建订单"""
        import uuid
        order_id = order_id or str(uuid.uuid4())[:16]
        
        order = Order(
            order_id=order_id,
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            price=price,
        )
        
        self.orders[order_id] = order
        
        # 索引到股票
        if stock_code not in self.stock_orders:
            self.stock_orders[stock_code] = []
        self.stock_orders[stock_code].append(order_id)
        
        logger.info(f"订单创建：{order_id} {side} {stock_code} x{quantity} @${price}")
        return order
    
    def update_order(self, order_id: str, **kwargs) -> Optional[Order]:
        """更新订单状态"""
        if order_id not in self.orders:
            logger.warning(f"订单不存在：{order_id}")
            return None
        
        order = self.orders[order_id]
        
        for key, value in kwargs.items():
            if hasattr(order, key):
                setattr(order, key, value)
        
        order.updated_at = datetime.now().isoformat()
        
        # 状态变更处理
        if 'status' in kwargs:
            logger.info(f"订单状态变更：{order_id} -> {order.status.value}")
            
            if order.status in [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED]:
                order.filled_at = order.updated_at
        
        return order
    
    def add_fill(self, order_id: str, fill_price: float, fill_quantity: int, fill_time: str = None):
        """添加成交明细"""
        if order_id not in self.orders:
            logger.warning(f"订单不存在：{order_id}")
            return
        
        order = self.orders[order_id]
        
        fill = {
            "price": fill_price,
            "quantity": fill_quantity,
            "time": fill_time or datetime.now().isoformat(),
        }
        order.fills.append(fill)
        
        # 更新累计成交
        total_value = sum(f['price'] * f['quantity'] for f in order.fills)
        total_qty = sum(f['quantity'] for f in order.fills)
        
        order.filled_quantity = total_qty
        order.avg_fill_price = total_value / total_qty if total_qty > 0 else 0
        
        # 检查是否完全成交
        if total_qty >= order.quantity:
            order.status = OrderStatus.FILLED
            order.filled_at = datetime.now().isoformat()
            logger.info(f"订单完全成交：{order_id} {total_qty}股 @ 均价${order.avg_fill_price:.2f}")
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
            logger.info(f"订单部分成交：{order_id} {total_qty}/{order.quantity}股")
        
        order.updated_at = datetime.now().isoformat()
    
    def cancel_order(self, order_id: str, reason: str = "") -> bool:
        """取消订单"""
        if order_id not in self.orders:
            logger.warning(f"订单不存在：{order_id}")
            return False
        
        order = self.orders[order_id]
        
        if order.status in [OrderStatus.FILLED, OrderStatus.CANCELLED]:
            logger.warning(f"订单不可取消：{order_id} (状态：{order.status.value})")
            return False
        
        order.status = OrderStatus.CANCELLED
        order.cancel_reason = reason
        order.updated_at = datetime.now().isoformat()
        order.filled_at = order.updated_at
        
        logger.info(f"订单已取消：{order_id} (原因：{reason})")
        return True
    
    def get_order(self, order_id: str) -> Optional[Order]:
        """获取订单"""
        return self.orders.get(order_id)
    
    def get_stock_orders(self, stock_code: str) -> List[Order]:
        """获取股票的所有订单"""
        order_ids = self.stock_orders.get(stock_code, [])
        return [self.orders[oid] for oid in order_ids if oid in self.orders]
    
    def get_pending_orders(self) -> List[Order]:
        """获取所有待成交订单"""
        return [o for o in self.orders.values() if o.status == OrderStatus.PENDING]
    
    def get_active_orders(self) -> List[Order]:
        """获取所有活跃订单 (未完成)"""
        active_statuses = [OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED]
        return [o for o in self.orders.values() if o.status in active_statuses]
    
    def get_statistics(self, stock_code: str = None) -> Dict:
        """获取统计信息"""
        orders = list(self.orders.values())
        if stock_code:
            orders = self.get_stock_orders(stock_code)
        
        if not orders:
            return {"total": 0}
        
        stats = {
            "total": len(orders),
            "pending": sum(1 for o in orders if o.status == OrderStatus.PENDING),
            "partially_filled": sum(1 for o in orders if o.status == OrderStatus.PARTIALLY_FILLED),
            "filled": sum(1 for o in orders if o.status == OrderStatus.FILLED),
            "cancelled": sum(1 for o in orders if o.status == OrderStatus.CANCELLED),
            "rejected": sum(1 for o in orders if o.status == OrderStatus.REJECTED),
            "total_quantity": sum(o.quantity for o in orders),
            "filled_quantity": sum(o.filled_quantity for o in orders),
            "total_value": sum(o.filled_quantity * o.avg_fill_price for o in orders),
        }
        
        return stats
    
    def export_to_json(self, filepath: str):
        """导出订单数据到 JSON"""
        data = {
            "exported_at": datetime.now().isoformat(),
            "orders": [o.to_dict() for o in self.orders.values()],
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        logger.info(f"订单数据已导出：{filepath}")
    
    def load_from_json(self, filepath: str):
        """从 JSON 加载订单数据"""
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        for order_data in data.get('orders', []):
            order = Order.from_dict(order_data)
            self.orders[order.order_id] = order
            
            if order.stock_code not in self.stock_orders:
                self.stock_orders[order.stock_code] = []
            self.stock_orders[order.stock_code].append(order.order_id)
        
        logger.info(f"订单数据已加载：{filepath}")


# 全局单例
_order_tracker = None

def get_order_tracker() -> OrderTracker:
    """获取订单追踪器单例"""
    global _order_tracker
    if _order_tracker is None:
        _order_tracker = OrderTracker()
    return _order_tracker
