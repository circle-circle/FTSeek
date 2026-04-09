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
import dataclasses
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Tuple, Any
from datetime import datetime, timedelta

from app.strategy.base import Signal, SignalAction

logger = logging.getLogger("ftseek.consensus")
logger.propagate = False  # 防止日志重复


# ============================================================
#  信号质量记录结构（增强版）
# ============================================================
@dataclass
class SignalOutcome:
    """信号成交结果记录（增强版，支持回溯分析）"""
    code: str
    action: str                    # 'BUY' / 'SELL'
    strategy: str                  # 触发策略
    entry_price: float
    exit_price: float
    pnl_pct: float                 # 盈亏百分比
    holding_periods: int           # 持仓周期数
    market_regime: str             # 市场状态 (trending/ranging/neutral)
    scenario: str                  # 场景标签 (Phase 1 新增)
    adx_value: float               # ADX值
    mtf_alignment: float           # 多周期对齐度 (0-1)
    sentiment_score: float         # 情绪得分
    consensus_score: float         # 共识得分
    quality_score: float           # 当时的质量分
    max_drawdown: float            # 持仓期间最大回撤
    max_profit: float              # 持仓期间最大浮盈
    timestamp: float = 0.0         # 平仓时间戳


# ============================================================
#  策略分类与基准权重
# ============================================================
# 趋势型策略: 在趋势行情中表现更好
TREND_STRATEGIES = {"supertrend", "macd", "ma_cross"}
# 震荡型策略: 在盘整行情中表现更好
OSCILLATOR_STRATEGIES = {"rsi", "bollinger"}
# 综合型策略: 本身已做多因子融合, 任何行情都有参考价値
HYBRID_STRATEGIES = {"confluence", "fusion_v2", "fusion_v3", "enhanced_confluence"}
# 情绪型策略: 基于新闻和情绪分析 (新增 2026-03-17)
SENTIMENT_STRATEGIES = {"sentiment", "sentiment_ai", "news_sentiment", "event_driven"}
# 多周期信号策略: 基于 15M/30M/60M/日线 K 线产生的中长周期信号 (新增 2026-03-25)
MTF_STRATEGIES = {"mtf_15m", "mtf_30m", "mtf_60m", "mtf_day"}
# 套利型策略：跨市场套利 (新增 2026-04-09)
ARBITRAGE_STRATEGIES = {"cross_market_arbitrage"}
# 时段型策略：基于特定时段的市场效应 (新增 2026-04-09)
TIMEBASED_STRATEGIES = {"open_close_effect"}
# 其他 (price_break, volume_alert 等)
OTHER_STRATEGIES = set()

# 基准权重 (可通过 config 覆盖)
# 优化于 2026-03-27: 精简策略组合，只保留核心3个策略
# 核心策略: confluence_v2(主力), supertrend(趋势), sentiment_ai(情绪)
DEFAULT_BASE_WEIGHTS = {
    "confluence_v2": 1.5,            # 主力融合策略，权重最高
    "supertrend":    1.0,            # 趋势跟踪主力
    "sentiment_ai":  0.5,            # AI 情绪辅助确认
    # 新增策略 (2026-04-09)
    "event_driven":           1.2,   # 事件驱动：高优先级突发信号
    "cross_market_arbitrage": 1.0,   # 跨市场套利：中优先级
    "enhanced_confluence":    1.3,   # 增强融合：多因子 + 多周期
    "open_close_effect":      0.8,   # 开盘/收盘效应：辅助信号
}

# 市场状态增益/衰减系数
TREND_BOOST   = 1.5   # 趋势市对趋势策略的增益
TREND_DECAY   = 0.6   # 趋势市对震荡策略的衰减
RANGE_BOOST   = 1.5   # 震荡市对震荡策略的增益
RANGE_DECAY   = 0.6   # 震荡市对趋势策略的衰减
# 多周期策略在各市场下的增益（市场趋势夹市时 MTF 信号更可靠）
MTF_TREND_BOOST  = 1.3  # 趋势市对 MTF 策略的增益
MTF_RANGE_DECAY  = 0.7  # 震荡市 MTF 策略的衰减加强（从 0.8 降至 0.7）

