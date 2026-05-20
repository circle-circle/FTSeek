"""
增强策略基类 — FTSeek-US 重构版

相比 app/decision/strategies/base.py 新增:
- MarketContext 数据类: 统一策略输入接口 (~30 字段)
- strategy_group: 策略族分组 (trend/reversion/sentiment/other)
- get_parameters() / update_parameters(): 运行时参数管理
- supports_regime(): 声明该策略支持的市况
- from_market_data(): 向后兼容的 MarketContext 构造器
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================
#  市况枚举
# ============================================================
class Regime(Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"
    UNKNOWN = "UNKNOWN"


# ============================================================
#  MarketContext — 统一策略输入
# ============================================================
@dataclass
class MarketContext:
    """统一的市场上下文数据类，作为所有策略的标准化输入"""

    symbol: str
    timestamp: float = 0.0

    # ── 价格数据 ──
    price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: int = 0
    avg_volume: int = 0

    # ── 技术指标 ──
    ma_5: float = 0.0
    ma_20: float = 0.0
    ma_50: float = 0.0
    rsi_14: float = 50.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    bollinger_upper: float = 0.0
    bollinger_lower: float = 0.0
    bollinger_mid: float = 0.0
    atr_14: float = 0.0

    # ── 市场状态 ──
    regime: str = "RANGE"
    vix: float = 0.0
    spy_trend: str = "NEUTRAL"
    spy_change_pct: float = 0.0
    market_sentiment: float = 0.0

    # ── 资金/情绪 ──
    net_capital_flow: float = 0.0
    large_order_ratio: float = 0.0
    volume_surge_ratio: float = 1.0
    news_sentiment: float = 0.0

    # ── 期权数据 (可选) ──
    iv_rank: Optional[float] = None
    put_call_ratio: Optional[float] = None
    option_volume: int = 0

    # ── 基本面 (可选) ──
    pe_ratio: Optional[float] = None
    earnings_date: Optional[str] = None
    sector: str = ""

    # ── 扩展字段 ──
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（向后兼容旧策略接口）"""
        return {
            "symbol": self.symbol,
            "price": self.price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "prev_close": self.prev_close,
            "volume": self.volume,
            "avg_volume": self.avg_volume,
            "ma_5": self.ma_5,
            "ma_20": self.ma_20,
            "ma_50": self.ma_50,
            "rsi_14": self.rsi_14,
            "macd": self.macd,
            "macd_signal": self.macd_signal,
            "macd_hist": self.macd_hist,
            "bollinger_upper": self.bollinger_upper,
            "bollinger_lower": self.bollinger_lower,
            "bollinger_mid": self.bollinger_mid,
            "atr_14": self.atr_14,
            "regime": self.regime,
            "vix": self.vix,
            "spy_trend": self.spy_trend,
            "spy_change_pct": self.spy_change_pct,
            "market_sentiment": self.market_sentiment,
            "net_capital_flow": self.net_capital_flow,
            "large_order_ratio": self.large_order_ratio,
            "volume_surge_ratio": self.volume_surge_ratio,
            "news_sentiment": self.news_sentiment,
            "iv_rank": self.iv_rank,
            "put_call_ratio": self.put_call_ratio,
            "option_volume": self.option_volume,
            "pe_ratio": self.pe_ratio,
            "earnings_date": self.earnings_date,
            "sector": self.sector,
            **self.extra,
        }

    @classmethod
    def from_market_data(cls, symbol: str, data: Dict[str, Any]) -> "MarketContext":
        """从旧式市场数据字典构造 MarketContext（向后兼容）"""
        return cls(
            symbol=symbol,
            timestamp=data.get("timestamp", 0.0),
            price=data.get("price", 0.0),
            open=data.get("open", 0.0),
            high=data.get("high", 0.0),
            low=data.get("low", 0.0),
            prev_close=data.get("prev_close", 0.0),
            volume=data.get("volume", 0),
            avg_volume=data.get("avg_volume", 0),
            ma_5=data.get("ma_5", 0.0),
            ma_20=data.get("ma_20", 0.0),
            ma_50=data.get("ma_50", 0.0),
            rsi_14=data.get("rsi_14", 50.0),
            macd=data.get("macd", 0.0),
            macd_signal=data.get("macd_signal", 0.0),
            macd_hist=data.get("macd_hist", 0.0),
            bollinger_upper=data.get("bollinger_upper", 0.0),
            bollinger_lower=data.get("bollinger_lower", 0.0),
            bollinger_mid=data.get("bollinger_mid", 0.0),
            atr_14=data.get("atr_14", 0.0),
            regime=data.get("regime", "RANGE"),
            vix=data.get("vix", 0.0),
            spy_trend=data.get("spy_trend", "NEUTRAL"),
            spy_change_pct=data.get("spy_change_pct", 0.0),
            market_sentiment=data.get("market_sentiment", 0.0),
            net_capital_flow=data.get("net_capital_flow", 0.0),
            large_order_ratio=data.get("large_order_ratio", 0.0),
            volume_surge_ratio=data.get("volume_surge_ratio", 1.0),
            news_sentiment=data.get("news_sentiment", 0.0),
            iv_rank=data.get("iv_rank"),
            put_call_ratio=data.get("put_call_ratio"),
            option_volume=data.get("option_volume", 0),
            pe_ratio=data.get("pe_ratio"),
            earnings_date=data.get("earnings_date"),
            sector=data.get("sector", ""),
            extra=data.get("extra", {}),
        )


