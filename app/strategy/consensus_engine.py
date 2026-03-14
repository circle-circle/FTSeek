"""
信号共识引擎 v2 — 动态加权评分模式 (Weighted Consensus Engine)

核心改进:
  每个策略不再是平等的"一票"，而是带有权重的"加权票"。
  最终根据加权分数是否超过阈值来决定 BUY / SELL。
  权重会根据市场状态 (趋势 vs 震荡) 由 ADX 自动调节。

评分公式:
  score = Σ(strategy_weight × direction)
  direction: +1 (BUY) / -1 (SELL)

  score >= +threshold → BUY
  score <= -threshold → SELL
  |score| < threshold → 不推送

市场自适应:
  ADX > 25 (趋势市) → 趋势策略权重 ×1.5, 震荡策略权重 ×0.6
  ADX < 20 (震荡市) → 震荡策略权重 ×1.5, 趋势策略权重 ×0.6
  20 ≤ ADX ≤ 25      → 使用基准权重

可配置项 (config.py):
  CONSENSUS_WINDOW     = 60     # 共识窗口秒数
  CONSENSUS_THRESHOLD  = 1.5    # 触发阈值 (加权分>=此值才推送)
  CONSENSUS_ADX_TREND  = 25     # ADX 高于此值判定为趋势
  CONSENSUS_ADX_RANGE  = 20     # ADX 低于此值判定为震荡
"""
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Tuple

from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.consensus")


# ============================================================
#  策略分类与基准权重
# ============================================================
# 趋势型策略: 在趋势行情中表现更好
TREND_STRATEGIES = {"supertrend", "macd", "ma_cross"}
# 震荡型策略: 在盘整行情中表现更好
OSCILLATOR_STRATEGIES = {"rsi", "bollinger"}
# 综合型策略: 本身已做多因子融合, 任何行情都有参考价值
HYBRID_STRATEGIES = {"confluence"}
# 其他 (price_break, volume_alert 等)
OTHER_STRATEGIES = set()

# 基准权重 (可通过 config 覆盖)
DEFAULT_BASE_WEIGHTS = {
    "supertrend":  1.0,    # 趋势追踪主力
    "macd":        0.8,    # 趋势 + 动量
    "ma_cross":    0.6,    # 经典趋势
    "rsi":         0.8,    # 震荡反转
    "bollinger":   0.8,    # 震荡/突破
    "confluence":  1.2,    # 已融合多因子, 给较高基准权重
    "price_break": 0.5,    # 简单阈值
    "volume_alert":0.4,    # 辅助确认
}

# 市场状态增益/衰减系数
TREND_BOOST   = 1.5   # 趋势市对趋势策略的增益
TREND_DECAY   = 0.6   # 趋势市对震荡策略的衰减
RANGE_BOOST   = 1.5   # 震荡市对震荡策略的增益
RANGE_DECAY   = 0.6   # 震荡市对趋势策略的衰减


@dataclass
class Vote:
    """一票: 策略 + 方向 + 信号详情"""
    strategy: str
    action: SignalAction
    price: float
    reason: str
    signal: Signal
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConsensusResult:
    """加权共识结果"""
    code: str
    action: SignalAction
    price: float
    score: float               # 最终加权得分
    threshold: float           # 使用的阈值
    market_regime: str         # "trending" / "ranging" / "neutral"
    adx_value: Optional[float]
    agree_strategies: List[str]       # 同向策略
    oppose_strategies: List[str]      # 反向策略
    weight_details: Dict[str, float]  # {策略: 实际权重}
    reasons: List[str]
    votes: List[Vote]