# 突发新闻权重倍增器 (2026-04-08 新增)
BREAKING_NEWS_MULTIPLIER = 2.0  # 突发新闻权重翻倍
BREAKING_NEWS_LOOKBACK_HOURS = 2  # 回溯 2 小时内的突发新闻


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
    scenario: str                  # 场景标签 (Phase 1 新增)
    adx_value: Optional[float]
    agree_strategies: List[str]       # 同向策略
    oppose_strategies: List[str]      # 反向策略
    weight_details: Dict[str, float]  # {策略: 实际权重}
    reasons: List[str]
    votes: List[Vote]
    confidence: float = 0.0    # 置信度 (0-1, 用于风控检查)
    trace_id: str = ""         # 【新增 2026-04-02】决策链路追踪ID


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
                 on_consensus: Optional[Callable] = None,
                 min_strategies: int = 1,
                 allow_short: bool = False):  # 2026-04-08 新增：默认禁止做空
        self.window = window_seconds
        self.threshold = threshold
        self.adx_trend = adx_trend
        self.adx_range = adx_range
        self.base_weights = base_weights or DEFAULT_BASE_WEIGHTS.copy()
        self.on_consensus = on_consensus
        self.min_strategies = min_strategies  # V2: 最少需要的策略数量
        self.allow_short = allow_short  # 是否允许做空

        # code -> [Vote, ...]
        self._ballots: Dict[str, List[Vote]] = defaultdict(list)
        # code -> Timer
        self._timers: Dict[str, threading.Timer] = {}
        # code -> latest ADX value (由外部注入)
        self._adx_cache: Dict[str, float] = {}
        # code -> daily trend: "up" / "down" / "neutral" (多周期对齐，Task1 新增)
        self._daily_trend_cache: Dict[str, str] = {}
        # code -> {"buy": float, "sell": float} MTF对齐度缓存 (2026-03-25 新增)
        # 由 DataSubscriber 在 _get_multi_timeframe_signal 调用后注入
        self._mtf_alignment_cache: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

        # ---- 信号质量管理（Task1 新增）----
        # code -> deque of (action_str, pnl) 最近20条成交记录
        self._signal_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        # code -> float [0.3, 1.5]  质量分，初始1.0
        self._quality_score: Dict[str, float] = {}
        # code -> (action_str, timestamp)  上一次触发信号的方向+时间
        self._last_signal_action: Dict[str, Tuple[str, float]] = {}

        # Phase 1+3: 共识增强器 (延迟初始化)
        self._enhancer = None

        # 2026-04-08 新增：突发新闻检测器（延迟初始化）
        self._news_analyzer = None

        # 2026-04-08 新增：突发新闻缓存 {code: (has_breaking, timestamp)}
        self._breaking_news_cache: Dict[str, Tuple[bool, float]] = {}
        # 突发新闻缓存 TTL（秒）
        self._breaking_news_cache_ttl = 300  # 5 分钟

        # Phase 1: 场景路由器集成（2026-04-09）
        # 延迟初始化，由 DataSubscriber 注入
        self._scenario_router = None
        # code -> ScenarioType 场景缓存（由 DataSubscriber 注入）
        self._scenario_cache: Dict[str, Any] = {}

        logger.info(
            f"加权共识引擎初始化: 窗口={self.window}s, 阈值={self.threshold}, "
            f"最少策略={self.min_strategies}, ADX趋势>{self.adx_trend}, ADX震荡<{self.adx_range}, "
            f"允许做空={self.allow_short}"
        )

    # ----------------------------------------------------------
    #  ADX 注入 (由 DataSubscriber 在 K 线回调时更新)
    # ----------------------------------------------------------
    def update_adx(self, code: str, adx_value: float):
        """更新标的的 ADX 值, 用于市场状态判定"""
        self._adx_cache[code] = adx_value

    def update_daily_trend(self, code: str, trend: str):
        """
        注入日线趋势方向（多周期对齐，Task1 新增）

        Args:
            code:  股票代码
            trend: "up" | "down" | "neutral"
        """
        self._daily_trend_cache[code] = trend
        logger.debug(f"[多周期] {code} 日线趋势更新 → {trend}")

    def update_scenario(self, code: str, scenario):
        """
        注入场景分类结果（Phase 1 新增）

        Args:
            code:      股票代码
            scenario:  ScenarioType 场景类型
        """
        self._scenario_cache[code] = scenario
        logger.debug(f"[场景] {code} 场景更新 → {scenario}")

    def set_scenario_router(self, router):
        """
        设置场景路由器（Phase 1 新增）

        Args:
            router: ScenarioRouter 实例
        """
        self._scenario_router = router
        logger.info("[场景路由] 场景路由器已设置")

    def update_mtf_alignment(self, code: str, buy_score: float, sell_score: float):
        """
        注入多周期对齐度分数，由 DataSubscriber 在计算 MTF 信号时同步调用（2026-03-25 新增）

        Args:
            code:       股票代码
            buy_score:  做多方向 MTF 对齐度 (0.0 ~ 1.0)
            sell_score: 做空方向 MTF 对齐度 (0.0 ~ 1.0)
        """
        self._mtf_alignment_cache[code] = {
            "buy":  round(max(0.0, min(1.0, buy_score)), 3),
            "sell": round(max(0.0, min(1.0, sell_score)), 3),
        }
        logger.debug(f"[MTF缓存] {code} buy={buy_score:.3f} sell={sell_score:.3f}")

    def record_signal_outcome(
        self,
        code: str,
        action: str,
        pnl: float,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        strategy: str = "unknown",
        market_regime: str = "neutral",
        scenario: str = None,  # 场景标签 (Phase 1 新增)
        adx_value: float = 0.0,
        mtf_alignment: float = 0.0,
        sentiment_score: float = 0.0,
        consensus_score: float = 0.0,
        max_drawdown: float = 0.0,
        max_profit: float = 0.0,
        holding_periods: int = 0,
    ):
        """
        记录信号成交结果，更新信号质量分（增强版）。
        由 data_subscriber 在平仓后调用。

        quality = clamp(win_rate × profit_factor, 0.3, 1.5)
        - win_rate:      近20笔中盈利笔数占比
        - profit_factor: avg_win / avg_loss (均值之比)
        
        新增：记录完整上下文，支持回溯分析"什么条件下某策略表现好"
        """
        action_str = action.value if hasattr(action, 'value') else str(action)
        
        # 创建增强版信号结果记录
        outcome = SignalOutcome(
            code=code,
            action=action_str,
            strategy=strategy,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=pnl,
            holding_periods=holding_periods,
            market_regime=market_regime,
            adx_value=adx_value,
            mtf_alignment=mtf_alignment,
            sentiment_score=sentiment_score,
            consensus_score=consensus_score,
            quality_score=self._quality_score.get(code, 1.0),
            max_drawdown=max_drawdown,
            max_profit=max_profit,
            timestamp=time.time(),
        )
        
        # P1-1: 持久化到 SQLite (异步写入，不阻塞交易流程)
        try:
            from app.analytics.signal_analytics import get_analytics
            analytics = get_analytics()
            analytics.record_outcome(outcome)
            logger.debug(f"[信号质量] {code} 已持久化到数据库")
        except Exception as e:
            logger.error(f"[信号质量] 持久化失败：{e}")

        # 兼容旧版：仍保留简单记录
        self._signal_history[code].append((action_str, pnl))

        records = list(self._signal_history[code])
        wins = [r[1] for r in records if r[1] > 0]
        losses = [abs(r[1]) for r in records if r[1] < 0]

        if len(records) < 3:
            # 样本不足，暂不调整
            logger.info(
                f"[信号质量] {code} 样本不足({len(records)}笔)，暂不调整质量分"
            )
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

    def _calc_weight(self, strategy: str, regime: str, code: str = None) -> float:
        """计算某策略在当前市场状态下的实际权重（支持突发新闻权重倍增 + 场景路由）"""
        base = self.base_weights.get(strategy, 0.5)

        # ============================================================
        # Phase 1: 场景路由权重调整（2026-04-09）
        # 根据场景分类结果，动态调整策略权重
        # ============================================================
        scenario_weight = 1.0
        if code and hasattr(self, '_scenario_router') and self._scenario_router is not None:
            try:
                # 获取当前场景（由 DataSubscriber 注入）
                current_scenario = self._scenario_cache.get(code)
                if current_scenario:
                    # 从路由器获取策略在该场景下的权重
                    route_result = self._scenario_router.route(current_scenario)
                    scenario_weight = route_result.active_strategies.get(strategy, 1.0)
                    logger.debug(
                        f"[场景权重] {code} {current_scenario.value} {strategy} "
                        f"权重={scenario_weight:.2f}"
                    )
            except Exception as _e:
                logger.debug(f"[场景权重] {code} {strategy} 计算异常：{_e}")

        # ============================================================
        # 2026-04-08 新增：突发新闻权重倍增
        # 情绪型策略在有突发新闻时权重翻倍，支持快速突发事件获利
        # ============================================================
        news_multiplier = 1.0
        if strategy in SENTIMENT_STRATEGIES and code:
            has_breaking = self._check_breaking_news(code)
            if has_breaking:
                news_multiplier = BREAKING_NEWS_MULTIPLIER
                logger.info(
                    f"[突发新闻权重] {code} {strategy} 权重倍增 ×{BREAKING_NEWS_MULTIPLIER:.1f}"
                )

        # 计算最终权重 = 基准权重 × 市场状态调整 × 场景权重 × 新闻倍增
        market_adjustment = 1.0
        if regime == "trending":
            if strategy in TREND_STRATEGIES:
                market_adjustment = TREND_BOOST
            elif strategy in OSCILLATOR_STRATEGIES:
                market_adjustment = TREND_DECAY
            elif strategy in MTF_STRATEGIES:
                market_adjustment = MTF_TREND_BOOST
        elif regime == "ranging":
            if strategy in OSCILLATOR_STRATEGIES:
                market_adjustment = RANGE_BOOST
            elif strategy in TREND_STRATEGIES:
                market_adjustment = RANGE_DECAY
            elif strategy in MTF_STRATEGIES:
                market_adjustment = MTF_RANGE_DECAY

        # 最终权重 = 基准 × 市场调整 × 场景权重 × 新闻倍增
        final_weight = base * market_adjustment * scenario_weight * news_multiplier
        return final_weight
    
    def _get_capital_flow_sentiment(self, code: str) -> float:
        """
        获取资金流向情绪 (富途 OpenD)
        
        Returns:
            float: 情绪得分 (-1 到 1)
                   1 = 强烈流入, -1 = 强烈流出, 0 = 中性
        """
        try:
            from app.news_collector import FutuMarketData
            futu = FutuMarketData()
            flow = futu.get_capital_flow(code)
            sentiment = flow.get('sentiment', 0)
            logger.debug(f"[{code}] 资金流向情绪: {sentiment:.2f}")
            return sentiment
        except Exception as e:
            logger.debug(f"获取资金流向情绪失败: {e}")
            return 0  # 默认中性

    def _check_breaking_news(self, code: str, lookback_hours: int = None) -> bool:
        """
        检查是否有突发新闻（2026-04-08 新增）

        逻辑:
        1. 检查缓存（TTL 5 分钟内）
        2. 无缓存时，调用新闻情绪分析器检查
        3. 有突发新闻 → 返回 True

        Args:
            code: 股票代码
            lookback_hours: 回溯时间（小时），默认 2 小时

        Returns:
            bool: 是否有突发新闻
        """
        if lookback_hours is None:
            lookback_hours = BREAKING_NEWS_LOOKBACK_HOURS

        # 1. 检查缓存
        current_time = time.time()
        if code in self._breaking_news_cache:
            has_breaking, timestamp = self._breaking_news_cache[code]
            if current_time - timestamp < self._breaking_news_cache_ttl:
                return has_breaking

        # 2. 调用新闻情绪分析器
        try:
            if self._news_analyzer is None:
                from app.factors.news_integration import get_news_sentiment_analyzer
                self._news_analyzer = get_news_sentiment_analyzer()

            # 分析新闻
            result = self._news_analyzer.analyze_stock(
                code,
                lookback_hours=lookback_hours,
                max_news=20
            )

            # 检查是否有突发新闻
            top_news = result.get('top_news', [])
            has_breaking = any(
                news.get('is_breaking', False) for news in top_news
            )

            # 3. 更新缓存
            self._breaking_news_cache[code] = (has_breaking, current_time)

            if has_breaking:
                logger.info(
                    f"[突发新闻检测] {code} 发现 {len([n for n in top_news if n.get('is_breaking')])} "
                    f"条突发新闻（{lookback_hours}小时内）"
                )

            return has_breaking

        except Exception as e:
            logger.debug(f"[突发新闻检测] {code} 检查失败：{e}")
            # 检测失败时，假设有突发新闻（宁可错杀，不可放过）
            return True

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

        # -------------------------------------------------------
        # 信号一致性检查：策略是否在窗口内频繁反转
        # 如果超过 50% 的策略出现过 BUY→SELL 或 SELL→BUY 反转，
        # 说明行情不明朗，放弃本窗口信号
        #
        # 2026-03-25 优化：confluence/confluence_v2 属于同一因子族，
        # 合并为单个逻辑策略计票，避免同因子占 2 票导致阈值过低虚假触发
        # -------------------------------------------------------
        flip_count = 0
        strategy_directions: Dict[str, set] = defaultdict(set)
        for v in votes:
            # 简化：V1已禁用，不再需要家族合并
            logical_strat = v.strategy
            strategy_directions[logical_strat].add(v.action.value)
        for strat, directions in strategy_directions.items():
            if len(directions) > 1:  # 同一策略在窗口内既投 BUY 又投 SELL
                flip_count += 1
        total_strategies = len(strategy_directions)
        # 混乱阈值：从 50% 提升至 60%，减少误判（信号稍微不一致时不急于放弃）
        CHAOS_THRESHOLD = 0.6
        if total_strategies > 0 and flip_count / total_strategies > CHAOS_THRESHOLD:
            logger.info(
                f"[共识] {code} ⚠️ 信号混乱（{flip_count}/{total_strategies} 策略反转），放弃本窗口"
            )
            return

        regime, adx_val = self._get_market_regime(code)

        # 加权评分
        score = 0.0
        weight_details = {}
        bullish_votes = []
        bearish_votes = []

        for v in unique_votes:
            w = self._calc_weight(v.strategy, regime, code)
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

        # 统计同向票数
        bullish_count = sum(1 for v in unique_votes if v.action == SignalAction.BUY)
        bearish_count = sum(1 for v in unique_votes if v.action == SignalAction.SELL)

        MIN_AGREE_VOTES = 2  # 最少需要2个策略同向才能触发

        # 在触发BUY信号之前检查同向票数
        if score >= self.threshold:
            if bullish_count < MIN_AGREE_VOTES:
                logger.info(
                    f"[同向票数不足] {code} score={score:.2f}>=阈值 但同向票数{bullish_count}<{MIN_AGREE_VOTES}，跳过"
                )
                score = 0

        # 在触发SELL信号之前检查同向票数
        if score <= -self.threshold:
            if bearish_count < MIN_AGREE_VOTES:
                logger.info(
                    f"[同向票数不足] {code} score={score:.2f}<=负阈值 但同向票数{bearish_count}<{MIN_AGREE_VOTES}，跳过"
                )
                score = 0

        result = None

        # V2: 策略数量检查
        def check_min_strategies(agree_votes: list, action: str) -> bool:
            """检查是否满足最少策略数量要求"""
            unique_strategies = set(v.strategy for v in agree_votes)
            if len(unique_strategies) < self.min_strategies:
                logger.info(
                    f"[共识] {code} ❌ {action} 策略数量不足: "
                    f"{len(unique_strategies)} < {self.min_strategies} "
                    f"(策略: {unique_strategies})"
                )
                return False
            return True

        if score >= self.threshold:
            # 检查策略数量
            if not check_min_strategies(bullish_votes, "BUY"):
                pass  # 不触发信号
            else:
                avg_price = sum(v.price for v in bullish_votes) / len(bullish_votes) if bullish_votes else votes[0].price
                
                # 获取资金流向情绪（BUY 方向：流入为正增益，与 SELL 对称）
                capital_sentiment = self._get_capital_flow_sentiment(code)
                
                # P1-1: 修复置信度公式 - 简化为加权平均（技术/情绪 0.5/0.5）
                # 技术项(50%): 分数项 + 共识项的线性组合
                # 分数项: score/threshold 线性映射，score=阈值时=0.45，score=2×阈值时=0.9
                score_part = min(1.0, abs(score) / self.threshold) * 0.45
                # 共识项: 同向票数/全部投票数（动态分母）
                unique_votes_buy = bullish_votes + bearish_votes
                consensus_part = min(1.0, len(bullish_votes) / max(1, len(unique_votes_buy))) * 0.25
                # 技术合计 = 分数项 + 共识项 (45% + 25% = 70%)
                # 按 0.5/0.5 比例重新归一化：技术权重 = 0.5
                technical_confidence = score_part + consensus_part  # 0.0 ~ 0.7
                # 归一化技术项到 0.0 ~ 1.0 (除以 0.7)
                technical_part = min(1.0, technical_confidence / 0.7) * 0.5
                
                # 情绪项 (50%): BULLISH + 资金流入 = 加分
                # 资金情绪项：-1 ~ 1 → 0 ~ 1 (取正数部分)
                sentiment_part = max(0, capital_sentiment) * 0.5

                # P1-4: 场景置信度纳入共识公式（2026-04-09 新增/2026-04-09 修复）
                # 从 _scenario_cache 获取场景对象（新问题 2 修复）
                scenario_confidence = 0.5  # 默认中性
                scenario_obj = self._scenario_cache.get(code)
                if scenario_obj:
                    # 场景对象可能有 confidence 属性或 confirm_count 属性
                    if hasattr(scenario_obj, 'confidence'):
                        scenario_confidence = scenario_obj.confidence
                    elif hasattr(scenario_obj, 'confirm_count'):
                        scenario_confidence = scenario_obj.confirm_count / 3.0  # CONFIRM_WINDOW=3
                    scenario_confidence = max(0.3, min(1.0, scenario_confidence))  # 限制在 0.3-1.0

                # 新问题 4 修复：正确的权重分配（总和=1.0）
                # 技术项 (40%) + 情绪项 (30%) + 场景项 (30%)
                base_confidence = (
                    technical_part * 0.4 +
                    sentiment_part * 0.3 +
                    scenario_confidence * 0.3
                )
                confidence = min(1.0, base_confidence)

                logger.info(
                    f"[共识] {code} BUY 置信度计算 | 技术项={technical_part:.2f} | "
                    f"情绪项={sentiment_part:.2f} | 场景项={scenario_confidence:.2f} | "
                    f"资金流={capital_sentiment:.2f} | 最终={confidence:.2f}"
                )

                
                result = ConsensusResult(
                    confidence=round(confidence, 2),
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
                    trace_id=f"TRC-{uuid.uuid4().hex[:12].upper()}",  # 【新增 2026-04-02】决策链路追踪ID
                )

        elif score <= -self.threshold:
            # 检查策略数量
            if not check_min_strategies(bearish_votes, "SELL"):
                pass  # 不触发信号
            else:
                avg_price = sum(v.price for v in bearish_votes) / len(bearish_votes) if bearish_votes else votes[0].price
                
                # 获取资金流向情绪
                capital_sentiment = self._get_capital_flow_sentiment(code)
                
                # P1-1: 修复置信度公式（SELL 对称版本 - 简化为加权平均）
                # 技术项(50%): 分数项 + 共识项
                # 情绪项 (50%): SELL + 负情绪 = 加分
                sentiment_part = max(0, -capital_sentiment) * 0.5

                # P1-4: 场景置信度纳入共识公式（2026-04-09 新增/2026-04-09 修复）
                # 从 _scenario_cache 获取场景对象（新问题 2 修复）
                scenario_confidence = 0.5  # 默认中性
                scenario_obj = self._scenario_cache.get(code)
                if scenario_obj:
                    # 场景对象可能有 confidence 属性或 confirm_count 属性
                    if hasattr(scenario_obj, 'confidence'):
                        scenario_confidence = scenario_obj.confidence
                    elif hasattr(scenario_obj, 'confirm_count'):
                        scenario_confidence = scenario_obj.confirm_count / 3.0  # CONFIRM_WINDOW=3
                    scenario_confidence = max(0.3, min(1.0, scenario_confidence))  # 限制在 0.3-1.0

                # 新问题 4 修复：正确的权重分配（总和=1.0）
                # 技术项 (40%) + 情绪项 (30%) + 场景项 (30%)
                base_confidence = (
                    technical_part * 0.4 +
                    sentiment_part * 0.3 +
                    scenario_confidence * 0.3
                )
                confidence = min(1.0, base_confidence)

                logger.info(
                    f"[共识] {code} SELL 置信度计算 | 技术项={technical_part:.2f} | "
                    f"情绪项={sentiment_part:.2f} | 场景项={scenario_confidence:.2f} | "
                    f"资金流={capital_sentiment:.2f} | 最终={confidence:.2f}"
                )

                result = ConsensusResult(
                    confidence=round(confidence, 2),
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
                trace_id=f"TRC-{uuid.uuid4().hex[:12].upper()}",  # 【新增 2026-04-02】决策链路追踪ID
            )

        if result:
            # -------------------------------------------------------
            # 多周期对齐验证（Task1 新增）
            # 短期信号方向与日线趋势反向时，置信度降权 50%
            # -------------------------------------------------------
            daily_trend = self._daily_trend_cache.get(code, "neutral")
            if daily_trend != "neutral":
                is_counter_trend = (
                    (result.action == SignalAction.BUY and daily_trend == "down") or
                    (result.action == SignalAction.SELL and daily_trend == "up")
                )
                if is_counter_trend:
                    penalized_conf = round(result.confidence * 0.5, 2)
                    result = dataclasses.replace(result, confidence=penalized_conf)
                    logger.info(
                        f"[多周期] {code} 短期 {result.action.value} 与日线 {daily_trend} 反向，"
                        f"置信度降权 → {penalized_conf}"
                    )

            # -------------------------------------------------------
            # 同方向重复信号去重（Task1 新增）
            # 若当前方向与上一次相同且间隔 < 300s，视为重复，置信度 × 0.5
            # -------------------------------------------------------
            action_str = result.action.value
            last_action_info = self._last_signal_action.get(code)
            if last_action_info:
                last_action, last_ts = last_action_info
                if last_action == action_str and (time.time() - last_ts) < 300:
                    penalized_conf = round(result.confidence * 0.5, 2)
                    result = dataclasses.replace(result, confidence=penalized_conf)
                    logger.info(
                        f"[去重] {code} 300s内同方向({action_str})重复信号，置信度降权 → {penalized_conf}"
                    )
            self._last_signal_action[code] = (action_str, time.time())

            # -------------------------------------------------------
            # 信号质量降权（Task1 新增）
            # 历史胜率×盈亏比得出 quality_score，低质量直接拒绝
            # -------------------------------------------------------
            quality = self._quality_score.get(code, 1.0)
            if quality < 0.4:
                logger.info(
                    f"[信号质量] {code} 质量分={quality:.2f} < 0.4，拒绝低质量信号"
                )
                return
            if quality != 1.0:
                # 用 quality 比例调整 score（不改变方向，只影响最终权重感知）
                adjusted_score = round(result.score * quality, 2)
                result = dataclasses.replace(result, score=adjusted_score)
                logger.info(
                    f"[信号质量] {code} 质量分={quality:.2f}，分数调整 {result.score:.2f}→{adjusted_score:.2f}"
                )

            logger.info(
                f"[共识] {code} ✅ 触发 {result.action.value} | "
                f"分数={result.score:+.2f} | 市场={regime} | "
                f"同向: {', '.join(result.agree_strategies)}"
            )
            
            # 2026-04-08: 模拟盘禁止做空检查
            if result.action == SignalAction.SELL and not self.allow_short:
                logger.info(
                    f"[共识] {code} ⛔ SELL信号被阻止 (模拟盘禁止做空) | "
                    f"allow_short={self.allow_short}"
                )
                return  # 直接返回，不触发回调
            
            if self.on_consensus:
                # Phase 1+3: 增强信号评估 (质量 + 时效性)
                enhanced_result = self._enhance_signal(result)
                if enhanced_result:
                    result = enhanced_result

                # TradeChain: 记录信号层数据
                self._record_signal_chain(code, result)

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


    def _enhance_signal(self, result) -> Optional[Any]:
        """
        Phase 1+3: 增强信号评估 (质量 + 时效性)

        Args:
            result: ConsensusResult 对象

        Returns:
            增强后的结果或 None
        """
        try:
            # 延迟加载增强器
            if self._enhancer is None:
                from app.strategy.consensus_enhancer import get_consensus_enhancer
                self._enhancer = get_consensus_enhancer()

            # 增强评估
            enhanced = self._enhancer.enhance_signal(result.code, result)

            # 记录增强信息
            logger.info(
                f"[信号增强] {result.code} | "
                f"质量分={enhanced.quality_score:.2f} | "
                f"时效性={enhanced.timeliness_factor:.2f} | "
                f"最终分={enhanced.final_score:.2f} | "
                f"建议={enhanced.recommendation}"
            )

            # 如果建议 suppress，跳过信号
            if enhanced.recommendation == "suppress":
                logger.info(
                    f"[信号增强] {result.code} 质量/时效性过低，跳过信号"
                )
                return None

            # 将增强信息附加到 result
            result.__dict__.update({
                "quality_score": enhanced.quality_score,
                "timeliness_factor": enhanced.timeliness_factor,
                "final_score": enhanced.final_score,
                "news_age_minutes": enhanced.news_age_minutes,
                "volatility_state": enhanced.volatility_state,
            })

            return result

        except Exception as e:
            logger.debug(f"信号增强失败：{e}")
            return result  # 返回原始结果

    def _record_signal_chain(self, code: str, result) -> None:
        """
        TradeChain: 记录信号层数据

        Args:
            code: 股票代码
            result: ConsensusResult 对象 (可能已增强)
        """
        try:
            from app.storage.trade_recorder import get_trade_chain_recorder
            recorder = get_trade_chain_recorder()

            # 生成唯一交易 ID
            trade_id = f"TC-{uuid.uuid4().hex[:8].upper()}"

            # 准备信号层数据
            signal_data = {
                'signal_id': f"SIG-{uuid.uuid4().hex[:8].upper()}",
                'signal_score': getattr(result, 'score', 0.0),
                'signal_confidence': getattr(result, 'confidence', 0.0),
                'consensus_passed': True,
                'consensus_weights': getattr(result, 'weight_details', {}),
                'risk_check_passed': True,  # 风控检查在 RiskManager 中进行
                'risk_reason': '',
                'market_regime': getattr(result, 'market_regime', 'unknown'),
                'volatility_atr': 0.0,  # 由外部注入
                'ai_sentiment_score': getattr(result, 'final_score', getattr(result, 'score', 0.0)),
            }

            # 准备决策层数据
            decision_data = {
                'decision_reason': 'confluence',  # 共识决策
                'decision_confidence': int(getattr(result, 'confidence', 0.5) * 10),
                'position_planned': 0.20,  # 默认 20% 仓位
                'position_adjusted': 0.20,
                'position_adjust_reason': '',
                'weight_config_snapshot': getattr(result, 'weight_details', {}),
                'account_state': {},  # 由外部注入
            }

            # 启动链路
            recorder.start_chain(trade_id, code, signal_data, decision_data)

            # 存储 trade_id 到 result，供后续执行层使用
            result.__dict__['_trade_chain_id'] = trade_id

            logger.debug(f"TradeChain: 记录信号 {trade_id} - {code}")

        except Exception as e:
            logger.debug(f"TradeChain 记录失败：{e}")

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
                    "adjusted": round(self._calc_weight(s, regime, code), 2),
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
        if "min_strategies" in params:
            self.min_strategies = int(params["min_strategies"])
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
            f"最少策略={self.min_strategies}, ADX趋势>{self.adx_trend}, ADX震荡<{self.adx_range}"
        )


