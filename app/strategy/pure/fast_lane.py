"""
快车道策略 — 作战计划匹配后跳过投票直接执行

输入: SymbolContext (已包含 price/spread/VIX 等)
输出: SignalCandidate (当价格在 entry_zone 内时) 或 None

纯函数 — 不依赖 scheduler/DB/网络, 仅做数据判断。
"""

import logging
from typing import Optional, Dict, Any, List

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register

logger = logging.getLogger(__name__)


@register("fast_lane")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """
    快车道: 如果当前价格落在计划的 entry_zone 内, 直接生成高置信度信号。

    config 参数 (从 YAML > strategies.fast_lane):
        min_confidence: float = 0.70       # 最低置信度要求
        require_entry_zone: bool = True    # 是否要求 entry_zone 匹配

    ctx 中通过 metadata 传入作战计划匹配结果:
        ctx.metadata["battle_plan_match"] = {
            "direction": "LONG",
            "entry_zone": [low, high],
            "catalyst": "...",
            "urgency": "NORMAL",
        }
    """
    plan_match: Optional[Dict] = ctx.metadata.get("battle_plan_match")
    if not plan_match:
        return None

    direction = str(plan_match.get("direction", "LONG")).upper()
    if direction not in ("LONG", "SHORT"):
        return None

    entry_zone: List[float] = plan_match.get("entry_zone", [])
    require_zone = config.get("require_entry_zone", True)

    if require_zone:
        # P1 修复 (2026-06-19): entry_zone 校验必须使用原始区间
        # 根因: 之前用 confirmed_entry_price ± 0.5% 创建新区间，导致校验形同虚设
        # 6/18 NTES: entry_zone=[119.69, 121.63]，实际入场 119.0，
        # 但 confirmed_price 可能是 119.0，导致 119.0 ± 0.5% 包含了 119.0
        # 修复: 必须使用原始 entry_zone，confirmed_price 仅作为参考
        if not entry_zone or len(entry_zone) != 2:
            return None
        low, high = entry_zone
        
        # 允许 0.3% 容差 (避免价格刚好在边界时被拒)
        tol = (high - low) * 0.03
        effective_low = low - tol
        effective_high = high + tol
        
        if not (effective_low <= ctx.price <= effective_high):
            logger.debug(
                f"[FastLane] {ctx.symbol} 价格 {ctx.price:.2f} 不在 entry_zone "
                f"[{low:.2f}, {high:.2f}] ± 3% 范围内，拒绝"
            )
            return None
        
        # RSI 辅助判断 (不阻止, 仅附加信息)
        if direction == "LONG" and ctx.rsi > 60:
            ctx.metadata["fast_lane_note"] = f"RSI {ctx.rsi:.0f} 偏高(入场偏晚)"
        elif direction == "SHORT" and ctx.rsi < 40:
            ctx.metadata["fast_lane_note"] = f"RSI {ctx.rsi:.0f} 偏低(做空偏早)"

    confidence = plan_match.get("confidence", 0.85)
    if confidence < config.get("min_confidence", 0.70):
        return None

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=int(float(confidence) * 100),
        confidence=0.85,
        strategy_id="BATTLE_PLAN_FAST_PATH",
        source="fast_lane",
        entry_zone=entry_zone,
        catalyst=str(plan_match.get("catalyst", "")),
        urgency=str(plan_match.get("urgency", "NORMAL")).upper(),
        rationale=str(plan_match.get("rationale", plan_match.get("catalyst", f"快车道 entry_zone 匹配 @ ${ctx.price:.2f}"))),
        context=ctx,
        metadata={
            "plan_match": plan_match,
            "price": ctx.price,
            "plan_stop_loss": plan_match.get("stop_loss"),
            "plan_take_profit": plan_match.get("take_profit"),
            "owner_strategy": "fast_lane",
        },
    )
