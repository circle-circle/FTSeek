#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
退出规则实现 (Exit Rules Implementation) — Phase 1

Phase 1 实现 3 个核心规则：
1. ATR 硬止损
2. 结构止损（固定百分比）
3. 移动止损（Chandelier）
"""

import logging
from typing import Optional

from app.risk.exit_rule_engine import ExitRule, ExitContext, ExitDecision

logger = logging.getLogger("ftseek.exit_rules")


# ============================================================
#  规则 1: ATR 硬止损
# ============================================================

class ATRStopRule(ExitRule):
    """ATR 硬止损：亏损超过 N×ATR 立即平仓"""
    
    @property
    def name(self) -> str:
        return "atr_hard_stop"
    
    @property
    def priority(self) -> int:
        return 1
    
    def enabled(self, config: dict) -> bool:
        return config.get("atr_hard_stop.enabled", True)
    
    def evaluate(self, ctx: ExitContext, config: dict) -> Optional[ExitDecision]:
        if not ctx.atr_value or ctx.atr_value <= 0:
            return None
        
        # 从配置读取参数
        atr_mult = config.get("atr_hard_stop.hard_multiplier", 1.5)
        min_pct = config.get("atr_hard_stop.min_stop_pct", 0.02)
        max_pct = config.get("atr_hard_stop.max_stop_pct", 0.05)
        
        # 计算 ATR 止损距离，受 min/max 限制
        atr_dist = atr_mult * ctx.atr_value / ctx.entry_price
        atr_dist = max(min_pct, min(atr_dist, max_pct))
        
        if ctx.pnl_pct <= -atr_dist:
            return ExitDecision(
                should_exit=True,
                reason=f"ATR硬止损({atr_mult}x): 亏损={ctx.pnl_pct:.1%}, ATR距离={atr_dist:.1%}",
                priority=self.priority,
                close_ratio=1.0,
                is_stop_loss=True
            )
        return None


# ============================================================
#  规则 2: 结构止损
# ============================================================

class StructuralStopRule(ExitRule):
    """结构止损：固定百分比止损（保底规则）"""
    
    @property
    def name(self) -> str:
        return "structural_stop"
    
    @property
    def priority(self) -> int:
        return 2
    
    def enabled(self, config: dict) -> bool:
        return True  # 永远启用，作为最后保底
    
    def evaluate(self, ctx: ExitContext, config: dict) -> Optional[ExitDecision]:
        stop_pct = abs(config.get("structural_stop.stop_loss", -0.035))
        
        if ctx.pnl_pct <= -stop_pct:
            return ExitDecision(
                should_exit=True,
                reason=f"结构止损: 亏损={ctx.pnl_pct:.1%} >= {stop_pct:.1%}",
                priority=self.priority,
                close_ratio=1.0,
                is_stop_loss=True
            )
        return None


# ============================================================
#  规则 3: 移动止损（Chandelier）
# ============================================================

class TrailingStopRule(ExitRule):
    """移动止损：盈利后跟随峰值，回撤触发平仓"""
    
    @property
    def name(self) -> str:
        return "trailing_stop"
    
    @property
    def priority(self) -> int:
        return 7
    
    def enabled(self, config: dict) -> bool:
        return config.get("trailing_stop.enabled", True)
    
    def evaluate(self, ctx: ExitContext, config: dict) -> Optional[ExitDecision]:
        trigger = config.get("trailing_stop.trigger_profit", 0.025)
        
        # 未达到触发利润，不检查
        if ctx.pnl_pct < trigger:
            return None
        
        # 计算回撤距离
        if ctx.atr_value and ctx.atr_value > 0:
            trail_mult = config.get("trailing_stop.atr_multiplier", 2.5)
            trail_dist = trail_mult * ctx.atr_value / ctx.entry_price
        else:
            trail_dist = config.get("trailing_stop.trail_percent", 0.04)
        
        # 计算峰值盈亏
        if ctx.direction == 'BUY':
            peak_pnl = (ctx.peak_price - ctx.entry_price) / ctx.entry_price
        else:
            peak_pnl = (ctx.entry_price - ctx.peak_price) / ctx.entry_price
        
        drawdown = peak_pnl - ctx.pnl_pct
        
        if drawdown >= trail_dist:
            return ExitDecision(
                should_exit=True,
                reason=f"移动止损: 峰值盈利={peak_pnl:.1%}, 当前={ctx.pnl_pct:.1%}, 回撤={drawdown:.1%} >= {trail_dist:.1%}",
                priority=self.priority,
                close_ratio=1.0,
                is_stop_loss=False
            )
        return None


# ============================================================
#  注册函数（Phase 1 双跑模式）
# ============================================================

def register_phase1_rules(engine) -> None:
    """注册 Phase 1 的 3 个核心规则"""
    from app.risk.exit_rule_engine import ExitRuleEngine
    
    engine.register(ATRStopRule())
    engine.register(StructuralStopRule())
    engine.register(TrailingStopRule())
    
    logger.info(f"[ExitRules] Phase 1 规则已注册: ATR, Structural, Trailing")
