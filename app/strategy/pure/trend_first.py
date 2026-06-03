"""
Trend-First 策略 (RFC-054) — 四层决策引擎

Layer 0  防御链:    数据完整性 + 时间门禁 + 闪崩检测
Layer 1  环境判断:  TREND_UP/TREND_DOWN 才激活 (RANGE/HIGH_VOL/LOW_VOL 不交易)
Layer 2  选股评分:  板块评分 + 龙头加分 + 过热惩罚 + 资金流修正
Layer 3  双模式入场: PULLBACK 回调 / BREAKOUT 突破

═══════════════════════════════════════════════════════════════════
RFC-054 致命 Bug 修复 (2026-05-30 审计 → 2026-06-02 实施)
═══════════════════════════════════════════════════════════════════
Bug #1: RegimeAdapter.get_current_regime() 不存在
        修复: 优先用 ctx.market_regime; 缺失时从 SPY/MA200 + VIX 推断 (_resolve_regime)

Bug #2: ctx.volume_ratio 上游可能未注入 (默认 1.0 永真)
        修复: 优先 ctx.volume / ctx.avg_volume 计算; 退回 ctx.volume_ratio (_get_volume_ratio)

Bug #3: ctx.price_change_5min 不存在于 SymbolContext
        修复: 优先用 ctx.slope_5m (5分钟斜率%); 退回 ctx.change_pct 日内涨跌幅 (_is_flash_crash)

所有修复均"优雅降级" — 缺数据时回退到安全默认, 不抛异常。
"""

