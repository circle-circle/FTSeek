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


@dataclass
class Signal:
    """一条交易信号"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    code: str = ""              # 标的代码, e.g. "US.AAPL"
    name: str = ""              # 标的名称
    action: SignalAction = SignalAction.ALERT
    price: float = 0.0          # 触发价格
    quantity: int = 1           # 建议数量
    reason: str = ""            # 触发原因描述
    strategy: str = ""          # 策略名称
    timestamp: float = field(default_factory=time.time)
    expire_at: float = 0.0      # 过期时间戳
    confirmed: bool = False     # 是否已被确认
    executed: bool = False       # 是否已执行
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
