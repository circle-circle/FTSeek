#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策日志触发器 (Decision Log Trigger) - Phase 1

职责:
在关键决策点自动写入 decision_log.json，包括:
1. 信号生成
2. 风控通过/拒绝
3. 交易执行
4. 归因完成
"""

import logging
from typing import Optional, Dict
from datetime import datetime

logger = logging.getLogger("ftseek.evidence.decision_log_trigger")


class DecisionLogTrigger:
    """
    决策日志触发器 -- 在关键决策点自动记录。
    """
    
    def __init__(self, decision_log=None):
        self.decision_log = decision_log
    
    def log_signal_generated(
        self,
        trace_id: str,
        code: str,
        action: str,
        strategy: str,
        confidence: float,
        market_regime: str = "",
        adx: float = 0,
        context: Optional[Dict] = None,
    ):
        """记录信号生成"""
        if not self.decision_log:
            return
        try:
            self.decision_log.log_trade_decision(
                symbol=code,
                action=action,
                confidence=confidence,
                decision="generated",
                context=context or {},
                result="pending",
                market_regime=market_regime,
                strategy=strategy,
                signal_id=trace_id,
            )
            logger.debug(f"[决策日志] 信号生成: {trace_id} {code} {action}")
        except Exception as e:
            logger.debug(f"[决策日志] 信号生成记录失败: {e}")
    
    def log_risk_passed(
        self,
        trace_id: str,
        code: str,
        action: str,
        confidence: float,
        strategy: str,
    ):
        """记录风控通过"""
        if not self.decision_log:
            return
        try:
            self.decision_log.log_trade_decision(
                symbol=code,
                action=action,
                confidence=confidence,
                decision="execute",
                result="risk_passed",
                strategy=strategy,
                signal_id=trace_id,
            )
            logger.debug(f"[决策日志] 风控通过: {trace_id} {code}")
        except Exception as e:
            logger.debug(f"[决策日志] 风控通过记录失败: {e}")
    
    def log_risk_rejected(
        self,
        trace_id: str,
        code: str,
        action: str,
        reason: str,
        strategy: str,
    ):
        """记录风控拒绝"""
        if not self.decision_log:
            return
        try:
            self.decision_log.log_trade_decision(
                symbol=code,
                action=action,
                confidence=0,
                decision="reject",
                result="risk_rejected",
                context={"reject_reason": reason},
                strategy=strategy,
                signal_id=trace_id,
            )
            logger.debug(f"[决策日志] 风控拒绝: {trace_id} {code} - {reason}")
        except Exception as e:
            logger.debug(f"[决策日志] 风控拒绝记录失败: {e}")
    
    def log_trade_executed(
        self,
        trace_id: str,
        code: str,
        action: str,
        price: float,
        quantity: int,
        strategy: str,
        confidence: float,
        market_regime: str = "",
    ):
        """记录交易执行"""
        if not self.decision_log:
            return
        try:
            self.decision_log.log_trade_decision(
                symbol=code,
                action=action,
                confidence=confidence,
                decision="executed",
                result="success",
                context={
                    "price": price,
                    "quantity": quantity,
                    "market_regime": market_regime,
                },
                strategy=strategy,
                signal_id=trace_id,
            )
            logger.debug(f"[决策日志] 交易执行: {trace_id} {code} {action} @ {price}")
        except Exception as e:
            logger.debug(f"[决策日志] 交易执行记录失败: {e}")
    
    def log_attribution_complete(
        self,
        trace_id: str,
        code: str,
        pnl: float,
        overall_score: int,
        direction_correct: bool,
        exit_reason: str,
        strategy: str,
    ):
        """记录归因完成"""
        if not self.decision_log:
            return
        try:
            self.decision_log.log_trade_decision(
                symbol=code,
                action="CLOSE",
                confidence=overall_score / 100.0,
                decision="attributed",
                result="closed",
                context={
                    "pnl": pnl,
                    "overall_score": overall_score,
                    "direction_correct": direction_correct,
                    "exit_reason": exit_reason,
                },
                strategy=strategy,
                signal_id=trace_id,
            )
            logger.debug(f"[决策日志] 归因完成: {trace_id} {code} PnL={pnl:.2f}")
        except Exception as e:
            logger.debug(f"[决策日志] 归因完成记录失败: {e}")


# 全局单例
_decision_log_trigger: Optional[DecisionLogTrigger] = None


def get_decision_log_trigger(decision_log=None) -> DecisionLogTrigger:
    """获取或创建决策日志触发器"""
    global _decision_log_trigger
    if _decision_log_trigger is None:
        _decision_log_trigger = DecisionLogTrigger(decision_log)
    elif decision_log is not None:
        _decision_log_trigger.decision_log = decision_log
    return _decision_log_trigger


def init_decision_log_trigger(decision_log) -> DecisionLogTrigger:
    """初始化决策日志触发器"""
    global _decision_log_trigger
    _decision_log_trigger = DecisionLogTrigger(decision_log)
    return _decision_log_trigger
