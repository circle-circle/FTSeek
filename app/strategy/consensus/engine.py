"""
共识引擎核心 — 投票聚合 + 共识评估 + 子模块集成

职责：
1. 接收信号 (submit)
2. 窗口管理 (清理过期信号)
3. 共识评估 (_evaluate) — 使用 WeightCalculator 计算加权分数
4. 触发回调 (on_consensus)
5. 集成 LLM / 自适应权重 / 审计

非职责：
- 权重计算细节 → weights.py (WeightCalculator)
- LLM 集成 → llm_integration.py (LLMIntegration)
- 自适应权重 → adaptive_weights.py (AdaptiveWeightManager)
- 审计日志 → audit_log.py (AuditLogger)
"""

import dataclasses
import time
import logging
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Optional, Dict, List, Any, Callable, Tuple
from collections import defaultdict
from datetime import datetime

from app.core.config_loader import cfg
from app.strategy.consensus.weights import WeightCalculator
from app.strategy.consensus.llm_integration import LLMIntegration
from app.strategy.consensus.adaptive_weights import AdaptiveWeightManager
from app.strategy.consensus.audit_log import AuditLogger

logger = logging.getLogger("ftseek.consensus.engine")


# ===== 数据模型 =====

@dataclass
class Vote:
    """一票：策略 + 方向 + 信号详情"""
    strategy: str
    action: Any  # SignalAction
    price: float
    reason: str
    signal: Any  # Signal
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ConsensusResult:
    """加权共识结果（frozen - 并发安全）"""
    code: str
    action: Any
    price: float
    score: float
    threshold: float
    market_regime: str
    scenario: str
    adx_value: Optional[float]
    agree_strategies: Tuple[str, ...]
    oppose_strategies: Tuple[str, ...]
    weight_details: Dict[str, float]
    reasons: Tuple[str, ...]
    votes: Tuple[Vote, ...]
    confidence: float = 0.0
    trace_id: str = ""
    divergence: float = 0.0
    quality_result: Optional[object] = None
    source: str = "TECHNICAL"
    extra: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not isinstance(self.agree_strategies, tuple):
            object.__setattr__(self, 'agree_strategies', tuple(self.agree_strategies))
        if not isinstance(self.oppose_strategies, tuple):
            object.__setattr__(self, 'oppose_strategies', tuple(self.oppose_strategies))
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, 'reasons', tuple(self.reasons))
        if not isinstance(self.votes, tuple):
            object.__setattr__(self, 'votes', tuple(self.votes))
        if not isinstance(self.weight_details, MappingProxyType):
            object.__setattr__(self, 'weight_details', MappingProxyType(self.weight_details))


# ===== 核心引擎 =====

