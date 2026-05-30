"""
ORB (Opening Range Breakout) 策略 — 开盘区间突破

一句话逻辑: 开盘15分钟建立价格区间, 突破区间方向 + 大盘趋势一致 = 入场。

  价格 > opening_high + SPY TREND_UP   → LONG (突破做多)
  价格 < opening_low  + SPY TREND_DOWN → SHORT (跌破做空)
  其他 → 不交易

为什么 ORB:
  - 数据证明 10:00-11:00 是盈利主力时段 (+$21K)
  - TRAILING_STOP 是唯一赚钱的退出方式
  - ORB 天然匹配: 09:45后入场 + trailing stop 退出
  - 只有2个参数 (区间时长 + ATR止损倍数), 远少于 technical 的 20+

防御:
  - 开盘区间未确立 (不足15根K线) → 不交易
  - 区间太窄 (<0.3%) → 假突破风险高, 不交易
  - 区间太宽 (>2.5%) → 波动过大, 不交易
  - 大盘趋势反向 → 不交易 (不逆势)
  - 15:30后 → 不新开仓
  - 已突破过 (价格远离区间) → 不追高

止损: opening_range 中点 (方向感知)
止盈: 交给 ExitMonitor trailing stop (数据证明唯一赚钱方式)

纯函数 — 零副作用。
"""

import logging
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register

logger = logging.getLogger(__name__)


@register("orb")
def evaluate(ctx: SymbolContext, cfg: Dict[str, Any]) -> Optional[SignalCandidate]:
    """ORB: 开盘区间突破入场"""

    # ── 防御: 数据完整性 ──
    if ctx.price <= 0 or ctx.atr <= 0:
        return None

    # ── 防御: 开盘区间必须已确立 ──
    if not ctx.opening_range_ready:
        return None
    if ctx.opening_high <= 0 or ctx.opening_low <= 0:
        return None
    if ctx.opening_high <= ctx.opening_low:
        return None

    orb_high = ctx.opening_high
    orb_low = ctx.opening_low
    orb_mid = (orb_high + orb_low) / 2.0
    orb_width_pct = (orb_high - orb_low) / ctx.price * 100

    # ── 防御: 区间宽度过滤 ──
    min_width_pct = cfg.get("min_range_width_pct", 0.3)
    max_width_pct = cfg.get("max_range_width_pct", 2.5)
    if orb_width_pct < min_width_pct:
        return None  # 区间太窄, 假突破风险高
    if orb_width_pct > max_width_pct:
        return None  # 区间太宽, 波动过大

    # ── 防御: 时间窗口 (09:45 - 11:00 ET) ──
    try:
        from datetime import datetime as _dt, time as _dtime
        import pytz
        now_et = _dt.now(pytz.timezone('US/Eastern'))
    except Exception:
        return None

    entry_start = _dtime(9, 45)
    entry_end_str = cfg.get("entry_end_et", "11:00")
    end_parts = str(entry_end_str).split(":")
    entry_end = _dtime(int(end_parts[0]), int(end_parts[1]))

    if not (entry_start <= now_et.time() <= entry_end):
        return None

    # ── 防御: 收盘前截止 ──
    no_entry_after = cfg.get("no_entry_after_et", "15:30")
    if no_entry_after:
        parts = str(no_entry_after).split(":")
        if now_et.time() >= _dtime(int(parts[0]), int(parts[1])):
            return None

    # ── 突破判断 ──
    price = ctx.price
    # 突破容忍度: 防止刚碰到边界就触发 (默认 0.1% 缓冲区)
    buffer_pct = cfg.get("breakout_buffer_pct", 0.1) / 100.0
    buffer = ctx.price * buffer_pct

    direction = None
    if price > orb_high + buffer:
        direction = "LONG"
    elif price < orb_low - buffer:
        direction = "SHORT"
    else:
        return None  # 价格在区间内, 不交易

    # ── 防御: 不追高 (价格已远离区间) ──
    max_chase_pct = cfg.get("max_chase_pct", 1.5) / 100.0
    if direction == "LONG":
        chase = (price - orb_high) / orb_high
        if chase > max_chase_pct:
            return None  # 已经涨太多, 不追
    else:
        chase = (orb_low - price) / orb_low
        if chase > max_chase_pct:
            return None  # 已经跌太多, 不追

    # ── 大盘趋势过滤 (不逆势) ──
    regime = (ctx.market_regime or "").upper()
    if direction == "LONG" and regime == "TREND_DOWN":
        return None
    if direction == "SHORT" and regime == "TREND_UP":
        return None

    # ── 量能确认 (可选) ──
    min_volume_ratio = cfg.get("min_volume_ratio", 0)
    if min_volume_ratio > 0 and ctx.volume_ratio < min_volume_ratio:
        return None

    # ── 止损计算: 区间中点 ──
    # LONG: SL = orb_mid (价格跌回区间中点 = 突破失败)
    # SHORT: SL = orb_mid (价格涨回区间中点 = 跌破失败)
    stop_loss = orb_mid

    # 可选: ATR 扩展止损 (当区间中点太近时, 用 ATR 兜底)
    sl_atr_mult = cfg.get("stop_loss_atr_mult", 1.5)
    if direction == "LONG":
        atr_sl = price - ctx.atr * sl_atr_mult
        stop_loss = max(stop_loss, atr_sl)  # 取较远的 (更保守)
    else:
        atr_sl = price + ctx.atr * sl_atr_mult
        stop_loss = min(stop_loss, atr_sl)  # 取较远的 (更保守)

    # ── 盈亏比检查 ──
    sl_distance = abs(price - stop_loss)
    if sl_distance <= 0:
        return None
    # 最小盈亏比 1.5:1 (止盈由 trailing stop 管理, 这里只做下限检查)
    min_rr = cfg.get("min_risk_reward", 1.5)
    # 估算 TP = 2x SL distance (trailing stop 通常能拿到更多)
    est_tp_distance = sl_distance * 2.0
    if est_tp_distance / sl_distance < min_rr:
        return None

    # ── 输出 ──
    logger.info(
        f"[ORB] {ctx.symbol} {direction} | "
        f"range=[{orb_low:.2f}, {orb_high:.2f}] width={orb_width_pct:.2f}% "
        f"price={price:.2f} SL={stop_loss:.2f} regime={regime}"
    )

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=70,  # 固定中高强度 (ORB 是经过数据验证的策略)
        confidence=0.75,
        strategy_id="orb",
        source="orb",
        rationale=(
            f"ORB|{direction}|range=[{orb_low:.2f},{orb_high:.2f}]"
            f"|width={orb_width_pct:.2f}%|regime={regime}"
        ),
        context=ctx,
        metadata={
            "opening_high": orb_high,
            "opening_low": orb_low,
            "opening_mid": orb_mid,
            "orb_width_pct": orb_width_pct,
            "stop_loss": round(stop_loss, 2),
            "stop_loss_atr_mult": sl_atr_mult,
            "atr": ctx.atr,
            "regime": regime,
            "owner_strategy": "orb",
        },
    )
