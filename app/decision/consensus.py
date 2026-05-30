"""
共识决策引擎

多策略投票/打分，生成共识信号

P1 修复 (2026-04-06):
- 从 config_us.py 读取 STRATEGY_WEIGHTS，移除硬编码
- 策略命名统一为 rsi_rebound
"""
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.models import SignalScore, Direction, MarketRegime, TradeEvent
from app.core.events import EventStore
from app.storage.vote_tables import VoteTables
from .vote_logger import VoteLogger
from .signal_scorer import SignalScorer
from .strategy_orthogonalizer import (
    StrategyOrthogonalizer,
    OrthogonalizerConfig,
    DEFAULT_STRATEGY_GROUPS,
    GROUP_PRIOR_CORR,
)

logger = logging.getLogger(__name__)

# Task #6 (2026-05-30) ATR适配优化：置信度保守校准
# 历史数据显示置信度与实际胜率存在反向偏差（过度乐观）
# 乘以 0.85 消除偏差，让下游 min_confidence 门槛反映真实胜率
CONFIDENCE_CALIBRATION_FACTOR = 0.85


class ConsensusEngine:
    """
    共识决策引擎
    
    多策略投票，生成共识信号

    2026-04-28 改造:
    - 接入 StrategyOrthogonalizer, 对同向信号做"有效独立信号数"压缩,
      避免趋势族 4 策略同时多头被当作 4 票独立证据。
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore, db_path: Optional[str] = None):
        self.config = config
        self.event_store = event_store
        self.scorer = SignalScorer(config, event_store)
        
        # P1 修复：从配置读取策略权重，不再硬编码
        # 优先从 config 参数读取，其次从 config_us.py 读取，最后 fallback 到默认值
        default_weights = {
            # 趋势类 (45%)
            "price_break": 0.25,
            "ma_cross": 0.20,
            # 均值回归类 (25%)
            "rsi_rebound": 0.25,
            # 预留扩展 (30%)
            "macd": 0.15,
            "bollinger": 0.10,
            "supertrend": 0.05,
        }
        self.strategy_weights = config.get("STRATEGY_WEIGHTS", default_weights)

        # 共识阈值
        # P0 优化 (2026-05-12): 55% → 45%，增加交易机会
        # 模拟盘阶段优先获取更多交易数据来验证策略
        self.consensus_threshold = 0.45
        self.min_strategies = 2  # P1 修复：最少 2 个策略 (原 3 个)
        
        # 投票日志
        self.db_path = db_path or config.get("DB_PATH", "data/ftseek.db")
        self.vote_tables = VoteTables(self.db_path) if self.db_path else None
        self.vote_logger = VoteLogger(self.vote_tables)

        # 2026-04-28 新增: 策略正交化器
        ortho_cfg = config.get("STRATEGY_ORTHOGONALIZATION", {}) or {}
        self.ortho_enabled = bool(ortho_cfg.get("enabled", True))
        self.orthogonalizer = StrategyOrthogonalizer(
            OrthogonalizerConfig(
                strategy_groups=ortho_cfg.get("strategy_groups") or dict(DEFAULT_STRATEGY_GROUPS),
                group_prior_corr=ortho_cfg.get("group_prior_corr") or dict(GROUP_PRIOR_CORR),
                min_samples=int(ortho_cfg.get("min_samples", 30)),
                window_size=int(ortho_cfg.get("window_size", 500)),
                group_weight_cap=float(ortho_cfg.get("group_weight_cap", 0.45)),
                persist_path=ortho_cfg.get("persist_path"),
            )
        )
        # 最近一次计算的有效权重 (用于日志与归因)
        self._last_effective_weights: Dict[str, float] = {}
        self._last_effective_n: float = 0.0
    
    def generate_consensus(
        self,
        symbol: str,
        signals: List[SignalScore],
        market_context: Dict[str, Any],
    ) -> Optional[SignalScore]:
        """
        生成共识信号
        
        Args:
            symbol: 股票代码
            signals: 各策略生成的信号
            market_context: 市场环境
        
        Returns:
            共识信号或 None
        """
        if not signals:
            return None
        
        # 生成交易 ID
        trade_id = self.vote_logger.generate_trade_id(symbol)
        
        # 1. 记录每个策略的投票
        votes = []
        for signal in signals:
            weight = self.strategy_weights.get(signal.strategy_id, 0.1)
            vote = self.vote_logger.record_agent_vote(
                trade_id=trade_id,
                agent_id=signal.strategy_id,
                signal=signal,
                weight=weight,
                reason=f"Strategy {signal.strategy_id} signal",
            )
            votes.append(vote)
        
        # 2. 分组统计 (多/空)
        long_signals = [s for s in signals if s.direction == Direction.LONG]
        short_signals = [s for s in signals if s.direction == Direction.SHORT]

        # 2.5 在线更新相关性矩阵 (喂入本批全部信号, 供未来 tick 参考)
        if self.ortho_enabled:
            try:
                self.orthogonalizer.update_from_signal_batch(signals)
            except Exception as e:
                logger.warning("[consensus] orthogonalizer update failed: %s", e)
        
        # 3. 计算加权得分 (使用正交化后的有效权重)
        long_score, long_eff = self._calculate_weighted_score(long_signals, market_context)
        short_score, short_eff = self._calculate_weighted_score(short_signals, market_context)
        
        # 4. 判断共识方向
        # 归一化分母: 原始权重之和仍为参考, 但分子使用 effective weights,
        # 因此"共识度"反映的是"有效独立证据的密度", 而非"堆叠策略数量"
        total_weight = sum(self.strategy_weights.values())
        
        long_consensus = long_score / total_weight if total_weight > 0 else 0
        short_consensus = short_score / total_weight if total_weight > 0 else 0

        # 缓存归因字段
        self._last_effective_weights = {**long_eff, **short_eff}
        self._last_effective_n = self.orthogonalizer.effective_n(
            [s.strategy_id for s in signals]
        ) if self.ortho_enabled else float(len(set(s.strategy_id for s in signals)))
        
        # 5. 生成共识信号
        if long_consensus >= self.consensus_threshold and long_consensus > short_consensus:
            final_decision = Direction.LONG
            consensus_signal = self._create_consensus_signal(
                symbol, Direction.LONG, long_signals, market_context
            )
        elif short_consensus >= self.consensus_threshold and short_consensus > long_consensus:
            final_decision = Direction.SHORT
            consensus_signal = self._create_consensus_signal(
                symbol, Direction.SHORT, short_signals, market_context
            )
        else:
            # 无共识
            final_decision = None
            consensus_signal = None
            logger.debug(f"无共识信号：{symbol} long={long_consensus:.2f} short={short_consensus:.2f}")
        
        # 6. 记录决策
        if final_decision:
            decision_confidence = (
                (consensus_signal.strength / 5.0) * consensus_signal.confidence
                if consensus_signal else 0
            )
            self.vote_logger.record_decision(
                trade_id=trade_id,
                symbol=symbol,
                final_decision=final_decision,
                consensus_level=max(long_consensus, short_consensus),
                total_weight=total_weight,
                long_weight=long_score,
                short_weight=short_score,
                votes=votes,
                decision_confidence=decision_confidence,
                market_regime=market_context.get("market_regime", MarketRegime.RANGE),
            )
        
        # 7. 记录共识事件
        if consensus_signal:
            self.event_store.append(
                TradeEvent(
                    event_type="consensus_generated",
                    signal_id=consensus_signal.signal_id,
                    data={
                        "trade_id": trade_id,
                        "symbol": symbol,
                        "direction": consensus_signal.direction.value,
                        "strength": consensus_signal.strength,
                        "confidence": consensus_signal.confidence,
                        "contributing_strategies": len(signals),
                        "long_consensus": long_consensus,
                        "short_consensus": short_consensus,
                        # 正交化归因
                        "effective_n": round(self._last_effective_n, 3),
                        "effective_weights": {
                            k: round(v, 4) for k, v in self._last_effective_weights.items()
                        },
                    },
                )
            )

            logger.info(
                "共识信号：%s %s strength=%s confidence=%s effective_n=%.2f/%d",
                symbol, consensus_signal.direction.value,
                consensus_signal.strength, consensus_signal.confidence,
                self._last_effective_n, len(set(s.strategy_id for s in signals)),
            )

        return consensus_signal
    
    def _calculate_weighted_score(
        self,
        signals: List[SignalScore],
        market_context: Optional[Dict[str, Any]] = None,
    ):
        """
        计算加权得分 (正交化版)

        返回 (total_score, effective_weights_used)

        原公式:
            score = Σ base_weight[i] × strength[i] × confidence[i]
        新公式:
            eff_w = Orthogonalizer.compute_effective_weights(signals, base_weights)
            score = Σ eff_w[i] × strength[i] × confidence[i]

        与旧版行为等价条件:
            ortho_enabled = False 或 所有策略均为独立 (ρ≈0)
        """
        if not signals:
            return 0.0, {}

        if self.ortho_enabled:
            try:
                effective_weights = self.orthogonalizer.compute_effective_weights(
                    signals, self.strategy_weights
                )
            except Exception as e:
                logger.warning("[consensus] ortho compute failed, fallback: %s", e)
                effective_weights = {
                    s.strategy_id: self.strategy_weights.get(s.strategy_id, 0.1)
                    for s in signals
                }
        else:
            effective_weights = {
                s.strategy_id: self.strategy_weights.get(s.strategy_id, 0.1)
                for s in signals
            }

        total_score = 0.0
        for signal in signals:
            w = effective_weights.get(signal.strategy_id, 0.1)
            total_score += w * signal.strength * signal.confidence

        return total_score, effective_weights
    
    def _create_consensus_signal(
        self,
        symbol: str,
        direction: Direction,
        contributing_signals: List[SignalScore],
        market_context: Dict[str, Any],
    ) -> SignalScore:
        """创建共识信号"""
        # 平均强度
        avg_strength = sum(s.strength for s in contributing_signals) / len(contributing_signals)
        
        # 平均置信度
        avg_confidence = sum(s.confidence for s in contributing_signals) / len(contributing_signals)
        
        # 共识信号
        consensus = SignalScore(
            strategy_id="consensus",
            symbol=symbol,
            direction=direction,
            strength=int(avg_strength),
            confidence=avg_confidence,
            expected_horizon=60,  # 默认 1 小时
            market_regime=market_context.get("market_regime", MarketRegime.RANGE),
            risk_reward_estimate=2.0,
        )
        
        # 使用评分器进行最终评分
        consensus = self.scorer.score(consensus, market_context)

        # Task #6 (2026-05-30): 置信度保守校准 — 乘以 0.85 消除过度乐观
        # 所有共识信号统一出口应用，确保下游门槛评估使用校准后置信度
        try:
            calibrated = float(consensus.confidence) * CONFIDENCE_CALIBRATION_FACTOR
            # 限制在 [0, 1]
            consensus.confidence = max(0.0, min(1.0, calibrated))
        except Exception as e:
            logger.warning("[consensus] confidence calibration failed: %s", e)

        return consensus
    
    def get_strategy_contributions(self, signals: List[SignalScore]) -> Dict[str, Dict[str, Any]]:
        """获取各策略贡献度"""
        contributions = {}
        
        for signal in signals:
            strategy_id = signal.strategy_id
            if strategy_id not in contributions:
                contributions[strategy_id] = {
                    "count": 0,
                    "total_strength": 0,
                    "total_confidence": 0,
                    "directions": {"LONG": 0, "SHORT": 0},
                }
            
            contributions[strategy_id]["count"] += 1
            contributions[strategy_id]["total_strength"] += signal.strength
            contributions[strategy_id]["total_confidence"] += signal.confidence
            contributions[strategy_id]["directions"][signal.direction.value] += 1
        
        # 计算平均值
        for strategy_id, stats in contributions.items():
            count = stats["count"]
            stats["avg_strength"] = stats["total_strength"] / count
            stats["avg_confidence"] = stats["total_confidence"] / count
        
        return contributions
