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
from app.strategy.regime_detector import MarketRegimeDetector

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

    Task #8 (2026-05-31) 改造:
    - 接入 MarketRegimeDetector, 按市场态切换 REGIME_WEIGHTS,
      趋势市提升 trend 系列, 震荡/低波动提升均值回归系列。
    """

    # ── Task #8: Market Regime 自适应权重表 ──
    # 不同 regime 下的策略权重分布; 未匹配的 regime 退回 self.strategy_weights
    REGIME_WEIGHTS: Dict[str, Dict[str, float]] = {
        "trending_up": {
            "price_break": 0.30,    # 趋势突破权重提升
            "ma_cross": 0.25,       # 均线交叉权重提升
            "rsi_rebound": 0.10,    # 均值回归降权
            "macd": 0.20,
            "bollinger": 0.05,
            "supertrend": 0.10,
        },
        "trending_down": {
            "price_break": 0.30,
            "ma_cross": 0.25,
            "rsi_rebound": 0.10,
            "macd": 0.20,
            "bollinger": 0.05,
            "supertrend": 0.10,
        },
        "range_bound": {
            "price_break": 0.10,    # 趋势降权
            "ma_cross": 0.10,
            "rsi_rebound": 0.35,    # 均值回归大幅提升
            "macd": 0.15,
            "bollinger": 0.25,      # 布林带提升
            "supertrend": 0.05,
        },
        "high_vol": {
            "price_break": 0.20,
            "ma_cross": 0.15,
            "rsi_rebound": 0.15,
            "macd": 0.25,           # MACD在高波动时更可靠
            "bollinger": 0.15,
            "supertrend": 0.10,
        },
        "crisis": {
            "price_break": 0.15,
            "ma_cross": 0.10,
            "rsi_rebound": 0.10,
            "macd": 0.30,
            "bollinger": 0.20,
            "supertrend": 0.15,
        },
        "trending_up_high_vol": {
            "price_break": 0.25,
            "ma_cross": 0.20,
            "rsi_rebound": 0.10,
            "macd": 0.25,
            "bollinger": 0.10,
            "supertrend": 0.10,
        },
        "trending_down_high_vol": {
            "price_break": 0.25,
            "ma_cross": 0.20,
            "rsi_rebound": 0.10,
            "macd": 0.25,
            "bollinger": 0.10,
            "supertrend": 0.10,
        },
        "low_vol": {
            "price_break": 0.15,
            "ma_cross": 0.15,
            "rsi_rebound": 0.30,    # 低波动适合均值回归
            "macd": 0.15,
            "bollinger": 0.20,
            "supertrend": 0.05,
        },
    }

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
        # Task #2 (2026-05-31): 默认阈值 → VIX 自适应, 详见 _resolve_consensus_threshold()
        # 当前胜率 30.3% 表明低阈值让太多弱信号通过，需按市场波动动态调整
        # 默认 fallback: 与 yaml strategy.consensus_threshold 对齐
        self.consensus_threshold = float(
            config.get("CONSENSUS_THRESHOLD")
            or config.get("consensus_threshold")
            or 0.60
        )
        # VIX 自适应阈值映射 (Task #2 2026-05-31)
        # 极端: 放宽 (允许做空把握行情); 平静: 收紧 (要求更高确认)
        self._vix_threshold_map = self._load_vix_threshold_map(config)
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

        # Task #8 (2026-05-31): Market Regime 检测器与可配置权重表
        self.regime_detector = MarketRegimeDetector()
        # 优先从 unified_config 读取 strategy.regime_weights, 缺省时使用类常量
        self.regime_weights_table: Dict[str, Dict[str, float]] = (
            self._load_regime_weights(config) or dict(self.REGIME_WEIGHTS)
        )
        # 当前激活的有效权重（generate_consensus 内部按 regime 切换）
        # 默认与 self.strategy_weights 一致, 保持向后兼容
        self.strategy_weights_active: Dict[str, float] = dict(self.strategy_weights)
        self._last_regime: str = "unknown"

        logger.warning(
            "[ConsensusEngine] ⚠️ 此模块当前未接入信号管线 (generate_consensus 零调用), "
            "VIX/regime 自适应阈值不生效。详见 RFC 提案。"
        )
    
    def generate_consensus(
        self,
        symbol: str,
        signals: List[SignalScore],
        market_context: Dict[str, Any],
    ) -> Optional[SignalScore]:
        """⚠️ DEPRECATED / 未接入 — 当前零调用 (2026-05-31 全链路分析确认)

        信号管线完全绕过此模块, 直接走 VotingCoordinator → _build_signal_from_vote。
        内含的策略加权共识、正交化、regime 权重、VIX 自适应阈值均不参与实际交易决策。

        TODO: 决策是否接入或删除此模块 (RFC 提案)

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
        
        # ── Task #8: 按 regime 切换激活权重表 ──
        # 在投票/打分前确定当前 regime 并选用对应权重, 未知 regime 回退默认。
        try:
            current_regime = self.regime_detector.detect(market_context or {})
        except Exception as e:
            logger.warning("[consensus·regime] detect failed, fallback default: %s", e)
            current_regime = "unknown"
        active_weights = self.regime_weights_table.get(
            current_regime, self.strategy_weights
        )
        # 暴露给 _calculate_weighted_score 与正交化器使用
        self.strategy_weights_active = dict(active_weights)
        self._last_regime = current_regime
        logger.info(
            "[consensus·regime] %s regime=%s using_weights=%s",
            symbol, current_regime, list(active_weights.keys()),
        )

        # 1. 记录每个策略的投票
        votes = []
        for signal in signals:
            weight = self.strategy_weights_active.get(signal.strategy_id, 0.1)
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
        # Task #8: 分母使用激活权重表, 与分子(基于 active_weights 的 effective weights)对齐
        total_weight = sum(self.strategy_weights_active.values())
        
        long_consensus = long_score / total_weight if total_weight > 0 else 0
        short_consensus = short_score / total_weight if total_weight > 0 else 0

        # 缓存归因字段
        self._last_effective_weights = {**long_eff, **short_eff}
        self._last_effective_n = self.orthogonalizer.effective_n(
            [s.strategy_id for s in signals]
        ) if self.ortho_enabled else float(len(set(s.strategy_id for s in signals)))
        
        # 5. 生成共识信号
        # Task #2 (2026-05-31): 使用 VIX 自适应阈值代替固定 self.consensus_threshold
        consensus_threshold = self._resolve_consensus_threshold(market_context)
        if long_consensus >= consensus_threshold and long_consensus > short_consensus:
            final_decision = Direction.LONG
            consensus_signal = self._create_consensus_signal(
                symbol, Direction.LONG, long_signals, market_context
            )
        elif short_consensus >= consensus_threshold and short_consensus > long_consensus:
            final_decision = Direction.SHORT
            consensus_signal = self._create_consensus_signal(
                symbol, Direction.SHORT, short_signals, market_context
            )
        else:
            # 无共识
            final_decision = None
            consensus_signal = None
            logger.debug(
                f"无共识信号：{symbol} long={long_consensus:.2f} short={short_consensus:.2f} "
                f"threshold={consensus_threshold:.2f}"
            )
        
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
                        # Task #8: 记录当前激活的 regime 与权重表
                        "regime": self._last_regime,
                        "regime_weights": {
                            k: round(v, 4) for k, v in self.strategy_weights_active.items()
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

        # Task #8: 改用 strategy_weights_active 作为正交化基础权重, 默认与 strategy_weights 等价
        base_weights = self.strategy_weights_active or self.strategy_weights
        if self.ortho_enabled:
            try:
                effective_weights = self.orthogonalizer.compute_effective_weights(
                    signals, base_weights
                )
            except Exception as e:
                logger.warning("[consensus] ortho compute failed, fallback: %s", e)
                effective_weights = {
                    s.strategy_id: base_weights.get(s.strategy_id, 0.1)
                    for s in signals
                }
        else:
            effective_weights = {
                s.strategy_id: base_weights.get(s.strategy_id, 0.1)
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

    # ── Task #8 (2026-05-31): Market Regime 权重表加载 ──
    @classmethod
    def _load_regime_weights(
        cls, config: Dict[str, Any]
    ) -> Optional[Dict[str, Dict[str, float]]]:
        """从 config / unified_config 加载 regime 权重表, 失败返回 None

        预期 config 结构:
            strategy.regime_weights:
              trending_up: { price_break: 0.30, ... }
              range_bound: { ... }
              ...
        """
        def _coerce(raw: Any) -> Optional[Dict[str, Dict[str, float]]]:
            if not isinstance(raw, dict):
                return None
            result: Dict[str, Dict[str, float]] = {}
            for regime, weights in raw.items():
                if not isinstance(weights, dict):
                    continue
                norm: Dict[str, float] = {}
                for k, v in weights.items():
                    try:
                        norm[str(k)] = float(v)
                    except (TypeError, ValueError):
                        continue
                if norm:
                    result[str(regime)] = norm
            return result or None

        try:
            if isinstance(config, dict):
                raw = (
                    config.get("REGIME_WEIGHTS")
                    or config.get("regime_weights")
                    or (config.get("strategy", {}) or {}).get("regime_weights")
                )
                merged = _coerce(raw)
                if merged:
                    # 与类常量做合并: 用户配置覆盖默认, 缺省项继承类常量
                    out = {k: dict(v) for k, v in cls.REGIME_WEIGHTS.items()}
                    for regime, weights in merged.items():
                        out[regime] = weights
                    return out
        except Exception:
            pass
        try:
            from app.core.config_container import get_config as _gc
            cfg = _gc()
            raw = cfg.get_dotted("strategy.regime_weights", None)
            merged = _coerce(raw)
            if merged:
                out = {k: dict(v) for k, v in cls.REGIME_WEIGHTS.items()}
                for regime, weights in merged.items():
                    out[regime] = weights
                return out
        except Exception:
            pass
        return None

    # ── Task #2 (2026-05-31): VIX 自适应共识阈值 ──
    @staticmethod
    def _load_vix_threshold_map(config: Dict[str, Any]) -> Dict[str, float]:
        """从 config 读取 VIX 阈值映射, 缺省时使用内置默认值

        预期 config 结构 (展平后或嵌套后均可):
            strategy.consensus_threshold_by_vix: {extreme, high, elevated, normal}
        """
        defaults = {"extreme": 0.55, "high": 0.60, "elevated": 0.70, "normal": 0.75}
        try:
            raw = (
                config.get("consensus_threshold_by_vix")
                or config.get("strategy", {}).get("consensus_threshold_by_vix")
                if isinstance(config, dict) else None
            )
            if isinstance(raw, dict):
                merged = dict(defaults)
                for k, v in raw.items():
                    try:
                        merged[k] = float(v)
                    except (TypeError, ValueError):
                        continue
                return merged
        except Exception:
            pass
        # 兜底: 直接尝试 unified_config
        try:
            from app.core.config_container import get_config as _gc
            cfg = _gc()
            raw = cfg.get_dotted("strategy.consensus_threshold_by_vix", None)
            if isinstance(raw, dict):
                merged = dict(defaults)
                for k, v in raw.items():
                    try:
                        merged[k] = float(v)
                    except (TypeError, ValueError):
                        continue
                return merged
        except Exception:
            pass
        return defaults

    def _resolve_consensus_threshold(self, market_context: Dict[str, Any]) -> float:
        """Task #2 (2026-05-31): 按 VIX 选择共识阈值

        - VIX > 40: extreme (0.55) 极端波动放宽 — 允许做空把握行情
        - VIX > 30: high     (0.60)
        - VIX > 20: elevated (0.70)
        - VIX <= 20: normal  (0.75) 平静市场要求更高确认

        VIX 来源优先级: market_context.vix → unified_config.market.vix → fallback 20
        """
        vix = None
        try:
            if isinstance(market_context, dict):
                vix = market_context.get("vix")
                if vix is None:
                    vix = market_context.get("vix_level")
        except Exception:
            vix = None

        if vix is None:
            try:
                from app.core.config_container import get_config as _gc
                cfg = _gc()
                vix = cfg.get_dotted("market.vix", None)
                if vix is None:
                    vix = cfg.get_dotted("runtime.vix", None)
            except Exception:
                vix = None

        try:
            vix_f = float(vix) if vix is not None else 20.0
        except (TypeError, ValueError):
            vix_f = 20.0

        m = self._vix_threshold_map
        if vix_f > 40:
            threshold, regime = m.get("extreme", 0.55), "extreme(VIX>40)"
        elif vix_f > 30:
            threshold, regime = m.get("high", 0.60), "high(VIX>30)"
        elif vix_f > 20:
            threshold, regime = m.get("elevated", 0.70), "elevated(VIX>20)"
        else:
            threshold, regime = m.get("normal", 0.75), "normal(VIX<=20)"

        logger.info(
            "[consensus·vix_adaptive] vix=%.2f regime=%s threshold=%.2f",
            vix_f, regime, threshold,
        )
        return float(threshold)
