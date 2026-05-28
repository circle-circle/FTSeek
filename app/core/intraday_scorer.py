"""
FTSeek-US 盘中技术面评分引擎 (Intraday Technical Scorer)

设计原则（幻方量化实践）：
1. 连续打分，不二元化 — 保留信息梯度
2. 多因子正交 — 各因子捕获不同 alpha 来源
3. 量价结合 — LV3 盘口数据参与方向判断
4. 波动率自适应 — 仓位 = f(stop_distance, ATR, equity)
5. 日内时段感知 — 不同时段不同确认门槛
6. 因子级可解释 — 每个因子贡献可追溯

使用方式:
    scorer = IntradayScorer()
    signal = scorer.score(context)  # context 来自 MarketDataRouter.get_full_market_context()
"""

from __future__ import annotations
import math
import logging
from datetime import datetime, time
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field

import pytz

logger = logging.getLogger("ftseek.intraday_scorer")

ET = pytz.timezone("America/New_York")


def vix_adaptive_std(vix: float, base_std: float = 2.0) -> float:
    """
    VIX 自适应 BB 标准差倍数

    dyn = base_std × (1 + 0.05 × (VIX - 20))
    Floor: 1.5σ, Cap: 4.0σ

    当 VIX > 20: BB 带宽扩大，2σ 突破在统计上不再显著
    当 VIX < 20: BB 带宽缩小，突破信号更可靠
    """
    if vix <= 0:
        return base_std
    dyn = base_std * (1.0 + 0.05 * (vix - 20.0))
    return max(1.5, min(4.0, dyn))


def vix_stop_multiplier(vix: float) -> float:
    """
    VIX 自适应止损 ATR 倍数

    VIX < 15  → 1.5x ATR  (低波动, 紧止损)
    VIX 15-25 → 2.0x ATR  (正常)
    VIX 25-35 → 2.5x ATR  (高波动, 放宽)
    VIX > 35  → 3.0x ATR  (恐慌, 极宽止损避免噪音震出)
    """
    if vix <= 0:
        return 2.0
    if vix < 15:
        return 1.5
    elif vix < 25:
        return 2.0
    elif vix < 35:
        return 2.5
    else:
        return 3.0


# ============================================================
# 数据结构
# ============================================================

@dataclass
class FactorScore:
    """单个因子的评分"""
    name: str
    raw: float          # 原始值 (信号强度, -100 ~ +100)
    confidence: float   # 该因子的置信度 (0-1)
    weight: float       # 动态权重
    reasoning: str = ""

    @property
    def contribution(self) -> float:
        """对最终得分的加权贡献"""
        return self.raw * self.confidence * self.weight


@dataclass
class IntradaySignal:
    """盘中交易信号"""
    symbol: str
    direction: str                # LONG / SHORT / NEUTRAL
    strength: int                 # 50±40, 兼容现有 vote_result 格式
    confidence: float             # 0.0-1.0
    consensus_level: float        # 因子间一致性 (0-1)
    position_pct: float           # 建议仓位比例 (净值%)
    stop_loss_pct: float          # 建议止损距离 (%)
    factors: Dict[str, FactorScore] = field(default_factory=dict)
    flags: Dict[str, bool] = field(default_factory=dict)
    reasoning: str = ""

    def to_vote_result(self) -> Dict[str, Any]:
        """转换为兼容现有 vote_result 的 dict 格式"""
        return {
            "final_strength": self.strength,
            "final_confidence": self.confidence,
            "direction": self.direction,
            "consensus_level": self.consensus_level,
            "std_dev": self._compute_pseudo_std(),
            "needs_manual_review": self.confidence < 0.50,
            "model_scores": [{
                "model_id": "intraday_scorer_v2",
                "strength": self.strength,
                "confidence": self.confidence,
                "direction": self.direction,
                "weight": 1.0,  # Single-factor scorer: full weight
                "reasoning": self.reasoning,
            }],
            "is_neutral": self.direction == "NEUTRAL",
            "symbol": self.symbol,
            "phase_gate": "TECHNICAL_ONLY",
            "position_suggestion": {
                "suggested_position": self.position_pct,
                "stop_loss_pct": self.stop_loss_pct,
                "confidence_level": (
                    "HIGH" if self.confidence >= 0.70 else
                    "MEDIUM" if self.confidence >= 0.60 else
                    "LOW" if self.confidence >= 0.50 else "MINIMAL"
                ),
                "reasoning": self.reasoning,
            },
            "market_regime": self.flags.get("regime_label", "NORMAL"),
            # 因子级明细 (调试/回测用)
            "_factor_detail": {
                name: {"raw": f.raw, "conf": f.confidence, "weight": f.weight, "reason": f.reasoning}
                for name, f in self.factors.items()
            },
        }

    def decompose_to_votes(self) -> list:
        """
        因子投票分解: 将每个内部因子导出为独立投票

        每个因子成为一个独立的 "model" 投票, 可被 VotingCoordinator 单独加权聚合。
        这样共识引擎看到的是 7 个独立技术因子投票, 而非 1 个黑盒 scorer。

        Returns:
            [{"model_id": "tech_trend", "direction": "LONG", "strength": 55, "confidence": 0.6}, ...]
        """
        votes = []
        factor_direction_map = {
            "trend":     "tech_trend",
            "momentum":  "tech_momentum",
            "volume":    "tech_volume",
            "orderflow": "tech_orderflow",
            "lifecycle": "tech_lifecycle",
        }

        for key, factor in self.factors.items():
            if factor.confidence <= 0:
                continue

            if factor.raw > 2:
                direction = "LONG"
                strength = 50 + min(abs(factor.raw) * 0.4, 40)
            elif factor.raw < -2:
                direction = "SHORT"
                strength = 50 - min(abs(factor.raw) * 0.4, 40)
            else:
                continue  # Skip neutral factors

            model_id = factor_direction_map.get(key, f"tech_{key}")

            votes.append({
                "model_id": model_id,
                "direction": direction,
                "strength": int(strength),
                "confidence": factor.confidence,
                "weight": factor.weight,
                "reasoning": f"{key}: {factor.reasoning}",
            })

        return votes

    def _compute_pseudo_std(self) -> float:
        """从因子一致性反推伪 std_dev (兼容下游 aggregator 期望的字段)"""
        # consensus_level 越高 → std_dev 越低
        # 映射: consensus=1.0 → std=0, consensus=0.0 → std=25
        return round((1.0 - self.consensus_level) * 25.0, 2)


