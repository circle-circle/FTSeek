"""
Trend-First 策略核心引擎 — RFC-054

核心理念: 趋势为王，只在明确趋势方向上寻找入场机会。
四层过滤架构:
  Layer 0: 防御链 (数据完整性 + 闪崩检测)
  Layer 1: 环境判断 (仅 TREND_UP / TREND_DOWN 时激活)
  Layer 2: 选股评分 (板块热度 + 龙头加分 + 资金流修正)
  Layer 3: 双模式入场 (PULLBACK 回调 / BREAKOUT 突破)

纯函数实现 — 无副作用, 不访问数据库, 不下单, 不修改状态。
"""

import logging
from typing import Optional, Dict, Any, Tuple

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Layer 0: 防御链
# ═══════════════════════════════════════════════════════════════════

def _layer0_defense(ctx: SymbolContext, cfg: Dict[str, Any]) -> Optional[str]:
    """返回拒绝原因或 None（通过）"""
    # L0-a: 数据完整性 — RSI/ATR 必须有效
    if ctx.rsi is None or ctx.rsi <= 0 or ctx.atr is None or ctx.atr <= 0:
        return "data_incomplete"

    # L0-b: 闪崩检测 — slope_5m < -2% 且 volume_ratio > 2.0
    if ctx.slope_5m < -2.0 and ctx.volume_ratio > 2.0:
        return "flash_crash_detected"

    # L0-c: 板块数据新鲜度 — 超过 max_age_hours 时在 Layer2 线性降权, 此处不拦截

    # L0-d (Task #19): 开盘延迟保护 — 避免数据不稳定期入场
    opening_delay = cfg.get('opening_delay_minutes', 15)
    if opening_delay and opening_delay > 0:
        try:
            from datetime import datetime as _dt
            import pytz
            et_tz = pytz.timezone('US/Eastern')
            now_et = _dt.now(et_tz)
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            minutes_since_open = (now_et - market_open).total_seconds() / 60.0
            if 0 < minutes_since_open < opening_delay:
                return f"opening_delay({minutes_since_open:.0f}min<{opening_delay})"
        except Exception:
            pass

    # L0-e (Task #19): 收盘前截止 — 保证最短持仓时间
    no_entry_after = cfg.get('no_entry_after_et', '15:30')
    if no_entry_after:
        try:
            from datetime import datetime as _dt, time as _dtime
            import pytz
            et_tz = pytz.timezone('US/Eastern')
            now_et = _dt.now(et_tz)
            cutoff_parts = str(no_entry_after).split(':')
            cutoff_time = _dtime(int(cutoff_parts[0]), int(cutoff_parts[1]))
            if now_et.time() >= cutoff_time:
                return f"entry_cutoff(after_{no_entry_after})"
        except Exception:
            pass

    return None


# ═══════════════════════════════════════════════════════════════════
#  Layer 1: 环境判断
# ═══════════════════════════════════════════════════════════════════

def _layer1_environment(ctx: SymbolContext) -> Optional[str]:
    """返回方向 'LONG'/'SHORT' 或 None（不激活）"""
    regime = ctx.market_regime.upper() if ctx.market_regime else ""

    if regime == "TREND_UP":
        return "LONG"
    elif regime == "TREND_DOWN":
        return "SHORT"
    else:
        # RANGE / HIGH_VOL / LOW_VOL / 空 → 不激活
        return None


# ═══════════════════════════════════════════════════════════════════
#  Layer 2: 选股评分
# ═══════════════════════════════════════════════════════════════════

def _layer2_stock_selection(
    ctx: SymbolContext, cfg: Dict[str, Any], sector_info: Dict[str, Any]
) -> Tuple[float, str]:
    """返回 (score, detail_str) — score < min_sector_score 时由调用方拦截"""
    base_score = float(sector_info.get("sector_score", 0))

    # 龙头加分
    if sector_info.get("is_leader", False):
        base_score += cfg.get("leader_bonus", 15)

    # 过热惩罚
    overheat_threshold = cfg.get("overheat_threshold", 80)
    if base_score > overheat_threshold:
        base_score *= cfg.get("overheat_penalty", 0.8)

    # 资金流修正 [-15%, +15%]
    flow_factor = 1.0
    if ctx.order_flow_imbalance != 0:
        flow_factor = max(0.85, min(1.15, 1.0 + ctx.order_flow_imbalance * 0.15))

    final_score = base_score * flow_factor
    detail = (
        f"sector={sector_info.get('sector_name', '?')}"
        f"|base={base_score:.0f}|flow={flow_factor:.2f}|final={final_score:.0f}"
    )

    return final_score, detail


# ═══════════════════════════════════════════════════════════════════
#  Layer 3: 双模式入场
# ═══════════════════════════════════════════════════════════════════