class ConsensusEngine:
    """
    加权共识引擎 v2

    参数:
      window_seconds: 共识窗口 (秒)
      threshold:      触发阈值 (加权总分)
      adx_trend:      ADX 趋势判定线
      adx_range:      ADX 震荡判定线
      base_weights:   {策略名: 基准权重} 字典
      on_consensus:   共识达成回调
    """

    def __init__(self, window_seconds: int = 60, threshold: float = 1.5,
                 adx_trend: float = 25, adx_range: float = 20,
                 base_weights: Optional[Dict[str, float]] = None,
                 on_consensus: Optional[Callable] = None):
        self.window = window_seconds
        self.threshold = threshold
        self.adx_trend = adx_trend
        self.adx_range = adx_range
        self.base_weights = base_weights or DEFAULT_BASE_WEIGHTS.copy()
        self.on_consensus = on_consensus

        # code -> [Vote, ...]
        self._ballots: Dict[str, List[Vote]] = defaultdict(list)
        # code -> Timer
        self._timers: Dict[str, threading.Timer] = {}
        # code -> latest ADX value (由外部注入)
        self._adx_cache: Dict[str, float] = {}
        self._lock = threading.Lock()

        logger.info(
            f"加权共识引擎初始化: 窗口={self.window}s, 阈值={self.threshold}, "
            f"ADX趋势>{self.adx_trend}, ADX震荡<{self.adx_range}"
        )

    # ----------------------------------------------------------
    #  ADX 注入 (由 DataSubscriber 在 K 线回调时更新)
    # ----------------------------------------------------------
    def update_adx(self, code: str, adx_value: float):
        """更新标的的 ADX 值, 用于市场状态判定"""
        self._adx_cache[code] = adx_value

    # ----------------------------------------------------------
    #  权重计算
    # ----------------------------------------------------------
    def _get_market_regime(self, code: str) -> Tuple[str, Optional[float]]:
        """根据 ADX 判定市场状态"""
        adx_val = self._adx_cache.get(code)
        if adx_val is None:
            return "neutral", None
        if adx_val >= self.adx_trend:
            return "trending", adx_val
        if adx_val <= self.adx_range:
            return "ranging", adx_val
        return "neutral", adx_val

    def _calc_weight(self, strategy: str, regime: str) -> float:
        """计算某策略在当前市场状态下的实际权重"""
        base = self.base_weights.get(strategy, 0.5)

        if regime == "trending":
            if strategy in TREND_STRATEGIES:
                return base * TREND_BOOST
            elif strategy in OSCILLATOR_STRATEGIES:
                return base * TREND_DECAY
        elif regime == "ranging":
            if strategy in OSCILLATOR_STRATEGIES:
                return base * RANGE_BOOST
            elif strategy in TREND_STRATEGIES:
                return base * RANGE_DECAY

        # neutral / hybrid / other: 不调整
        return base

    # ----------------------------------------------------------
    #  投票
    # ----------------------------------------------------------
    def submit(self, signal: Signal):
        """提交策略信号到共识窗口"""
        vote = Vote(
            strategy=signal.strategy,
            action=signal.action,
            price=signal.price,
            reason=signal.reason,
            signal=signal,
        )

        with self._lock:
            code = signal.code
            self._ballots[code].append(vote)
            count = len(self._ballots[code])

            logger.info(
                f"[共识] {code} 投票: {signal.strategy} -> "
                f"{signal.action.value} (窗口内 {count} 票)"
            )

            # 第一票开窗
            if code not in self._timers or not self._timers[code].is_alive():
                timer = threading.Timer(self.window, self._evaluate, args=[code])
                timer.daemon = True
                timer.start()
                self._timers[code] = timer
                logger.info(f"[共识] {code} 窗口已开启, {self.window}s 后加权评估")

    # ----------------------------------------------------------
    #  评估
    # ----------------------------------------------------------
    def _evaluate(self, code: str):
        """窗口到期, 加权评分"""
        with self._lock:
            votes = self._ballots.pop(code, [])
            self._timers.pop(code, None)

        if not votes:
            return

        # 去重: 同策略只取最后一票
        latest = {}
        for v in votes:
            latest[v.strategy] = v
        unique_votes = list(latest.values())

        regime, adx_val = self._get_market_regime(code)

        # 加权评分
        score = 0.0
        weight_details = {}
        bullish_votes = []
        bearish_votes = []

        for v in unique_votes:
            w = self._calc_weight(v.strategy, regime)
            weight_details[v.strategy] = round(w, 2)

            if v.action == SignalAction.BUY:
                score += w
                bullish_votes.append(v)
            elif v.action == SignalAction.SELL:
                score -= w
                bearish_votes.append(v)
            # ALERT 不计分

        adx_str = f"ADX={adx_val:.1f}" if adx_val else "ADX=N/A"
        logger.info(
            f"[共识] {code} 评估完毕 | 市场={regime}({adx_str}) | "
            f"加权分={score:+.2f} (阈值=±{self.threshold}) | "
            f"权重={weight_details}"
        )

        result = None

        if score >= self.threshold:
            avg_price = sum(v.price for v in bullish_votes) / len(bullish_votes) if bullish_votes else votes[0].price
            result = ConsensusResult(
                code=code,
                action=SignalAction.BUY,
                price=avg_price,
                score=round(score, 2),
                threshold=self.threshold,
                market_regime=regime,
                adx_value=round(adx_val, 1) if adx_val else None,
                agree_strategies=[v.strategy for v in bullish_votes],
                oppose_strategies=[v.strategy for v in bearish_votes],
                weight_details=weight_details,
                reasons=[v.reason for v in bullish_votes],
                votes=bullish_votes,
            )

        elif score <= -self.threshold:
            avg_price = sum(v.price for v in bearish_votes) / len(bearish_votes) if bearish_votes else votes[0].price
            result = ConsensusResult(
                code=code,
                action=SignalAction.SELL,
                price=avg_price,
                score=round(score, 2),
                threshold=self.threshold,
                market_regime=regime,
                adx_value=round(adx_val, 1) if adx_val else None,
                agree_strategies=[v.strategy for v in bearish_votes],
                oppose_strategies=[v.strategy for v in bullish_votes],
                weight_details=weight_details,
                reasons=[v.reason for v in bearish_votes],
                votes=bearish_votes,
            )

        if result:
            logger.info(
                f"[共识] {code} ✅ 触发 {result.action.value} | "
                f"分数={result.score:+.2f} | 市场={regime} | "
                f"同向: {', '.join(result.agree_strategies)}"
            )
            if self.on_consensus:
                self.on_consensus(result)
        else:
            logger.info(
                f"[共识] {code} ❌ 未触发 (分数={score:+.2f}, "
                f"需 ≥{self.threshold} 或 ≤{-self.threshold}), "
                f"丢弃 {len(votes)} 票"
            )

    # ----------------------------------------------------------
    #  运维接口
    # ----------------------------------------------------------
    def get_pending(self) -> Dict[str, int]:
        """当前各标的待评估票数"""
        with self._lock:
            return {code: len(votes) for code, votes in self._ballots.items()}

    def get_weights_snapshot(self, code: str = None) -> Dict:
        """查看当前权重配置 (含市场调整)"""
        if code:
            regime, adx_val = self._get_market_regime(code)
        else:
            regime, adx_val = "neutral", None

        return {
            "regime": regime,
            "adx": adx_val,
            "threshold": self.threshold,
            "weights": {
                s: {
                    "base": self.base_weights.get(s, 0.5),
                    "adjusted": round(self._calc_weight(s, regime), 2),
                    "type": "趋势" if s in TREND_STRATEGIES
                            else "震荡" if s in OSCILLATOR_STRATEGIES
                            else "综合" if s in HYBRID_STRATEGIES
                            else "其他",
                }
                for s in self.base_weights
            },
        }

    def update_params(self, params: dict):
        """动态更新参数"""
        if "window" in params:
            self.window = int(params["window"])
        if "threshold" in params:
            self.threshold = float(params["threshold"])
        if "adx_trend" in params:
            self.adx_trend = float(params["adx_trend"])
        if "adx_range" in params:
            self.adx_range = float(params["adx_range"])
        # 支持更新单个策略权重: weight_supertrend=1.5
        for k, v in params.items():
            if k.startswith("weight_"):
                strat = k[7:]  # 去掉 "weight_" 前缀
                self.base_weights[strat] = float(v)
                logger.info(f"策略权重更新: {strat} = {v}")

        logger.info(
            f"共识引擎参数更新: 窗口={self.window}s, 阈值={self.threshold}, "
            f"ADX趋势>{self.adx_trend}, ADX震荡<{self.adx_range}"
        )
