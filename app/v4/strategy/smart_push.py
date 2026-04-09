#!/usr/bin/env python3
"""
SmartPushDecision - 智能推送决策模块

FTSeek V4 日内交易优化：
- 自动执行：普通日内信号（置信度<0.75）
- 人工确认：高质量信号（置信度≥0.75 + 涨势确认）

决策逻辑：
1. 信号质量评估（四步确认结果）
2. 涨势确认（技术面 + 动量）
3. 市场环境适配
4. 推送决策（自动执行 or 人工确认）
"""

import time
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger("ftseek.v4.smart_push")
logger.propagate = False  # 防止日志重复


class PushDecision(str, Enum):
    """推送决策类型"""
    AUTO_EXECUTE = "auto_execute"      # 自动执行（普通信号）
    MANUAL_CONFIRM = "manual_confirm"  # 人工确认（高质量）
    SKIP = "skip"                       # 跳过（不符合条件）


@dataclass
class PushRecommendation:
    """推送建议"""
    decision: PushDecision
    reason: str
    confidence: float
    signal_quality: str  # HIGH, MEDIUM, LOW
    momentum_confirmed: bool
    pattern_confirmed: bool
    market_condition: str
    
    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "signal_quality": self.signal_quality,
            "momentum_confirmed": self.momentum_confirmed,
            "pattern_confirmed": self.pattern_confirmed,
            "market_condition": self.market_condition,
        }


