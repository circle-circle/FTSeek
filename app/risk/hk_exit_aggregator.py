#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股主动防御退出聚合器 (HK Exit Aggregator) — Phase 1

设计哲学:
- 多规则并行评估 → 聚合决策（替代优先级短路）
- 动态 ATR 止损 → 替代固定百分比结构止损
- 时段感知波动率 → 适配港股分段交易特性
- 精确归因 → 规则名 + 参数记录

Phase 1 实现:
1. ATR 动态止损（Regime 差异化）
2. Chandelier 追踪止盈（ATR 口径）
3. Breakeven 保本（ATR 口径）
4. 聚合层：最紧有效线选择 + Cost Buffer

双跑模式: 与 dynamic_exit_manager 并行，仅记录差异，不改变行为。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger("ftseek.hk_exit_aggregator")


# ============================================================
#  数据模型
# ============================================================

@dataclass
class StopLineCandidate:
    """候选止损线"""
    rule_name: str
    price: float
    priority: int  # 越小越紧急
    reason: str
    is_stop_loss: bool = True
    close_ratio: float = 1.0


@dataclass
class ExitAggregationResult:
    """聚合退出结果"""
    should_exit: bool = False
    reason: str = ""
    rule_name: str = ""
    close_ratio: float = 1.0
    is_stop_loss: bool = False
    final_stop_price: float = 0.0
    candidates: List[Dict] = field(default_factory=list)  # 所有候选线（用于日志）


# ============================================================
#  时段感知工具
# ============================================================

def get_current_hk_session() -> str:
    """获取当前港股交易时段"""
    now = datetime.now()
    hour, minute = now.hour, now.minute
    t = hour * 60 + minute
    
    if 930 <= t < 1000:
        return "opening"    # 开盘 9:30-10:00
    elif 1000 <= t < 1200:
        return "morning"    # 早盘 10:00-12:00
    elif 1200 <= t < 1300:
        return "lunch"      # 午休 12:00-13:00
    elif 1300 <= t < 1530:
        return "afternoon"  # 午盘 13:00-15:30
    elif 1530 <= t < 1600:
        return "closing"    # 尾盘 15:30-16:00
    else:
        return "off_hours"  # 非交易时段


def get_session_vol_ratio(session: str) -> float:
    """时段波动率比率（基于历史统计）"""
    ratios = {
        "opening": 1.4,     # 开盘高波动
        "morning": 1.0,     # 早盘标准
        "lunch": 0.6,       # 午休低流动性
        "afternoon": 1.0,   # 午盘标准
        "closing": 1.2,     # 尾盘波动上升
        "off_hours": 1.0,   # 默认
    }
    return ratios.get(session, 1.0)


# ============================================================
#  规则实现
# ============================================================

class ATRDynamicStopRule:
    """ATR 动态止损：entry ± atr × multiplier × session_ratio"""
    
    name = "atr_dynamic_stop"
    priority = 1
    
    def calculate_stop_line(
        self,
        entry_price: float,
        current_price: float,
        atr_value: float,
        direction: str,
        config: dict,
    ) -> Optional[StopLineCandidate]:
        if not atr_value or atr_value <= 0 or entry_price <= 0:
            return None
        
        # Regime 差异化乘数（从配置读取，fallback 到默认）
        regime = config.get("market_regime", "normal")
        mult_map = {
            "trending": 2.5,
            "normal": 2.0,
            "volatile": 1.5,
        }
        base_mult = mult_map.get(regime, 2.0)
        
        # 时段感知融合
        session = get_current_hk_session()
        session_ratio = get_session_vol_ratio(session)
        
        effective_atr = atr_value * session_ratio
        stop_distance = effective_atr * base_mult
        
        if direction == "BUY":
            stop_price = entry_price - stop_distance
        else:
            stop_price = entry_price + stop_distance
        
        return StopLineCandidate(
            rule_name=self.name,
            price=stop_price,
            priority=self.priority,
            reason=f"ATR动态止损({base_mult}x,session={session},ratio={session_ratio:.1f})",
            is_stop_loss=True,
        )


