"""
策略基类和信号模型
所有自定义策略继承 BaseStrategy 并实现 on_quote / on_kline 方法
"""
import uuid
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    ALERT = "ALERT"  # 仅提醒，不触发交易

class TradeTier(str, Enum):
    """交易分层枚举 (Phase 3: Trading Tiering)"""
    TIER_TREND = "TREND"           # 趋势波段 (原有逻辑, 1-5天)
    TIER_SHORT = "SHORT_ALPHA"     # 短线突击 (盘口+大单, 5-120分钟)
    TIER_SCALP = "SCALP"           # 超短打板 (逐笔Tick, <30分钟)

from app.strategy.consensus_types import SignalSource

@dataclass
class Signal:
    """一条交易信号"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    code: str = ""              # 标的代码, e.g. "US.AAPL"
    name: str = ""              # 标的名称
    action: SignalAction = SignalAction.ALERT
    price: float = 0.0          # 触发价格
    quantity: int = 1           # 建议数量
    source: SignalSource = SignalSource.TECHNICAL # 信号来源 (Phase 2)
    reason: str = ""            # 触发原因描述
    strategy: str = ""          # 策略名称
    trace_id: str = ""          # 全链路唯一标识 (Traceability ID) - Phase 1 新增
    priority: int = 2           # 优先级 (1=LOW, 2=MEDIUM, 3=HIGH, 4=CRITICAL, 5=EMERGENCY)
    timestamp: float = field(default_factory=time.time)
    expire_at: float = 0.0      # 过期时间戳
    confirmed: bool = False     # 是否已被确认
    executed: bool = False       # 是否已执行
    
    # Phase 3: 交易分层字段 (Phase 3: Trading Tiering)
    tier: TradeTier = TradeTier.TIER_TREND  # 交易层级
    tier_stop_loss: float = 0.0             # 独立止损线 (如 0.015 代表 1.5%)
    tier_take_profit: float = 0.0           # 独立止盈线 (如 0.03 代表 3%)
    tier_max_hold_minutes: int = 0          # 最大持仓分钟数 (0 表示跟随全局配置)
    
    extra: dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expire_at <= 0:
            return False
        return time.time() > self.expire_at


class BaseStrategy:
    """策略基类"""
    name: str = "base"

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)

    def on_quote(self, code: str, quote: dict) -> Optional[Signal]:
        """
        实时报价回调
        :param code: 标的代码
        :param quote: 报价数据 dict
        :return: 产生信号时返回 Signal, 否则 None
        """
        return None

    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        """
        K线数据回调
        :param code: 标的代码
        :param kline: K线数据 dict
        :param kline_type: K线类型
        :return: 产生信号时返回 Signal, 否则 None
        """
        return None
