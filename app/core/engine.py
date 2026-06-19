"""
三大引擎基类

定义 Risk/Execution/Observability 引擎的统一接口
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from datetime import datetime
from .models import SignalScore, Order, Position, TradeEvent, Alert, FailureRecord
from .events import EventStore


class BaseEngine(ABC):
    """
    引擎基类
    
    所有引擎继承此基类，实现统一接口
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore):
        self.config = config
        self.event_store = event_store
        self.enabled = True
        self.last_heartbeat = datetime.now()
    
    @abstractmethod
    def initialize(self):
        """初始化引擎"""
        pass
    
    @abstractmethod
    def shutdown(self):
        """关闭引擎"""
        pass
    
    def heartbeat(self):
        """心跳检查"""
        self.last_heartbeat = datetime.now()
    
    def is_healthy(self) -> bool:
        """检查引擎健康状态"""
        return self.enabled and (datetime.now() - self.last_heartbeat).total_seconds() < 60
    
    def emit_event(self, event: TradeEvent):
        """发出事件"""
        event.source = self.__class__.__name__
        self.event_store.append(event)
    
    def emit_alert(self, alert: Alert):
        """发出告警"""
        alert.source = self.__class__.__name__
        self.event_store.record_alert(alert)
    
    def record_failure(self, failure: FailureRecord):
        """记录失败"""
        self.event_store.record_failure(failure)


class RiskEngine(BaseEngine):
    """
    风险引擎
    
    职责:
    - 熔断检查
    - 仓位限制
    - 账户风险
    - 回撤限制
    - 关联资产敞口
    - 动态止损/止盈判定
    """
    
    def initialize(self):
        """初始化风险引擎"""
        self.circuit_breakers = {}
        self.position_limits = {}
        self.drawdown_levels = {}
    
    def shutdown(self):
        """关闭风险引擎"""
        self.enabled = False
    
    @abstractmethod
    def pre_trade_check(self, signal: SignalScore, position: Optional[Position] = None) -> bool:
        """预交易风控检查"""
        pass
    
    @abstractmethod
    def intra_trade_check(self, positions: List[Position]) -> List[Alert]:
        """持仓中风控检查"""
        pass
    
    @abstractmethod
    def post_trade_check(self, order: Order, position: Position) -> bool:
        """交易后风控检查"""
        pass
    
    @abstractmethod
    def check_circuit_breakers(self) -> Dict[str, bool]:
        """检查熔断器状态"""
        pass
    
    @abstractmethod
    def should_exit(self, position: Position, current_price: float) -> Optional[str]:
        """判断是否应该退出持仓"""
        pass


class ExecutionEngine(BaseEngine):
    """
    执行引擎
    
    职责:
    - 路由到哪个交易所/券商
    - 下单方式选择：限价/市价/IOC/FOK/TWAP/VWAP
    - 重试、撤单、补单
    - 滑点控制和成交追踪
    """
    
    def initialize(self):
        """初始化执行引擎"""
        self.order_book: Dict[str, Order] = {}
        self.pending_orders: List[Order] = []
        self.broker_adapter = None
    
    def shutdown(self):
        """关闭执行引擎"""
        self.enabled = False
        self.pending_orders.clear()
    
    @abstractmethod
    def submit_order(self, order: Order) -> Order:
        """提交订单"""
        pass
    
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        pass
    
    @abstractmethod
    def query_order(self, order_id: str) -> Order:
        """查询订单状态"""
        pass
    
    @abstractmethod
    def select_order_type(self, signal: SignalScore) -> str:
        """选择订单类型"""
        pass
    
    @abstractmethod
    def optimize_execution(self, order: Order) -> Order:
        """优化执行策略"""
        pass


class ObservabilityEngine(BaseEngine):
    """
    观测引擎
    
    职责:
    - 实时监控
    - 告警
    - 失败追踪
    - 审计日志
    - 可回放的交易轨迹
    """
    
    def initialize(self):
        """初始化观测引擎"""
        self.metrics = {}
        self.alert_rules = {}
        self.failure_patterns = {}
    
    def shutdown(self):
        """关闭观测引擎"""
        self.enabled = False
    
    @abstractmethod
    def collect_metrics(self) -> Dict[str, Any]:
        """收集监控指标"""
        pass
    
    @abstractmethod
    def check_alerts(self, metrics: Dict[str, Any]) -> List[Alert]:
        """检查告警条件"""
        pass
    
    @abstractmethod
    def track_failure(self, failure: FailureRecord):
        """追踪失败"""
        pass
    
    @abstractmethod
    def get_health_dashboard(self) -> Dict[str, Any]:
        """获取健康度看板数据"""
        pass
    
    @abstractmethod
    def replay_trades(self, start_time: datetime, end_time: datetime) -> List[TradeEvent]:
        """回放交易"""
        pass
