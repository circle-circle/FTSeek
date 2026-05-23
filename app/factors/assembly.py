"""
因子组合器 (Factor Assembly)

将多个因子组合成策略入场信号，支持：
- 因子权重配置
- 入场条件表达式 (如 "SSS.score >= -1.5")
- 方向门禁 (ALL_LONG / ALL_SHORT / CONSENSUS / ANY)
- 综合评分计算
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import re

from app.factors.base import FactorValue


@dataclass
class FactorAssembly:
    """
    因子组合 — 可序列化为 YAML
    
    定义策略的入场因子配置和条件。
    """
    name: str                                    # "stable_15m_relaxed_oversold_tight_trend"
    factors: List[Dict[str, Any]]                # [{name: "SSS", params: {...}, weight: 0.4}, ...]
    direction_gate: str                          # "ALL_LONG" | "ALL_SHORT" | "CONSENSUS" | "ANY"
    min_composite_score: float                   # 最低综合评分门槛
    timeframe: str                               # 组合的工作时间框架
    entry_conditions: List[str]                  # ["SSS.score >= -1.5", "GDIAN.value <= 35"]
    description: str = ""
    
    def evaluate(self, factor_values: Dict[str, FactorValue]) -> Tuple[bool, float, str]:
        """
        评估是否满足入场条件
        
        Args:
            factor_values: 因子名称 -> FactorValue 映射
            
        Returns:
            Tuple[bool, float, str]: (是否入场, 综合评分, 方向建议)
        """
        # 1. 检查所有必需因子是否存在
        required_factors = {f['name'] for f in self.factors}
        missing = required_factors - set(factor_values.keys())
        if missing:
            return False, 0.0, "NEUTRAL"
        
        # 2. 评估入场条件
        conditions_met = self._evaluate_conditions(factor_values)
        if not conditions_met:
            return False, 0.0, "NEUTRAL"
        
        # 3. 计算综合评分 (加权)
        composite_score = self._compute_composite_score(factor_values)
        if composite_score < self.min_composite_score:
            return False, composite_score, "NEUTRAL"
        
        # 4. 方向门禁
        direction = self._evaluate_direction_gate(factor_values)
        if direction == "NEUTRAL":
            return False, composite_score, "NEUTRAL"
        
        return True, composite_score, direction
    
    def _evaluate_conditions(self, factor_values: Dict[str, FactorValue]) -> bool:
        """评估所有入场条件"""
        for condition in self.entry_conditions:
            if not self._evaluate_single_condition(condition, factor_values):
                return False
        return True
    
    def _evaluate_single_condition(
        self, 
        condition: str, 
        factor_values: Dict[str, FactorValue]
    ) -> bool:
        """
        评估单个条件表达式
        
        支持格式: "FACTOR_NAME.attribute op value"
        例如: "SSS.score >= -1.5", "GDIAN.value <= 35"
        """
        # 解析表达式
        match = re.match(r'(\w+)\.(\w+)\s*(>=|<=|>|<|==|!=)\s*([+-]?\d*\.?\d+)', condition)
        if not match:
            return False
        
        factor_name = match.group(1)
        attribute = match.group(2)
        operator = match.group(3)
        threshold = float(match.group(4))
        
        # 获取因子值
        if factor_name not in factor_values:
            return False
        
        fv = factor_values[factor_name]
        
        # 获取属性值
        value = getattr(fv, attribute, None)
        if value is None:
            return False
        
        # 比较
        if operator == '>=':
            return value >= threshold
        elif operator == '<=':
            return value <= threshold
        elif operator == '>':
            return value > threshold
        elif operator == '<':
            return value < threshold
        elif operator == '==':
            return abs(value - threshold) < 1e-6
        elif operator == '!=':
            return abs(value - threshold) >= 1e-6
        
        return False
    
    def _compute_composite_score(self, factor_values: Dict[str, FactorValue]) -> float:
        """
        计算加权综合评分
        
        使用因子的 score 字段 (归一化到 [-1, 1])
        """
        total_weight = 0.0
        weighted_sum = 0.0
        
        for factor_config in self.factors:
            name = factor_config['name']
            weight = factor_config.get('weight', 1.0)
            
            if name in factor_values:
                fv = factor_values[name]
                # score 已在 [-1, 1] 范围，乘以 confidence
                contribution = fv.score * fv.confidence * weight
                weighted_sum += contribution
                total_weight += weight
        
        if total_weight == 0:
            return 0.0
        
        return weighted_sum / total_weight
    
    def _evaluate_direction_gate(self, factor_values: Dict[str, FactorValue]) -> str:
        """
        评估方向门禁
        
        - ALL_LONG: 所有因子 direction_hint == "LONG"
        - ALL_SHORT: 所有因子 direction_hint == "SHORT"
        - CONSENSUS: 多数因子方向一致
        - ANY: 任意因子有方向即可
        """
        directions = []
        for factor_config in self.factors:
            name = factor_config['name']
            if name in factor_values:
                fv = factor_values[name]
                directions.append(fv.direction_hint)
        
        if not directions:
            return "NEUTRAL"
        
        if self.direction_gate == "ALL_LONG":
            if all(d == "LONG" for d in directions):
                return "LONG"
            return "NEUTRAL"
        
        elif self.direction_gate == "ALL_SHORT":
            if all(d == "SHORT" for d in directions):
                return "SHORT"
            return "NEUTRAL"
        
        elif self.direction_gate == "CONSENSUS":
            long_count = sum(1 for d in directions if d == "LONG")
            short_count = sum(1 for d in directions if d == "SHORT")
            
            if long_count > short_count and long_count >= len(directions) / 2:
                return "LONG"
            elif short_count > long_count and short_count >= len(directions) / 2:
                return "SHORT"
            return "NEUTRAL"
        
        elif self.direction_gate == "ANY":
            # 返回多数方向
            long_count = sum(1 for d in directions if d == "LONG")
            short_count = sum(1 for d in directions if d == "SHORT")
            
            if long_count > short_count:
                return "LONG"
            elif short_count > long_count:
                return "SHORT"
            return "NEUTRAL"
        
        return "NEUTRAL"
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            'name': self.name,
            'factors': self.factors,
            'direction_gate': self.direction_gate,
            'min_composite_score': self.min_composite_score,
            'timeframe': self.timeframe,
            'entry_conditions': self.entry_conditions,
            'description': self.description,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FactorAssembly':
        """从字典创建"""
        return cls(
            name=data['name'],
            factors=data['factors'],
            direction_gate=data['direction_gate'],
            min_composite_score=data['min_composite_score'],
            timeframe=data['timeframe'],
            entry_conditions=data['entry_conditions'],
            description=data.get('description', ''),
        )
