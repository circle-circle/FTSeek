"""
ATR 倍数止损模块

基于 ATR (Average True Range) 计算动态止损位。

止损类型:
- fixed_pct: 固定百分比止损 (e.g., -5%)
- atr_multi: ATR 倍数止损 (e.g., entry - 2.0×ATR)

适用场景:
- 短线策略 (5min/15min)
- 需要动态适应波动率
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from app.exit.base import BaseExitModule, ExitDecision, ExitProfile

logger = logging.getLogger(__name__)


class AtrExitModule(BaseExitModule):
    """
    ATR 倍数止损模块
    
    评估逻辑:
    1. 计算止损价: stop_price = entry ± multiplier × ATR
    2. 计算当前价与止损价距离
    3. 如果触及或突破止损 → CLOSE 100%
    
    参数 (stop_loss_params):
    - multiplier: ATR 倍数 (默认 2.0)
    - max_loss_pct: 最大亏损百分比 (默认 -15%)
    """
    
    name = "atr_exit"
    
    def evaluate(
        self,
        position: Any,
        ctx: Dict[str, Any],
        profile: ExitProfile,
    ) -> ExitDecision:
        """评估是否触发 ATR 止损"""
        
        # 提取参数
        entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
        direction = str(getattr(position, "direction", "LONG")).upper()
        current_price = float(ctx.get("current_price", 0) or 0)
        atr = float(ctx.get("atr", 0) or 0)
        
        if entry_price <= 0 or current_price <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price_data")
        
        # 根据 stop_loss_type 计算止损价
        sl_type = profile.stop_loss_type
        params = profile.stop_loss_params
        max_loss_pct = params.get("max_loss_pct", -0.15)
        
        if direction == "LONG":
            if sl_type == "fixed_pct":
                pct = params.get("pct", -0.05)
                stop_price = entry_price * (1 + pct)
            else:  # atr_multi
                multiplier = params.get("multiplier", 2.0)
                stop_price = entry_price - atr * multiplier
            
            pnl_pct = (current_price - entry_price) / entry_price
            
            # 检查是否触及止损
            if current_price <= stop_price:
                return ExitDecision(
                    action="CLOSE",
                    qty_pct=1.0,
                    reason="ATR_STOP" if sl_type == "atr_multi" else "FIXED_PCT_STOP",
                    priority=10,
                    metadata={
                        "stop_price": round(stop_price, 4),
                        "current_price": current_price,
                        "atr": atr,
                        "sl_type": sl_type,
                        "pnl_pct": round(pnl_pct, 4),
                    },
                )
        else:  # SHORT
            if sl_type == "fixed_pct":
                pct = abs(params.get("pct", -0.05))
                stop_price = entry_price * (1 + pct)
            else:  # atr_multi
                multiplier = params.get("multiplier", 2.0)
                stop_price = entry_price + atr * multiplier
            
            pnl_pct = (entry_price - current_price) / entry_price
            
            # 检查是否触及止损
            if current_price >= stop_price:
                return ExitDecision(
                    action="CLOSE",
                    qty_pct=1.0,
                    reason="ATR_STOP" if sl_type == "atr_multi" else "FIXED_PCT_STOP",
                    priority=10,
                    metadata={
                        "stop_price": round(stop_price, 4),
                        "current_price": current_price,
                        "atr": atr,
                        "sl_type": sl_type,
                        "pnl_pct": round(pnl_pct, 4),
                    },
                )
        
        # 检查最大亏损百分比
        if pnl_pct <= max_loss_pct:
            return ExitDecision(
                action="CLOSE",
                qty_pct=1.0,
                reason="MAX_LOSS_PCT",
                priority=9,
                metadata={
                    "pnl_pct": pnl_pct,
                    "max_loss_pct": max_loss_pct,
                },
            )
        
        # 未触发
        return ExitDecision(
            action="NO_ACTION",
            reason="atr_not_triggered",
            metadata={
                "stop_price": stop_price,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
            },
        )
