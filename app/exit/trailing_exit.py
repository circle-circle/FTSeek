"""
Chandelier 追踪止损模块

追踪止损: 止损位随价格有利方向移动而上移 (多头) / 下移 (空头)，永不倒退。

止损类型: chandelier
- activation_profit_atr: 盈利达到 N×ATR 后激活追踪
- multiplier: 从最高价回撤 N×ATR 触发

适用场景:
- 趋势跟踪策略
- 让利润奔跑，同时锁定部分利润
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from app.exit.base import BaseExitModule, ExitDecision, ExitProfile

logger = logging.getLogger(__name__)


class TrailingExitModule(BaseExitModule):
    """
    Chandelier 追踪止损模块
    
    评估逻辑:
    1. 检查是否激活: profit >= activation_profit_atr × ATR
    2. 如果激活，计算追踪止损价: trail_price = peak_price - multiplier × ATR
    3. 如果当前价 <= trail_price → CLOSE 100%
    
    参数 (stop_loss_params):
    - activation_profit_atr: 激活追踪所需的盈利 ATR 倍数 (默认 1.2)
    - multiplier: 追踪止损的 ATR 倍数 (默认 2.5)
    
    上下文 (ctx) 需要:
    - peak_price: 持仓期间的最高价 (多头) 或最低价 (空头)
    """
    
    name = "trailing_exit"
    
    def evaluate(
        self,
        position: Any,
        ctx: Dict[str, Any],
        profile: ExitProfile,
    ) -> ExitDecision:
        """评估是否触发 Chandelier 追踪止损"""
        
        # 提取参数
        entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
        direction = str(getattr(position, "direction", "LONG")).upper()
        current_price = float(ctx.get("current_price", 0) or 0)
        atr = float(ctx.get("atr", 0) or 0)
        peak_price = float(ctx.get("peak_price", 0) or 0)
        
        if entry_price <= 0 or current_price <= 0 or atr <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price_data")
        
        # 如果没有 peak_price，使用当前价
        if peak_price <= 0:
            peak_price = current_price
        
        # 提取追踪参数
        activation_atr = profile.stop_loss_params.get("activation_profit_atr", 1.2)
        multiplier = profile.stop_loss_params.get("multiplier", 2.5)
        
        # 计算峰值盈利 (以 ATR 为单位) — 用于激活判断
        # 关键: 用 peak_price 而非 current_price，一旦激活就持续生效
        if direction == "LONG":
            peak_profit_atr = (peak_price - entry_price) / atr
            current_profit_atr = (current_price - entry_price) / atr
        else:
            # SHORT: peak_price 是最低价
            peak_profit_atr = (entry_price - peak_price) / atr
            current_profit_atr = (entry_price - current_price) / atr
        
        # 检查是否激活追踪 (基于峰值盈利)
        if peak_profit_atr < activation_atr:
            return ExitDecision(
                action="NO_ACTION",
                reason="trailing_not_activated",
                metadata={
                    "peak_profit_atr": round(peak_profit_atr, 2),
                    "activation_atr": activation_atr,
                },
            )
        
        # 计算追踪止损价
        if direction == "LONG":
            trail_price = peak_price - atr * multiplier
            pnl_pct = (current_price - entry_price) / entry_price
            
            # 检查是否触及追踪止损
            if current_price <= trail_price:
                return ExitDecision(
                    action="CLOSE",
                    qty_pct=1.0,
                    reason="CHANDELIER_TRAIL",
                    priority=8,
                    metadata={
                        "trail_price": trail_price,
                        "peak_price": peak_price,
                        "current_price": current_price,
                        "atr": atr,
                        "multiplier": multiplier,
                        "peak_profit_atr": round(peak_profit_atr, 2),
                        "current_profit_atr": round(current_profit_atr, 2),
                        "pnl_pct": pnl_pct,
                    },
                )
        else:  # SHORT
            trail_price = peak_price + atr * multiplier
            pnl_pct = (entry_price - current_price) / entry_price
            
            # 检查是否触及追踪止损
            if current_price >= trail_price:
                return ExitDecision(
                    action="CLOSE",
                    qty_pct=1.0,
                    reason="CHANDELIER_TRAIL",
                    priority=8,
                    metadata={
                        "trail_price": trail_price,
                        "peak_price": peak_price,
                        "current_price": current_price,
                        "atr": atr,
                        "multiplier": multiplier,
                        "peak_profit_atr": round(peak_profit_atr, 2),
                        "current_profit_atr": round(current_profit_atr, 2),
                        "pnl_pct": pnl_pct,
                    },
                )
        
        # 未触发
        return ExitDecision(
            action="NO_ACTION",
            reason="trailing_not_triggered",
            metadata={
                "trail_price": trail_price,
                "peak_price": peak_price,
                "current_price": current_price,
                "peak_profit_atr": round(peak_profit_atr, 2),
                "current_profit_atr": round(current_profit_atr, 2),
            },
        )
