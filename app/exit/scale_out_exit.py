"""
分层止盈模块 (Scale-Out Exit)

分批次减仓，在不同利润层级锁定利润。

止盈类型: scale_out
- tiers: 分层配置列表 [{profit_pct, close_pct, move_sl_to}, ...]
  - profit_pct: 触发盈利的百分比 (e.g., 5.0 = +5%)
  - close_pct: 平仓比例 (e.g., 30 = 平 30%)
  - move_sl_to: 止损移动目标 "breakeven" | "chandelier_2x" | "chandelier_1x"

适用场景:
- 中长线策略
- 分批锁定利润，让剩余仓位继续奔跑
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.exit.base import BaseExitModule, ExitDecision, ExitProfile

logger = logging.getLogger(__name__)


class ScaleOutExitModule(BaseExitModule):
    """
    分层止盈模块
    
    评估逻辑:
    1. 遍历 tiers，找到第一个满足条件的层级
    2. 如果触发 → REDUCE close_pct%
    3. 同时更新 metadata 中的 move_sl_to (供上层系统执行)
    
    参数 (take_profit_params):
    - tiers: List[Dict] 分层配置
    - tiers[*].profit_pct: 触发盈利百分比
    - tiers[*].close_pct: 平仓比例 (0-100)
    - tiers[*].move_sl_to: 止损移动目标 (可选)
    
    上下文 (ctx) 需要:
    - realized_tiers: List[int] 已触发的层级编号 (避免重复触发)
    """
    
    name = "scale_out_exit"
    
    def evaluate(
        self,
        position: Any,
        ctx: Dict[str, Any],
        profile: ExitProfile,
    ) -> ExitDecision:
        """评估是否触发分层止盈"""
        
        # 提取参数
        entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
        direction = str(getattr(position, "direction", "LONG")).upper()
        current_price = float(ctx.get("current_price", 0) or 0)
        
        if entry_price <= 0 or current_price <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price_data")
        
        # 计算当前盈亏百分比
        if direction == "LONG":
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100
        
        # 提取分层配置
        tiers = profile.take_profit_params.get("tiers", [])
        if not tiers:
            return ExitDecision(action="NO_ACTION", reason="no_tiers_configured")
        
        # 已触发的层级
        realized_tiers = set(ctx.get("realized_tiers", []))
        
        # 遍历 tiers，找第一个满足条件的
        for i, tier in enumerate(tiers):
            level = i + 1  # 层级编号从 1 开始
            
            # 已触发过，跳过
            if level in realized_tiers:
                continue
            
            trigger_pct = tier.get("profit_pct", 0)
            close_pct = tier.get("close_pct", 0)
            move_sl_to = tier.get("move_sl_to", "")
            
            # 检查是否达到触发条件
            if pnl_pct >= trigger_pct and close_pct > 0:
                return ExitDecision(
                    action="REDUCE",
                    qty_pct=close_pct / 100.0,
                    reason=f"SCALE_OUT_TIER_{level}",
                    priority=5 + i,  # 层级越高优先级越高
                    metadata={
                        "tier": level,
                        "trigger_pct": trigger_pct,
                        "close_pct": close_pct,
                        "pnl_pct": round(pnl_pct, 2),
                        "move_sl_to": move_sl_to,
                        "entry_price": entry_price,
                        "current_price": current_price,
                    },
                )
        
        # 未触发任何层级
        return ExitDecision(
            action="NO_ACTION",
            reason="scale_out_not_triggered",
            metadata={
                "pnl_pct": round(pnl_pct, 2),
                "realized_tiers": sorted(realized_tiers),
                "next_tier": self._next_tier_info(tiers, realized_tiers, pnl_pct),
            },
        )
    
    @staticmethod
    def _next_tier_info(
        tiers: List[Dict],
        realized_tiers: set,
        current_pnl_pct: float,
    ) -> Dict[str, Any]:
        """获取下一个未触发层级的信息"""
        for i, tier in enumerate(tiers):
            level = i + 1
            if level not in realized_tiers:
                return {
                    "level": level,
                    "trigger_pct": tier.get("profit_pct", 0),
                    "close_pct": tier.get("close_pct", 0),
                    "gap_pct": round(tier.get("profit_pct", 0) - current_pnl_pct, 2),
                }
        return {}