# ============================================================
#  策略上下文 (兼容旧接口)
# ============================================================
StrategyContext = MarketContext  # 别名，兼容旧代码


# ============================================================
#  BaseStrategy — 增强策略基类
# ============================================================
class BaseStrategy(ABC):
    """
    增强的策略基类

    子类必须定义:
    - strategy_id: 策略唯一标识
    - strategy_group: 策略族 (trend / reversion / sentiment / other)
    - generate_signal(): 核心信号生成逻辑

    子类可选覆盖:
    - version: 策略版本号
    - description: 策略描述
    - supported_regimes: 支持的市况列表
    - get_parameters(): 运行时参数获取
    - update_parameters(): 运行时参数更新
    """

    # ── 子类必须覆盖的类属性 ──
    strategy_id: str = ""
    strategy_group: str = "other"

    # ── 子类可选覆盖的类属性 ──
    version: str = "1.0.0"
    description: str = ""
    supported_regimes: List[str] = field(default_factory=lambda: [
        "TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL"
    ])

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self._params: Dict[str, Any] = params or {}
        self.execution_count: int = 0
        self.last_signal_time: float = 0.0

    # ── 抽象方法 ──
    @abstractmethod
    def generate_signal(self, symbol: str, context: MarketContext):
        """
        生成交易信号

        Args:
            symbol: 股票代码
            context: 市场上下文

        Returns:
            SignalScore 对象，或无信号时返回 None
        """
        ...

    # ── 参数管理 ──
    def get_parameters(self) -> Dict[str, Any]:
        """获取当前运行时参数"""
        return dict(self._params)

    def update_parameters(self, params: Dict[str, Any]) -> None:
        """更新运行时参数（部分更新）"""
        self._params.update(params)
        logger.info("[%s] 参数已更新: %s", self.strategy_id,
                    {k: v for k, v in params.items()})

    # ── 市况支持 ──
    def supports_regime(self, regime: str) -> bool:
        """检查策略是否支持给定市况"""
        if not isinstance(self.supported_regimes, list):
            return True
        return regime in self.supported_regimes or "ALL" in self.supported_regimes

    # ── 工具方法 ──
    def is_healthy(self) -> bool:
        """策略健康检查（子类可覆盖）"""
        return True

    def reset(self) -> None:
        """重置策略内部状态"""
        self.execution_count = 0
        self.last_signal_time = 0.0

    def __repr__(self) -> str:
        return (f"<{self.__class__.__name__}("
                f"id={self.strategy_id}, "
                f"group={self.strategy_group}, "
                f"v{self.version})>")