# ============================================================
# 辅助: 日内时段检测
# ============================================================

def _get_intraday_session() -> str:
    """识别当前日内时段 (美东) → canonical name

    幻方 P2 (2026-05-01): 路由到 SharedKernel，入口和出口共用一套 taxonomy。
    """
    from app.core.shared_kernel import get_shared_kernel
    return get_shared_kernel().get_current_session()


def _session_multiplier() -> Tuple[float, float]:
    """
    返回 (confidence_multiplier, strength_multiplier) 基于日内时段。

    信号质量评估 (不同于 session_vol_ratio):
      - opening_drive: 假突破率高 → 降低置信度/仓位
      - midday:        波动低幅度小 → 降低强度
      - closing_drive: 趋势持续性强 → 提升强度

    幻方 P2 (2026-05-01): key 改为 canonical session name, 从 SharedKernel 统一 taxonomy。
    """
    from app.core.shared_kernel import get_shared_kernel
    session = get_shared_kernel().get_current_session()
    multipliers = {
        "opening_drive":  (0.75, 0.85),   # 开盘: 信号不可靠, 仓位保守
        "morning":        (0.90, 0.95),   # 盘中前段: 逐步正常化
        "midday":         (0.85, 0.80),   # 午间: 波动低, 幅度小
        "afternoon":      (0.95, 1.00),   # 午后: 正常
        "closing_drive":  (1.00, 1.10),   # 收盘: 趋势可靠, 适度激进
    }
    return multipliers.get(session, (0.80, 0.80))


# ============================================================
# 评分引擎
# ============================================================

