"""
Trend-First 策略 — 极简版

一句话逻辑: 大盘趋势方向 + RSI回调 = 入场。

  TREND_UP  + RSI 回调到 [40, 55] → LONG
  TREND_DOWN + RSI 反弹到 [45, 60] → SHORT
  其他 → 不交易

防御:
  - 数据完整性 (RSI/ATR 必须有效)
  - 开盘15分钟延迟 (等指标预热)
  - 15:30后不新开仓

止损: entry ± 1.5×ATR (方向感知)

纯函数 — 零副作用。
"""

import logging
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register

logger = logging.getLogger(__name__)


@register("trend_first")
def evaluate(ctx: SymbolContext, cfg: Dict[str, Any]) -> Optional[SignalCandidate]:
    """Trend-First: 趋势方向 + RSI回调入场

    ATR 来源说明 (Task #4 统一ATR计算):
        ctx.atr 由 SymbolContext.from_raw() 填充，其原始值来自
        MarketDataRouter.get_full_market_context()。本函数不独立计算 ATR，
        与 ATRProvider 同根同源（均走 MarketDataRouter），数据一致。
    """

    # ── 防御: 数据完整性 ──
    if ctx.rsi is None or ctx.rsi <= 0 or ctx.atr is None or ctx.atr <= 0:
        return None

    # ── 防御: 开盘延迟 ──
    opening_delay = cfg.get('opening_delay_minutes', 15)
    if opening_delay and opening_delay > 0:
        try:
            from datetime import datetime as _dt
            import pytz
            now_et = _dt.now(pytz.timezone('US/Eastern'))
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            if 0 < (now_et - market_open).total_seconds() / 60.0 < opening_delay:
                return None
        except Exception:
            pass

    # ── 防御: 收盘前截止 ──
    no_entry_after = cfg.get('no_entry_after_et', '15:30')
    if no_entry_after:
        try:
            from datetime import datetime as _dt, time as _dtime
            import pytz
            now_et = _dt.now(pytz.timezone('US/Eastern'))
            parts = str(no_entry_after).split(':')
            if now_et.time() >= _dtime(int(parts[0]), int(parts[1])):
                return None
        except Exception:
            pass

    # ── 方向: 从 market_regime 判断 ──
    regime = (ctx.market_regime or "").upper()
    direction_str = None
    if regime == "TREND_UP":
        direction_str = "LONG"
    elif regime == "TREND_DOWN":
        direction_str = "SHORT"
    else:
        return None  # RANGE / HIGH_VOL / 空 → 不激活

    # ── 确认层 1: 量能确认 ──
    # Fix 8 (2026-05-31): read breakout_min_volume_ratio with fallback to min_volume_ratio
    # Use explicit None check (not `or`) since 0 is a valid value meaning "disabled"
    _bvr = cfg.get('breakout_min_volume_ratio')
    _mvr = cfg.get('min_volume_ratio')
    min_volume_ratio = _bvr if _bvr is not None else (_mvr if _mvr is not None else 2.0)
    volume_ratio = getattr(ctx, "volume_ratio", None)
    volume_confirmed = False
    if min_volume_ratio and min_volume_ratio > 0:
        if volume_ratio is None or float(volume_ratio) < min_volume_ratio:
            return None
        volume_confirmed = True
    elif volume_ratio is not None and float(volume_ratio) >= 1.3:
        volume_confirmed = True

    # ── 确认层 2: 均线结构 ──
    require_ma_alignment = cfg.get("require_ma_alignment", False)
    ma20 = getattr(ctx, "ma20", 0) or 0
    ma50 = getattr(ctx, "ma50", 0) or 0
    price = ctx.price
    ma_aligned = False
    ma_weak = False
    if direction_str == "LONG":
        if price > ma20 > ma50 > 0:
            ma_aligned = True
            ma_weak = True
        elif price > ma20 and ma20 > 0:
            ma_weak = True
    else:  # SHORT
        if 0 < ma20 < ma50 and price < ma20:
            ma_aligned = True
            ma_weak = True
        elif price < ma20 and ma20 > 0:
            ma_weak = True
    if require_ma_alignment and not ma_weak:
        return None

    # ── 入场: RSI 回调/反弹 ──
    if direction_str == "LONG":
        rsi_low, rsi_high = cfg.get("pullback_rsi_long", [40, 55])
    else:
        rsi_low, rsi_high = cfg.get("pullback_rsi_short", [45, 60])

    # ── 确认层 3: VIX 自适应 RSI 区间 ──
    vix = getattr(ctx, "vix", None)
    if vix is not None and vix > 0:
        try:
            vix_f = float(vix)
            if vix_f > 30:
                rsi_low += 3
                rsi_high -= 3
            elif vix_f < 15:
                rsi_low -= 5
                rsi_high += 3
        except (TypeError, ValueError):
            pass

    if not (rsi_low <= ctx.rsi <= rsi_high):
        return None

    # ── 动态 confidence ──
    base_conf = 0.55
    if volume_confirmed:
        base_conf += 0.08
    if ma_aligned:
        base_conf += 0.07
    elif ma_weak:
        base_conf += 0.03
    # RSI position quality
    rsi_center = (rsi_low + rsi_high) / 2
    rsi_quality = 1.0 - abs(ctx.rsi - rsi_center) / max(rsi_high - rsi_low, 1)
    base_conf += max(0, rsi_quality) * 0.05
    # Order flow bonus
    ofi = getattr(ctx, "order_flow_imbalance", None)
    if ofi is not None:
        try:
            ofi_f = float(ofi)
            if (direction_str == "LONG" and ofi_f > 0.1) or (direction_str == "SHORT" and ofi_f < -0.1):
                base_conf += 0.05
        except (TypeError, ValueError):
            pass
    # VIX bonus
    if vix is not None:
        try:
            if float(vix) <= 25:
                base_conf += 0.05
        except (TypeError, ValueError):
            pass
    confidence = round(min(0.85, max(0.45, base_conf)), 3)

    # ── 动态 strength ──
    base_str = 50
    if volume_confirmed and getattr(ctx, 'volume_ratio', 0) and ctx.volume_ratio > 1.5:
        base_str += 8
    if ma_aligned:
        base_str += 7
    if rsi_quality > 0.7:
        base_str += 5
    strength = min(90, max(30, base_str))

    # ── 输出 ──
    sl_atr_mult = cfg.get("stop_loss_atr_mult", 1.5)

    logger.info(
        f"[TrendFirst] {ctx.symbol} {direction_str} | "
        f"regime={regime} RSI={ctx.rsi:.0f} ATR={ctx.atr:.2f} "
        f"conf={confidence} str={strength}"
    )

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction_str,
        strength=strength,
        confidence=confidence,
        strategy_id="trend_first",
        source="trend_first",
        rationale=f"trend|{regime}|RSI={ctx.rsi:.0f}|PULLBACK",
        context=ctx,
        metadata={
            "stop_loss_atr_mult": sl_atr_mult,
            "atr": ctx.atr,
            "owner_strategy": "trend_first",
            "volume_confirmed": volume_confirmed,
            "ma_aligned": ma_aligned,
            "ma_weak": ma_weak,
            "rsi_quality": round(rsi_quality, 3),
        },
    )