class ChandelierTrailRule:
    """Chandelier 追踪止盈：峰值 ± atr × trail_mult (盈利≥activation 时激活)"""
    
    name = "chandelier_trail"
    priority = 3
    
    def calculate_stop_line(
        self,
        entry_price: float,
        current_price: float,
        peak_price: float,
        atr_value: float,
        direction: str,
        config: dict,
    ) -> Optional[StopLineCandidate]:
        if not atr_value or atr_value <= 0 or entry_price <= 0:
            return None
        
        # 有向 profit_atr（亏损时不激活）
        if direction == "BUY":
            profit_atr = (current_price - entry_price) / atr_value
        else:
            profit_atr = (entry_price - current_price) / atr_value
        
        # 激活门槛（ATR 口径）
        activation = config.get("chandelier_activation_atr", 1.5)
        if profit_atr < activation:
            return None  # 未达激活门槛
        
        # Regime 差异化 trail 乘数
        regime = config.get("market_regime", "normal")
        trail_map = {
            "trending": 3.0,   # 趋势行情给更多空间
            "normal": 2.5,
            "volatile": 2.0,   # 高波动收紧
        }
        trail_mult = trail_map.get(regime, 2.5)
        
        if direction == "BUY":
            stop_price = peak_price - atr_value * trail_mult
        else:
            stop_price = peak_price + atr_value * trail_mult
        
        return StopLineCandidate(
            rule_name=self.name,
            price=stop_price,
            priority=self.priority,
            reason=f"Chandelier追踪({trail_mult}x,regime={regime},profit={profit_atr:.1f}ATR)",
            is_stop_loss=False,
        )


class BreakevenGuardRule:
    """保本止损：盈利≥trigger_atr 后，止损移至 entry±buffer"""
    
    name = "breakeven_guard"
    priority = 2
    
    def calculate_stop_line(
        self,
        entry_price: float,
        current_price: float,
        atr_value: float,
        direction: str,
        config: dict,
    ) -> Optional[StopLineCandidate]:
        if not atr_value or atr_value <= 0 or entry_price <= 0:
            return None
        
        # 有向 profit_atr
        if direction == "BUY":
            profit_atr = (current_price - entry_price) / atr_value
        else:
            profit_atr = (entry_price - current_price) / atr_value
        
        # 触发门槛
        trigger = config.get("breakeven_trigger_atr", 1.0)
        if profit_atr < trigger:
            return None  # 未达触发门槛
        
        # 保本价 + buffer
        buffer_pct = config.get("breakeven_buffer_pct", config.get("breakeven_stop.buffer_pct", 0.005))  # 0.5% default, 0.6% from YAML default regime
        
        if direction == "BUY":
            stop_price = entry_price * (1 + buffer_pct)
        else:
            stop_price = entry_price * (1 - buffer_pct)
        
        return StopLineCandidate(
            rule_name=self.name,
            price=stop_price,
            priority=self.priority,
            reason=f"保本止损(buffer={buffer_pct:.1%},profit={profit_atr:.1f}ATR)",
            is_stop_loss=False,
        )


# ============================================================
#  聚合层
# ============================================================