class IntradayScorer:
    """
    盘中技术面评分引擎 v2.0

    6 因子体系:
      F1 — 趋势强度 (Trend Strength)
      F2 — 动量质量 (Momentum Quality)
      F3 — 量价关系 (Volume-Price Relationship)
      F4 — 订单流失衡 (Order Flow Imbalance)  [LV3 专有]
      F5 — 波动率环境 (Volatility Context)     [调节器]
      F6 — 日内时段  (Session Modifier)        [调节器]

    架构:
      Direction = sign(F1*W1 + F2*W2 + F3*W3 + F4*W4)
      Strength = weighted |sum| after session × volatility modifiers
      Confidence = weighted average of factor confidences × session multiplier
      Consensus = 1 - normalized σ of factor directions
    """

    # ── 因子权重（可调参） ──
    DEFAULT_WEIGHTS = {
        "trend":     0.25,
        "momentum":  0.25,
        "volume":    0.18,
        "orderflow": 0.17,
        "lifecycle": 0.15,   # F7: 动量生命周期
    }

    # 波动率环境 → 因子权重偏移
    REGIME_WEIGHT_MODIFIERS = {
        # (vix_level, trend) → {factor: delta}
        # 低波动: 趋势+动量+生命周期有效, 订单流噪声大
        "LOW_VOLATILITY":     {"trend": +0.05, "momentum": +0.05, "lifecycle": +0.05, "orderflow": -0.05},
        # 高波动: 订单流和量价更有价值, 趋势滞后, 生命周期噪声大
        "HIGH_VOLATILITY":    {"trend": -0.05, "orderflow": +0.10, "volume": +0.05, "lifecycle": -0.05},
        # 极端波动: 全部降低权重, 现金为王
        "EXTREME_VOLATILITY": {"trend": -0.10, "momentum": -0.10, "orderflow": -0.05, "volume": -0.05, "lifecycle": -0.10},
    }

    # ── 动量生命周期参数 ──
    LIFECYCLE_EMA_PERIOD: int = 5       # 二阶 EMA 平滑周期
    LIFECYCLE_ACCEL_BARS: int = 3       # 连续增强触发加速信号
    LIFECYCLE_CONV_BARS: int = 3        # 连续收缩触发收敛信号
    LIFECYCLE_MAX_HISTORY: int = 200    # 每标的收盘价最大保留数

    def __init__(self):
        self._atr_history: Dict[str, float] = {}  # 日内缓存
        self._prev_vix: Optional[float] = None    # VIX 跳变检测
        # 动量生命周期状态 {symbol: {"closes": [], "momentum": [], "prev_momentum": float}}
        self._lifecycle_state: Dict[str, Dict] = {}

    # ============================================================
    # 主入口
    # ============================================================

    def score(self, context: Dict[str, Any]) -> IntradaySignal:
        """主评分入口"""
        symbol = context.get("symbol", "UNKNOWN")
        price = float(context.get("price", 0))

        if price <= 0:
            return self._empty_signal(symbol, "price unavailable")

        # P0-S1 (2026-05-14): warmup 检查 — 09:30-10:00 间 5min bar 数 < 14, 指标失真
        # ATR(14)/RSI(14)/EMA(20) 等需至少 14 根 5min bar 才有意义
        # bars_5m_count 由 MarketDataRouter 在 ctx 中提供; 缺省时跳过检查 (兼容旧路径)
        bars_count = int(context.get("bars_5m_count", 999))
        if bars_count < 14:
            return self._empty_signal(symbol, f"warmup_incomplete(bars_5m={bars_count})")

        # P0-S2 (2026-05-14): 停牌检测 — Futu 推送的 last_quote 在停牌期仍是停牌前最后一价
        if context.get("is_halted", False):
            return self._empty_signal(symbol, "halted")

        # P0-D3 (2026-05-14): 拆股 guard — 当日拆股 symbol 跳过信号
        try:
            from app.data.split_event_guard import get_split_guard
            _guard = get_split_guard()
            if _guard.is_split_today(symbol):
                return self._empty_signal(symbol, "split_event_today")
            # 启发式: 价格突变 > 30% 自动标记
            prev_close = float(context.get("prev_close", 0))
            if prev_close > 0:
                _guard.detect_split_anomaly(symbol, price, prev_close)
                if _guard.is_split_today(symbol):
                    return self._empty_signal(symbol, "split_anomaly_detected")
        except Exception:
            pass  # guard 异常不阻断信号

        # ── Step 1: 评估波动率环境 (决定后续因子权重) ──
        vix = float(context.get("vix", 20))
        regime = self._assess_regime(vix, context)

        # ── Step 2: 计算各因子 ──
        factors: Dict[str, FactorScore] = {}
        weights = self._get_regime_weights(regime)

        factors["trend"] = self._score_trend(context, price, weights.get("trend", 0.25))
        factors["momentum"] = self._score_momentum(context, price, weights.get("momentum", 0.25))
        factors["volume"] = self._score_volume(context, price, weights.get("volume", 0.18))
        factors["orderflow"] = self._score_orderflow(context, weights.get("orderflow", 0.17))
        factors["lifecycle"] = self._score_momentum_lifecycle(context, price, vix, weights.get("lifecycle", 0.15))

        # ── Step 3: 聚合方向 + 强度 ──
        direction, raw_strength = self._aggregate_direction(factors)

        # ── Step 4: 计算置信度 (因子内部一致性) ──
        confidence = self._compute_confidence(factors, direction)

        # ── Step 5: VIX 跳变检测 → 置信度校准 ──
        vix_jump = self._detect_vix_jump(vix)
        if vix_jump:
            confidence = round(confidence * 0.80, 2)
            logger.warning(
                f"[VIX Jump] {vix_jump['type']} | VIX {vix_jump['prev_vix']:.1f}→{vix:.1f} "
                f"({vix_jump['ratio']:.1%}) | 置信度削弱至 {confidence:.2f}"
            )

        # ── Step 6: 计算共识度 (因子间方向一致性) ──
        consensus = self._compute_consensus(factors, direction)

        # ── Step 7: 日内时段调整 ──
        session_conf_mul, session_str_mul = _session_multiplier()
        confidence = round(min(confidence * session_conf_mul, 0.92), 2)
        adjusted_strength = int(raw_strength * session_str_mul)

        # 映射到 50±40 格式 (兼容下游 vote_result)
        if direction == "LONG":
            strength = 50 + adjusted_strength
        elif direction == "SHORT":
            strength = 50 - adjusted_strength
        else:
            strength = 50
        strength = self._clamp_strength(strength)

        # ── Step 8: 波动率自适应仓位 + 止损 ──
        atr = float(context.get("atr", 0))
        position_pct, stop_loss_pct = self._compute_position_and_stop(
            confidence, atr, price, vix, direction
        )
        # P0-D2 (2026-05-14): ATR=0 或 price=0 → _compute 返回 (0,0) → 拒信号
        if position_pct <= 0 or stop_loss_pct <= 0:
            return self._empty_signal(symbol, f"data_missing(atr={atr},price={price})")

        # ── Step 9: 构建信号 ──
        reasoning = "; ".join(
            f"{f.name}={f.raw:+.0f}" for f in factors.values() if abs(f.raw) > 5
        ) or "no clear signal"

        flags = {
            "regime": regime,
            "regime_label": self._regime_label(regime),
            "session": _get_intraday_session(),
            "is_strong": confidence >= 0.65 and raw_strength >= 30,
            "is_weak": confidence < 0.50 or raw_strength < 15,
            "short_blocked": direction == "SHORT" and raw_strength < 25,
            "vix_jump": vix_jump is not None,
        }

        return IntradaySignal(
            symbol=symbol,
            direction=direction,
            strength=strength,
            confidence=confidence,
            consensus_level=consensus,
            position_pct=position_pct,
            stop_loss_pct=stop_loss_pct,
            factors=factors,
            flags=flags,
            reasoning=reasoning,
        )

    # ============================================================
    # F1: 趋势强度 (Trend Strength)
    # ============================================================

    def _score_trend(self, ctx: Dict[str, Any], price: float, weight: float) -> FactorScore:
        """
        连续趋势评分：MA 位置 + MA 斜率 + 多时间框架一致性

        技术依据: 美股机构主导，MA20/50/200 是关键参考线。
        MA 结构完整的多头排列 > 均线粘合 > 空头排列
        """
        ma20 = float(ctx.get("ma20", 0))
        ma50 = float(ctx.get("ma50", 0))
        ma200 = float(ctx.get("ma200", 0))
        spy_ma50 = float(ctx.get("spy_ma50", 0))
        spy_price = float(ctx.get("spy_price", 0))

        checks = 0
        bull_score = 0.0
        bear_score = 0.0

        # 1) 价格相对 MA 的偏离度 (连续, 非二元)
        for ma_name, ma_val in [("MA20", ma20), ("MA50", ma50)]:
            if ma_val > 0:
                checks += 1
                deviation = (price - ma_val) / ma_val  # 如 +0.03 表示在 MA 上方 3%
                # tanh 映射: 偏离 5% → ~1.0, 偏离 1% → ~0.2
                sig = math.tanh(deviation * 20)  # scale factor 20 → 5%偏离=tanh(1)≈0.76
                if sig > 0:
                    bull_score += abs(sig)
                else:
                    bear_score += abs(sig)

        # 2) MA 排列结构: 多头排列+均线发散 > 空头 > 缠绕
        if ma20 > 0 and ma50 > 0:
            checks += 1
            if ma20 > ma50:
                spread = (ma20 - ma50) / ma50
                bull_score += min(spread * 10, 1.0)  # 2% spread → 0.2, 10% → 1.0
            else:
                spread = (ma50 - ma20) / ma20
                bear_score += min(spread * 10, 1.0)

        if ma50 > 0 and ma200 > 0:
            checks += 1
            if ma50 > ma200:
                bull_score += 0.5
            else:
                bear_score += 0.5

        # 3) SPY 趋势共振 (宏观参考)
        if spy_price > 0 and spy_ma50 > 0:
            checks += 1
            spy_dev = (spy_price - spy_ma50) / spy_ma50
            spy_sig = math.tanh(spy_dev * 20)
            if spy_sig > 0.1:
                bull_score += 0.3   # 大盘多头, 个股做多更安全
            elif spy_sig < -0.1:
                bear_score += 0.3

        # 综合
        if bull_score > bear_score:
            raw = min(bull_score / max(checks, 1) * 100, 100)
        elif bear_score > bull_score:
            raw = -min(bear_score / max(checks, 1) * 100, 100)
        else:
            raw = 0.0

        dominance = abs(bull_score - bear_score) / max(bull_score + bear_score, 0.001)
        confidence = 0.35 + dominance * 0.50  # 0.35 ~ 0.85

        return FactorScore(
            name="trend",
            raw=round(raw, 1),
            confidence=round(confidence, 2),
            weight=weight,
            reasoning=f"bull={bull_score:.1f} bear={bear_score:.1f} checks={checks}",
        )

    # ============================================================
    # F2: 动量质量 (Momentum Quality)
    # ============================================================

    def _score_momentum(self, ctx: Dict[str, Any], price: float, weight: float) -> FactorScore:
        """
        动量多维评分: RSI 水平 + RSI 方向 + MACD 柱 + 价格变化率

        关键洞察: RSI 梯度比 RSI 绝对值更有预测价值。
        RSI=72 且在上升 → 强势追涨; RSI=72 且在下降 → 顶部背离。
        """
        rsi = float(ctx.get("rsi", 50))
        rsi_prev = float(ctx.get("rsi_prev", rsi))  # 上一根 K 线的 RSI
        macd = float(ctx.get("macd", 0))
        macd_signal = float(ctx.get("macd_signal", 0))
        change_pct = float(ctx.get("change_percent", 0))

        bull_score = 0.0
        bear_score = 0.0

        # 1) RSI 水平分 (连续映射)
        # 理想做多区间 [30, 65], 理想做空区间 [35, 70]
        # 使用分段 sigmoid 替代硬阈值
        if rsi < 40:
            bull_score += (40 - rsi) / 30 * 1.5    # RSI=30→0.5, RSI=20→1.0
        elif rsi < 65:
            bull_score += (rsi - 40) / 25 * 1.0    # RSI=52→0.48, 温和看多区域
        if rsi > 60:
            bear_score += (rsi - 60) / 30 * 1.5    # RSI=70→0.5, RSI=80→1.0
        elif rsi > 35:
            bear_score += (60 - rsi) / 25 * 0.8    # RSI=48→0.38, 温和看空区域

        # 2) RSI 动量 (梯度) — 关键增量信息
        rsi_delta = rsi - rsi_prev
        if rsi_delta > 0:
            bull_score += min(rsi_delta / 10, 0.8)   # 10 点变化 → 0.8 分
            if rsi < 50:
                bull_score += 0.3  # 低位反弹加成
        elif rsi_delta < 0:
            bear_score += min(abs(rsi_delta) / 10, 0.8)
            if rsi > 50:
                bear_score += 0.3  # 高位回落加成

        # 2.5) 极端 RSI 惩罚 — P0 修复 (2026-05-28)
        # RSI > 80: 极度超买, 涨幅视为动量耗竭而非延续
        # RSI < 20: 极度超卖, 跌幅视为恐慌出清而非趋势延续
        if rsi > 80:
            overbought_penalty = (rsi - 80) / 20 * 2.0  # RSI=90→1.0, RSI=95→1.5, RSI=100→2.0
            bear_score += overbought_penalty
        elif rsi < 20:
            oversold_penalty = (20 - rsi) / 20 * 2.0
            bull_score += oversold_penalty

        # 3) MACD 柱: 使用 hist = macd - macd_signal
        hist = macd - macd_signal
        if hist > 0:
            bull_score += min(hist * 5, 1.0)
            # 水下金叉加成
            if macd < 0:
                bull_score += 0.5
        else:
            bear_score += min(abs(hist) * 5, 1.0)
            if macd > 0:
                bear_score += 0.5

        # 4) 短期价格变化率 — P1 2026-05-12 审计修复
        # 优先使用 slope_5m (最近 5 根 1-min K 线的变化率), 反映"当下"动量。
        # 如果 slope_5m 不可用 (fallback 路径), 退回到 change_percent (日累计)。
        # P0 修复 (2026-05-28): 极端 RSI 区域反转变化率信号 — 超买区涨幅=耗竭
        is_extreme_overbought = rsi > 80
        is_extreme_oversold = rsi < 20
        slope_5m = ctx.get("slope_5m")
        if slope_5m is not None:
            # slope_5m 是百分比 (如 +0.5 表示最近 5 分钟涨了 0.5%)
            if slope_5m > 0:
                if is_extreme_overbought:
                    bear_score += slope_5m / 8  # 超买区涨幅 → 反转看空
                else:
                    bull_score += slope_5m / 8
            else:
                if is_extreme_oversold:
                    bull_score += min(abs(slope_5m) / 8, 1.0)  # 超卖区跌幅 → 反转看多
                else:
                    bear_score += min(abs(slope_5m) / 8, 1.0)
        else:
            # Fallback: 日累计 change_percent (已知语义问题, 但比没有好)
            if change_pct > 0:
                if is_extreme_overbought:
                    bear_score += min(change_pct / 8, 1.5)  # 超买区涨幅 → 耗竭
                else:
                    bull_score += change_pct / 8  # 线性加分, 不封顶
            else:
                if is_extreme_oversold:
                    bull_score += min(abs(change_pct) / 8, 1.5)  # 超卖区跌幅 → 恐慌出清
                else:
                    bear_score += min(abs(change_pct) / 8, 1.0)

        if bull_score > bear_score:
            raw = min(bull_score / 3 * 100, 100)
        elif bear_score > bull_score:
            raw = -min(bear_score / 3 * 100, 100)
        else:
            raw = 0.0

        dominance = abs(bull_score - bear_score) / max(bull_score + bear_score, 0.001)
        # 动量因子自身可靠性: RSI + MACD 都是主流有效指标
        base_conf = 0.40 + dominance * 0.45 + (0.10 if abs(rsi_delta) > 3 else 0)

        return FactorScore(
            name="momentum",
            raw=round(raw, 1),
            confidence=round(min(base_conf, 0.90), 2),
            weight=weight,
            reasoning=f"RSI={rsi:.0f}(Δ{rsi_delta:+.0f}) MACD={macd:.4f} chg={change_pct:+.1f}%",
        )

    # ============================================================
    # F3: 量价关系 (Volume-Price Relationship)
    # ============================================================

    def _score_volume(self, ctx: Dict[str, Any], price: float, weight: float) -> FactorScore:
        """
        量价关系分析: 放量方向确认 + 缩量警告

        核心逻辑（Wyckoff 方法）:
        - 放量上涨 → 需求主导, 强势
        - 放量下跌 → 供给主导, 弱势
        - 缩量上涨 → 虚假突破, 谨慎
        - 缩量下跌 → 抛压枯竭, 可能见底
        """
        volume_ratio = float(ctx.get("volume_ratio", 1.0))
        change_pct = float(ctx.get("change_percent", 0))
        turnover = float(ctx.get("turnover", 0))
        amplitude = float(ctx.get("amplitude", 0))  # 振幅

        bull_score = 0.0
        bear_score = 0.0

        # 1) 量价方向: 放量配合方向 = 有效
        is_high_volume = volume_ratio > 1.3
        is_low_volume = volume_ratio < 0.6
        is_up = change_pct > 0
        is_down = change_pct < 0

        if is_high_volume and is_up:
            # 放量上涨 → 真突破
            magnitude = min(volume_ratio - 1.0, 2.0)  # cap at 2
            bull_score += 0.5 * magnitude
        elif is_high_volume and is_down:
            # 放量下跌 → 真破位
            magnitude = min(volume_ratio - 1.0, 2.0)
            bear_score += 0.5 * magnitude
        elif is_low_volume and is_up:
            # 缩量上涨 → 可疑反弹
            bull_score += 0.15
            bear_score += 0.15  # 中性偏空
        elif is_low_volume and is_down:
            # 缩量下跌 → 可能见底
            bear_score += 0.15
            bull_score += 0.20  # 轻微看多

        # 2) 振幅 × 量能: 高振幅+高量 = 方向可信
        if amplitude > 0 and turnover > 0:
            vigor = amplitude * min(turnover / 1e8, 2.0)  # 归一化能量
            if is_up:
                bull_score += vigor * 0.05
            elif is_down:
                bear_score += vigor * 0.05

        # 3) 量比趋势(如果可用): 量能递增还是递减
        volume_trend = float(ctx.get("volume_trend", 0))  # 多周期量比趋势
        if volume_trend > 0.05:
            # 量能在放大 → 当前方向可信度提升
            if is_up:
                bull_score += 0.3
            elif is_down:
                bear_score += 0.3

        # 4) 2026-05-20 增强: Push Ticker 实时量能突增因子
        # ticker_volume_surge = 短期均量 / 中期均量 (>1.5 表示放量)
        ticker_surge = float(ctx.get("ticker_volume_surge", 0))
        if ticker_surge > 0:
            if ticker_surge > 2.0:
                # 强放量: 方向确认度极高
                if is_up:
                    bull_score += 0.5
                elif is_down:
                    bear_score += 0.5
            elif ticker_surge > 1.5:
                # 中等放量: 方向辅助确认
                if is_up:
                    bull_score += 0.25
                elif is_down:
                    bear_score += 0.25

            # VWAP 偏离: 价格在 VWAP 上方 → 多头强势
            vwap_dev = float(ctx.get("ticker_vwap_deviation", 0))
            if vwap_dev > 0.003:
                bull_score += 0.2
            elif vwap_dev < -0.003:
                bear_score += 0.2

        if bull_score > bear_score:
            raw = min(bull_score * 60, 100)
        elif bear_score > bull_score:
            raw = -min(bear_score * 60, 100)
        else:
            raw = 0.0

        dominance = abs(bull_score - bear_score) / max(bull_score + bear_score, 0.001)
        # 量价关系作为辅助因子的置信度
        confidence = 0.30 + dominance * 0.40

        return FactorScore(
            name="volume",
            raw=round(raw, 1),
            confidence=round(confidence, 2),
            weight=weight,
            reasoning=f"VR={volume_ratio:.1f} chg={change_pct:+.1f}% ampl={amplitude:.1f}%",
        )

    # ============================================================
    # F4: 订单流失衡 (Order Flow Imbalance) — LV3 专有
    # ============================================================

    def _score_orderflow(self, ctx: Dict[str, Any], weight: float) -> FactorScore:
        """
        LV3 盘口数据: bid/ask 量失衡 + 买卖价差 + 逐笔量能 + 深度因子

        技术依据: 美股 Level 2 数据中, 买卖盘口的挂单量失衡
        提前 3-10 秒预示价格方向。bid_vol > ask_vol * 1.3 表示买盘主导。

        2026-05-20 增强: 融合 Push Ticker 逐笔数据 + Push OrderBook 深度因子
        """
        bid = float(ctx.get("lv3_bid", 0))
        ask = float(ctx.get("lv3_ask", 0))
        bid_vol = float(ctx.get("lv3_bid_vol", 0))
        ask_vol = float(ctx.get("lv3_ask_vol", 0))
        imbalance = float(ctx.get("order_flow_imbalance", 0))
        price = float(ctx.get("price", 0))

        # 如果没有任何 LV3 数据, 该因子退化为中性
        has_lv3 = (bid > 0 or ask > 0 or bid_vol > 0 or ask_vol > 0)

        # 2026-05-20: 检查是否有 Push 增强数据
        has_ticker = ctx.get("ticker_score") is not None
        has_ob = ctx.get("ob_direction_score") is not None

        if not has_lv3 and not has_ticker and not has_ob:
            return FactorScore(
                name="orderflow",
                raw=0.0, confidence=0.0, weight=weight,
                reasoning="LV3 data unavailable",
            )

        bull_score = 0.0
        bear_score = 0.0
        reasoning_parts = []

        # 1) 盘口量失衡 (核心因子)
        if bid_vol > 0 and ask_vol > 0:
            ratio = bid_vol / max(ask_vol, 1)
            if ratio > 1.2:
                bull_score += min((ratio - 1.0) * 2, 1.0)  # 1.5:1 → 1.0
            elif ratio < 0.8:
                bear_score += min((1.0 - ratio) * 2, 1.0)
            reasoning_parts.append(f"bid/ask={bid_vol:.0f}/{ask_vol:.0f}")

        # 2) 预计算的 imbalance 指标
        if imbalance > 0.15:
            bull_score += imbalance * 2.0
        elif imbalance < -0.15:
            bear_score += abs(imbalance) * 2.0
        reasoning_parts.append(f"imb={imbalance:+.2f}")

        # 3) 买卖价差 (spread): 过大表示流动性差, 降低置信度
        spread_pct = 0.0
        if bid > 0 and ask > 0 and price > 0:
            spread_pct = (ask - bid) / price

        # ★★★ 4) 2026-05-20 增强: Push Ticker 逐笔量能因子
        if has_ticker:
            ticker_bs_ratio = float(ctx.get("ticker_buy_sell_ratio", 1.0))
            ticker_volume_surge = float(ctx.get("ticker_volume_surge", 1.0))
            ticker_large_order = float(ctx.get("ticker_large_order_ratio", 0))
            ticker_score = float(ctx.get("ticker_score", 50))

            # 逐笔买卖力道: ratio > 1.2 多头, < 0.8 空头
            if ticker_bs_ratio > 1.5:
                bull_score += 0.8
            elif ticker_bs_ratio > 1.2:
                bull_score += 0.4
            elif ticker_bs_ratio < 0.67:
                bear_score += 0.8
            elif ticker_bs_ratio < 0.83:
                bear_score += 0.4

            # 放量 + 方向确认
            if ticker_volume_surge > 1.5 and ticker_bs_ratio > 1.0:
                bull_score += 0.3
            elif ticker_volume_surge > 1.5 and ticker_bs_ratio < 1.0:
                bear_score += 0.3

            # 大单占比: 高比例 → 机构参与 → 加分
            if ticker_large_order > 0.2:
                if ticker_bs_ratio > 1.0:
                    bull_score += 0.3
                else:
                    bear_score += 0.3

            reasoning_parts.append(f"ticker_bs={ticker_bs_ratio:.2f} surge={ticker_volume_surge:.2f}")

        # ★★★ 5) 2026-05-20 增强: Push OrderBook 深度因子
        if has_ob:
            ob_depth_imb = float(ctx.get("ob_depth_imbalance", 0))
            ob_liquidity = float(ctx.get("ob_liquidity_score", 50))
            ob_order_flow_imb = float(ctx.get("ob_order_flow_imbalance", 0))
            ob_spread_bps = float(ctx.get("ob_spread_bps", 0))

            # 深度不平衡: 买盘深度 > 卖盘
            if ob_depth_imb > 0.3:
                bull_score += 0.5
            elif ob_depth_imb > 0.15:
                bull_score += 0.25
            elif ob_depth_imb < -0.3:
                bear_score += 0.5
            elif ob_depth_imb < -0.15:
                bear_score += 0.25

            # 订单流失衡
            if ob_order_flow_imb > 0.2:
                bull_score += 0.3
            elif ob_order_flow_imb < -0.2:
                bear_score += 0.3

            # 流动性: 高流动性 → 信号更可靠 → 提升置信度
            if ob_liquidity > 70:
                spread_pct *= 0.5  # 高流动性等效缩小 spread
            elif ob_liquidity < 30:
                spread_pct *= 2.0  # 低流动性放大 spread 惩罚

            reasoning_parts.append(f"ob_depth={ob_depth_imb:+.2f} liq={ob_liquidity:.0f}")

        if bull_score > bear_score:
            raw = min(bull_score * 50, 100)
        elif bear_score > bull_score:
            raw = -min(bear_score * 50, 100)
        else:
            raw = 0.0

        # 置信度: 受 spread 影响, Push 数据增强时提升
        if spread_pct > 0.005:   # 0.5% spread → 低流动性
            base_conf = 0.15
        elif spread_pct > 0.002:
            base_conf = 0.25
        else:
            base_conf = 0.35

        # Push 数据增强置信度
        if has_ticker and has_ob:
            base_conf += 0.10  # 同时有逐笔+深度, 信号更可靠
        elif has_ticker or has_ob:
            base_conf += 0.05

        dominance = abs(bull_score - bear_score) / max(bull_score + bear_score, 0.001)
        confidence = base_conf + dominance * 0.35

        return FactorScore(
            name="orderflow",
            raw=round(raw, 1),
            confidence=round(min(confidence, 0.85), 2),
            weight=weight,
            reasoning=" | ".join(reasoning_parts) if reasoning_parts else f"imb={imbalance:+.2f} bid/ask={bid_vol:.0f}/{ask_vol:.0f} spread={spread_pct:.3%}",
        )

    # ============================================================
    # F7: 动量生命周期 (Momentum Lifecycle)
    # ============================================================

    def _compute_momentum_lifecycle(self, closes: list) -> Optional[float]:
        """
        二阶 EMA 平滑瞬时变化率 (basis points)

        momentum = (EMA(EMA(close, N), N) / prev - 1) × 1000
        """
        n = self.LIFECYCLE_EMA_PERIOD
        min_len = n * 2 + 1
        if len(closes) < min_len:
            return None

        # Layer 1 EMA
        k1 = 2.0 / (n + 1)
        ema1 = closes[0]
        for c in closes[1:]:
            ema1 = c * k1 + ema1 * (1 - k1)

        # Layer 2 EMA
        ema2_values = [ema1]
        k2 = 2.0 / (n + 1)
        for c in closes[1:]:
            ema1_i = c * k1 + ema2_values[-1] * (1 - k1)  # not exact but close enough
            ema2_values.append(ema1_i)

        # Recompute properly
        # First pass: compute ema1 series
        ema1_series = [closes[0]]
        for c in closes[1:]:
            ema1_series.append(c * k1 + ema1_series[-1] * (1 - k1))

        # Second pass: ema of ema1
        ema2_series = [ema1_series[0]]
        for v in ema1_series[1:]:
            ema2_series.append(v * k2 + ema2_series[-1] * (1 - k2))

        if len(ema2_series) < 2:
            return None

        prev = ema2_series[-2]
        curr = ema2_series[-1]
        if prev <= 0:
            return None

        return (curr / prev - 1.0) * 1000.0

    def _classify_lifecycle(self, momentum: float, prev_momentum: float,
                            history: list, vix: float) -> str:
        """
        动量生命周期分类 (VIX 自适应)

        Returns: "accelerating" | "coasting" | "fading" |
                 "declining" | "converging" | "turning_up" |
                 "turning_down" | "idle"
        """
        # 穿零轴 = 拐点
        if prev_momentum <= 0 and momentum > 0:
            return "turning_up"
        if prev_momentum >= 0 and momentum < 0:
            return "turning_down"

        # VIX 自适应: 高波动放宽阈值 (momentum noise 更大)
        vix_noise_factor = 1.0 + max(0, (vix - 20) * 0.03) if vix > 0 else 1.0

        if momentum > 0:
            if momentum > prev_momentum * vix_noise_factor and prev_momentum > 0:
                return "accelerating"
            elif momentum < prev_momentum and prev_momentum > 0:
                return "coasting"
            return "accelerating"

        if momentum < 0:
            if momentum < prev_momentum * vix_noise_factor:
                return "declining"
            else:
                if len(history) >= self.LIFECYCLE_CONV_BARS:
                    recent = history[-self.LIFECYCLE_CONV_BARS:]
                    if all(
                        recent[i] < 0 and (i == 0 or abs(recent[i]) < abs(recent[i - 1]))
                        for i in range(len(recent))
                    ):
                        return "converging"
                return "declining"

        return "idle"

    def _score_momentum_lifecycle(self, ctx: Dict[str, Any], price: float,
                                   vix: float, weight: float) -> FactorScore:
        """
        F7: 动量生命周期因子

        信号映射:
          accelerating   → LONG  (动量增强)
          turning_up     → LONG  (零轴上穿)
          converging     → LONG  (下跌衰竭)
          declining      → SHORT (动量恶化)
          turning_down   → SHORT (零轴下穿)
          coasting/fading → NEUTRAL (衰减中)
        """
        symbol = ctx.get("symbol", "UNKNOWN")
        if symbol not in self._lifecycle_state:
            self._lifecycle_state[symbol] = {
                "closes": [], "momentum": [], "prev_momentum": 0.0,
            }

        state = self._lifecycle_state[symbol]

        # Accumulate close
        state["closes"].append(price)
        if len(state["closes"]) > self.LIFECYCLE_MAX_HISTORY:
            state["closes"] = state["closes"][-self.LIFECYCLE_MAX_HISTORY:]

        momentum = self._compute_momentum_lifecycle(state["closes"])
        if momentum is None:
            return FactorScore(
                name="lifecycle", raw=0.0, confidence=0.0, weight=weight,
                reasoning=f"insufficient data ({len(state['closes'])}/{self.LIFECYCLE_EMA_PERIOD * 2 + 1})",
            )

        prev_momentum = state["prev_momentum"]
        state["prev_momentum"] = momentum

        state["momentum"].append(momentum)
        if len(state["momentum"]) > 50:
            state["momentum"] = state["momentum"][-50:]

        lifecycle = self._classify_lifecycle(momentum, prev_momentum, state["momentum"], vix)

        # Map lifecycle → (raw, base_conf)
        lifecycle_signals = {
            "accelerating":  (75, 0.65),
            "turning_up":    (60, 0.55),
            "converging":    (40, 0.45),
            "declining":     (-70, 0.60),
            "turning_down":  (-55, 0.50),
            "coasting":      (15, 0.30),
            "fading":        (-15, 0.30),
        }
        raw, base_conf = lifecycle_signals.get(lifecycle, (0, 0.0))

        # VIX 自适应: 高波动降低生命周期信号可靠性
        if vix > 25:
            base_conf *= 0.80
        elif vix > 30:
            base_conf *= 0.65

        # 加速确认: 三连增强叠加置信度
        if lifecycle == "accelerating" and len(state["momentum"]) >= self.LIFECYCLE_ACCEL_BARS:
            recent = state["momentum"][-self.LIFECYCLE_ACCEL_BARS:]
            if all(recent[i] > recent[i - 1] for i in range(1, len(recent))):
                base_conf = min(base_conf + 0.15, 0.85)

        return FactorScore(
            name="lifecycle",
            raw=round(raw, 1),
            confidence=round(min(base_conf, 0.85), 2),
            weight=weight,
            reasoning=f"lifecycle={lifecycle} mom={momentum:.1f}bp VIX={vix:.0f}",
        )

    # ============================================================
    # F5: 波动率环境评估 (调节器, 不直接参与方向)
    # ============================================================

    def _assess_regime(self, vix: float, ctx: Dict[str, Any]) -> str:
        """评估波动率环境"""
        spy_ma50 = float(ctx.get("spy_ma50", 0))
        spy_price = float(ctx.get("spy_price", 0))

        # VIX 层级
        if vix < 15:
            vix_level = "LOW_VOLATILITY"
        elif vix < 22:
            vix_level = "NORMAL"
        elif vix < 30:
            vix_level = "HIGH_VOLATILITY"
        else:
            vix_level = "EXTREME_VOLATILITY"

        # 叠加趋势方向形成完整 regime
        if spy_price > 0 and spy_ma50 > 0:
            spy_above = spy_price > spy_ma50
            if "EXTREME" in vix_level:
                return "PANIC" if not spy_above else "EUPHORIA"
            if "HIGH" in vix_level:
                return "HIGH_VOL_BULL" if spy_above else "HIGH_VOL_BEAR"

        return vix_level

    def _detect_vix_jump(self, vix: float) -> Optional[Dict[str, Any]]:
        """
        VIX 跳变检测

        触发条件:
          1. VIX 从前值跳升 > 20% → 恐慌突袭
          2. VIX 从 < 20 跨越至 > 28 → 平静→恐慌的 regime break

        Returns:
            {"type": "spike"/"regime_break", "ratio": float, "prev_vix": float} or None
        """
        if vix <= 0:
            return None

        prev = self._prev_vix
        self._prev_vix = vix

        if prev is None or prev <= 0:
            return None

        ratio = vix / prev

        # Condition 1: spike > 20%
        if ratio > 1.20:
            return {"type": "spike", "ratio": ratio, "prev_vix": prev}

        # Condition 2: regime break (calm < 20 → panic > 28)
        if prev < 20.0 and vix > 28.0:
            return {"type": "regime_break", "ratio": ratio, "prev_vix": prev}

        return None
        """评估波动率环境"""
        spy_ma50 = float(ctx.get("spy_ma50", 0))
        spy_price = float(ctx.get("spy_price", 0))

        # VIX 层级
        if vix < 15:
            vix_level = "LOW_VOLATILITY"
        elif vix < 22:
            vix_level = "NORMAL"
        elif vix < 30:
            vix_level = "HIGH_VOLATILITY"
        else:
            vix_level = "EXTREME_VOLATILITY"

        # 叠加趋势方向形成完整 regime
        if spy_price > 0 and spy_ma50 > 0:
            spy_above = spy_price > spy_ma50
            if "EXTREME" in vix_level:
                return "PANIC" if not spy_above else "EUPHORIA"
            if "HIGH" in vix_level:
                return "HIGH_VOL_BULL" if spy_above else "HIGH_VOL_BEAR"

        return vix_level

    def _regime_label(self, regime: str) -> str:
        labels = {
            "LOW_VOLATILITY": "低波动",
            "NORMAL": "正常",
            "HIGH_VOLATILITY": "高波动",
            "HIGH_VOL_BULL": "高波牛市",
            "HIGH_VOL_BEAR": "高波熊市",
            "EXTREME_VOLATILITY": "极端波动",
            "PANIC": "恐慌",
            "EUPHORIA": "亢奋",
        }
        return labels.get(regime, regime)

    def _get_regime_weights(self, regime: str) -> Dict[str, float]:
        """根据波动率环境获取动态因子权重"""
        weights = dict(self.DEFAULT_WEIGHTS)
        modifiers = self.REGIME_WEIGHT_MODIFIERS.get(regime, {})
        for factor, delta in modifiers.items():
            if factor in weights:
                weights[factor] += delta

        # 归一化
        total = sum(weights.values())
        if total > 0:
            weights = {k: v / total for k, v in weights.items()}

        # 极端波动: 全局降权
        if "EXTREME" in regime or regime in ("PANIC", "EUPHORIA"):
            weights = {k: v * 0.6 for k, v in weights.items()}

        return weights

    # ============================================================
    # 聚合逻辑
    # ============================================================

    def _aggregate_direction(self, factors: Dict[str, FactorScore]) -> Tuple[str, float]:
        """加权聚合方向 + 原始强度"""
        total_contribution = sum(f.contribution for f in factors.values())

        if abs(total_contribution) < 3:  # 噪声阈值
            return "NEUTRAL", 0.0

        direction = "LONG" if total_contribution > 0 else "SHORT"
        raw_strength = min(abs(total_contribution) * 0.8, 45)  # 0-45, 映射到 50±45

        return direction, raw_strength

    def _compute_confidence(self, factors: Dict[str, FactorScore], direction: str) -> float:
        """多因子置信度: 各因子置信度的加权平均 × 方向一致性奖励"""
        active = [f for f in factors.values() if f.confidence > 0]
        if not active:
            return 0.0

        weighted_conf = sum(f.confidence * f.weight for f in active) / max(sum(f.weight for f in active), 0.001)

        # 方向一致性奖励: 多因子同向 = 更高置信度
        same_direction = sum(
            1 for f in active
            if (direction == "LONG" and f.raw > 3) or (direction == "SHORT" and f.raw < -3)
        )
        direction_bonus = min((same_direction - 1) * 0.08, 0.20)  # 2/4→0.08, 4/4→0.20

        return round(min(weighted_conf + direction_bonus, 0.90), 2)

    def _compute_consensus(self, factors: Dict[str, FactorScore], direction: str) -> float:
        """因子间方向一致性: 同向因子数 / 总有效因子数"""
        active = [f for f in factors.values() if abs(f.raw) > 3]
        if not active:
            return 0.0

        aligned = sum(
            1 for f in active
            if (direction == "LONG" and f.raw > 0) or (direction == "SHORT" and f.raw < 0)
        )
        return round(aligned / len(active), 2)

    def _compute_position_and_stop(
        self, confidence: float, atr: float, price: float, vix: float, direction: str
    ) -> Tuple[float, float]:
        """
        F10 修复 (2026-05-12): 风险单位 R 统一 — 仓位由风险预算 / 止损距离决定

        核心原则: 每笔交易的最大可接受亏损 (R) 先确定, 再反推仓位。
        - 先算止损距离 (与仓位无关)
        - 按 confidence 确定风险预算
        - 仓位 = 风险预算 / 止损距离
        - VIX 高时主动降仓

        旧版问题: 仓位和止损独立计算, 每笔交易的实际风险不可控。
        """
        # ── P0-D2 (2026-05-14): ATR=0 或 price=0 → 拒交易 (返回 0,0 由调用方丢弃) ──
        # 旧行为: 用 0.02 兜底, 高波动股 (TSLA 4% ATR) 用 2% 止损被噪声打掉率 70%+
        # 新行为: 数据缺失即 NEUTRAL, 让上游补数据后重试
        if atr <= 0 or price <= 0:
            return 0.0, 0.0

        # ── Step 1: 先确定止损距离 (与仓位无关) ──
        stop_mult = vix_stop_multiplier(vix)
        stop_distance_pct = max(stop_mult * atr / price, 0.02)

        # 恐慌模式额外放宽
        if vix > 35:
            stop_distance_pct *= 1.3

        # ── Step 2: 确定本笔风险预算 (按 confidence 缩放) ──
        # F10 (2026-05-14): 从 yaml 读取, 不再硬编码 — 让 ConfigWriter 可调
        try:
            from app.core.config_container import get_config
            _cfg = get_config()
            base_risk = float(_cfg.get_dotted("risk.per_trade_risk_pct", 0.005))
            max_risk = float(_cfg.get_dotted("risk.max_trade_risk_pct", 0.010))
        except Exception:
            base_risk, max_risk = 0.005, 0.010
        risk_scaler = 0.5 + min(confidence, 0.85) * 1.0  # conf=0.5→1.0R, 0.85→1.35R
        trade_risk = min(base_risk * risk_scaler, max_risk)

        # ── Step 3: 仓位 = 风险预算 / 止损距离 ──
        if stop_distance_pct > 0:
            position_pct = trade_risk / stop_distance_pct
        else:
            position_pct = 0.10

        # ── Step 4: VIX 阻尼 (高波动期主动降仓) ──
        vix_damp = 1.0 if vix < 22 else (0.8 if vix < 30 else 0.5)
        position_pct *= vix_damp

        # ── Step 5: 硬上下限 ──
        position_pct = round(max(0.02, min(position_pct, 0.20)), 3)  # 2%-20%
        stop_distance_pct = round(stop_distance_pct, 3)

        return position_pct, stop_distance_pct

    # ============================================================
    # 工具方法
    # ============================================================

    @staticmethod
    def _clamp_strength(strength: int) -> int:
        """钳位到 10-90, 保持 50±40 输出"""
        return max(10, min(90, strength))

    @staticmethod
    def _empty_signal(symbol: str, reason: str) -> IntradaySignal:
        return IntradaySignal(
            symbol=symbol,
            direction="NEUTRAL",
            strength=50,
            confidence=0.0,
            consensus_level=0.0,
            position_pct=0.0,
            stop_loss_pct=0.0,
            reasoning=reason,
        )


# ============================================================
# 全局单例
# ============================================================

_scorer: Optional[IntradayScorer] = None


def get_intraday_scorer() -> IntradayScorer:
    global _scorer
    if _scorer is None:
        _scorer = IntradayScorer()
    return _scorer