# ============================================================
#  策略权重自适应学习管理器
# ============================================================

class AdaptiveWeightManager:
    """
    策略权重自适应学习管理器（增强版 - Contextual Bandit简化实现）

    功能:
    - 记录每次共识中各策略的投票方向与最终盈亏
    - 新增：记录市场状态，构建(策略×市场状态)二维表现矩阵
    - 每 N 次交易后（或每天）自动重新计算各策略胜率
    - 胜率高的策略权重上调，胜率低的权重下调
    - 权重变化幅度受限（±30%），防止过拟合
    - 新增：UCB探索机制，避免过早收敛到局部最优

    使用:
        manager = AdaptiveWeightManager(engine)
        manager.record_trade(
            agree_strategies=['supertrend', 'macd'], 
            pnl=120.0,
            market_regime='trending',
            adx_value=35.0
        )
        # 每天调用一次（或每 20 笔交易后）:
        manager.update_weights_if_ready()
    """

    # 调整边界（相对基准权重的比例）
    MAX_BOOST  = 1.30   # 最多上调 30%
    MAX_DECAY  = 0.70   # 最多下调 30%
    MIN_TRADES = 5      # P2-1: 从10降为5，模拟盘数据量有限
    MIN_TRADES_PER_REGIME = 3  # 每个市场状态最少交易数
    UPDATE_INTERVAL_DAYS = 1  # 每天更新一次
    
    # UCB探索参数
    UCB_EXPLORATION = 0.1  # 探索系数（10%的探索空间）

    def __init__(self, engine: "ConsensusEngine"):
        self.engine = engine
        # {策略: [{'pnl': float, 'ts': datetime}, ...]}
        self._trade_history: Dict[str, List[Dict]] = defaultdict(list)
        # 新增：{策略: {市场状态: [{'pnl': float, 'ts': datetime, 'adx': float}, ...]}}
        self._regime_performance: Dict[str, Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
        self._last_update: Optional[datetime] = None
        self._lock = threading.Lock()
        logger.info("自适应权重管理器已初始化（Contextual Bandit增强版）")

    def record_trade(
        self, 
        agree_strategies: List[str], 
        pnl: float,
        market_regime: str = "neutral",
        scenario: str = None,  # 场景标签 (Phase 1 新增)
        adx_value: float = 0.0,
    ):
        """
        记录一笔已平仓的交易结果（增强版）

        Args:
            agree_strategies: 本次共识中同向投票的策略列表
            pnl: 已实现盈亏（正=盈利，负=亏损）
            market_regime: 市场状态 (trending/ranging/neutral)
            adx_value: ADX值
        """
        now = datetime.now()
        with self._lock:
            for strategy in agree_strategies:
                # 旧版记录（兼容）
                self._trade_history[strategy].append({'pnl': pnl, 'ts': now})
                # 新增：二维矩阵记录
                self._regime_performance[strategy][market_regime].append({
                    'pnl': pnl, 
                    'ts': now,
                    'adx': adx_value,
                })

        logger.debug(
            f"[自适应权重] 记录交易: 策略={agree_strategies}, pnl={pnl:.2f}, "
            f"市场状态={market_regime}, ADX={adx_value:.1f}"
        )
        self.update_weights_if_ready()

    def update_weights_if_ready(self):
        """检查是否满足更新条件，满足则重新计算并应用权重"""
        with self._lock:
            # 控制更新频率：至少间隔 UPDATE_INTERVAL_DAYS 天
            if self._last_update is not None:
                elapsed = datetime.now() - self._last_update
                if elapsed < timedelta(days=self.UPDATE_INTERVAL_DAYS):
                    return
            self._do_update_weights()

    def _do_update_weights(self):
        """执行权重重计算（调用时须持有 _lock）"""
        # 只保留最近 30 天数据
        cutoff = datetime.now() - timedelta(days=30)
        for strategy in list(self._trade_history.keys()):
            self._trade_history[strategy] = [
                r for r in self._trade_history[strategy] if r['ts'] > cutoff
            ]
        
        # 清理二维矩阵过期数据
        for strategy in list(self._regime_performance.keys()):
            for regime in list(self._regime_performance[strategy].keys()):
                self._regime_performance[strategy][regime] = [
                    r for r in self._regime_performance[strategy][regime] 
                    if r['ts'] > cutoff
                ]

        updated = {}
        for strategy in set(list(self._trade_history.keys()) + list(self._regime_performance.keys())):
            records = self._trade_history.get(strategy, [])
            if len(records) < self.MIN_TRADES:
                continue  # 数据不足，跳过

            # 计算整体胜率
            wins = sum(1 for r in records if r['pnl'] > 0)
            win_rate = wins / len(records)
            
            # 新增：计算市场状态调整系数
            regime_adjustment = self._calculate_regime_adjustment(strategy)
            
            # 新增：UCB探索加成
            ucb_bonus = self._calculate_ucb_bonus(strategy, len(records))
            
            base_weight = DEFAULT_BASE_WEIGHTS.get(strategy, 0.5)

            # 线性映射：胜率 0% → 0.7x，50% → 1.0x，100% → 1.3x
            base_multiplier = self.MAX_DECAY + (self.MAX_BOOST - self.MAX_DECAY) * win_rate
            
            # 综合调整 = 基础倍数 × 市场状态调整 × (1 + UCB探索)
            final_multiplier = base_multiplier * regime_adjustment * (1 + ucb_bonus)
            
            # 限制调整范围
            final_multiplier = max(self.MAX_DECAY, min(self.MAX_BOOST, final_multiplier))
            
            new_weight = round(base_weight * final_multiplier, 3)

            old_weight = self.engine.base_weights.get(strategy, base_weight)
            self.engine.base_weights[strategy] = new_weight
            updated[strategy] = {
                'old': old_weight,
                'new': new_weight,
                'win_rate': round(win_rate, 3),
                'trades': len(records),
                'regime_adj': round(regime_adjustment, 3),
                'ucb_bonus': round(ucb_bonus, 3),
            }

        if updated:
            logger.info(
                f"[自适应权重] 权重已更新 ({len(updated)} 个策略): "
                + ", ".join(
                    f"{s}={v['old']:.2f}→{v['new']:.2f}(胜率{v['win_rate']:.0%}/{v['trades']}笔, "
                    f"状态调整{v['regime_adj']:.2f}, UCB{v['ucb_bonus']:.2f})"
                    for s, v in updated.items()
                )
            )
        else:
            logger.debug("[自适应权重] 无策略满足最少交易笔数要求，跳过本次更新")

        self._last_update = datetime.now()
    
    def _calculate_regime_adjustment(self, strategy: str) -> float:
        """
        计算市场状态调整系数（新增）
        
        逻辑：
        - 如果策略在某个市场状态表现特别好（胜率>70%），且该状态频繁出现，给予加成
        - 如果策略在某个市场状态表现特别差（胜率<30%），且该状态频繁出现，给予惩罚
        - 否则返回1.0（无调整）
        """
        regime_perf = self._regime_performance.get(strategy, {})
        if not regime_perf:
            return 1.0
        
        total_adjustment = 0.0
        total_weight = 0.0
        
        for regime, records in regime_perf.items():
            if len(records) < self.MIN_TRADES_PER_REGIME:
                continue  # 该状态样本不足
            
            wins = sum(1 for r in records if r['pnl'] > 0)
            win_rate = wins / len(records)
            
            # 根据胜率计算调整系数
            if win_rate > 0.7:
                adjustment = 1.1  # 表现好，加成10%
            elif win_rate < 0.3:
                adjustment = 0.9  # 表现差，惩罚10%
            else:
                adjustment = 1.0  # 正常
            
            # 以交易次数为权重
            total_adjustment += adjustment * len(records)
            total_weight += len(records)
        
        if total_weight == 0:
            return 1.0
        
        return total_adjustment / total_weight
    
    def _calculate_ucb_bonus(self, strategy: str, total_trades: int) -> float:
        """
        计算UCB探索加成（新增）
        
        UCB (Upper Confidence Bound) 算法简化版：
        - 交易次数少的策略给予探索加成，避免过早忽略
        - 交易次数多的策略探索加成逐渐减小
        """
        if total_trades < self.MIN_TRADES:
            return self.UCB_EXPLORATION  # 数据不足，给予最大探索
        
        # UCB公式：exploration / sqrt(total_trades)
        ucb = self.UCB_EXPLORATION / (total_trades ** 0.5)
        return min(ucb, self.UCB_EXPLORATION)  # 限制最大值

    def get_strategy_stats(self) -> Dict[str, Dict]:
        """获取各策略历史统计"""
        stats = {}
        cutoff = datetime.now() - timedelta(days=30)
        with self._lock:
            for strategy, records in self._trade_history.items():
                recent = [r for r in records if r['ts'] > cutoff]
                if not recent:
                    continue
                wins = sum(1 for r in recent if r['pnl'] > 0)
                total_pnl = sum(r['pnl'] for r in recent)
                stats[strategy] = {
                    'trades': len(recent),
                    'wins': wins,
                    'win_rate': round(wins / len(recent), 3),
                    'total_pnl': round(total_pnl, 2),
                    'current_weight': self.engine.base_weights.get(strategy, 0.5),
                }
        return stats


# ============================================================
# 支撑/阻力位判断 (2026-03-27 新增 - 假突破过滤)
# ============================================================

def get_support_levels(data, periods=None):
    """
    获取关键支撑位
    
    Args:
        data: 历史价格数据 (包含 close, high, low)
        periods: 均线周期列表
    
    Returns:
        支撑位列表 (从近到远)
    """
    if periods is None:
        periods = [20, 50, 200]
    
    supports = []
    
    try:
        # 1. 移动平均线作为动态支撑
        for period in periods:
            col_name = f'close_{period}ma'
            if col_name not in data.columns:
                data[col_name] = data['close'].rolling(window=period).mean()
            ma_value = data[col_name].iloc[-1]
            if pd.notna(ma_value):
                supports.append(ma_value)
        
        # 2. 近期低点作为静态支撑
        recent_lows = data['low'].rolling(window=20).min().iloc[-1]
        if pd.notna(recent_lows):
            supports.append(recent_lows)
        
        # 3. 布林带下轨
        if 'bb_lower' not in data.columns:
            data['bb_lower'] = data['close'].rolling(window=20).mean() - 2 * data['close'].rolling(window=20).std()
        bb_lower = data['bb_lower'].iloc[-1]
        if pd.notna(bb_lower):
            supports.append(bb_lower)
    except Exception as e:
        logger.debug(f"获取支撑位失败：{e}")
    
    return sorted(supports, reverse=True)  # 从大到小排序


def get_resistance_levels(data, periods=None):
    """
    获取关键阻力位
    
    Args:
        data: 历史价格数据
        periods: 均线周期列表
    
    Returns:
        阻力位列表 (从近到远)
    """
    if periods is None:
        periods = [20, 50, 200]
    
    resistances = []
    
    try:
        # 1. 移动平均线作为动态阻力
        for period in periods:
            col_name = f'close_{period}ma'
            if col_name not in data.columns:
                data[col_name] = data['close'].rolling(window=period).mean()
            ma_value = data[col_name].iloc[-1]
            if pd.notna(ma_value):
                resistances.append(ma_value)
        
        # 2. 近期高点作为静态阻力
        recent_highs = data['high'].rolling(window=20).max().iloc[-1]
        if pd.notna(recent_highs):
            resistances.append(recent_highs)
        
        # 3. 布林带上轨
        if 'bb_upper' not in data.columns:
            data['bb_upper'] = data['close'].rolling(window=20).mean() + 2 * data['close'].rolling(window=20).std()
        bb_upper = data['bb_upper'].iloc[-1]
        if pd.notna(bb_upper):
            resistances.append(bb_upper)
    except Exception as e:
        logger.debug(f"获取阻力位失败：{e}")
    
    return sorted(resistances)  # 从小到大排序


def is_valid_breakout(current_price, supports, resistances, breakout_threshold=0.02):
    """
    判断价格是否有效突破关键支撑/阻力位
    
    Args:
        current_price: 当前价格
        supports: 支撑位列表
        resistances: 阻力位列表
        breakout_threshold: 突破阈值 (默认 2%)
    
    Returns:
        是否有效突破
    """
    if not supports or not resistances:
        return True  # 无参考数据时默认通过
    
    # 向上突破阻力位
    max_resistance = max(resistances)
    if current_price > max_resistance * (1 + breakout_threshold):
        return True
    
    # 向下跌破支撑位
    min_support = min(supports)
    if current_price < min_support * (1 - breakout_threshold):
        return True
    
    # 价格在支撑和阻力之间，非有效突破
    return False



# ============================================================
# Unknown 策略过滤 (2026-03-27 新增 - 专家团决策)
# ============================================================

def is_unknown_strategy(strategy_name: str) -> bool:
    """
    判断是否为 Unknown 策略
    
    Args:
        strategy_name: 策略名称
    
    Returns:
        是否为 Unknown 策略
    """
    if not strategy_name:
        return True
    
    # Unknown 策略标识
    unknown_keywords = ['Unknown', 'unknown', '未知', 'unknown_strategy']
    
    for keyword in unknown_keywords:
        if keyword.lower() in strategy_name.lower():
            return True
    
    return False


def filter_unknown_signals(signals: list) -> list:
    """
    过滤 Unknown 策略信号
    
    Args:
        signals: 信号列表
    
    Returns:
        过滤后的信号列表
    """
    filtered_signals = []
    unknown_count = 0
    
    for signal in signals:
        strategy = getattr(signal, 'strategy', None) or signal.get('strategy', None)
        
        if strategy and not is_unknown_strategy(strategy):
            filtered_signals.append(signal)
        else:
            unknown_count += 1
            logger.debug(f"[Unknown 策略过滤] 过滤信号：{signal.get('code', 'N/A')} - {strategy}")
    
    if unknown_count > 0:
        logger.info(f"[Unknown 策略过滤] 已过滤 {unknown_count} 个 Unknown 策略信号")
    
    return filtered_signals