import logging
from typing import Optional, Dict, Any, Tuple

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  辅助: 数据派生 (Bug #1/#2/#3 修复点)
# ═══════════════════════════════════════════════════════════════════

def _get_volume_ratio(ctx: SymbolContext) -> float:
    """量比派生 (Bug #2 修复).

    优先从 ctx.volume / ctx.avg_volume 实时计算 (规避上游 volume_ratio 未注入的问题);
    若 avg_volume 不可用 (=0), 退回 ctx.volume_ratio 字段; 兜底返回 1.0。
    """
    try:
        avg_volume = getattr(ctx, 'avg_volume', 0) or 0
        volume = getattr(ctx, 'volume', 0) or 0
        if avg_volume > 0 and volume > 0:
            return float(volume) / float(avg_volume)
    except Exception:
        pass
    vr = getattr(ctx, 'volume_ratio', None)
    try:
        return float(vr) if vr is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _resolve_regime(ctx: SymbolContext) -> str:
    """市场状态解析 (Bug #1 修复).

    优先使用 ctx.market_regime (上游正确注入时);
    缺失/为空时从 VIX + SPY 200日均线推断:
        VIX>30 或 vix_regime='extreme'         → HIGH_VOL
        SPY > MA200 * 1.01                     → TREND_UP
        SPY < MA200 * 0.99                     → TREND_DOWN
        其他 / 数据不全                          → RANGE (安全降级)
    """
    explicit = (getattr(ctx, "market_regime", "") or "").upper()
    if explicit:
        return explicit

    # ── 高波动优先 (避险) ──
    try:
        vix = float(getattr(ctx, 'vix', 0) or 0)
        vix_regime = (getattr(ctx, 'vix_regime', '') or '').lower()
        if vix_regime == "extreme" or vix > 30:
            return "HIGH_VOL"
    except (TypeError, ValueError):
        pass

    # ── SPY 趋势推断 ──
    try:
        spy_price = float(getattr(ctx, 'spy_price', 0) or 0)
        spy_ma200 = float(getattr(ctx, 'spy_ma200', 0) or 0)
        if spy_price > 0 and spy_ma200 > 0:
            if spy_price > spy_ma200 * 1.01:
                return "TREND_UP"
            if spy_price < spy_ma200 * 0.99:
                return "TREND_DOWN"
    except (TypeError, ValueError):
        pass

    return "RANGE"  # 安全降级


def _is_flash_crash(ctx: SymbolContext, vol_ratio: float) -> bool:
    """闪崩检测 (Bug #3 修复).

    优先使用 ctx.slope_5m (5分钟价格斜率, %) — 与放量同时出现 = 恐慌;
    slope_5m 不可用时退回 ctx.change_pct (日内涨跌幅, decimal) 作近似。
    阈值: slope_5m < -2.0% (或 change_pct < -2%) 且 vol_ratio > 2.0。
    """
    try:
        slope = float(getattr(ctx, 'slope_5m', 0) or 0)
    except (TypeError, ValueError):
        slope = 0.0
    if slope < -2.0 and vol_ratio > 2.0:
        return True
    # ── slope_5m 缺失时的兜底: 用日内变化率 ──
    if slope == 0.0:
        try:
            cp = float(getattr(ctx, 'change_pct', 0) or 0)
        except (TypeError, ValueError):
            cp = 0.0
        if cp < -0.02 and vol_ratio > 2.0:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
#  Layer 0: 防御链
# ═══════════════════════════════════════════════════════════════════

def _layer0_defense(ctx: SymbolContext, cfg: Dict[str, Any]) -> bool:
    """Layer 0: 数据完整性 + 时间门禁 + 闪崩检测。返回 True=通过, False=拦截。"""

    # ── 数据完整性 ──
    if ctx.rsi is None or ctx.rsi <= 0:
        return False
    if ctx.atr is None or ctx.atr <= 0:
        return False

    # ── 时间门禁: 开盘延迟 ──
    opening_delay = cfg.get('opening_delay_minutes', 0)
    if opening_delay and opening_delay > 0:
        try:
            from datetime import datetime as _dt
            import pytz
            now_et = _dt.now(pytz.timezone('US/Eastern'))
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed_min = (now_et - market_open).total_seconds() / 60.0
            if 0 < elapsed_min < opening_delay:
                return False
        except Exception:
            pass

    # ── 时间门禁: 收盘前截止 ──
    no_entry_after = cfg.get('no_entry_after_et')
    if no_entry_after:
        try:
            from datetime import datetime as _dt, time as _dtime
            import pytz
            now_et = _dt.now(pytz.timezone('US/Eastern'))
            parts = str(no_entry_after).split(':')
            if now_et.time() >= _dtime(int(parts[0]), int(parts[1])):
                return False
        except Exception:
            pass

    # ── 闪崩检测 (Bug #3 修复点) ──
    vol_ratio = _get_volume_ratio(ctx)
    if _is_flash_crash(ctx, vol_ratio):
        logger.info(
            f"[TrendFirst] {ctx.symbol} L0 闪崩拦截 | "
            f"slope_5m={getattr(ctx, 'slope_5m', 0):.2f}% "
            f"change_pct={getattr(ctx, 'change_pct', 0):.3f} "
            f"vol_ratio={vol_ratio:.2f}"
        )
        return False

    return True


# ═══════════════════════════════════════════════════════════════════
#  Layer 1: 环境判断
# ═══════════════════════════════════════════════════════════════════

def _layer1_environment(ctx: SymbolContext) -> Optional[str]:
    """Layer 1: 仅 TREND_UP/TREND_DOWN 时激活。返回方向 'LONG'/'SHORT' 或 None。"""
    regime = _resolve_regime(ctx)
    if regime == "TREND_UP":
        return "LONG"
    if regime == "TREND_DOWN":
        return "SHORT"
    return None  # RANGE / HIGH_VOL / LOW_VOL → 不激活


# ═══════════════════════════════════════════════════════════════════
#  Layer 2: 选股评分
# ═══════════════════════════════════════════════════════════════════

def _resolve_sector_info(ctx: SymbolContext, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """sector_info 优先级: cfg > ctx.metadata > 动态计算 > 空字典
    
    P0 (2026-06-03): 静态 sector_score=0 时，回退到动态行业强度计算。
    数据源: 11 个 SPDR 行业 ETF 的 20 日动量排名。
    """
    sector_info = cfg.get('sector_info')
    if sector_info and float(sector_info.get('sector_score', 0) or 0) > 0:
        return sector_info
    meta = getattr(ctx, 'metadata', None) or {}
    meta_info = meta.get('sector_info') or {}
    if meta_info and float(meta_info.get('sector_score', 0) or 0) > 0:
        return meta_info
    
    # 动态回退: 从行业 ETF 实时强度计算
    try:
        from app.signals.pre_market_ranker import get_sector_strength, _get_symbol_sector
        symbol = getattr(ctx, 'symbol', '')
        sector_name = _get_symbol_sector(symbol)
        if sector_name:
            dynamic_score = get_sector_strength(sector_name)
            return {
                'sector_name': sector_name,
                'sector_score': dynamic_score,
                'is_leader': False,  # 动态计算无法判断龙头
            }
    except Exception:
        pass
    
    return meta_info or sector_info or {}


def _layer2_selection(
    ctx: SymbolContext, cfg: Dict[str, Any],
) -> Tuple[bool, float, Dict[str, Any]]:
    """Layer 2: 板块评分 + 龙头加分 + 过热惩罚 + 资金流修正。

    返回 (是否通过, 有效得分, 评分详情字典)。
    """
    sector_info = _resolve_sector_info(ctx, cfg)
    base_score = float(sector_info.get('sector_score', 0) or 0)
    is_leader = bool(sector_info.get('is_leader', False))
    sector_name = sector_info.get('sector_name', 'Unknown')

    # ── 龙头加分 ──
    leader_bonus = float(cfg.get('leader_bonus', 15)) if is_leader else 0.0
    score_with_bonus = base_score + leader_bonus

    # ── 过热惩罚 ──
    overheat_threshold = float(cfg.get('overheat_threshold', 80))
    overheat_penalty = float(cfg.get('overheat_penalty', 0.8))
    overheated = score_with_bonus > overheat_threshold
    score_after_overheat = score_with_bonus * overheat_penalty if overheated else score_with_bonus

    # ── 资金流修正 (clamp 到 [0.85, 1.15]) ──
    try:
        ofi = float(getattr(ctx, 'order_flow_imbalance', 0) or 0)
    except (TypeError, ValueError):
        ofi = 0.0
    flow_factor = max(0.85, min(1.15, 1.0 + ofi * 0.15))
    effective_score = score_after_overheat * flow_factor

    # ── 阈值检查 ──
    min_sector_score = float(cfg.get('min_sector_score', 40))
    passed = effective_score >= min_sector_score

    detail = {
        'sector_name': sector_name,
        'base_score': base_score,
        'is_leader': is_leader,
        'leader_bonus': leader_bonus,
        'overheated': overheated,
        'flow_factor': round(flow_factor, 3),
        'effective_score': round(effective_score, 2),
    }
    return passed, effective_score, detail


# ═══════════════════════════════════════════════════════════════════
#  Layer 3: 双模式入场 (PULLBACK / BREAKOUT)
# ═══════════════════════════════════════════════════════════════════

def _layer3_entry(
    ctx: SymbolContext, direction: str, cfg: Dict[str, Any], vol_ratio: float,
) -> Optional[str]:
    """Layer 3: 决定入场模式。返回 'PULLBACK' / 'BREAKOUT' / None。"""

    # ── PULLBACK 区间 (含 VIX 自适应) ──
    if direction == "LONG":
        rsi_low, rsi_high = cfg.get("pullback_rsi_long", [40, 55])
    else:
        rsi_low, rsi_high = cfg.get("pullback_rsi_short", [45, 60])

    try:
        vix = float(getattr(ctx, 'vix', 0) or 0)
        if vix > 30:
            rsi_low += 3
            rsi_high -= 3
        elif 0 < vix < 15:
            rsi_low -= 5
            rsi_high += 3
    except (TypeError, ValueError):
        pass

    if rsi_low <= ctx.rsi <= rsi_high:
        return "PULLBACK"

    # ── BREAKOUT (Bug #2 修复点: vol_ratio 派生) ──
    # 默认 2.0; cfg 设为 0/None 视为"使用默认", 而非禁用 (与历史测试约定一致)
    breakout_min_vol = cfg.get("breakout_min_volume_ratio")
    if not breakout_min_vol or breakout_min_vol <= 0:
        breakout_min_vol = 2.0
    breakout_min_vol = float(breakout_min_vol)

    breakout_rsi_long = float(cfg.get("breakout_rsi_long", 65))
    breakout_rsi_short = float(cfg.get("breakout_rsi_short", 35))

    if direction == "LONG":
        if ctx.rsi > breakout_rsi_long and vol_ratio >= breakout_min_vol:
            return "BREAKOUT"
    else:
        if ctx.rsi < breakout_rsi_short and vol_ratio >= breakout_min_vol:
            return "BREAKOUT"

    return None


# ═══════════════════════════════════════════════════════════════════
#  评分: confidence + strength
# ═══════════════════════════════════════════════════════════════════

def _compute_scores(
    ctx: SymbolContext, direction: str, mode: str, cfg: Dict[str, Any],
    vol_ratio: float, layer2_detail: Dict[str, Any],
) -> Tuple[float, int, Dict[str, Any]]:
    """计算 confidence (0.45-0.85) 和 strength (30-90), 返回 (conf, str, factor_detail)。"""

    base_conf = 0.55
    base_str = 50

    # ── 量能确认 ──
    volume_confirmed = vol_ratio >= 1.3
    if volume_confirmed:
        base_conf += 0.08
        if vol_ratio >= 1.5:
            base_str += 8

    # ── 均线结构 (LONG: price>ma20>ma50; SHORT: price<ma20<ma50) ──
    ma20 = getattr(ctx, "ma20", 0) or 0
    ma50 = getattr(ctx, "ma50", 0) or 0
    price = ctx.price or 0
    ma_aligned = False
    ma_weak = False
    if direction == "LONG":
        if price > ma20 > ma50 > 0:
            ma_aligned = True
            ma_weak = True
        elif price > ma20 > 0:
            ma_weak = True
    else:
        if 0 < ma20 < ma50 and price < ma20:
            ma_aligned = True
            ma_weak = True
        elif ma20 > 0 and price < ma20:
            ma_weak = True

    if ma_aligned:
        base_conf += 0.07
        base_str += 7
    elif ma_weak:
        base_conf += 0.03

    # ── RSI 位置质量 ──
    if direction == "LONG":
        rsi_low, rsi_high = cfg.get("pullback_rsi_long", [40, 55])
    else:
        rsi_low, rsi_high = cfg.get("pullback_rsi_short", [45, 60])
    rsi_center = (rsi_low + rsi_high) / 2.0
    rsi_quality = max(0.0, 1.0 - abs(ctx.rsi - rsi_center) / max(rsi_high - rsi_low, 1))
    base_conf += rsi_quality * 0.05
    if rsi_quality > 0.7:
        base_str += 5

    # ── Order Flow 加成 ──
    try:
        ofi = float(getattr(ctx, "order_flow_imbalance", 0) or 0)
        if (direction == "LONG" and ofi > 0.1) or (direction == "SHORT" and ofi < -0.1):
            base_conf += 0.05
    except (TypeError, ValueError):
        pass

    # ── VIX 加成 (低波动环境奖励) ──
    try:
        vix = float(getattr(ctx, "vix", 0) or 0)
        if 0 < vix <= 25:
            base_conf += 0.05
    except (TypeError, ValueError):
        pass

    # ── BREAKOUT 模式略增强度 ──
    if mode == "BREAKOUT":
        base_str += 5

    confidence = round(min(0.85, max(0.45, base_conf)), 3)
    strength = min(90, max(30, int(base_str)))

    factor_detail = {
        'mode': mode,
        'vol_ratio': round(vol_ratio, 3),
        'volume_confirmed': volume_confirmed,
        'ma_aligned': ma_aligned,
        'ma_weak': ma_weak,
        'rsi_quality': round(rsi_quality, 3),
        **layer2_detail,
    }
    return confidence, strength, factor_detail


# ═══════════════════════════════════════════════════════════════════
#  策略入口
# ═══════════════════════════════════════════════════════════════════

@register("trend_first")
def evaluate(ctx: SymbolContext, cfg: Dict[str, Any]) -> Optional[SignalCandidate]:
    """Trend-First 四层决策引擎入口.

    数据来源说明:
        ctx.atr / ctx.rsi / ctx.market_regime 由 SymbolContext.from_raw() 注入,
        原始来自 MarketDataRouter.get_full_market_context() — 与 ATRProvider 同根同源。
        本函数不独立计算 ATR/RSI, 仅做派生计算 (volume_ratio fallback / regime fallback)。
    """

    # ── Layer 0: 防御链 ──
    if not _layer0_defense(ctx, cfg):
        return None

    # ── Layer 1: 环境判断 ──
    direction = _layer1_environment(ctx)
    if direction is None:
        return None

    # ── Layer 2: 选股评分 ──
    l2_passed, _l2_score, l2_detail = _layer2_selection(ctx, cfg)
    if not l2_passed:
        return None

    # ── Layer 3: 入场模式 ──
    vol_ratio = _get_volume_ratio(ctx)
    mode = _layer3_entry(ctx, direction, cfg, vol_ratio)
    if mode is None:
        return None

    # ── 可选: 均线对齐硬约束 ──
    if cfg.get("require_ma_alignment", False):
        ma20 = getattr(ctx, "ma20", 0) or 0
        ma50 = getattr(ctx, "ma50", 0) or 0
        price = ctx.price or 0
        if direction == "LONG":
            if not (price > ma20 > ma50 > 0):
                return None
        else:
            if not (0 < ma20 < ma50 and price < ma20):
                return None

    # ── 评分 ──
    confidence, strength, factor_detail = _compute_scores(
        ctx, direction, mode, cfg, vol_ratio, l2_detail,
    )

    # ── min_score 过滤 ──
    min_score = float(cfg.get("min_score", 0) or 0)
    if strength < min_score:
        return None

    # ── 止损 ATR 倍数: BREAKOUT 通常更紧 (1.0×ATR), PULLBACK 标准 (1.5×ATR) ──
    if mode == "BREAKOUT":
        sl_atr_mult = float(cfg.get("breakout_stop_loss_atr_mult", 1.0))
    else:
        sl_atr_mult = float(cfg.get("stop_loss_atr_mult", 1.5))

    regime_resolved = _resolve_regime(ctx)
    logger.info(
        f"[TrendFirst] {ctx.symbol} {direction} {mode} | "
        f"regime={regime_resolved} RSI={ctx.rsi:.0f} ATR={ctx.atr:.2f} "
        f"vol_ratio={vol_ratio:.2f} sector={l2_detail.get('sector_name', 'NA')}"
        f" eff_score={l2_detail.get('effective_score', 0)} "
        f"conf={confidence} str={strength}"
    )

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=strength,
        confidence=confidence,
        strategy_id="trend_first",
        source="trend_first",
        rationale=f"trend|{regime_resolved}|RSI={ctx.rsi:.0f}|{mode}",
        context=ctx,
        metadata={
            "stop_loss_atr_mult": sl_atr_mult,
            "atr": ctx.atr,
            "owner_strategy": "trend_first",
            "entry_mode": mode,
            "regime": regime_resolved,
            "vol_ratio": round(vol_ratio, 3),
            **factor_detail,
        },
    )
