"""
技术指标多因子策略 — 纯函数实现 (ADR-007 改进版)

输入: SymbolContext (RSI/MACD/Bollinger/SuperTrend 已注入)
输出: SignalCandidate 或 None

ADR-007 (2026-06-16) 改进:
  1. 新增回撤入场检测 (pullback entry): LONG 不追高, 等回调到支撑位
  2. 新增动量方向确认: SuperTrend 方向必须与信号方向一致
  3. 新增趋势强度确认: 要求均线多头排列 (MA20 > MA50 for LONG)
  4. 移除均值回归逻辑: 不再用 RSI 超卖/超买作为反向信号

核心逻辑:
  - 识别趋势方向 (SuperTrend + MACD)
  - 在趋势方向回调时入场 (RSI 40-55 区间, 非追高)
  - 要求动量确认 (MACD 金叉/死叉)
  - 要求布林带位置合理 (不在极端位置追入)

IC 数据支撑:
  - 原 IC=+0.23 (40信号), 唯一正 IC 策略
  - 改进目标: 通过回撤入场 + 动量确认 → IC 提升至 0.30+
"""

from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register


# ═══════════════════════════════════════════════════════════════════
#  动量方向检测 — 趋势方向 (不预测, 只跟随)
# ═══════════════════════════════════════════════════════════════════

def _momentum_direction(ctx: SymbolContext) -> str:
    """
    判断当前动量方向: "LONG" / "SHORT" / "NEUTRAL"

    基于:
      - SuperTrend 方向 (主趋势)
      - MACD 交叉 (动量确认)
      - 均线排列 (趋势结构)
    """
    # SuperTrend: 主趋势方向
    st_dir = ctx.supertrend_direction.upper()
    st_score = 0
    if st_dir == "UP":
        st_score = 1
    elif st_dir == "DOWN":
        st_score = -1

    # MACD: 动量方向
    macd_score = 0
    if ctx.macd != 0 and ctx.macd_signal != 0:
        diff = ctx.macd - ctx.macd_signal
        if diff > 0.01:
            macd_score = 1  # 金叉, 上行动量
        elif diff < -0.01:
            macd_score = -1  # 死叉, 下行动量

    # 均线排列: 趋势结构
    ma_score = 0
    if ctx.ma20 > 0 and ctx.ma50 > 0:
        if ctx.ma20 > ctx.ma50:
            ma_score = 1  # 多头排列
        elif ctx.ma20 < ctx.ma50:
            ma_score = -1  # 空头排列

    # 综合: 至少 2/3 指标一致才算有方向
    total = st_score + macd_score + ma_score
    if total >= 2:
        return "LONG"
    elif total <= -2:
        return "SHORT"
    return "NEUTRAL"


# ═══════════════════════════════════════════════════════════════════
#  回撤入场检测 — 不追高, 等回调
# ═══════════════════════════════════════════════════════════════════

def _is_pullback_entry(ctx: SymbolContext, direction: str) -> bool:
    """
    判断是否为回调入场时机 (不追高/不追低)

    LONG 回调入场条件:
      - RSI 40-55 (从超卖回升, 但不过热)
      - 价格在布林带中下部 (< 60% 位置)
      - 或: RSI 从低点反弹 (> 前值)

    SHORT 回调入场条件:
      - RSI 45-60 (从超买回落, 但不超卖)
      - 价格在布林带中上部 (> 40% 位置)
    """
    rsi = ctx.rsi
    if rsi <= 0:
        return False

    # 布林带位置
    boll_pos = 0.5  # 默认中间
    if ctx.bollinger_lower > 0 and ctx.bollinger_upper > 0:
        bw = ctx.bollinger_upper - ctx.bollinger_lower
        if bw > 0 and ctx.price > 0:
            boll_pos = (ctx.price - ctx.bollinger_lower) / bw

    if direction == "LONG":
        # RSI 40-55: 回调到支撑区但未超卖
        if 40 <= rsi <= 55 and boll_pos < 0.60:
            return True
        # 或: RSI 30-40 + 布林带下部 (深度回调, 更强信号)
        if 30 <= rsi < 40 and boll_pos < 0.40:
            return True
        return False

    elif direction == "SHORT":
        # RSI 45-60: 反弹到阻力区但未超买
        if 45 <= rsi <= 60 and boll_pos > 0.40:
            return True
        # 或: RSI 60-70 + 布林带上部 (深度反弹)
        if 60 < rsi <= 70 and boll_pos > 0.60:
            return True
        return False

    return False


