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
    """Trend-First: 趋势方向 + RSI回调入场"""

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
    if regime == "TREND_UP":
        direction = "LONG"
    elif regime == "TREND_DOWN":
        direction = "SHORT"
    else:
        return None  # RANGE / HIGH_VOL / 空 → 不激活

    # ── 入场: RSI 回调/反弹 ──
    if direction == "LONG":
        rsi_low, rsi_high = cfg.get("pullback_rsi_long", [40, 55])
    else:
        rsi_low, rsi_high = cfg.get("pullback_rsi_short", [45, 60])

    if not (rsi_low <= ctx.rsi <= rsi_high):
        return None

    # ── 输出 ──
    sl_atr_mult = cfg.get("stop_loss_atr_mult", 1.5)

    logger.info(
        f"[TrendFirst] {ctx.symbol} {direction} | "
        f"regime={regime} RSI={ctx.rsi:.0f} ATR={ctx.atr:.2f}"
    )

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=60,
        confidence=0.70,
        strategy_id="trend_first",
        source="trend_first",
        rationale=f"trend|{regime}|RSI={ctx.rsi:.0f}",
        context=ctx,
        metadata={
            "stop_loss_atr_mult": sl_atr_mult,
            "atr": ctx.atr,
        },
    )
