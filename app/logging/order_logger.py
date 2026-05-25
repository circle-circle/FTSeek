#!/usr/bin/env python3
"""
订单执行日志增强模块

功能:
1. 记录每个订单的完整生命周期
2. 追踪富途订单ID与数据库记录的关联
3. 实时监控订单状态
4. 异常告警
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass, asdict, field
import uuid

logger = logging.getLogger("ftseek.order_logger")
logger.propagate = False  # 防止日志重复

# 日志目录
ORDER_LOG_DIR = Path('/opt/ftseek/FTSeek/data/order_logs')
ORDER_LOG_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class OrderLog:
    """订单日志记录"""
    order_id: str                    # 内部订单ID
    futu_order_id: Optional[str]     # 富途订单ID
    code: str                        # 股票代码
    action: str                      # BUY/SELL
    trade_type: str                  # OPEN/CLOSE
    quantity: int                    # 数量
    price: float                     # 价格
    order_type: str                  # MARKET/LIMIT
    status: str                      # pending/submitted/filled/failed/cancelled
    position_id: Optional[int]       # 数据库持仓ID
    signal_id: Optional[str]         # 信号ID
    strategy: Optional[str]          # 策略名称
    trd_env: str                     # SIMULATE/REAL
    
    # 执行结果
    filled_quantity: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    
    # 时间戳
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    submitted_at: Optional[str] = None
    filled_at: Optional[str] = None
    
    # 错误信息
    error_message: Optional[str] = None
    retry_count: int = 0
    
    # 富途响应
    futu_response: Optional[Dict] = None
    
    def to_dict(self) -> Dict:
        return asdict(self)


class OrderLogger:
    """订单日志管理器"""
    
    def __init__(self):
        self.pending_orders: Dict[str, OrderLog] = {}
        self._load_today_orders()
    
    def _get_today_file(self) -> Path:
        """获取今日日志文件"""
        today = datetime.now().strftime('%Y-%m-%d')
        return ORDER_LOG_DIR / f"orders_{today}.json"
    
    def _load_today_orders(self):
        """加载今日订单"""
        log_file = self._get_today_file()
        if log_file.exists():
            try:
                with open(log_file, 'r') as f:
                    data = json.load(f)
                    for order_data in data:
                        order = OrderLog(**order_data)
                        if order.status in ['pending', 'submitted']:
                            self.pending_orders[order.order_id] = order
                logger.info(f"加载 {len(self.pending_orders)} 个待处理订单")
            except Exception as e:
                logger.error(f"加载订单日志失败: {e}")
    
    def _save_orders(self):
        """保存订单日志"""
        log_file = self._get_today_file()
        
        # 合并文件中的历史记录和内存中的记录
        all_orders = []
        if log_file.exists():
            try:
                with open(log_file, 'r') as f:
                    all_orders = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        
        # 更新内存中的订单
        order_ids = set(o['order_id'] for o in all_orders)
        for order in self.pending_orders.values():
            if order.order_id not in order_ids:
                all_orders.append(order.to_dict())
            else:
                # 更新已存在的记录
                for i, o in enumerate(all_orders):
                    if o['order_id'] == order.order_id:
                        all_orders[i] = order.to_dict()
                        break
        
        with open(log_file, 'w') as f:
            json.dump(all_orders, f, indent=2, ensure_ascii=False)
    
    def create_order(self, 
                    code: str,
                    action: str,
                    trade_type: str,
                    quantity: int,
                    price: float,
                    order_type: str,
                    trd_env: str = 'SIMULATE',
                    signal_id: str = None,
                    strategy: str = None) -> OrderLog:
        """
        创建订单记录
        
        Returns:
            OrderLog: 订单日志对象
        """
        order_id = f"ORD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        
        order = OrderLog(
            order_id=order_id,
            futu_order_id=None,
            code=code,
            action=action,
            trade_type=trade_type,
            quantity=quantity,
            price=price,
            order_type=order_type,
            status='pending',
            position_id=None,
            signal_id=signal_id,
            strategy=strategy,
            trd_env=trd_env
        )
        
        self.pending_orders[order_id] = order
        self._save_orders()
        
        logger.info(f"[订单创建] {order_id} | {code} {action} {quantity}股 @ {price:.2f} | {trade_type}")
        
        return order
    
    def submit_order(self, order_id: str, futu_order_id: str, futu_response: Dict = None):
        """记录订单提交"""
        if order_id not in self.pending_orders:
            logger.warning(f"订单未找到: {order_id}")
            return
        
        order = self.pending_orders[order_id]
        order.futu_order_id = futu_order_id
        order.status = 'submitted'
        order.submitted_at = datetime.now().isoformat()
        order.futu_response = futu_response
        
        self._save_orders()
        
        logger.info(f"[订单提交] {order_id} -> 富途订单 {futu_order_id}")
    
    def fill_order(self, 
                  order_id: str,
                  position_id: int = None,
                  filled_quantity: int = None,
                  filled_price: float = None,
                  commission: float = 0):
        """记录订单成交"""
        if order_id not in self.pending_orders:
            logger.warning(f"订单未找到: {order_id}")
            return
        
        order = self.pending_orders[order_id]
        order.status = 'filled'
        order.position_id = position_id
        order.filled_quantity = filled_quantity or order.quantity
        order.filled_price = filled_price or order.price
        order.commission = commission
        order.filled_at = datetime.now().isoformat()
        
        # 移出待处理
        del self.pending_orders[order_id]
        
        self._save_orders()
        
        logger.info(f"[订单成交] {order_id} | 持仓ID={position_id} | 成交价={order.filled_price:.2f}")
    
    def fail_order(self, order_id: str, error_message: str):
        """记录订单失败"""
        if order_id not in self.pending_orders:
            logger.warning(f"订单未找到: {order_id}")
            return
        
        order = self.pending_orders[order_id]
        order.status = 'failed'
        order.error_message = error_message
        order.retry_count += 1
        
        # 如果重试次数少于3次，保留在待处理中
        if order.retry_count >= 3:
            del self.pending_orders[order_id]
        
        self._save_orders()
        
        logger.error(f"[订单失败] {order_id} | 原因: {error_message} | 重试: {order.retry_count}")
    
    def get_order_by_futu_id(self, futu_order_id: str) -> Optional[OrderLog]:
        """通过富途订单ID查找订单"""
        for order in self.pending_orders.values():
            if order.futu_order_id == futu_order_id:
                return order
        return None
    
    def get_statistics(self) -> Dict:
        """获取今日统计"""
        log_file = self._get_today_file()
        
        stats = {
            'total': 0,
            'filled': 0,
            'failed': 0,
            'pending': 0,
            'open_orders': len(self.pending_orders),
            'total_commission': 0.0,
            'total_volume': 0.0
        }
        
        if log_file.exists():
            try:
                with open(log_file, 'r') as f:
                    orders = json.load(f)
                
                stats['total'] = len(orders)
                
                for order in orders:
                    if order['status'] == 'filled':
                        stats['filled'] += 1
                        stats['total_commission'] += order.get('commission', 0)
                        stats['total_volume'] += order.get('filled_price', 0) * order.get('filled_quantity', 0)
                    elif order['status'] == 'failed':
                        stats['failed'] += 1
                    elif order['status'] in ['pending', 'submitted']:
                        stats['pending'] += 1
            except (KeyError, TypeError, IOError):
                pass
        
        return stats


# 全局单例
order_logger = OrderLogger()