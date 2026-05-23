"""
因子基类定义

所有因子必须继承 BaseFactor 并实现 compute() 方法。
因子是纯函数: 输入 (symbol, market_data, ctx) → 输出 FactorValue，无副作用。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FactorTimeframe(str, Enum):
    """因子工作周期"""
    TICK = "tick"
    MIN_1 = "1min"
    MIN_5 = "5min"
    MIN_15 = "15min"
    MIN_30 = "30min"
    HOUR_1 = "1h"
    DAILY = "daily"


@dataclass
class FactorParams:
    """
    因子参数 — 可序列化为 YAML，可网格扫描
    
    Attributes:
        params: 参数键值对 (e.g., {"rsi_period": 14, "threshold": -1.5})
        version: 参数版本 (用于回测追溯)
        description: 参数说明
    """
    params: Dict[str, Any] = field(default_factory=dict)
    version: str = "1.0"
    description: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.params[key]

    def __contains__(self, key: str) -> bool:
        return key in self.params


@dataclass
class FactorValue:
    """
    因子输出 — 标准化结构
    
    所有因子的 compute() 方法必须返回此结构。
    
    Attributes:
        name: 因子名称 (e.g., "SSS", "GDIAN", "MFFF")
        value: 原始计算值 (无归一化)
        score: 归一化评分 [-1, 1]，可跨因子比较
        direction_hint: 方向建议 "LONG" | "SHORT" | "NEUTRAL"
        confidence: 因子自评置信度 [0, 1]
        metadata: 额外信息 (e.g., lookback 窗口, 中间变量)
        params_used: 本次计算使用的参数快照
    """
    name: str
    value: float
    score: float
    direction_hint: str = "NEUTRAL"  # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    params_used: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典 (用于 JSON 存储 / 辩论引擎输入)"""
        return {
            "name": self.name,
            "value": round(self.value, 6),
            "score": round(self.score, 4),
            "direction_hint": self.direction_hint,
            "confidence": round(self.confidence, 4),
            "metadata": self.metadata,
            "params_used": self.params_used,
        }


class BaseFactor(ABC):
    """
    因子基类 — 所有因子必须实现
    
    设计原则:
    1. compute() 是纯函数，不修改输入，不写文件/DB
    2. 参数通过 FactorParams 注入，支持网格扫描
    3. 输出统一为 FactorValue，可跨因子比较
    4. param_space 声明参数搜索空间，用于自动优化
    
    Usage:
        class MyFactor(BaseFactor):
            name = "MY_FACTOR"
            timeframe = FactorTimeframe.MIN_15
            param_space = {"threshold": [-2, -1, 0, 1]}
            
            def compute(self, symbol, market_data, ctx):
                # 纯计算逻辑
                ...
                return FactorValue(name=self.name, value=x, score=s)
    """

    # 子类必须设置
    name: str = ""
    timeframe: FactorTimeframe = FactorTimeframe.MIN_5
    version: str = "1.0"

    # 参数搜索空间 (用于网格扫描)
    # 格式: {"param_name": [value1, value2, ...], ...}
    param_space: Dict[str, List[Any]] = {}

    def __init__(self, params: Optional[FactorParams] = None):
        self.params = params or FactorParams(params={})
        if not self.name:
            raise ValueError(f"{type(self).__name__} must set class attribute 'name'")

    @abstractmethod
    def compute(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        ctx: Optional[Dict[str, Any]] = None,
    ) -> FactorValue:
        """
        计算因子值 — 纯函数，无副作用
        
        Args:
            symbol: 标的代码 (e.g., "AAPL", "00700")
            market_data: 市场数据字典，包含:
                - bars: List[Dict] OHLCV K线 (最近 N 根)
                - 每根 bar: {open, high, low, close, volume, timestamp}
                - 可选: atr, vix, spy_change 等市场上下文
            ctx: 额外上下文 (e.g., 持仓信息, 当前 regime)
        
        Returns:
            FactorValue: 标准化的因子输出
        """
        ...

    def validate_params(self) -> bool:
        """参数合法性检查 — 子类可覆写"""
        return True

    def describe(self) -> str:
        """人类可读的因子描述"""
        param_str = ", ".join(f"{k}={v}" for k, v in self.params.params.items()) if self.params.params else "default"
        return f"{self.name} v{self.version} [{self.timeframe.value}] ({param_str})"

    def get_param_combinations(self) -> List[FactorParams]:
        """
        生成参数空间的所有组合 (用于网格扫描)
        
        Returns:
            List[FactorParams]: 所有参数组合
        """
        if not self.param_space:
            return [self.params]

        import itertools
        keys = list(self.param_space.keys())
        value_lists = [self.param_space[k] for k in keys]

        combinations = []
        for values in itertools.product(*value_lists):
            params_dict = dict(zip(keys, values))
            combinations.append(FactorParams(
                params=params_dict,
                version=self.version,
                description=f"Grid scan: {params_dict}",
            ))
        return combinations

    @staticmethod
    def _clamp(value: float, min_val: float, max_val: float) -> float:
        """将值限制在 [min_val, max_val] 范围内"""
        return max(min_val, min(max_val, value))

    @staticmethod
    def _safe_divide(a: float, b: float, default: float = 0.0) -> float:
        """安全除法，避免 ZeroDivisionError"""
        if b == 0 or b is None:
            return default
        return a / b