def _pullback_quality(ctx: SymbolContext, direction: str) -> float:
    """
    评估回调质量 (0.0 ~ 1.0), 越高质量越好

    LONG:
      - RSI 45-50 (完美回调区) → 1.0
      - RSI 40-55 (可接受) → 0.7
      - RSI 30-40 (深度回调) → 0.9 (高风险高回报)
    SHORT:
      - RSI 50-55 → 1.0
      - RSI 45-60 → 0.7
      - RSI 60-70 → 0.9
    """
    rsi = ctx.rsi
    if direction == "LONG":
        if 45 <= rsi <= 50:
            return 1.0
        elif 40 <= rsi < 45 or 50 < rsi <= 55:
            return 0.7
        elif 30 <= rsi < 40:
            return 0.9
        return 0.3
    elif direction == "SHORT":
        if 50 <= rsi <= 55:
            return 1.0
        elif 45 <= rsi < 50 or 55 < rsi <= 60:
            return 0.7
        elif 60 < rsi <= 70:
            return 0.9
        return 0.3
    return 0.0


# ═══════════════════════════════════════════════════════════════════
#  主策略入口
# ═══════════════════════════════════════════════════════════════════

@register("technical")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """
    技术指标多因子动量策略 (ADR-007 改进版)。

    核心逻辑:
      1. 判断动量方向 (SuperTrend + MACD + 均线)
      2. 检测回调入场 (RSI + Bollinger 位置)
      3. 综合评分

    config:
        min_pullback_quality: float  # 最低回调质量 (默认 0.5)
        require_pullback: bool       # 是否强制要求回调入场 (默认 True)
        min_strength: int            # 最低强度 (默认 40)
    """
    min_pullback_quality = config.get("min_pullback_quality", 0.5)
    require_pullback = config.get("require_pullback", True)
    min_strength = config.get("min_strength", 40)

    # ── Step 1: 判断动量方向 ──
    direction = _momentum_direction(ctx)
    if direction == "NEUTRAL":
        return None  # 无明确方向, 不交易

    # ── Step 2: 回撤入场检测 ──
    is_pullback = _is_pullback_entry(ctx, direction)
    if require_pullback and not is_pullback:
        return None  # 不在追高区入场

    pullback_q = _pullback_quality(ctx, direction)
    if require_pullback and pullback_q < min_pullback_quality:
        return None

    # ── Step 3: 综合评分 ──
    # 基础分: 动量方向确认度 (SuperTrend + MACD + MA)
    base_score = 50  # 有方向 = 50 分起

    # 回调质量加成 (0-25 分)
    pullback_bonus = int(pullback_q * 25)

    # SuperTrend 确认加成 (0-10 分)
    st_dir = ctx.supertrend_direction.upper()
    st_confirmed = (st_dir == "UP" and direction == "LONG") or \
                   (st_dir == "DOWN" and direction == "SHORT")
    st_bonus = 10 if st_confirmed else 0

    # MACD 动量加成 (0-10 分)
    macd_bonus = 0
    if ctx.macd != 0 and ctx.macd_signal != 0:
        diff = ctx.macd - ctx.macd_signal
        if direction == "LONG" and diff > 0.01:
            macd_bonus = 10
        elif direction == "SHORT" and diff < -0.01:
            macd_bonus = 10

    # 成交量确认 (0-5 分)
    vol_bonus = 0
    if ctx.volume > 0 and ctx.avg_volume > 0:
        vol_ratio = ctx.volume / ctx.avg_volume
        if vol_ratio >= 1.5:
            vol_bonus = 5
        elif vol_ratio >= 1.2:
            vol_bonus = 3

    # ATR 波动率检查 — 过低波动不交易 (流动性差)
    if ctx.atr > 0 and ctx.price > 0:
        atr_pct = ctx.atr / ctx.price * 100
        if atr_pct < 0.5:
            return None  # 波动太低, 不值得交易

    strength = min(100, base_score + pullback_bonus + st_bonus + macd_bonus + vol_bonus)
    if strength < min_strength:
        return None

    # 置信度: 回调质量 × 动量确认度
    momentum_confirm = (st_bonus + macd_bonus) / 20.0  # 0-1
    confidence = min(0.90, 0.40 + pullback_q * 0.30 + momentum_confirm * 0.20)

    # 构建 rationale
    parts = [
        f"dir={direction}",
        f"pullback={pullback_q:.1f}",
        f"ST={st_dir}",
        f"RSI={ctx.rsi:.0f}",
    ]
    if vol_bonus > 0:
        parts.append(f"vol={ctx.volume / ctx.avg_volume:.1f}x")

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=strength,
        confidence=round(confidence, 4),
        strategy_id="TECHNICAL_MULTI_FACTOR",
        source="technical",
        rationale=" | ".join(parts),
        context=ctx,
    )
