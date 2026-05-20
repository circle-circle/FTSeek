"""
智能订单路由器

选择最佳券商/交易所执行订单
"""
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass


@dataclass
class BrokerInfo:
    name: str
    priority: int
    available: bool
    latency_ms: float = 0.0
    fee_rate: float = 0.0


class SmartRouter:
    """智能订单路由器"""
    
    def __init__(self):
        self.brokers: Dict[str, BrokerInfo] = {}
    
    def add_broker(self, name: str, priority: int, available: bool, **kwargs):
        """添加券商"""
        self.brokers[name] = BrokerInfo(
            name=name,
            priority=priority,
            available=available,
            **kwargs
        )
    
    def select_broker(self, symbol: str) -> Optional[str]:
        """选择最佳券商"""
        available_brokers = [
            (name, info) for name, info in self.brokers.items()
            if info.available
        ]
        
        if not available_brokers:
            return None
        
        # 按优先级排序
        available_brokers.sort(key=lambda x: x[1].priority)
        
        return available_brokers[0][0]
    
    def mark_unavailable(self, broker_name: str):
        """标记券商不可用"""
        if broker_name in self.brokers:
            self.brokers[broker_name].available = False
    
    def mark_available(self, broker_name: str):
        """标记券商可用"""
        if broker_name in self.brokers:
            self.brokers[broker_name].available = True
