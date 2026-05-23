"""
因子基类 (Factor Base) — FTSeek-US 策略因子可插拔框架

设计原则:
1. 所有因子统一接口: compute() → FactorValue
2. 参数可序列化、可扫描 (param_space)
3. 纯函数，无副作用 — compute() 不修改任何外部状态
4. 与现有 GateResult 体系共存，不替换

时间框架:
- TICK/1min/5min/15min/daily — 因子声明自己的工作频率
- 组合器 (FactorAssembly) 负责对齐多时间框架

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum
from itertools import product

logger = logging.getLogger(__name__)


# ============================================================
#  时间框架枚举
# ============================================================
class FactorTimeframe(Enum):
    TICK = "tick"
    MIN_1 = "1min"
    MIN_5 = "5min"
    MIN_15 = "15min"
    MIN_30 = "30min"
    HOURLY = "1h"
    DAILY = "daily"
    WEEKLY = "weekly"


# ============================================================
#  因子输出 — 标准化结构
# ============================================================
@dataclass
class FactorValue:
    """
    因子计算输出

    Attributes:
        name: 因子名称 (e.g., "SSS", "GDIAN", "MFFF")
        value: 原始数值 (因子自身的度量单位)
        score: 归一化评分 [-1, 1]
            -1 = 极端看空信号
             0 = 中性
            +1 = 极端看多信号
        direction_hint: 因子建议的交易方向
        confidence: 自评置信度 [0, 1]
        params_used: 本次计算使用的参数快照
        metadata: 额外信息 (中间值、debug 信息等)
    """
    name: str
    value: float = 0.0
    score: float = 0.0
    direction_hint: str = "NEUTRAL"   # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float = 0.5
    params_used: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_bullish(self, threshold: float = 0.3) -> bool:
        """因子是否看多"""
        return self.score >= threshold

    def is_bearish(self, threshold: float = -0.3) -> bool:
        """因子是否看空"""
        return self.score <= threshold

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "score": round(self.score, 4),
            "direction_hint": self.direction_hint,
            "confidence": round(self.confidence, 3),
            "params_used": self.params_used,
            "metadata": self.metadata,
        }


# ============================================================
#  因子参数 — 可序列化、可扫描
# ============================================================
@dataclass
class FactorParams:
    """
    因子参数

    params: 实际参数键值对
    version: 参数版本 (用于追溯)
    description: 人类可读描述
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "params": self.params,
            "version": self.version,
            "description": self.description,
        }


# ============================================================
#  因子基类
# ============================================================
class BaseFactor(ABC):
    """
    因子基类 — 所有因子必须实现

    子类职责:
    1. 声明 name, timeframe, version
    2. 定义 param_space (参数扫描空间)
    3. 实现 compute() — 纯函数
    4. 可选 override validate_params()
    """

    # 子类必须覆盖
    name: str = ""
    timeframe: FactorTimeframe = FactorTimeframe.MIN_5
    version: str = "1.0"

    # 参数扫描空间: {param_name: [value1, value2, ...]}
    # 为空表示该因子无参数可调
    param_space: Dict[str, List[Any]] = {}

    def __init__(self, params: Optional[FactorParams] = None):
        self.params = params or FactorParams(params={})

    @abstractmethod
    def compute(self, symbol: str, market_data: Dict[str, Any],
                ctx: Optional[Dict[str, Any]] = None) -> FactorValue:
        """
        计算因子值 — 纯函数，无副作用

        Args:
            symbol: 标的代码 (e.g., "US.AAPL")
            market_data: 行情数据，结构由 timeframe 决定:
                - 必须包含: price, volume, timestamp
                - 通常包含: open, high, low, close, prev_close
                - 技术指标: ma_5, ma_20, ma_50, rsi_14, macd, bollinger_*, atr_14
                - K线序列: bars (List[Dict], 最近 N 根 K 线)
            ctx: 可选的额外上下文 (宏观数据、新闻情绪等)

        Returns:
            FactorValue: 标准化因子输出
        """
        ...

    def validate_params(self) -> bool:
        """参数合法性检查。子类可 override。"""
        return True

    def get_param_combinations(self) -> List[FactorParams]:
        """
        生成 param_space 的所有组合 (用于网格扫描)

        Returns:
            List[FactorParams]: 所有参数组合
        """
        if not self.param_space:
            return [self.params]

        keys = list(self.param_space.keys())
        values = list(self.param_space.values())
        combinations = []

        for combo in product(*values):
            params_dict = dict(zip(keys, combo))
            # 保留非扫描参数
            for k, v in self.params.params.items():
                if k not in params_dict:
                    params_dict[k] = v
            combinations.append(FactorParams(
                params=params_dict,
                version=self.version,
                description=f"scan: {params_dict}",
            ))

        return combinations

    def describe(self) -> str:
        """人类可读描述"""
        return f"{self.name} v{self.version} [{self.timeframe.value}]"

    def __repr__(self):
        return f"<{self.__class__.__name__}({self.describe()})>"


# ============================================================
#  因子条件评估器 — 用于 FactorAssembly 的 entry_conditions
# ============================================================
class ConditionEvaluator:
    """
    安全评估因子条件表达式

    支持的表达式格式:
        "SSS.score >= -1.5"
        "GDIAN.value <= 35"
        "MFFF.score >= 1.0"
        "VolRegime.value < 30"
    """

    _SAFE_OPS = {">=": lambda a, b: a >= b,
                 "<=": lambda a, b: a <= b,
                 ">": lambda a, b: a > b,
                 "<": lambda a, b: a < b,
                 "==": lambda a, b: abs(a - b) < 1e-9,
                 "!=": lambda a, b: abs(a - b) >= 1e-9}

    @classmethod
    def evaluate(cls, condition: str,
                 factor_values: Dict[str, FactorValue]) -> Tuple[bool, str]:
        """
        评估单个条件

        Args:
            condition: 条件表达式 (e.g., "SSS.score >= -1.5")
            factor_values: 已计算的因子值字典

        Returns:
            (passed, reason)
        """
        try:
            # 解析: "FACTOR.ATTR OP VALUE"
            parts = condition.strip().split()
            if len(parts) != 3:
                return False, f"invalid condition format: {condition}"

            factor_attr, op, value_str = parts

            if op not in cls._SAFE_OPS:
                return False, f"unsupported operator: {op}"

            # 解析 factor.attr
            if "." not in factor_attr:
                return False, f"missing attribute in: {factor_attr}"

            factor_name, attr = factor_attr.split(".", 1)
            if factor_name not in factor_values:
                return False, f"factor not found: {factor_name}"

            fv = factor_values[factor_name]
            actual = getattr(fv, attr, None)
            if actual is None:
                return False, f"attribute not found: {factor_name}.{attr}"

            expected = float(value_str)
            passed = cls._SAFE_OPS[op](float(actual), expected)

            return passed, f"{factor_name}.{attr}={actual:.4f} {op} {expected} → {'✅' if passed else '❌'}"

        except Exception as e:
            return False, f"eval error: {condition} → {e}"