class HKExitAggregator:
    """
    港股退出聚合器：多规则评估 → 聚合决策
    
    替代原有优先级短路逻辑，实现：
    1. 所有规则并行评估
    2. 选择最紧有效止损线
    3. 应用 Cost Buffer
    4. 精确归因
    """
    
    def __init__(self):
        self.rules = [
            ATRDynamicStopRule(),
            BreakevenGuardRule(),
            ChandelierTrailRule(),
        ]
    
    def evaluate(
        self,
        code: str,
        entry_price: float,
        current_price: float,
        peak_price: float,
        atr_value: float,
        direction: str,
        config: dict,
    ) -> ExitAggregationResult:
        """
        评估所有规则，聚合决策
        
        Args:
            code: 股票代码
            entry_price: 入场价
            current_price: 当前价
            peak_price: 峰值价
            atr_value: ATR 值
            direction: 'BUY' 或 'SELL'
            config: 配置字典
        
        Returns:
            ExitAggregationResult
        """
        if entry_price <= 0 or current_price <= 0:
            return ExitAggregationResult()
        
        candidates: List[StopLineCandidate] = []
        
        # 1. 所有规则并行评估
        for rule in self.rules:
            try:
                if hasattr(rule, 'calculate_stop_line'):
                    # 需要 peak_price 的规则
                    if "peak_price" in rule.calculate_stop_line.__code__.co_varnames:
                        result = rule.calculate_stop_line(
                            entry_price=entry_price,
                            current_price=current_price,
                            peak_price=peak_price,
                            atr_value=atr_value,
                            direction=direction,
                            config=config,
                        )
                    else:
                        result = rule.calculate_stop_line(
                            entry_price=entry_price,
                            current_price=current_price,
                            atr_value=atr_value,
                            direction=direction,
                            config=config,
                        )
                    
                    if result:
                        candidates.append(result)
            except Exception as e:
                logger.warning(f"[HKAggregator] {rule.name} 评估异常: {e}")
        
        if not candidates:
            return ExitAggregationResult()
        
        # 2. 选择最紧有效线
        final_stop = self._aggregate_stop_lines(candidates, direction)
        
        # 3. 应用 Cost Buffer（港股成本约 0.14%）
        cost_buffer = self._calculate_cost_buffer(current_price)
        if direction == "BUY":
            final_stop -= cost_buffer
        else:
            final_stop += cost_buffer
        
        # 4. 触发判定
        triggered = self._check_trigger(current_price, final_stop, direction)
        
        if triggered:
            # 找出触发的规则（最紧线对应的规则）
            triggering_rule = self._get_triggering_rule(candidates, direction, final_stop)
            
            return ExitAggregationResult(
                should_exit=True,
                reason=triggering_rule.reason if triggering_rule else "聚合止损触发",
                rule_name=triggering_rule.rule_name if triggering_rule else "unknown",
                close_ratio=triggering_rule.close_ratio if triggering_rule else 1.0,
                is_stop_loss=triggering_rule.is_stop_loss if triggering_rule else True,
                final_stop_price=final_stop,
                candidates=[
                    {"rule": c.rule_name, "price": c.price, "reason": c.reason}
                    for c in candidates
                ],
            )
        
        return ExitAggregationResult(
            should_exit=False,
            final_stop_price=final_stop,
            candidates=[
                {"rule": c.rule_name, "price": c.price, "reason": c.reason}
                for c in candidates
            ],
        )
    
    def _aggregate_stop_lines(
        self, candidates: List[StopLineCandidate], direction: str
    ) -> float:
        """
        聚合止损线：多头取最高（最紧），空头取最低（最紧）
        """
        if direction == "BUY":
            return max(c.price for c in candidates)
        else:
            return min(c.price for c in candidates)
    
    def _calculate_cost_buffer(self, current_price: float) -> float:
        """
        计算成本缓冲：让出 k × cost_bps 的价格空间
        港股成本：佣金 + 印花税 + 交易费 ≈ 0.14% (140 bps)
        """
        HK_COST_BPS = 140
        k = 1.5  # 缓冲系数
        return current_price * (HK_COST_BPS / 10000.0) * k
    
    def _check_trigger(
        self, current_price: float, final_stop: float, direction: str
    ) -> bool:
        """触发判定"""
        if direction == "BUY":
            return current_price <= final_stop
        else:
            return current_price >= final_stop
    
    def _get_triggering_rule(
        self,
        candidates: List[StopLineCandidate],
        direction: str,
        final_stop_with_buffer: float,
    ) -> Optional[StopLineCandidate]:
        """找出触发的规则（最紧线对应的规则）"""
        if not candidates:
            return None
        
        # 找到与 final_stop 最接近的候选线
        best = None
        min_diff = float("inf")
        
        for c in candidates:
            diff = abs(c.price - final_stop_with_buffer)
            if diff < min_diff:
                min_diff = diff
                best = c
        
        return best


# ============================================================
#  全局单例
# ============================================================

_hk_aggregator: Optional[HKExitAggregator] = None

def get_hk_exit_aggregator() -> HKExitAggregator:
    global _hk_aggregator
    if _hk_aggregator is None:
        _hk_aggregator = HKExitAggregator()
    return _hk_aggregator