class SmartPushDecision:
    """
    智能推送决策系统
    
    根据信号质量和市场条件，决定是自动执行还是推送人工确认。
    
    决策流程：
    1. 检查四步确认结果
    2. 评估涨势（技术面 + 动量）
    3. 检查市场环境
    4. 做出推送决策
    
    推送标准：
    - 人工确认：置信度≥0.75 + 涨势确认 + 强信号
    - 自动执行：置信度 0.60-0.75 或 普通日内信号
    - 跳过：置信度<0.60 或 不符合条件
    
    Example:
        >>> push = SmartPushDecision()
        >>> signal = {
        ...     'symbol': 'AAPL',
        ...     'action': 'BUY',
        ...     'final_score': 0.82,
        ...     'confidence': 0.85,
        ... }
        >>> recommendation = push.evaluate(signal)
        >>> if recommendation.decision == PushDecision.MANUAL_CONFIRM:
        ...     print("推送人工确认")
    """
    
    # 默认配置
    DEFAULT_CONFIG = {
        # 人工确认阈值
        'manual_confirm_min_confidence': 0.75,
        'manual_confirm_min_score': 0.70,
        
        # 涨势确认阈值
        'momentum_threshold': 0.70,
        'pattern_threshold': 0.65,
        
        # 自动执行阈值
        'auto_execute_min_confidence': 0.60,
        
        # 市场环境
        'min_market_condition': 0.50,
    }
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """初始化智能推送决策系统"""
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self.logger = logging.getLogger("ftseek.v4.smart_push")
logger.propagate = False  # 防止日志重复
    
    def evaluate_momentum(
        self,
        signal: Dict[str, Any],
        market_data: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, float]:
        """
        评估涨势（动量确认）
        
        Args:
            signal: 交易信号
            market_data: 市场数据
        
        Returns:
            (是否确认，动量评分)
        """
        # 从信号中获取动量评分
        momentum_score = signal.get('momentum_score', 0.0)
        
        # 如果有市场数据，进一步验证
        if market_data:
            # 检查价格趋势
            price_trend = market_data.get('price_trend', 'neutral')
            volume_ratio = market_data.get('volume_ratio', 1.0)
            
            # 涨势确认：价格上涨 + 放量
            if price_trend == 'up' and volume_ratio > 1.2:
                momentum_score = min(1.0, momentum_score + 0.1)
            elif price_trend == 'down':
                momentum_score = max(0.0, momentum_score - 0.2)
        
        confirmed = momentum_score >= self.config['momentum_threshold']
        
        return confirmed, momentum_score
    
    def evaluate_pattern(
        self,
        signal: Dict[str, Any]
    ) -> Tuple[bool, float]:
        """
        评估技术形态
        
        Args:
            signal: 交易信号
        
        Returns:
            (是否确认，形态评分)
        """
        pattern_score = signal.get('pattern_score', 0.0)
        confirmed = pattern_score >= self.config['pattern_threshold']
        
        return confirmed, pattern_score
    
    def evaluate_market_condition(
        self,
        market_data: Optional[Dict[str, Any]] = None
    ) -> Tuple[str, float]:
        """
        评估市场环境
        
        Args:
            market_data: 市场数据
        
        Returns:
            (市场状况，市场评分)
        """
        if not market_data:
            return "neutral", 0.50
        
        market_score = market_data.get('market_score', 0.50)
        
        if market_score >= 0.70:
            condition = "bullish"
        elif market_score >= 0.50:
            condition = "neutral"
        else:
            condition = "bearish"
        
        return condition, market_score
    
    def evaluate(
        self,
        signal: Dict[str, Any],
        market_data: Optional[Dict[str, Any]] = None
    ) -> PushRecommendation:
        """
        综合评估推送决策
        
        Args:
            signal: 交易信号
            market_data: 市场数据
        
        Returns:
            PushRecommendation: 推送建议
        """
        symbol = signal.get('symbol', 'UNKNOWN')
        action = signal.get('action', 'BUY')
        
        self.logger.info(f"评估推送决策：{symbol} {action}")
        
        # 1. 获取信号质量指标
        final_score = signal.get('final_score', 0.0)
        confidence = signal.get('confidence', 0.0)
        
        # 2. 评估涨势
        momentum_confirmed, momentum_score = self.evaluate_momentum(signal, market_data)
        pattern_confirmed, pattern_score = self.evaluate_pattern(signal)
        
        # 3. 评估市场环境
        market_condition, market_score = self.evaluate_market_condition(market_data)
        
        # 4. 综合评分
        combined_score = (
            final_score * 0.35 +
            confidence * 0.35 +
            momentum_score * 0.15 +
            pattern_score * 0.15
        )
        
        # 5. 做出决策
        decision = PushDecision.SKIP
        reason = ""
        signal_quality = "LOW"
        
        # 人工确认条件：高置信度 + 涨势确认 + 强信号
        if (confidence >= self.config['manual_confirm_min_confidence'] and
            final_score >= self.config['manual_confirm_min_score'] and
            momentum_confirmed and
            pattern_confirmed and
            market_score >= self.config['min_market_condition']):
            
            decision = PushDecision.MANUAL_CONFIRM
            reason = f"高质量信号：置信度{confidence:.0%} + 涨势确认 + 形态确认"
            signal_quality = "HIGH"
            
            self.logger.info(f"✅ 推送人工确认：{symbol} {action}")
        
        # 自动执行条件：中等置信度
        elif (confidence >= self.config['auto_execute_min_confidence'] and
              market_score >= self.config['min_market_condition'] - 0.1):
            
            decision = PushDecision.AUTO_EXECUTE
            reason = f"普通信号：置信度{confidence:.0%}，自动执行"
            signal_quality = "MEDIUM"
            
            self.logger.info(f"⚡ 自动执行：{symbol} {action}")
        
        # 跳过条件：低质量信号
        else:
            if confidence < self.config['auto_execute_min_confidence']:
                reason = f"置信度过低：{confidence:.0%} < {self.config['auto_execute_min_confidence']:.0%}"
            elif market_score < self.config['min_market_condition'] - 0.1:
                reason = f"市场环境不佳：{market_condition}"
            else:
                reason = f"综合评分不足：{combined_score:.2f}"
            
            signal_quality = "LOW"
            
            self.logger.warning(f"❌ 跳过：{symbol} {action} - {reason}")
        
        return PushRecommendation(
            decision=decision,
            reason=reason,
            confidence=combined_score,
            signal_quality=signal_quality,
            momentum_confirmed=momentum_confirmed,
            pattern_confirmed=pattern_confirmed,
            market_condition=market_condition,
        )
    
    def should_push_manual_confirm(
        self,
        signal: Dict[str, Any],
        market_data: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        """
        快速判断是否推送人工确认
        
        Args:
            signal: 交易信号
            market_data: 市场数据
        
        Returns:
            (是否推送，原因)
        """
        recommendation = self.evaluate(signal, market_data)
        
        if recommendation.decision == PushDecision.MANUAL_CONFIRM:
            return True, recommendation.reason
        else:
            return False, recommendation.reason


# 全局实例
_push_instance: Optional[SmartPushDecision] = None


def get_push_decision_system(config: Optional[Dict[str, Any]] = None) -> SmartPushDecision:
    """获取智能推送决策系统实例"""
    global _push_instance
    if _push_instance is None:
        _push_instance = SmartPushDecision(config)
    return _push_instance