class ConsensusEngine:
    """
    加权共识引擎 — 核心投票聚合 + 子模块协调

    对外接口完全兼容旧 consensus_engine.ConsensusEngine。
    内部将权重计算、LLM、自适应权重、审计委托给子模块。
    """

    def __init__(self, window_seconds: int = 60, threshold: float = 1.5,
                 adx_trend: float = 25, adx_range: float = 20,
                 on_consensus: Optional[Callable] = None,
                 allow_short: bool = False):
        self.window_seconds = window_seconds
        self.threshold = threshold
        self.adx_trend = adx_trend
        self.adx_range = adx_range
        self.on_consensus = on_consensus
        self.allow_short = allow_short

        # 投票存储: code -> [Vote]
        self._votes: Dict[str, List[Vote]] = defaultdict(list)
        self._lock = threading.Lock()

        # 缓存
        self._adx_cache: Dict[str, float] = {}
        self._regime_cache: Dict[str, str] = {}
        self._scenario_cache: Dict[str, str] = {}
        self._daily_trend_cache: Dict[str, str] = {}
        self._mtf_alignment_cache: Dict[str, Dict] = {}
        self._scenario_phase_cache: Dict[str, Any] = {}
        self._scenario_tags_cache: Dict[str, List] = {}
        self._quality_score: Dict[str, float] = {}
        self._signal_history: Dict[str, List] = defaultdict(list)

        # 已触发记录 (防重复): code -> timestamp
        self._triggered: Dict[str, float] = {}
        self._trigger_cooldown = 60

        # ── 子模块 ──────────────────────────────────────────────
        self.weight_calc = WeightCalculator()
        self.llm = LLMIntegration(engine_ref=self)
        self.adaptive_weights = AdaptiveWeightManager(engine_ref=self)
        self.audit = AuditLogger()

        # 场景路由器 (由外部注入)
        self._scenario_router = None

        # LLM 兼容属性
        self._llm_analyst = None
        self._llm_consensus_weight = 0.15
        self._llm_weight_config: Dict[str, Any] = {}
        self._llm_consensus_enabled = False
        self._llm_audit_log: Dict[str, Any] = {}

    # ===== 信号提交 =====

    def submit(self, signal):
        """提交信号到共识窗口
        
        P0 修复 (2026-04-30): 按策略去重 — 同一策略在窗口内只保留最新投票。
        修复前: confluence_v2 每根K线都产生 Signal → 60+ 个同策略投票累积 →
                分数虚高 + agree_strategies 全是重复的 "confluence_v2"
        修复后: 同策略新投票覆盖旧投票 → 每个策略窗口内最多 1 票
        """
        if signal is None:
            return

        code = signal.code
        now = time.time()

        with self._lock:
            vote = Vote(
                strategy=signal.strategy,
                action=signal.action,
                price=signal.price,
                reason=signal.reason,
                signal=signal,
                timestamp=now,
            )
            # P0 修复: 按策略去重 — 移除旧的同策略投票，只保留最新
            self._votes[code] = [
                v for v in self._votes[code]
                if v.strategy != signal.strategy
            ]
            self._votes[code].append(vote)
            logger.debug(f"[共识] {code} 收到投票: {signal.strategy} -> {signal.action.value}")

        self._evaluate(code)

    # ===== 共识评估 =====

    def _evaluate(self, code: str):
        """评估某只股票的共识状态"""
        now = time.time()

        with self._lock:
            # 清理过期投票
            self._votes[code] = [
                v for v in self._votes[code]
                if now - v.timestamp <= self.window_seconds
            ]

            votes = self._votes[code]
            if not votes:
                return

            # 冷却检查
            last_trigger = self._triggered.get(code, 0)
            if now - last_trigger < self._trigger_cooldown:
                return

            # 分离多空投票
            buy_votes = [v for v in votes if v.action.value == "BUY"]
            sell_votes = [v for v in votes if v.action.value == "SELL"]

            # 防御：如果某一方向为空，直接使用另一方
            if not buy_votes and not sell_votes:
                return

            regime = self._regime_cache.get(code, "neutral")
            buy_score: float = 0.0
            sell_score: float = 0.0

            if not buy_votes:
                action = sell_votes[0].action
                agree_votes = sell_votes
                oppose_votes = buy_votes
                score, weight_details = self._calc_weighted_score(sell_votes, regime, code)
                sell_score = score
            elif not sell_votes:
                action = buy_votes[0].action
                agree_votes = buy_votes
                oppose_votes = sell_votes
                score, weight_details = self._calc_weighted_score(buy_votes, regime, code)
                buy_score = score
            else:
                # 计算加权得分 (委托给 WeightCalculator)
                buy_score, buy_details = self._calc_weighted_score(buy_votes, regime, code)
                sell_score, sell_details = self._calc_weighted_score(sell_votes, regime, code)

                # 确定主导方向
                if buy_score >= sell_score:
                    action = buy_votes[0].action
                    agree_votes = buy_votes
                    oppose_votes = sell_votes
                    score = buy_score
                    weight_details = buy_details
                else:
                    action = sell_votes[0].action
                    agree_votes = sell_votes
                    oppose_votes = buy_votes
                    score = sell_score
                    weight_details = sell_details

            # 阈值检查
            if score < self.threshold:
                vote_names = [v.strategy for v in votes]
                logger.info(
                    f"[共识评估] {code} 未达阈值 | "
                    f"方向={action.value if hasattr(action, 'value') else str(action)} | "
                    f"得分={score:.2f} < 阈值={self.threshold:.1f} | "
                    f"投票: {vote_names} | 权重: {weight_details}"
                )
                return

            # 最少策略数检查 (从 config 读取 min_strategies)
            unique_strategies = set(v.strategy for v in agree_votes)
            min_strategies = cfg.get("strategy.consensus.min_strategies", 1)
            if len(unique_strategies) < min_strategies:
                logger.info(
                    f"[共识评估] {code} 策略数不足 | "
                    f"同向策略={len(unique_strategies)} < 最少={min_strategies} | "
                    f"策略: {list(unique_strategies)} | 得分={score:.2f}"
                )
                return

            # 共识达成日志
            logger.info(
                f"[共识达成] {code} {action.value if hasattr(action, 'value') else str(action)} | "
                f"得分={score:.2f} >= 阈值={self.threshold:.1f} | "
                f"同向策略={list(unique_strategies)}({len(unique_strategies)}个) | "
                f"反对策略={[v.strategy for v in oppose_votes]} | "
                f"价格={votes[0].price} | 置信度={min(score / (self.threshold * 2), 1.0):.2f}"
            )

            # 构建共识结果
            result = ConsensusResult(
                code=code,
                action=action,
                price=votes[0].price,
                score=score,
                threshold=self.threshold,
                market_regime=regime,
                scenario=self._scenario_cache.get(code, "unknown"),
                adx_value=self._adx_cache.get(code),
                agree_strategies=tuple(v.strategy for v in agree_votes),
                oppose_strategies=tuple(v.strategy for v in oppose_votes),
                weight_details=weight_details,
                reasons=tuple(v.reason for v in agree_votes),
                votes=tuple(votes),
                confidence=min(score / (self.threshold * 2), 1.0),
                divergence=self._calc_divergence(buy_score, sell_score),
            )

            # 市场状态转换置信度调整 (2026-04-30)
            # 突破型转换 → 信号置信度 +15%, 衰减型转换 → -10%
            trans = self._detect_regime_transition(code)
            if trans:
                base_conf = result.confidence
                if trans.is_breakout:
                    result = dataclasses.replace(
                        result,
                        confidence=min(1.0, base_conf * 1.15),
                        extra={'transition': trans.to_dict()})
                elif trans.is_fade:
                    result = dataclasses.replace(
                        result,
                        confidence=max(0.0, base_conf * 0.9),
                        extra={'transition': trans.to_dict()})

            # 记录触发
            self._triggered[code] = now

            # 清理已触发投票
            self._votes[code] = []

            # 审计记录
            try:
                self.audit.record_signal(
                    trace_id=result.trace_id or f"{code}_{int(now)}",
                    code=code,
                    strategy="consensus",
                    action=action.value if hasattr(action, 'value') else str(action),
                    price=votes[0].price,
                    confidence=result.confidence,
                    consensus_score=result.score,
                    market_regime=regime,
                    adx=self._adx_cache.get(code),
                    agree_strategies=list(result.agree_strategies),
                )
            except Exception as e:
                logger.debug(f"[审计] 记录信号失败: {e}")

        # 触发回调 (在锁外)
        if self.on_consensus:
            try:
                self.on_consensus(result)
            except Exception as e:
                logger.error(f"[共识] 回调异常: {e}")

    def _calc_weighted_score(self, votes: List[Vote], regime: str, code: str) -> Tuple[float, Dict]:
        """计算投票加权得分 (委托给 WeightCalculator)"""
        score = 0.0
        details = {}
        for vote in votes:
            w, w_details = self.weight_calc.calc_weight(vote.strategy, regime, code)
            score += w
            details[vote.strategy] = w
        return score, details

    def _calc_divergence(self, buy_score: float, sell_score: float) -> float:
        """计算分歧度 (0=一致, 1=完全分裂)"""
        total = buy_score + sell_score
        if total == 0:
            return 0.0
        return 2 * min(buy_score, sell_score) / total

    def _detect_regime_transition(self, code: str) -> Optional[Any]:
        """
        检测市场状态转换，用于共识置信度调整。

        委托给 MarketRegimeDetector.detect_transition()，
        灰化失败降级返回 None (不影响共识结果)。
        """
        try:
            from app.strategy.market_regime import market_regime_detector
            return market_regime_detector.detect_transition(code)
        except Exception as e:
            logger.debug(f"[状态转换] {code} 检测失败: {e}")
            return None

    # ===== 状态更新 (兼容旧接口) =====

    def update_adx(self, code: str, adx_value: float):
        """更新 ADX 值"""
        self._adx_cache[code] = adx_value
        # 根据 ADX 更新市场状态
        if adx_value >= self.adx_trend:
            self._regime_cache[code] = "trending"
        elif adx_value <= self.adx_range:
            self._regime_cache[code] = "ranging"
        else:
            self._regime_cache[code] = "neutral"

    def update_daily_trend(self, code: str, trend: str):
        """注入日线趋势方向（多周期对齐）"""
        self._daily_trend_cache[code] = trend
        logger.debug(f"[多周期] {code} 日线趋势更新 → {trend}")

    def update_scenario(self, code: str, scenario, phase=None, tags=None):
        """注入场景分类结果"""
        self._scenario_cache[code] = scenario
        if phase is not None:
            self._scenario_phase_cache[code] = phase
        if tags is not None:
            self._scenario_tags_cache[code] = tags
        logger.debug(f"[场景] {code} 场景更新 → {scenario}")

    def set_scenario_router(self, router):
        """设置场景路由器"""
        self._scenario_router = router
        logger.info("[场景路由] 场景路由器已设置")

    def update_mtf_alignment(self, code: str, buy_score: float, sell_score: float):
        """注入多周期对齐度分数"""
        self._mtf_alignment_cache[code] = {
            "buy": round(max(0.0, min(1.0, buy_score)), 3),
            "sell": round(max(0.0, min(1.0, sell_score)), 3),
        }
        logger.debug(f"[MTF缓存] {code} buy={buy_score:.3f} sell={sell_score:.3f}")

    # ===== LLM 集成 (兼容旧接口) =====

    def set_llm_analyst(self, llm_analyst, weight: float = 0.15, adaptive_config: Dict = None):
        """注入 LLM 市场分析师 (兼容旧 init_llm_integration)"""
        self.llm.inject_llm(llm_analyst, weight, adaptive_config)
        # 同步兼容属性
        self._llm_analyst = llm_analyst
        self._llm_consensus_weight = self.llm.llm_weight
        self._llm_consensus_enabled = self.llm.has_llm

    def init_llm_integration(self, llm_analyst, weight: float = 0.15, adaptive_config: Dict = None):
        """旧方法名兼容"""
        self.set_llm_analyst(llm_analyst, weight, adaptive_config)

    def get_llm_audit_log(self, code: str = None, limit: int = 50) -> List[Dict]:
        """获取 LLM 审计日志"""
        return self.llm.get_audit_log(code, limit)

    def get_llm_accuracy_stats(self, scenario: str = None, limit: int = 100) -> Dict[str, Any]:
        """获取 LLM 准确率统计"""
        return self.llm.get_accuracy_stats(scenario, limit)

    # ===== 信号结果记录 =====

    def record_signal_outcome(self, code: str, action: str, pnl: float,
                              entry_price: float = 0.0, exit_price: float = 0.0,
                              strategy: str = "unknown", market_regime: str = "neutral",
                              scenario: str = None, adx_value: float = 0.0,
                              mtf_alignment: float = 0.0, sentiment_score: float = 0.0,
                              consensus_score: float = 0.0, max_drawdown: float = 0.0,
                              max_profit: float = 0.0, holding_periods: int = 0,
                              exit_reason: str = None):
        """记录信号成交结果，更新信号质量分"""
        action_str = action.value if hasattr(action, 'value') else str(action)

        # 记录到历史
        self._signal_history[code].append((action_str, pnl))
        records = list(self._signal_history[code])
        wins = [r[1] for r in records if r[1] > 0]
        losses = [abs(r[1]) for r in records if r[1] < 0]

        if len(records) < 3:
            logger.info(f"[信号质量] {code} 样本不足({len(records)}笔)，暂不调整质量分")
            return

        win_rate = len(wins) / len(records)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 1.0
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 2.0

        raw_quality = win_rate * profit_factor
        quality = max(0.3, min(1.5, raw_quality))
        self._quality_score[code] = quality

        logger.info(
            f"[信号质量] {code} 胜率={win_rate:.0%} 盈亏比={profit_factor:.2f} "
            f"质量分={quality:.2f} (样本={len(records)}笔) | "
            f"市场状态={market_regime} ADX={adx_value:.1f} MTF={mtf_alignment:.2f}"
        )

        # 记录到审计
        try:
            self.audit.record_execution(
                trace_id=f"{code}_outcome",
                code=code,
                executed=True,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                exit_reason=exit_reason or "",
                holding_hours=holding_periods / 60.0 if holding_periods else 0,
            )
        except Exception as e:
            logger.debug(f"[审计] 记录执行结果失败: {e}")

        # LLM 结果记录
        if self._llm_consensus_enabled:
            self.llm.record_outcome(code, action_str, pnl, holding_periods / 60.0 if holding_periods else 0)

        # 自适应权重记录
        try:
            self.adaptive_weights.record_trade(strategy, code, action_str, pnl)
        except Exception as e:
            logger.debug(f"[自适应权重] 记录交易失败: {e}")

    # ===== 查询 & 管理 =====

    def get_pending(self) -> Dict[str, int]:
        """获取待评估的信号数量"""
        with self._lock:
            return {code: len(votes) for code, votes in self._votes.items() if votes}

    def get_votes(self, code: str) -> List[Vote]:
        """获取某只股票的当前投票"""
        with self._lock:
            now = time.time()
            self._votes[code] = [
                v for v in self._votes[code]
                if now - v.timestamp <= self.window_seconds
            ]
            return list(self._votes[code])

    def clear(self, code: Optional[str] = None):
        """清理投票"""
        with self._lock:
            if code:
                self._votes.pop(code, None)
            else:
                self._votes.clear()

    def get_weights_snapshot(self, code: str = None) -> Dict:
        """获取权重快照"""
        return {
            "base_weights": self.weight_calc.get_base_weights(),
            "llm_weight": self.llm.llm_weight,
            "adaptive_stats": self.adaptive_weights.get_strategy_stats(),
        }

    def update_params(self, params: dict):
        """更新引擎参数"""
        if "threshold" in params:
            self.threshold = params["threshold"]
        if "window_seconds" in params:
            self.window_seconds = params["window_seconds"]
        if "weights_updated" in params:
            logger.info("[共识] 自适应权重已更新，将在下次评估时生效")

    @property
    def scenario_cache(self):
        return self._scenario_cache