def _layer3_entry(
    ctx: SymbolContext, cfg: Dict[str, Any], direction: str
) -> Optional[Tuple[str, float]]:
    """返回 (entry_mode, bonus_score) 或 None（不入场）"""
    rsi = ctx.rsi

    # PULLBACK 模式: 趋势中的回调
    if direction == "LONG":
        pb_range = cfg.get("pullback_rsi_long", [35, 55])
        if pb_range[0] <= rsi <= pb_range[1]:
            return ("PULLBACK", 10)
    else:  # SHORT
        pb_range = cfg.get("pullback_rsi_short", [45, 65])
        if pb_range[0] <= rsi <= pb_range[1]:
            return ("PULLBACK", 10)

    # BREAKOUT 模式: 放量突破
    min_vol = cfg.get("breakout_min_volume_ratio", 2.0)
    if ctx.volume_ratio >= min_vol:
        if direction == "LONG" and rsi > 65:
            return ("BREAKOUT", 15)
        elif direction == "SHORT" and rsi < 35:
            return ("BREAKOUT", 15)

    return None


# ═══════════════════════════════════════════════════════════════════
#  主评估函数 (注册为 trend_first)
# ═══════════════════════════════════════════════════════════════════

@register("trend_first")
def evaluate(ctx: SymbolContext, cfg: Dict[str, Any]) -> Optional[SignalCandidate]:
    """Trend-First 策略主入口（纯函数）

    扩展签名约定:
      cfg 中可包含 "sector_info" 键传入板块数据, 或由调度层预注入到 ctx.metadata。
      {"sector_score": float, "is_leader": bool, "sector_name": str}
    """
    # 提取板块信息: 优先从 cfg, 其次从 ctx.metadata
    sector_info: Dict[str, Any] = cfg.get("sector_info") or {}
    if not sector_info:
        sector_info = (getattr(ctx, "metadata", None) or {}).get("sector_info", {})

    # Layer 0: 防御
    reject = _layer0_defense(ctx, cfg)
    if reject:
        logger.debug(f"[TrendFirst] {ctx.symbol} L0拒绝: {reject}")
        return None

    # Layer 1: 环境
    direction = _layer1_environment(ctx)
    if direction is None:
        logger.debug(
            f"[TrendFirst] {ctx.symbol} L1环境不满足: regime={ctx.market_regime}"
        )
        return None

    # Layer 2: 选股
    min_score = cfg.get("min_sector_score", 40)
    score, detail = _layer2_stock_selection(ctx, cfg, sector_info)
    if score < min_score:
        logger.debug(
            f"[TrendFirst] {ctx.symbol} L2评分不足: {score:.0f} < {min_score}"
        )
        return None

    # Layer 3: 入场
    entry = _layer3_entry(ctx, cfg, direction)
    if entry is None:
        logger.debug(
            f"[TrendFirst] {ctx.symbol} L3入场条件不满足: "
            f"RSI={ctx.rsi}, vol_ratio={ctx.volume_ratio}"
        )
        return None

    entry_mode, bonus = entry
    total_score = score + bonus

    # 计算置信度: min(0.90, 0.55 + (total - min_score_cfg) / 100)
    min_score_cfg = cfg.get("min_score", 55)
    confidence = min(0.90, 0.55 + (total_score - min_score_cfg) / 100.0)
    confidence = max(0.55, confidence)  # 下限与系统 min_confidence 对齐

    # 强度映射到 0-100
    strength = min(99, int(total_score))

    factor_detail = (
        f"{entry_mode}|{detail}|RSI={ctx.rsi:.1f}|vol={ctx.volume_ratio:.1f}"
    )

    logger.info(
        f"[TrendFirst] {ctx.symbol} 信号: {direction} "
        f"mode={entry_mode} score={total_score:.0f} conf={confidence:.2f} "
        f"detail={factor_detail}"
    )

    # 根据入场模式确定止损倍数
    if entry_mode == "BREAKOUT":
        sl_atr_mult = cfg.get("breakout_stop_loss_atr_mult", 1.0)
    else:
        sl_atr_mult = cfg.get("stop_loss_atr_mult", 1.5)

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=strength,
        confidence=round(confidence, 4),
        strategy_id="trend_first",
        source="trend_first",
        rationale=f"{entry_mode}|score={total_score:.0f}|{detail}",
        context=ctx,
        metadata={
            "entry_mode": entry_mode,
            "stop_loss_atr_mult": sl_atr_mult,
            "trail_activation_atr": cfg.get("trail_activation_atr", 1.0),
            "trail_atr_mult": cfg.get("trail_atr_mult", 1.5),
            "atr": ctx.atr,
            "sector_name": sector_info.get("sector_name", ""),
            "sector_score": sector_info.get("sector_score", 0),
            "is_leader": sector_info.get("is_leader", False),
        },
    )
