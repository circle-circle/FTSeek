"""
模块接口契约定义 - 6大核心组件的抽象基类
所有新模块开发应实现对应接口，确保模块间互操作性。
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TradeSignal:
    """交易信号数据结构"""
    symbol: str
    direction: str  # 'BUY' or 'SELL'
    strength: float  # 0-100
    confidence: float  # 0-1
    strategy_name: str
    entry_reason: str = ""
    trace_id: str = ""
    timestamp: Optional[datetime] = None


@dataclass
class ExitDecisionContract:
    """退出决策数据结构"""
    should_exit: bool
    rule_name: str
    priority: int
    exit_price: Optional[float] = None
    reason: str = ""


@dataclass
class TradeRecord:
    """交易记录数据结构"""
    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: Optional[float] = None
    quantity: int = 0
    realized_pnl: Optional[float] = None
    exit_reason: str = ""
    trade_mode: str = "simulate"


class StrategyInterface(ABC):
    """策略接口 - 所有策略模块必须实现"""

    @abstractmethod
    def generate_signals(self, market_data: Dict[str, Any]) -> List[TradeSignal]:
        """生成交易信号"""
        ...

    @abstractmethod
    def get_strategy_name(self) -> str:
        """返回策略名称"""
        ...

    @abstractmethod
    def is_enabled(self) -> bool:
        """策略是否启用"""
        ...


class ExitRuleInterface(ABC):
    """退出规则接口"""

    @abstractmethod
    def should_exit(self, position: Dict, market_data: Dict,
                    trail_state: Optional[Dict] = None) -> Optional[ExitDecisionContract]:
        """评估是否应该退出"""
        ...

    @abstractmethod
    def get_rule_name(self) -> str:
        """规则名称"""
        ...

    @abstractmethod
    def get_priority(self) -> int:
        """规则优先级（数字越小越高）"""
        ...


class StorageInterface(ABC):
    """存储接口"""

    @abstractmethod
    def save_trade(self, trade: TradeRecord) -> str:
        """保存交易记录，返回 trade_id"""
        ...

    @abstractmethod
    def query_trades(self, filters: Dict = None) -> List[TradeRecord]:
        """查询交易记录"""
        ...

    @abstractmethod
    def get_trade_by_id(self, trade_id: str) -> Optional[TradeRecord]:
        """按ID查询"""
        ...


class RiskCheckInterface(ABC):
    """风控检查接口"""

    @abstractmethod
    def check(self, signal: TradeSignal, context: Dict) -> tuple:
        """
        执行风控检查
        Returns: (passed: bool, reason: str)
        """
        ...

    @abstractmethod
    def get_check_name(self) -> str:
        """检查项名称"""
        ...


class AttributionInterface(ABC):
    """归因分析接口"""

    @abstractmethod
    def attribute_trade(self, trade: TradeRecord, context: Dict) -> Dict:
        """对单笔交易进行归因分析"""
        ...

    @abstractmethod
    def get_attribution_summary(self, trades: List[TradeRecord]) -> Dict:
        """获取归因汇总"""
        ...


class MonitorInterface(ABC):
    """监控接口"""

    @abstractmethod
    def scan(self, positions: List[Dict], market_data: Dict) -> List[Dict]:
        """扫描持仓，返回需要处理的列表"""
        ...

    @abstractmethod
    def get_health_status(self) -> Dict:
        """获取健康状态"""
        ...
