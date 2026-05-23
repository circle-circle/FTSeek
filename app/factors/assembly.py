"""
因子组合器 (FactorAssembly) — 将多个因子组合为一个完整的入场策略

设计:
1. 每个 assembly 定义: 因子列表 + 入场条件 + 方向门控
2. 条件表达式安全评估 (ConditionEvaluator)
3. 从 YAML 加载预定义策略变体
4. 支持动态组装新变体

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    BaseFactor, ConditionEvaluator, FactorParams,
    FactorTimeframe, FactorValue,
)
from .registry import FactorRegistry

logger = logging.getLogger(__name__)


# ============================================================
#  因子槽位定义
# ============================================================
@dataclass
class FactorSlot:
    """
    因子在组合中的槽位

    name: 因子名称 (必须在 registry 中注册)
    params: 该槽位的参数覆盖
    weight: 在综合评分中的权重
    condition: 入场条件表达式 (e.g., "SSS.score >= -1.5")
    """
    name: str
    params: Dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    condition: str = ""


# ============================================================
#  因子组合评估结果
# ============================================================
@dataclass
class AssemblyResult:
    """组合评估结果"""
    assembly_name: str
    passed: bool                      # 是否满足所有入场条件
    composite_score: float            # 综合评分
    direction: str                    # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float                 # 综合置信度
    factor_values: Dict[str, FactorValue] = field(default_factory=dict)
    condition_results: List[Tuple[str, bool, str]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assembly_name": self.assembly_name,
            "passed": self.passed,
            "composite_score": round(self.composite_score, 4),
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "factors": {k: v.to_dict() for k, v in self.factor_values.items()},
            "conditions": [(c, p, r) for c, p, r in self.condition_results],
            "metadata": self.metadata,
        }


# ============================================================
#  因子组合器
# ============================================================
class FactorAssembly:
    """
    因子组合 — 可序列化为 YAML 的策略入场定义

    示例:
        assembly = FactorAssembly(
            name="stable_15m_relaxed_oversold_tight_trend",
            timeframe="15min",
            slots=[
                FactorSlot(name="SSS", params={"threshold": -1.5},
                           weight=0.4, condition="SSS.score >= -1.5"),
                FactorSlot(name="GDIAN", params={"tight_threshold": 35},
                           weight=0.3, condition="GDIAN.value <= 35"),
                FactorSlot(name="MFFF", params={"threshold": 1.0},
                           weight=0.3, condition="MFFF.score >= 1.0"),
            ],
            direction_gate="CONSENSUS",
            min_composite_score=0.3,
        )
    """

    def __init__(
        self,
        name: str,
        timeframe: str = "15min",
        slots: Optional[List[FactorSlot]] = None,
        direction_gate: str = "CONSENSUS",
        min_composite_score: float = 0.3,
        direction_preference: str = "ANY",  # "LONG" | "SHORT" | "ANY"
        description: str = "",
    ):
        self.name = name
        self.timeframe = timeframe
        self.slots = slots or []
        self.direction_gate = direction_gate
        self.min_composite_score = min_composite_score
        self.direction_preference = direction_preference
        self.description = description

    def evaluate(self, symbol: str, market_data: Dict[str, Any],
                 ctx: Optional[Dict[str, Any]] = None,
                 registry: Optional[FactorRegistry] = None
                 ) -> AssemblyResult:
        """
        评估因子组合

        Args:
            symbol: 标的代码
            market_data: 行情数据
            ctx: 额外上下文
            registry: 因子注册中心 (None 使用全局单例)

        Returns:
            AssemblyResult
        """
        if registry is None:
            registry = FactorRegistry.instance()

        factor_values: Dict[str, FactorValue] = {}
        condition_results: List[Tuple[str, bool, str]] = []
        weighted_scores: List[float] = []
        total_weight = 0.0

        # 1. 计算所有因子值
        for slot in self.slots:
            try:
                params = FactorParams(params=slot.params) if slot.params else None
                factor = registry.create(slot.name, params)
                fv = factor.compute(symbol, market_data, ctx)
                factor_values[slot.name] = fv
                weighted_scores.append(fv.score * slot.weight)
                total_weight += slot.weight
            except Exception as e:
                logger.warning("[%s] 因子 %s 计算失败: %s", self.name, slot.name, e)
                condition_results.append(
                    (f"{slot.name} compute", False, f"error: {e}"))

        # 2. 评估入场条件
        all_conditions_passed = True
        for slot in self.slots:
            if not slot.condition:
                continue  # 无条件 = 始终通过

            passed, reason = ConditionEvaluator.evaluate(
                slot.condition, factor_values)
            condition_results.append((slot.condition, passed, reason))

            if not passed:
                all_conditions_passed = False

        # 3. 综合评分
        if total_weight > 0:
            composite_score = sum(weighted_scores) / total_weight
        else:
            composite_score = 0.0

        # 4. 方向判定
        direction = self._determine_direction(factor_values, composite_score)

        # 5. 方向偏好过滤
        if self.direction_preference != "ANY":
            if direction != self.direction_preference and direction != "NEUTRAL":
                all_conditions_passed = False
                condition_results.append(
                    ("direction_preference", False,
                     f"prefer {self.direction_preference} but got {direction}"))

        # 6. 最低综合评分检查
        score_passed = abs(composite_score) >= self.min_composite_score
        if not score_passed:
            condition_results.append(
                ("min_composite_score", False,
                 f"|{composite_score:.4f}| < {self.min_composite_score}"))

        # 7. 综合置信度
        if factor_values:
            avg_confidence = sum(fv.confidence for fv in factor_values.values()) / len(factor_values)
        else:
            avg_confidence = 0.0

        passed = all_conditions_passed and score_passed

        return AssemblyResult(
            assembly_name=self.name,
            passed=passed,
            composite_score=composite_score,
            direction=direction if passed else "NEUTRAL",
            confidence=avg_confidence,
            factor_values=factor_values,
            condition_results=condition_results,
            metadata={
                "timeframe": self.timeframe,
                "direction_gate": self.direction_gate,
                "min_composite_score": self.min_composite_score,
                "n_factors": len(factor_values),
            },
        )

    def _determine_direction(self, factor_values: Dict[str, FactorValue],
                              composite_score: float) -> str:
        """根据 direction_gate 策略确定交易方向"""
        if not factor_values:
            return "NEUTRAL"

        directions = [fv.direction_hint for fv in factor_values.values()
                       if fv.direction_hint != "NEUTRAL"]

        if not directions:
            return "NEUTRAL"

        if self.direction_gate == "CONSENSUS":
            # 多数投票
            long_count = directions.count("LONG")
            short_count = directions.count("SHORT")
            if long_count > short_count and long_count > len(directions) / 2:
                return "LONG"
            elif short_count > long_count and short_count > len(directions) / 2:
                return "SHORT"
            return "NEUTRAL"

        elif self.direction_gate == "ALL_LONG":
            return "LONG" if all(d == "LONG" for d in directions) else "NEUTRAL"

        elif self.direction_gate == "ALL_SHORT":
            return "SHORT" if all(d == "SHORT" for d in directions) else "NEUTRAL"

        elif self.direction_gate == "ANY":
            # 跟随综合评分方向
            if composite_score > 0:
                return "LONG"
            elif composite_score < 0:
                return "SHORT"
            return "NEUTRAL"

        return "NEUTRAL"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "timeframe": self.timeframe,
            "direction_gate": self.direction_gate,
            "min_composite_score": self.min_composite_score,
            "direction_preference": self.direction_preference,
            "description": self.description,
            "slots": [
                {"name": s.name, "params": s.params,
                 "weight": s.weight, "condition": s.condition}
                for s in self.slots
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FactorAssembly:
        """从字典构建"""
        slots = [
            FactorSlot(
                name=s["name"],
                params=s.get("params", {}),
                weight=s.get("weight", 1.0),
                condition=s.get("condition", ""),
            )
            for s in data.get("slots", [])
        ]
        return cls(
            name=data["name"],
            timeframe=data.get("timeframe", "15min"),
            slots=slots,
            direction_gate=data.get("direction_gate", "CONSENSUS"),
            min_composite_score=data.get("min_composite_score", 0.3),
            direction_preference=data.get("direction_preference", "ANY"),
            description=data.get("description", ""),
        )


# ============================================================
#  策略变体库 (从 YAML 加载)
# ============================================================
class StrategyVariantLibrary:
    """
    策略变体库 — 管理所有预定义的因子组合

    从 config/strategy_variants.yaml 加载。
    """

    def __init__(self):
        self._variants: Dict[str, FactorAssembly] = {}

    def load(self, yaml_path: str = "config/strategy_variants.yaml") -> int:
        """加载策略变体配置"""
        path = Path(yaml_path)
        if not path.exists():
            logger.warning("[StrategyVariantLibrary] 配置文件不存在: %s", yaml_path)
            return 0

        with open(path) as f:
            data = yaml.safe_load(f)

        count = 0
        for name, variant_data in data.get("strategy_variants", {}).items():
            variant_data["name"] = name
            try:
                assembly = FactorAssembly.from_dict(variant_data)
                self._variants[name] = assembly
                count += 1
            except Exception as e:
                logger.warning("[StrategyVariantLibrary] 加载 %s 失败: %s", name, e)

        logger.info("[StrategyVariantLibrary] 已加载 %d 个策略变体", count)
        return count

    def get(self, name: str) -> Optional[FactorAssembly]:
        return self._variants.get(name)

    def list_variants(self) -> List[str]:
        return sorted(self._variants.keys())

    def add(self, assembly: FactorAssembly):
        self._variants[assembly.name] = assembly

    def __len__(self):
        return len(self._variants)

    def __contains__(self, name: str):
        return name in self._variants
