"""
技术指标多因子策略 — 纯函数实现

输入: SymbolContext (RSI/MACD/Bollinger/SuperTrend 已注入)
输出: SignalCandidate 或 None

完全无副作用 — 不调用 IntradayScorer, 不读 DB。
信号生成规则是 YAML 配置驱动的, 不做硬编码判断。
"""

from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SymbolContext, SignalCandidate
from app.strategy.pure import register


# ═══════════════════════════════════════════════════════════════════
#  单一指标评估器 — 每个指标是独立纯函数
# ═══════════════════════════════════════════════════════════════════

def _eval_rsi(ctx: SymbolContext) -> float:
    """RSI: 超卖=做多 (+), 超买=做空 (-), 中性=0"""
    if ctx.rsi <= 0:
        return 0
    if ctx.rsi < 30:
        return 30.0  # 强超卖, 强做多信号
    if ctx.rsi < 40:
        return 15.0
    if ctx.rsi > 70:
        return -30.0  # 强超买, 强做空信号
    if ctx.rsi > 60:
        return -15.0
    return 0  # 中性区间


def _eval_macd(ctx: SymbolContext) -> float:
    """MACD: 金叉=做多 (+), 死叉=做空 (-)"""
    if ctx.macd == 0 or ctx.macd_signal == 0:
        return 0
    diff = ctx.macd - ctx.macd_signal
    if diff > 0.01:
        return 20.0
    if diff < -0.01:
        return -20.0
    return 0


def _eval_bollinger(ctx: SymbolContext) -> float:
    """Bollinger: 触下轨=做多 (+), 触上轨=做空 (-)"""
    if ctx.bollinger_lower <= 0 or ctx.bollinger_upper <= 0 or ctx.price <= 0:
        return 0
    band_width = ctx.bollinger_upper - ctx.bollinger_lower
    if band_width <= 0:
        return 0
    position_in_band = (ctx.price - ctx.bollinger_lower) / band_width
    if position_in_band < 0.15:
        return 25.0
    if position_in_band > 0.85:
        return -25.0
    return 0


def _eval_supertrend(ctx: SymbolContext) -> float:
    """SuperTrend: UP=做多 (+), DOWN=做空 (-)"""
    d = ctx.supertrend_direction.upper()
    if d == "UP":
        return 20.0
    if d == "DOWN":
        return -20.0
    return 0


# 指标评估器注册表
INDICATOR_EVALUATORS = {
    "rsi": _eval_rsi,
    "macd": _eval_macd,
    "bollinger": _eval_bollinger,
    "supertrend": _eval_supertrend,
}


@register("technical")
def evaluate(ctx: SymbolContext, config: Dict[str, Any]) -> Optional[SignalCandidate]:
    """
    技术指标多因子共振策略。

    config:
        indicators: list[str]           # 启用的指标 ["rsi", "macd", "bollinger", "supertrend"]
        weights: dict[str, float]       # 指标权重
        min_signals: int                # 最少共振信号数 (默认 2)
        min_score: int                  # 最低加权分 (默认 50)

    逻辑: 加权求和各指标信号 → 正=做多, 负=做空, 绝对值<阈值=无信号
    """
    indicators = config.get("indicators", ["rsi", "macd", "bollinger", "supertrend"])
    weights = config.get("weights", {"rsi": 0.30, "macd": 0.25, "bollinger": 0.25, "supertrend": 0.20})
    min_signals = config.get("min_signals", 2)

    total_score = 0.0
    active_signals = 0

    for ind in indicators:
        evaluator = INDICATOR_EVALUATORS.get(ind)
        if evaluator is None:
            continue
        score = evaluator(ctx)
        if score != 0:
            active_signals += 1
        total_score += score * weights.get(ind, 0.25)

    if active_signals < min_signals:
        return None

    abs_score = abs(total_score)
    min_score = config.get("min_score", 50)
    if abs_score < min_score:
        return None

    direction = "LONG" if total_score > 0 else "SHORT"
    strength = min(100, int(abs_score))
    confidence = min(0.95, active_signals / len(indicators) * 0.80)

    return SignalCandidate(
        symbol=ctx.symbol,
        direction=direction,
        strength=strength,
        confidence=round(confidence, 4),
        strategy_id="TECHNICAL_MULTI_FACTOR",
        source="technical",
        rationale=f"{active_signals}/{len(indicators)} 指标共振 | score={total_score:.0f}",
        context=ctx,
    )
