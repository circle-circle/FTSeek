"""
ExitPolicy 实现 — Phase B 升级 (PB-3, 2026-05-15)

每个策略对应一个 ExitPolicy. PB-3 (Phase B.2 minimal):
- 从 NO_ACTION stub 升级到 thesis-aware
- 优先按 entry_thesis 选退出策略
- thesis 缺失 → fallback NO_ACTION (走 PositionExitMonitor 6 规则)

thesis 退出规则:
- momentum: chandelier_trail (顺势保利)
- breakout: 跌破前高/前低即退 (technical breach)
- reversion: 紧 ATR 短线 (1.5x), 5min trail
- event: 事件 horizon 到期强平 (24-48h)
- technical_setup: trail + 形态破位
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """ExitPolicy.evaluate() 返回值"""
    action: str = "NO_ACTION"     # NO_ACTION | REDUCE | CLOSE
    qty_pct: float = 0.0
    reason: str = ""
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


def _read_entry_thesis(position: Any) -> Optional[str]:
    """从 trade_attribution 读 entry_thesis (异步 thesis_inferer 写入)"""
    try:
        import sqlite3
        from app.core.paths import get_trade_records_db
        trace_id = getattr(position, "trace_id", "") or ""
        if not trace_id:
            return None
        conn = sqlite3.connect(str(get_trade_records_db()))
        row = conn.execute(
            "SELECT entry_thesis FROM trade_attribution WHERE trace_id = ? LIMIT 1",
            (trace_id,)
        ).fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


class _BaseExitPolicy:
    """基础 ExitPolicy — thesis-aware (PB-3 2026-05-15)"""
    name: str = "base"
    owner: str = "main"

    def evaluate(self, position: Any, ctx: Any) -> ExitDecision:
        thesis = _read_entry_thesis(position)
        if not thesis:
            # 无 thesis → fallback NO_ACTION (走 PositionExitMonitor 6 规则)
            return ExitDecision(
                action="NO_ACTION",
                reason="no_thesis_fallback_to_legacy",
                metadata={"owner_strategy": self.owner},
            )

        # thesis-driven 退出规则
        return self._evaluate_by_thesis(position, ctx, thesis)

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """子类按 owner+thesis 组合定制. 默认 NO_ACTION"""
        return ExitDecision(
            action="NO_ACTION",
            reason=f"thesis={thesis}_no_specific_rule",
            metadata={"owner_strategy": self.owner, "thesis": thesis},
        )


class MainPipelineExitPolicy(_BaseExitPolicy):
    name = "main_exit"
    owner = "main"

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """主线 thesis-driven 退出规则"""
        cur = float(ctx.get("current_price", 0) or 0)
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        if cur <= 0 or entry <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price")

        direction = str(getattr(position, "direction", "LONG"))
        is_long = "LONG" in direction.upper()
        pnl_pct = (cur - entry) / entry if is_long else (entry - cur) / entry

        # 1. event: 事件驱动 — 持仓时长上限 48h, 到期强平
        if thesis == "event":
            holding_hours = float(getattr(position, "holding_hours", 0) or 0)
            if holding_hours > 48:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="EVENT_HORIZON_48H",
                    priority=10,
                    metadata={"thesis": thesis},
                )

        # 2. reversion: 反转入场 — 浮盈 +1.5% 立即减半保利
        if thesis == "reversion":
            if pnl_pct >= 0.015:
                return ExitDecision(
                    action="REDUCE", qty_pct=0.5,
                    reason="REVERSION_QUICK_PROFIT",
                    priority=8,
                    metadata={"thesis": thesis, "pnl_pct": pnl_pct},
                )

        # 3. breakout: 跌破入场价 -0.5% 即退 (设的不是大止损)
        if thesis == "breakout":
            if pnl_pct <= -0.005:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="BREAKOUT_FAILED",
                    priority=9,
                    metadata={"thesis": thesis, "pnl_pct": pnl_pct},
                )

        # momentum / technical_setup → 走 chandelier (PositionExitMonitor 默认)
        return ExitDecision(
            action="NO_ACTION",
            reason=f"main_thesis={thesis}_delegate",
            metadata={"thesis": thesis},
        )


class FastLaneExitPolicy(_BaseExitPolicy):
    name = "fast_lane_exit"
    owner = "fast_lane"

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """FastLane: 1ATR 立即保本"""
        atr = float(ctx.get("atr", 0) or 0)
        cur = float(ctx.get("current_price", 0) or 0)
        entry = float(getattr(position, "avg_entry_price", 0) or 0)
        if atr <= 0 or cur <= 0 or entry <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_data")
        is_long = "LONG" in str(getattr(position, "direction", "LONG")).upper()
        profit_atr = (cur - entry) / atr if is_long else (entry - cur) / atr
        if profit_atr >= 1.0:
            return ExitDecision(
                action="REDUCE", qty_pct=0.5,
                reason="FAST_LANE_1ATR_PROFIT_LOCK",
                priority=8,
                metadata={"thesis": thesis, "profit_atr": profit_atr},
            )
        return ExitDecision(action="NO_ACTION", reason="fast_lane_below_1atr")


class LLMPlanExitPolicy(_BaseExitPolicy):
    name = "llm_plan_exit"
    owner = "llm_plan"

    def _evaluate_by_thesis(self, position: Any, ctx: Any, thesis: str) -> ExitDecision:
        """LLM Plan: 按 BattlePlan 中的 target/stop 直接退出"""
        # 暂从 position.metadata 取 plan_target / plan_stop (Phase B 完整实现)
        meta = getattr(position, "metadata", None) or {}
        plan_target = float(meta.get("plan_target", 0) or 0)
        plan_stop = float(meta.get("plan_stop", 0) or 0)
        cur = float(ctx.get("current_price", 0) or 0)
        if cur <= 0:
            return ExitDecision(action="NO_ACTION", reason="missing_price")
        is_long = "LONG" in str(getattr(position, "direction", "LONG")).upper()

        # 命中 plan target → 全平
        if plan_target > 0:
            hit = (is_long and cur >= plan_target) or (not is_long and cur <= plan_target)
            if hit:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="LLM_PLAN_TARGET_HIT",
                    priority=10,
                    metadata={"thesis": thesis, "plan_target": plan_target},
                )
        # 命中 plan stop → 全平
        if plan_stop > 0:
            hit = (is_long and cur <= plan_stop) or (not is_long and cur >= plan_stop)
            if hit:
                return ExitDecision(
                    action="CLOSE", qty_pct=1.0,
                    reason="LLM_PLAN_STOP_HIT",
                    priority=10,
                    metadata={"thesis": thesis, "plan_stop": plan_stop},
                )
        return ExitDecision(action="NO_ACTION", reason="llm_plan_neither_target_nor_stop")
