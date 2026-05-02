"""
信号质量评分器 (Signal Quality Scorer)

从 consensus_engine.py 提取的信号质量管理和置信度计算逻辑。
包含:
  - 信号质量分数管理 (_quality_score)
  - 信号结果记录 (record_signal_outcome)
  - 置信度公式计算
  - LLM 权重自适应

拆分自 consensus_engine.py (原行 304-310, 953-1045, 1545-1640)
"""
import logging
from collections import deque
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

logger = logging.getLogger("ftseek.strategy.signal_scorer")


class SignalQualityManager:
    """
    信号质量分数管理

    维护每个标的的近期信号质量分数，基于历史成交结果动态调整。
    """

    def __init__(self, maxlen: int = 20):
        self._signal_history: Dict[str, deque] = {}
        self._quality_score: Dict[str, float] = {}
        self._last_signal_action: Dict[str, Tuple[str, float]] = {}
        self._maxlen = maxlen

    def get_quality_score(self, code: str) -> float:
        """获取标的的质量分数，默认 1.0"""
        return self._quality_score.get(code, 1.0)

    def record_outcome(
        self,
        code: str,
        action: str,
        pnl: float,
        market_regime: str = "",
        scenario: str = "",
    ):
        """
        记录信号结果，更新质量分数

        Args:
            code: 股票代码
            action: "BUY" / "SELL"
            pnl: 盈亏百分比
            market_regime: 市场状态
            scenario: 场景标签
        """
        import time

        history = self._signal_history.setdefault(code, deque(maxlen=self._maxlen))
        history.append((action, pnl))

        # 更新质量分数
        if len(history) >= 5:
            recent_pnls = [p for _, p in list(history)]
            win_rate = sum(1 for p in recent_pnls if p > 0) / len(recent_pnls)
            avg_pnl = sum(recent_pnls) / len(recent_pnls)

            # 质量分数 = 胜率 (60%) + 平均盈亏归一化 (40%)
            quality = win_rate * 0.6 + max(0, min(1, (avg_pnl + 0.05) / 0.10)) * 0.4
            self._quality_score[code] = max(0.3, min(1.5, quality))

            logger.debug(
                f"[信号质量] {code} 胜率={win_rate:.0%} 均盈={avg_pnl:+.2%} "
                f"→ 质量分={self._quality_score[code]:.2f}"
            )

        self._last_signal_action[code] = (action, time.time())

    def get_last_action(self, code: str) -> Optional[Tuple[str, float]]:
        """获取最后一次信号的方向和时间戳"""
        return self._last_signal_action.get(code)

    def get_history(self, code: str) -> List[Tuple[str, float]]:
        """获取信号历史记录"""
        return list(self._signal_history.get(code, deque()))


class ConfidenceCalculator:
    """
    置信度计算器

    计算共识信号的最终置信度分数。
    公式: 技术项 (40%) + 情绪项 (30%) + 场景项 (30%) [无LLM时]
    """

    def calculate_buy_confidence(
        self,
        score: float,
        threshold: float,
        bullish_votes: list,
        bearish_votes: list,
        structural_votes: list,
        event_votes: list,
        capital_sentiment: float,
        scenario_confidence: float,
        correlation_discount: float = 1.0,
        llm_enabled: bool = False,
        llm_weight: float = 0.0,
        llm_confidence: float = 0.0,
        llm_direction_alignment: float = 0.0,
    ) -> Tuple[float, Dict[str, float]]:
        """
        计算 BUY 信号的置信度

        Returns:
            (final_confidence, breakdown_dict)
        """
        # 技术项
        score_part = min(1.0, abs(score) / threshold) * 0.50
        unique_votes_all = bullish_votes + bearish_votes
        consensus_part = min(1.0, len(bullish_votes) / max(1, len(unique_votes_all))) * 0.25
        technical_confidence = score_part + consensus_part
        technical_part = min(1.0, technical_confidence / 0.75) * 0.7
        technical_part *= correlation_discount

        # 情绪项
        sentiment_part = max(0, capital_sentiment) * 0.5

        # LLM 项
        llm_part = 0.0
        if llm_enabled and llm_confidence > 0:
            alignment_factor = 1.0 + llm_direction_alignment * 0.3
            alignment_factor = max(0.5, min(1.3, alignment_factor))
            llm_part = llm_confidence * alignment_factor

        # 融合
        if llm_part > 0 and llm_enabled:
            w_llm = llm_weight
            w_remaining = 1.0 - w_llm
            w_tech = 0.4 * w_remaining
            w_sent = 0.3 * w_remaining
            w_scen = 0.3 * w_remaining
            base_confidence = (
                technical_part * w_tech
                + sentiment_part * w_sent
                + scenario_confidence * w_scen
                + llm_part * w_llm
            )
        else:
            base_confidence = (
                technical_part * 0.7
                + sentiment_part * 0.0
                + scenario_confidence * 0.3
            )

        confidence = min(1.0, base_confidence)

        breakdown = {
            "technical": round(technical_part, 3),
            "sentiment": round(sentiment_part, 3),
            "scenario": round(scenario_confidence, 3),
            "llm": round(llm_part, 3),
            "correlation_discount": round(correlation_discount, 3),
        }

        return confidence, breakdown

    def calculate_sell_confidence(
        self,
        score: float,
        threshold: float,
        bullish_votes: list,
        bearish_votes: list,
        capital_sentiment: float,
        scenario_confidence: float,
        correlation_discount: float = 1.0,
        llm_enabled: bool = False,
        llm_weight: float = 0.0,
        llm_confidence: float = 0.0,
        llm_direction_alignment: float = 0.0,
    ) -> Tuple[float, Dict[str, float]]:
        """计算 SELL 信号的置信度 (对称版本)"""
        score_part = min(1.0, abs(score) / threshold) * 0.45
        unique_votes_all = bearish_votes + bullish_votes
        consensus_part = min(1.0, len(bearish_votes) / max(1, len(unique_votes_all))) * 0.25
        technical_confidence = score_part + consensus_part
        technical_part = min(1.0, technical_confidence / 0.7) * 0.7
        technical_part *= correlation_discount

        sentiment_part = max(0, -capital_sentiment) * 0.5

        llm_part = 0.0
        if llm_enabled and llm_confidence > 0:
            alignment_factor = 1.0 + llm_direction_alignment * 0.3
            alignment_factor = max(0.5, min(1.3, alignment_factor))
            llm_part = llm_confidence * alignment_factor

        if llm_part > 0 and llm_enabled:
            w_llm = llm_weight
            w_remaining = 1.0 - w_llm
            w_tech = 0.4 * w_remaining
            w_sent = 0.3 * w_remaining
            w_scen = 0.3 * w_remaining
            base_confidence = (
                technical_part * w_tech
                + sentiment_part * w_sent
                + scenario_confidence * w_scen
                + llm_part * w_llm
            )
        else:
            base_confidence = (
                technical_part * 0.7
                + sentiment_part * 0.0
                + scenario_confidence * 0.3
            )

        confidence = min(1.0, base_confidence)

        breakdown = {
            "technical": round(technical_part, 3),
            "sentiment": round(sentiment_part, 3),
            "scenario": round(scenario_confidence, 3),
            "llm": round(llm_part, 3),
            "correlation_discount": round(correlation_discount, 3),
        }

        return confidence, breakdown
