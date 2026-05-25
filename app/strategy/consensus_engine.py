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
  CONSENSUS_WINDOW     = 90     # 共识窗口秒数 (SSOT: config_center)
  CONSENSUS_THRESHOLD  = 1.0    # 触发阈值 (加权分>=此值才推送, SSOT: config_center)
  CONSENSUS_ADX_TREND  = 25     # ADX 高于此值判定为趋势
  CONSENSUS_ADX_RANGE  = 20     # ADX 低于此值判定为震荡

  以上 ADX 阈值已迁移到 config/unified_config.yaml 的 market_regime.adx_thresholds 节集中管理。
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
from app.strategy.consensus_types import SignalSource, LayeredConsensusResult
from app.services.adaptive_weight_manager import DEFAULT_BASE_WEIGHTS  # P0 fix (2026-05-20): 缺失导入

# Phase 18: 向后兼容 re-export — SignalOutcome, Vote, ConsensusResult 已提取
from app.strategy.consensus_types import SignalOutcome, Vote, ConsensusResult

# AdaptiveWeightManager: 使用 app.strategy.consensus.adaptive_weights 版本
# (services 版本构造函数不兼容，已移除 re-export)

logger = logging.getLogger("ftseek.consensus")
# 2026-04-10 修复: 移除 propagate=False，让日志通过 ftseek 父级 logger 输出
# 之前设为 False 导致共识引擎日志静默丢失

# ============================================================
#  自适应阈值 (Adaptive Threshold) — 从 config_center 读取
#  2026-05-20: 修复硬编码与 YAML 不一致 (P0)
# ============================================================
def _read_adaptive_threshold_cfg():
    """从 config_center 读取自适应阈值参数（延迟导入避免循环依赖）"""
    from config.config_center import get_config_center
    cc = get_config_center()
    return {
        'enabled': cc.get('strategy.consensus.adaptive_threshold_enabled', True),
        'window': int(cc.get('strategy.consensus.adaptive_threshold_window', 200)),
        'percentile': int(cc.get('strategy.consensus.adaptive_threshold_percentile', 75)),
        'min': float(cc.get('strategy.consensus.adaptive_threshold_min', 0.8)),
        'max': float(cc.get('strategy.consensus.adaptive_threshold_max', 2.5)),
    }

# 模块加载时的快照（供外部引用，如 _score_history maxlen）
_cfg_snapshot = _read_adaptive_threshold_cfg()
ADAPTIVE_THRESHOLD_ENABLED = _cfg_snapshot['enabled']
ADAPTIVE_THRESHOLD_WINDOW = _cfg_snapshot['window']
ADAPTIVE_THRESHOLD_PERCENTILE = _cfg_snapshot['percentile']
ADAPTIVE_THRESHOLD_MIN = _cfg_snapshot['min']
ADAPTIVE_THRESHOLD_MAX = _cfg_snapshot['max']

# ============================================================
#  因子权重 — 从 config_center 读取 (Phase 2b SSOT 对齐)
# ============================================================
def _read_factor_weights_cfg() -> dict:
    """从 config_center 读取因子权重（延迟导入避免循环依赖）"""
    try:
        from config.config_center import get_config_center
        cc = get_config_center()
        weights_cfg = cc.get('strategy.weights', {})
        if isinstance(weights_cfg, dict):
            return {k: float(v) for k, v in weights_cfg.items() if isinstance(v, (int, float))}
    except Exception:
        pass
    return {}

# ============================================================
#  共识参数 — 从 config_center 读取 (P1 SSOT 2026-05-22)
# ============================================================
def _read_consensus_params_cfg() -> dict:
    """从 config_center 读取共识引擎参数（消除魔法数字，SSOT 对齐 unified_config.yaml）"""
    _defaults = {
        'hybrid_weight_enabled': True,
        'trend_boost': 1.5,
        'trend_decay': 0.6,
        'range_boost': 1.5,
        'range_decay': 0.6,
        'mtf_trend_boost': 1.3,
        'mtf_range_decay': 0.7,
        'breaking_news_multiplier': 2.0,
        'breaking_news_lookback_hours': 2,
        'gray_zone_penalty': -0.3,
        'divergence_threshold': 0.7,
        'divergence_score_decay': 0.3,
        'divergence_gray_zone': True,
        'scenario_consecutive_fail_limit': 3,
        'scenario_fail_penalty': -0.1,
        'chaos_threshold': 0.7,
    }
    try:
        from config.config_center import get_config_center
        cc = get_config_center()
        result = {}
        for key, default in _defaults.items():
            val = cc.get(f'strategy.consensus.{key}', default)
            # 类型安全: bool/int/float
            if isinstance(default, bool):
                result[key] = bool(val) if val is not None else default
            elif isinstance(default, int):
                result[key] = int(val) if val is not None else default
            else:
                result[key] = float(val) if val is not None else default
        return result
    except Exception:
        return dict(_defaults)

_consensus_params = _read_consensus_params_cfg()

# 混合架构开关 (SSOT: strategy.consensus.hybrid_weight_enabled)
HYBRID_WEIGHT_ENABLED = _consensus_params['hybrid_weight_enabled']

# ============================================================
#  策略分类与基准权重 (从 git HEAD 恢复, P0 2026-05-20)
# ============================================================
# 趋势型策略 (MOMENTUM factors): 在趋势行情中表现更好
TREND_STRATEGIES = {"st_factor", "macd_factor", "ma_cross_factor", "vol_factor"}
# 震荡型策略 (MEAN_REVERSION factors): 在盘整行情中表现更好
OSCILLATOR_STRATEGIES = {"rsi_factor", "bb_factor"}
# 综合型策略: 本身已做多因子融合, 任何行情都有参考价値
HYBRID_STRATEGIES = {"confluence", "fusion_v2", "fusion_v3", "enhanced_confluence"}
# 情绪型策略: 基于新闻和情绪分析 (新增 2026-03-17)
SENTIMENT_STRATEGIES = {"sentiment", "sentiment_ai", "news_sentiment", "event_driven", "premarket_analyst"}
# 多周期信号策略: 基于 15M/30M/60M/日线 K 线产生的中长周期信号 (新增 2026-03-25)
MTF_STRATEGIES = {"mtf_15m", "mtf_30m", "mtf_60m", "mtf_day"}
# 套利型策略：跨市场套利 (新增 2026-04-09)
ARBITRAGE_STRATEGIES = {"cross_market_arbitrage"}
# 时段型策略：基于特定时段的市场效应 (新增 2026-04-09)
TIMEBASED_STRATEGIES = {"open_close_effect"}
# 其他 (price_break, volume_alert 等)
OTHER_STRATEGIES = set()

# ─────────────────────────────────────────────────────────
#  时段参数 & 因子族 (P1 2026-05-04, P0 修复 2026-05-20)
# ─────────────────────────────────────────────────────────
SESSION_PARAMS_ENABLED = _cfg_snapshot.get('session_params_enabled', True) if isinstance(_cfg_snapshot, dict) else True
try:
    from config.config_center import get_config_center as _cc_sp
    SESSION_PARAMS_ENABLED = _cc_sp().get('strategy.consensus.session_params_enabled', True)
except Exception:
    SESSION_PARAMS_ENABLED = True

# (window_multiplier, threshold_multiplier)
SESSION_CONFIG = {
    "market_open":  (0.8, 1.2),   # 09:30-10:00  波动大，缩短窗口、提高阈值
    "midday":       (1.2, 1.3),   # 12:00-13:00  午间休市，放宽窗口、提高阈值
    "afternoon":    (1.0, 1.0),   # 13:00-15:00  正常
    "market_close": (0.7, 1.5),   # 15:00-16:00  尾盘，缩短窗口、大幅提高阈值
    "regular":      (1.0, 1.0),   # 10:00-12:00  正常
}

# 因子族分组 — 同族因子共线性惩罚
FACTOR_FAMILIES = {
    "momentum":  ["rsi_factor", "macd_factor", "stoch_factor"],
    "trend":     ["adx_factor", "ema_factor", "supertrend_factor"],
    "volatility":["bb_factor", "atr_factor", "vol_factor"],
    "volume":    ["obv_factor", "vwap_factor", "volume_factor"],
}
FAMILY_MAX_WEIGHT = {
    "momentum":  1.5,
    "trend":     1.5,
    "volatility":0.8,  # 2026-05-21: 收紧 (原1.2, bb+rsi+vol组合22%WR)
    "volume":    1.0,
}

# 基准权重 (可通过 config 覆盖)
DEFAULT_BASE_WEIGHTS = {
    "confluence_v2": 1.5,            # 主力融合策略
    "supertrend":    1.0,            # 趋势跟踪主力
    "sentiment_ai":  0.5,            # AI 情绪辅助确认
    "event_driven":           1.2,   # 事件驱动
    "cross_market_arbitrage": 1.0,   # 跨市场套利
    "enhanced_confluence":    1.3,   # 增强融合
    "open_close_effect":      0.8,   # 开盘/收盘效应
    "premarket_analyst":     1.0,   # 盘前LLM分析
    "rsi_factor":        1.0,
    "bb_factor":         1.0,
    "macd_factor":       1.0,
    "st_factor":         1.0,
    "ma_cross_factor":   0.8,
    "vol_factor":        0.6,
}

# 市场状态增益/衰减系数 (SSOT: strategy.consensus.trend_boost / trend_decay / ...)
TREND_BOOST   = _consensus_params['trend_boost']    # 趋势市对趋势策略的增益
TREND_DECAY   = _consensus_params['trend_decay']    # 趋势市对震荡策略的衰减
RANGE_BOOST   = _consensus_params['range_boost']    # 震荡市对震荡策略的增益
RANGE_DECAY   = _consensus_params['range_decay']    # 震荡市对趋势策略的衰减
MTF_TREND_BOOST  = _consensus_params['mtf_trend_boost']   # 趋势市对 MTF 策略的增益
MTF_RANGE_DECAY  = _consensus_params['mtf_range_decay']   # 震荡市 MTF 策略的衰减

# 突发新闻权重倍增器 (SSOT: strategy.consensus.breaking_news_multiplier)
BREAKING_NEWS_MULTIPLIER = _consensus_params['breaking_news_multiplier']
BREAKING_NEWS_LOOKBACK_HOURS = _consensus_params['breaking_news_lookback_hours']

# 灰区防守惩罚 (SSOT: strategy.consensus.gray_zone_penalty)
GRAY_ZONE_PENALTY = _consensus_params['gray_zone_penalty']

# 策略分歧度度量 (SSOT: strategy.consensus.divergence_threshold / ...)
DIVERGENCE_THRESHOLD = _consensus_params['divergence_threshold']
DIVERGENCE_SCORE_DECAY = _consensus_params['divergence_score_decay']
DIVERGENCE_GRAY_ZONE = _consensus_params['divergence_gray_zone']

# 策略场景失效检测 (SSOT: strategy.consensus.scenario_consecutive_fail_limit / ...)
SCENARIO_CONSECUTIVE_FAIL_LIMIT = _consensus_params['scenario_consecutive_fail_limit']
SCENARIO_FAIL_PENALTY = _consensus_params['scenario_fail_penalty']

# 信号混乱阈值 (SSOT: strategy.consensus.chaos_threshold)
CHAOS_THRESHOLD = _consensus_params['chaos_threshold']

# 【P0 修复 2026-04-26】原实现写死 /opt/ftseek/FTSeek 绝对路径，
# 一旦部署目录变更或使用工作区软链接指向其他路径，importlib 就会报 FileNotFoundError。
# 统一改为：
#   ① 优先 env var FTSEEK_WEIGHT_ENGINE_PATH
#   ② 次选 unified_config.yaml: strategy.weight_engine_path
#   ③ Fallback：相对当前文件的 <project_root>/strategies/weight_engine.py
def _resolve_weight_engine_path() -> str:
    import os as _os
    from pathlib import Path as _Path
    env_v = _os.getenv("FTSEEK_WEIGHT_ENGINE_PATH")
    if env_v:
        return env_v
    try:
        from config.config_center import get_config_center as _gcc
        _v = _gcc().get("strategy.weight_engine_path")
        if _v:
            return str(_v)
    except Exception:
        pass
    # <consensus_engine.py>.parent.parent.parent = FTSeek 项目根
    return str(_Path(__file__).resolve().parent.parent.parent / "strategies" / "weight_engine.py")


WEIGHT_ENGINE_PATH = _resolve_weight_engine_path()

# ============================================================
#  市场状态 ADX 阈值 — 从 config/unified_config.yaml market_regime 节加载
#  保留硬编码作为 fallback，修改请统一在 unified_config.yaml 的 market_regime.adx_thresholds 节
# ============================================================
def _load_adx_thresholds_from_config() -> Tuple[float, float]:
    """
    从 config_center 加载 ADX 趋势/震荡判定阈值。

    Returns:
        (adx_trend, adx_range): 趋势阈值和震荡阈值。
        若 config 中无该配置，返回默认值 (25, 20)。
    """
    _fallback_trend = 25.0
    _fallback_range = 20.0
    try:
        from config.config_center import get_config_center
        _cfg = get_config_center()
        _thresholds = _cfg.get('market_regime.adx_thresholds', {})
        # ConfigProxy 转 dict
        if hasattr(_thresholds, 'to_dict'):
            _thresholds = _thresholds.to_dict()
        if isinstance(_thresholds, dict):
            _trend = float(_thresholds.get('trend_threshold', _fallback_trend))
            _range = float(_thresholds.get('range_threshold', _fallback_range))
            return _trend, _range
    except Exception as _e:
        logger.debug(f"[市场状态] 加载 ADX 阈值失败，使用默认值: {_e}")
    return _fallback_trend, _fallback_range


# 模块级常量：由 config_center 加载，因模块导入时可能配置尚未就绪，
# ConsensusEngine.__init__ 中会再次读取以确保最新值
_CONSENSUS_ADX_TREND, _CONSENSUS_ADX_RANGE = _load_adx_thresholds_from_config()
# 保留与旧代码兼容的别名
CONSENSUS_ADX_TREND = _CONSENSUS_ADX_TREND
CONSENSUS_ADX_RANGE = _CONSENSUS_ADX_RANGE



class ConsensusEngine:
    """
    加权共识引擎 v2

    参数:
      window_seconds: 共识窗口 (秒)
      threshold:      触发阈值 (加权总分)
      adx_trend:      ADX 趋势判定线 (优先从 unified_config.yaml market_regime.adx_thresholds 读取)
      adx_range:      ADX 震荡判定线 (优先从 unified_config.yaml market_regime.adx_thresholds 读取)
      base_weights:   {策略名: 基准权重} 字典
      on_consensus:   共识达成回调
    """

    def __init__(self, window_seconds: int = 60, threshold: float = 1.5,
                 adx_trend: float = None, adx_range: float = None,
                 base_weights: Optional[Dict[str, float]] = None,
                 on_consensus: Optional[Callable] = None,
                 min_strategies: int = 1,
                 allow_short: bool = False):  # 2026-04-08 新增：默认禁止做空
        self.window = window_seconds
        self.threshold = threshold
        # ADX 阈值: 优先使用传入值，其次从 config_center 读取，最后用硬编码 fallback
        if adx_trend is None or adx_range is None:
            _trend_cfg, _range_cfg = _load_adx_thresholds_from_config()
            adx_trend = adx_trend if adx_trend is not None else _trend_cfg
            adx_range = adx_range if adx_range is not None else _range_cfg
        self.adx_trend = adx_trend
        self.adx_range = adx_range
        self.base_weights = base_weights or DEFAULT_BASE_WEIGHTS.copy()

        # Phase 2b (2026-05-21): SSOT 对齐 — 用 config 权重覆盖默认值
        if not base_weights:  # 仅当未显式传入 base_weights 时
            _cfg_weights = _read_factor_weights_cfg()
            for strategy, weight in _cfg_weights.items():
                if strategy in self.base_weights:
                    self.base_weights[strategy] = weight
        self.on_consensus = on_consensus
        self.min_strategies = min_strategies  # V2: 最少需要的策略数量
        self.allow_short = allow_short  # 是否允许做空

        # code -> [Vote, ...]
        self._ballots: Dict[str, List[Vote]] = defaultdict(list)
        # P2 优化 (2026-04-14): code -> expiry_timestamp (替代 threading.Timer)
        self._eval_schedule: Dict[str, float] = {}
        # code -> latest ADX value (由外部注入)
        self._adx_cache: Dict[str, float] = {}
        # code -> daily trend: "up" / "down" / "neutral" (多周期对齐，Task1 新增)
        self._daily_trend_cache: Dict[str, str] = {}
        # code -> {"buy": float, "sell": float} MTF对齐度缓存 (2026-03-25 新增)
        # 由 DataSubscriber 在 _get_multi_timeframe_signal 调用后注入
        self._mtf_alignment_cache: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

        # P2 优化: 评估调度线程（单线程定期检查到期窗口）
        self._eval_thread = None
        self._eval_stop_event = threading.Event()
        self._start_eval_scheduler()

        # ---- 信号质量管理（Task1 新增）----
        # code -> deque of (action_str, pnl) 最近20条成交记录
        self._signal_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
        # code -> float [0.3, 1.5]  质量分，初始1.0
        self._quality_score: Dict[str, float] = {}
        # code -> (action_str, timestamp)  上一次触发信号的方向+时间
        self._last_signal_action: Dict[str, Tuple[str, float]] = {}
        # P1 优化 2026-05-04: 自适应阈值 — 滚动分数历史
        self._score_history: deque = deque(maxlen=ADAPTIVE_THRESHOLD_WINDOW)
        # P1 优化 2026-05-04: 每日触发防重
        self._midday_triggered_date: Optional[str] = None
        self._eod_triggered_date: Optional[str] = None
        # P1 优化 2026-05-04: PnL 反馈闭环 — 策略级追踪
        self._strategy_outcomes: Dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self._strategy_trades_since_update = 0
        self._pnl_update_interval = 10  # 每 10 笔交易更新一次策略权重

        # Phase 1+3: 共识增强器 (延迟初始化)
        self._enhancer = None

        # AI 否决层观察器 (2026-05-10 新增 — 模拟盘异步观察模式)
        self._veto_observer = None
        self._veto_thread_pool = None

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

        # 混合架构集成：WeightEngine（2026-04-09）
        self._weight_engine = None
        if HYBRID_WEIGHT_ENABLED:
            try:
                # 延迟初始化 WeightEngine
                self._init_weight_engine()
                logger.info("[混合权重] WeightEngine 已启用")
            except Exception as e:
                logger.warning(f"[混合权重] WeightEngine 初始化失败：{e}")
                self._weight_engine = None

        # LLM 市场研判集成（2026-04-11 新增）
        self._llm_analyst = None
        self._llm_consensus_enabled = False
        self._llm_consensus_weight = 0.15  # 默认权重
        # LLM 研判审计日志 {trace_id: llm_info}（用于回溯分析和权重调整反馈闭环）
        self._llm_audit_log: Dict[str, Dict] = {}

        # LLM 准确率追踪与权重自适应（2026-04-11 Task7 新增）
        self._llm_outcomes: List[Dict] = []  # LLM 研判结果记录
        self._llm_weight_config = {
            "min_weight": 0.05,         # 最低权重
            "max_weight": 0.25,         # 最高权重
            "base_weight": 0.15,        # 基准权重
            "min_samples": 5,           # 最低样本数
            "confidence_samples": 20,   # 完全信任所需样本数
            "prior_accuracy": 0.50,     # 先验准确率
            "update_every_n": 5,        # 每N笔触发更新
            "data_retention_days": 30,  # 数据保留天数
        }
        self._llm_trades_since_update = 0

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

    def update_scenario(self, code: str, scenario, phase=None, tags=None):
        """
        注入场景分类结果（Phase 1 新增，2026-04-10 增强: 支持阶段信息，2026-04-13: 支持标签）

        Args:
            code:      股票代码
            scenario:  ScenarioType 场景类型
            phase:     ScenarioPhase 生命周期阶段 (可选)
            tags:      List[str] 辅助标签 (可选)
        """
        self._scenario_cache[code] = scenario
        if phase is not None:
            if not hasattr(self, '_scenario_phase_cache'):
                self._scenario_phase_cache = {}
            self._scenario_phase_cache[code] = phase
        if tags is not None:
            if not hasattr(self, '_scenario_tags_cache'):
                self._scenario_tags_cache = {}
            self._scenario_tags_cache[code] = tags
        logger.debug(f"[场景] {code} 场景更新 → {scenario} (phase={phase}, tags={tags or []})")

    def set_scenario_router(self, router):
        """
        设置场景路由器（Phase 1 新增）

        Args:
            router: ScenarioRouter 实例
        """
        self._scenario_router = router
        logger.info("[场景路由] 场景路由器已设置")

    def init_llm_integration(self, llm_analyst, weight: float = 0.15, adaptive_config: Dict = None):
        """注入 LLM 市场研判引擎用于置信度调节（2026-04-11 新增，Task7 增强 adaptive_config）

        Args:
            llm_analyst:     LLMMarketAnalyst 实例
            weight:          LLM 项在置信度公式中的权重，限制在 [0.05, 0.25]
            adaptive_config: 权重自适应配置字典，可覆盖 _llm_weight_config 中的默认值
        """
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        if isinstance(llm_analyst, LLMMarketAnalyst) and llm_analyst.enabled:
            self._llm_analyst = llm_analyst
            self._llm_consensus_weight = max(0.05, min(0.25, weight))  # 限制 5%-25%
            # 同步 base_weight 为初始权重
            self._llm_weight_config["base_weight"] = self._llm_consensus_weight
            self._llm_consensus_enabled = True
            if adaptive_config:
                self._llm_weight_config.update(adaptive_config)
                logger.info(f"LLM权重自适应配置: {adaptive_config}")
            logger.info(f"共识引擎LLM集成已启用: weight={self._llm_consensus_weight}")
        else:
            logger.info("共识引擎LLM集成未启用（analyst未就绪或已禁用）")

    def get_llm_audit_log(self, code: str = None, limit: int = 50) -> List[Dict]:
        """获取 LLM 研判审计日志（用于回溯分析和权重调整反馈闭环）

        Args:
            code: 按标的代码过滤，None 表示不过滤
            limit: 返回最近 N 条

        Returns:
            按时间倒序排列的审计记录列表
        """
        logs = list(self._llm_audit_log.values())
        if code:
            logs = [l for l in logs if l.get("code") == code]
        logs.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return logs[:limit]

    def _record_llm_audit(
        self,
        trace_id: str,
        code: str,
        action: str,
        llm_assessment,
        direction_alignment: float,
        alignment_factor: float,
        llm_part: float,
    ):
        """将 LLM 研判信息记录到审计日志（限制 reasoning 到 100 字，避免存大文本）"""
        reasoning_snippet = ""
        if llm_assessment.reasoning:
            reasoning_snippet = llm_assessment.reasoning[:100]

        self._llm_audit_log[trace_id] = {
            "trace_id": trace_id,
            "code": code,
            "timestamp": time.time(),
            "signal_action": action,
            "llm_direction": llm_assessment.direction,
            "llm_confidence_level": llm_assessment.confidence_level,
            "llm_confidence_value": llm_assessment.confidence_value,
            "llm_direction_score": llm_assessment.direction_score,
            "llm_key_factors": llm_assessment.key_factors,
            "llm_risk_flags": llm_assessment.risk_flags,
            "llm_timing": llm_assessment.recommended_timing,
            "llm_reasoning": reasoning_snippet,
            "direction_alignment": round(direction_alignment, 3),
            "alignment_factor": round(alignment_factor, 3),
            "llm_part": round(llm_part, 4),
        }

        # 淘汰超限：保留最新 500 条，淘汰最旧 250 条
        if len(self._llm_audit_log) > 500:
            sorted_keys = sorted(
                self._llm_audit_log,
                key=lambda k: self._llm_audit_log[k]["timestamp"],
            )
            for k in sorted_keys[:250]:
                del self._llm_audit_log[k]

    def _record_llm_outcome(self, code: str, action: str, pnl: float,
                             market_regime: str = "", scenario: str = ""):
        """记录 LLM 研判与实际交易结果的匹配度（Task7 新增）"""
        # 从审计日志中找到该标的最近的 LLM 研判（2 小时内）
        recent_llm = None
        for log in reversed(list(self._llm_audit_log.values())):
            if log.get("code") == code:
                if time.time() - log.get("timestamp", 0) < 7200:
                    recent_llm = log
                    break

        if not recent_llm:
            return  # 该笔交易没有 LLM 研判参与

        # 判断 LLM 研判是否正确
        trade_profitable = pnl > 0
        llm_direction = recent_llm.get("llm_direction", "NEUTRAL")
        # action 可能是 SignalAction 枚举或字符串
        signal_action = action.value if hasattr(action, 'value') else str(action)

        llm_bullish = llm_direction in ("STRONG_BUY", "BUY")
        llm_bearish = llm_direction in ("STRONG_SELL", "SELL")

        if not (llm_bullish or llm_bearish):
            # NEUTRAL 不参与准确率统计
            return

        llm_correct = False
        if signal_action == "BUY":
            llm_correct = (llm_bullish and trade_profitable) or (llm_bearish and not trade_profitable)
        elif signal_action == "SELL":
            llm_correct = (llm_bearish and trade_profitable) or (llm_bullish and not trade_profitable)

        outcome = {
            "code": code,
            "timestamp": time.time(),
            "signal_action": signal_action,
            "llm_direction": llm_direction,
            "llm_confidence": recent_llm.get("llm_confidence_level", "LOW"),
            "llm_confidence_value": recent_llm.get("llm_confidence_value", 0.45),
            "direction_alignment": recent_llm.get("direction_alignment", 0.0),
            "pnl": pnl,
            "llm_correct": llm_correct,
            "market_regime": market_regime,
            "scenario": scenario,
            "trace_id": recent_llm.get("trace_id", ""),
        }

        self._llm_outcomes.append(outcome)
        self._llm_trades_since_update += 1

        logger.info(
            f"LLM准确率追踪: {code} action={signal_action} llm={llm_direction} "
            f"pnl={pnl:+.2f} correct={llm_correct} "
            f"(累计{len(self._llm_outcomes)}条)"
        )

        # 清理过期数据
        cutoff = time.time() - self._llm_weight_config["data_retention_days"] * 86400
        self._llm_outcomes = [o for o in self._llm_outcomes if o["timestamp"] > cutoff]

        # 检查是否触发权重更新
        if self._llm_trades_since_update >= self._llm_weight_config["update_every_n"]:
            self._update_llm_weight()
            self._llm_trades_since_update = 0

    def _update_llm_weight(self):
        """基于 LLM 准确率自动调整共识权重（贝叶斯收缩，Task7 新增）"""
        cfg = self._llm_weight_config
        outcomes = self._llm_outcomes

        if len(outcomes) < cfg["min_samples"]:
            logger.debug(f"LLM权重更新: 样本不足 {len(outcomes)}/{cfg['min_samples']}，跳过")
            return

        total = len(outcomes)
        correct = sum(1 for o in outcomes if o["llm_correct"])
        raw_accuracy = correct / total

        # 贝叶斯收缩（与 AdaptiveWeightManager 同一思路）
        confidence = min(1.0, total / cfg["confidence_samples"])
        shrunk_accuracy = confidence * raw_accuracy + (1 - confidence) * cfg["prior_accuracy"]

        # 准确率 → 权重映射
        base = cfg["base_weight"]
        min_w = cfg["min_weight"]
        max_w = cfg["max_weight"]

        if shrunk_accuracy >= cfg["prior_accuracy"]:
            ratio = (shrunk_accuracy - cfg["prior_accuracy"]) / (1.0 - cfg["prior_accuracy"])
            new_weight = base + (max_w - base) * ratio
        else:
            ratio = (cfg["prior_accuracy"] - shrunk_accuracy) / cfg["prior_accuracy"]
            new_weight = base - (base - min_w) * ratio

        new_weight = max(min_w, min(max_w, new_weight))

        old_weight = self._llm_consensus_weight
        self._llm_consensus_weight = new_weight

        logger.info(
            f"LLM权重更新: {old_weight:.3f} -> {new_weight:.3f} "
            f"(准确率={raw_accuracy:.1%}, 收缩后={shrunk_accuracy:.1%}, "
            f"样本={total}, 信心度={confidence:.1%})"
        )

    def get_llm_accuracy_stats(self, scenario: str = None,
                                market_regime: str = None,
                                days: int = 30) -> Dict:
        """获取 LLM 研判准确率统计（用于监控和调参，Task7 新增）

        Args:
            scenario:      按场景过滤，None 表示不过滤
            market_regime: 按市场状态过滤，None 表示不过滤
            days:          统计天数（默认30天）

        Returns:
            dict 包含 total_samples, correct, accuracy, current_weight, by_confidence, by_scenario 等
        """
        cutoff = time.time() - days * 86400
        outcomes = [o for o in self._llm_outcomes if o["timestamp"] > cutoff]

        if scenario:
            outcomes = [o for o in outcomes if o.get("scenario") == scenario]
        if market_regime:
            outcomes = [o for o in outcomes if o.get("market_regime") == market_regime]

        total = len(outcomes)
        correct = sum(1 for o in outcomes if o["llm_correct"])
        accuracy = correct / total if total > 0 else 0.0

        # 按置信度分层
        by_confidence: Dict[str, Dict] = {}
        for level in ("HIGH", "MEDIUM", "LOW"):
            level_outcomes = [o for o in outcomes if o.get("llm_confidence") == level]
            level_total = len(level_outcomes)
            level_correct = sum(1 for o in level_outcomes if o["llm_correct"])
            if level_total > 0:
                by_confidence[level] = {
                    "total": level_total,
                    "correct": level_correct,
                    "accuracy": level_correct / level_total,
                }

        # 按场景分层
        by_scenario: Dict[str, Dict] = {}
        scenarios = set(o.get("scenario", "") for o in outcomes if o.get("scenario"))
        for s in scenarios:
            s_outcomes = [o for o in outcomes if o.get("scenario") == s]
            s_total = len(s_outcomes)
            s_correct = sum(1 for o in s_outcomes if o["llm_correct"])
            if s_total > 0:
                by_scenario[s] = {
                    "total": s_total,
                    "correct": s_correct,
                    "accuracy": s_correct / s_total,
                }

        return {
            "total_samples": total,
            "correct": correct,
            "accuracy": accuracy,
            "current_weight": self._llm_consensus_weight,
            "base_weight": self._llm_weight_config["base_weight"],
            "weight_range": [self._llm_weight_config["min_weight"], self._llm_weight_config["max_weight"]],
            "by_confidence": by_confidence,
            "by_scenario": by_scenario,
            "recent_outcomes": outcomes[-10:] if outcomes else [],
        }

    def _init_weight_engine(self):
        """
        初始化 WeightEngine（混合架构集成，2026-04-09）

        WeightEngine 负责：
        1. 新闻权重计算 (0-40%)
        2. 港股美股跟随权重 (0-35%)
        3. 技术指标权重 (25-100%)
        """
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "weight_engine",
                WEIGHT_ENGINE_PATH
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # 实例化 WeightEngine
            self._weight_engine = module.WeightEngine()
            logger.debug("[混合权重] WeightEngine 初始化成功")
        except Exception as e:
            logger.warning(f"[混合权重] WeightEngine 初始化失败：{e}")
            self._weight_engine = None

    def _get_hybrid_weight(
        self,
        strategy: str,
        regime: str,
        code: str = None
    ) -> Tuple[float, Dict[str, Any]]:
        """
        混合权重计算（2026-04-09 新增）

        整合 WeightCalculator 和 WeightEngine:
        - WeightCalculator: 基于策略胜率/市场状态的权重
        - WeightEngine: 基于新闻因子/HK-US 相关性的权重调整

        Args:
            strategy: 策略名称
            regime: 市场状态 ("trending" / "ranging" / "neutral")
            code: 股票代码

        Returns:
            Tuple[float, Dict]: (最终权重，权重详情)
        """
        weight_details = {
            'base_weight': 0.0,
            'market_adjustment': 1.0,
            'scenario_delta': 0.0,
            'news_multiplier': 1.0,
            'hybrid_adjustment': 1.0,
        }

        # 1. 基础权重（来自 base_weights）
        base = self.base_weights.get(strategy, 0.5)
        weight_details['base_weight'] = base

        # 2. 市场状态调整
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

        weight_details['market_adjustment'] = market_adjustment

        # 3. 场景权重（v2: 乘法→加法 delta）
        scenario_delta = 0.0
        if code and self._scenario_router is not None:
            try:
                current_scenario = self._scenario_cache.get(code)
                if current_scenario:
                    # 灰区防守: 直接应用负 delta，不走路由逻辑
                    if current_scenario.value == 'gray_zone':
                        scenario_delta = GRAY_ZONE_PENALTY
                        logger.debug(
                            f"[灰区防守] {code} {strategy} delta={scenario_delta:+.2f}"
                        )
                    else:
                        route_result = self._scenario_router.route(current_scenario)
                        scenario_weight = route_result.active_strategies.get(strategy, 1.0)
                        # v2: 转为加法 delta
                        scenario_delta = (scenario_weight - 1.0) * 0.3

                        # DECAYING 阶段: delta 减半
                        if hasattr(self, '_scenario_phase_cache'):
                            phase = self._scenario_phase_cache.get(code)
                            if phase is not None and phase.value == 'decaying':
                                original_delta = scenario_delta
                                scenario_delta *= 0.5
                                logger.debug(
                                    f"[混合权重] {code} {strategy} DECAYING衰减: "
                                    f"Δ{original_delta:+.3f} → Δ{scenario_delta:+.3f}"
                                )

                    logger.debug(
                        f"[场景权重] {code} {current_scenario.value} {strategy} "
                        f"delta={scenario_delta:+.3f}"
                    )
            except Exception as _e:
                logger.debug(f"[场景权重] {code} {strategy} 计算异常：{_e}")

        weight_details['scenario_delta'] = scenario_delta

        # 4. 突发新闻权重倍增（情绪策略）
        news_multiplier = 1.0
        if strategy in SENTIMENT_STRATEGIES and code:
            has_breaking = self._check_breaking_news(code)
            if has_breaking:
                news_multiplier = BREAKING_NEWS_MULTIPLIER
                logger.info(
                    f"[突发新闻权重] {code} {strategy} 权重倍增 ×{BREAKING_NEWS_MULTIPLIER:.1f}"
                )

        weight_details['news_multiplier'] = news_multiplier

        # 5. 混合架构调整：WeightEngine 因子调整（2026-04-09）
        hybrid_adjustment = 1.0
        if HYBRID_WEIGHT_ENABLED and self._weight_engine is not None and code:
            try:
                # 从 WeightEngine 获取新闻因子和 HK/US 因子
                # 注意：这里使用简化的同步调用，实际可能需要异步
                news_factor = self._get_news_factor(code)
                hk_us_factor = self._get_hk_us_factor(code)

                # 根据新闻重要性调整权重
                if news_factor:
                    if news_factor.get('importance') == 'major':
                        # 重大新闻：提高所有策略权重 20%
                        hybrid_adjustment *= 1.2
                        logger.debug(f"[混合权重] {code} 重大新闻 → +20%")
                    elif news_factor.get('importance') == 'normal':
                        # 一般新闻：提高 10%
                        hybrid_adjustment *= 1.1
                        logger.debug(f"[混合权重] {code} 一般新闻 → +10%")

                # 根据 HK/US 相关性调整权重
                if hk_us_factor and hk_us_factor.get('enabled'):
                    correlation = hk_us_factor.get('correlation', 0)
                    if correlation >= 0.8:
                        # 高相关性：提高权重 15%
                        hybrid_adjustment *= 1.15
                        logger.debug(f"[混合权重] {code} 高相关性 (corr={correlation:.2f}) → +15%")
                    elif correlation >= 0.6:
                        # 中相关性：提高权重 5%
                        hybrid_adjustment *= 1.05
                        logger.debug(f"[混合权重] {code} 中相关性 (corr={correlation:.2f}) → +5%")

            except Exception as e:
                logger.debug(f"[混合权重] {code} {strategy} 计算异常：{e}")

        weight_details['hybrid_adjustment'] = hybrid_adjustment

        # 计算最终权重: v2 混合模型
        # 基准 × 市场 × 新闻 × 混合调整 (乘法) + 场景 delta (加法)
        final_weight = (
            base
            * market_adjustment
            * news_multiplier
            * hybrid_adjustment
            + scenario_delta
        )
        # 保护下界
        final_weight = max(0.1, final_weight)

        logger.debug(
            f"[混合权重] {code} {strategy}: "
            f"{base:.2f} × {market_adjustment:.2f} × {news_multiplier:.2f} × "
            f"{hybrid_adjustment:.2f} + Δ{scenario_delta:+.3f} = {final_weight:.2f}"
        )

        return final_weight, weight_details

    def _get_news_factor(self, code: str) -> Optional[Dict]:
        """
        获取新闻因子（WeightEngine 集成）

        Args:
            code: 股票代码

        Returns:
            Optional[Dict]: 新闻因子详情
        """
        if not self._weight_engine:
            return None

        try:
            # P0 修复 (2026-04-11): 使用 ThreadPoolExecutor 代替 asyncio.get_event_loop()
            # asyncio.get_event_loop() 在非主线程调用会失败
            from concurrent.futures import ThreadPoolExecutor
            import asyncio
            
            def _run_async():
                """在新事件循环中运行异步代码"""
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    return loop.run_until_complete(
                        self._weight_engine.news_monitor.get_aggregation(code)
                    )
                finally:
                    loop.close()
            
            # 使用线程池执行异步调用
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run_async)
                news_agg = future.result(timeout=10)  # 10秒超时

            news_items = [n.to_dict() for n in news_agg.news_items]
            sources = list(set(n.source for n in news_agg.news_items))

            # 计算新闻权重和因子
            weight, news_factor = self._weight_engine.calculate_news_weight(news_items, sources)

            return {
                'news_count': news_factor.news_count,
                'major_news_count': news_factor.major_news_count,
                'importance': news_factor.importance.value if hasattr(news_factor.importance, 'value') else news_factor.importance,
                'sentiment': news_factor.sentiment,
                'confidence': news_factor.confidence,
            }
        except Exception as e:
            logger.debug(f"[新闻因子] {code} 获取失败：{e}")
            return None

    def _get_hk_us_factor(self, code: str) -> Optional[Dict]:
        """
        获取港股美股因子（WeightEngine 集成）

        Args:
            code: 港股股票代码

        Returns:
            Optional[Dict]: HK/US 因子详情
        """
        if not self._weight_engine:
            return None

        try:
            # 获取 HK/US 配对
            pair = self._weight_engine.get_hk_us_pair(code)
            if not pair:
                return None

            us_stock = pair['us_stock']
            correlation = self._weight_engine.calculate_correlation(code, us_stock)

            return {
                'hk_stock': code,
                'us_stock': us_stock,
                'correlation': correlation,
                'enabled': correlation >= self._weight_engine.config.correlation_threshold,
            }
        except Exception as e:
            logger.debug(f"[HK/US 因子] {code} 获取失败：{e}")
            return None

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
        exit_reason: str = None,  # 退出理由 (2026-04-13 新增)
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
            from app.signal.analytics import get_analytics
            analytics = get_analytics()
            analytics.record_outcome(outcome)
            logger.debug(f"[信号质量] {code} 已持久化到数据库")
        except Exception as e:
            logger.error(f"[信号质量] 持久化失败：{e}")

        # 兼容旧版：仍保留简单记录
        self._signal_history[code].append((action_str, pnl))

        # ── P1 优化 2026-05-04: PnL 反馈闭环 — 策略级追踪 ──
        if strategy and strategy != "unknown":
            self._strategy_outcomes[strategy].append(pnl)
            self._strategy_trades_since_update += 1
            if self._strategy_trades_since_update >= self._pnl_update_interval:
                self._apply_pnl_weight_update()
                self._strategy_trades_since_update = 0

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

        # --- LLM 研判准确率追踪 ---
        if self._llm_consensus_enabled:
            self._record_llm_outcome(code, action, pnl, market_regime, scenario)

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

    def _calc_weight(self, strategy: str, regime: str, code: str = None) -> Tuple[float, Dict]:
        """
        计算某策略在当前市场状态下的实际权重（混合架构，2026-04-09）

        整合 WeightCalculator 和 WeightEngine:
        - WeightCalculator: 基于策略胜率/市场状态的权重
        - WeightEngine: 基于新闻因子/HK-US 相关性的权重调整

        Args:
            strategy: 策略名称
            regime: 市场状态 ("trending" / "ranging" / "neutral")
            code: 股票代码

        Returns:
            Tuple[float, Dict]: (最终权重，权重详情)
        """
        # 使用混合权重计算（如果启用）
        if HYBRID_WEIGHT_ENABLED and self._weight_engine is not None:
            return self._get_hybrid_weight(strategy, regime, code)

        # 降级到原有逻辑（兼容模式）
        base = self.base_weights.get(strategy, 0.5)

        # ============================================================
        # Phase 1 → v2: 场景路由权重调整（从乘法改为加法 delta）
        # 场景与策略的关系是辅助偏移，不应级联放大
        # ============================================================
        scenario_delta = 0.0
        if code and hasattr(self, '_scenario_router') and self._scenario_router is not None:
            try:
                # 获取当前场景（由 DataSubscriber 注入）
                current_scenario = self._scenario_cache.get(code)
                if current_scenario:
                    # 灰区防守: 直接应用负 delta，不走路由逻辑
                    if current_scenario.value == 'gray_zone':
                        scenario_delta = GRAY_ZONE_PENALTY
                        logger.debug(
                            f"[灰区防守] {code} {strategy} delta={scenario_delta:+.2f}"
                        )
                    else:
                        # 从路由器获取策略在该场景下的权重
                        route_result = self._scenario_router.route(current_scenario)
                        scenario_weight = route_result.active_strategies.get(strategy, 1.0)
                        # v2: 转为加法 delta（偏离1.0的部分作为叠加项）
                        scenario_delta = (scenario_weight - 1.0) * 0.3  # 贴现系数
        
                    # DECAYING 阶段: delta 减半
                    if hasattr(self, '_scenario_phase_cache'):
                        phase = self._scenario_phase_cache.get(code)
                        if phase is not None and phase.value == 'decaying':
                            original_delta = scenario_delta
                            scenario_delta *= 0.5
                            logger.debug(
                                f"[场景权重] {code} {strategy} DECAYING衰减: "
                                f"Δ{original_delta:+.3f} → Δ{scenario_delta:+.3f}"
                            )
        
                    logger.debug(
                        f"[场景权重] {code} {current_scenario.value} {strategy} "
                        f"delta={scenario_delta:+.3f}"
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

        # 计算最终权重 = 基准权重 × 市场状态调整 × 新闻倍增 + 场景delta
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

        # v2: 基准×市场×新闻 (乘法) + 场景 (加法)
        final_weight = base * market_adjustment * news_multiplier + scenario_delta

        # 标签微调 (2026-04-13 新增: 场景标签叠加影响权重)
        if code and hasattr(self, '_scenario_tags_cache'):
            tags = self._scenario_tags_cache.get(code, [])
            tag_delta = 0.0
            if "vol_expanding" in tags and strategy in TREND_STRATEGIES:
                tag_delta += 0.02  # 波动扩张给趋势策略加分
            if "reversal_risk" in tags and strategy in TREND_STRATEGIES:
                tag_delta -= 0.03  # 反转风险给趋势策略减分
            if "mean_reversion" in tags and strategy in OSCILLATOR_STRATEGIES:
                tag_delta += 0.02  # 均值回归给震荡策略加分
            if "mtf_divergent" in tags:
                tag_delta -= 0.02  # 多周期分歧全局减分
            if tag_delta != 0.0:
                final_weight += tag_delta
                logger.debug(
                    f"[标签微调] {code} {strategy} tags={tags} delta={tag_delta:+.3f}"
                )

        # 保护下界: 权重不能小于 0.1
        final_weight = max(0.1, final_weight)

        # 返回兼容格式
        weight_details = {
            'base_weight': base,
            'market_adjustment': market_adjustment,
            'scenario_delta': scenario_delta,
            'news_multiplier': news_multiplier,
            'hybrid_adjustment': 1.0,
        }

        return final_weight, weight_details
    
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

    def _get_strategy_health_multiplier(self, strategy: str) -> float:
        """
        获取策略健康度权重乘数 (2026-05-19 P0-3)
        
        基于 StrategyHealthTracker 的动态权重调整：
        - EXCELLENT (>80): 1.2x
        - GOOD (60-80): 1.0x
        - NORMAL (40-60): 1.0x
        - POOR (20-40): 0.8x
        - CRITICAL (<20): 0.5x
        
        Args:
            strategy: 策略名称
        
        Returns:
            float: 权重乘数 (0.5 - 1.2)
        """
        try:
            from app.analytics.strategy_health import get_strategy_health_tracker
            tracker = get_strategy_health_tracker()
            health = tracker.get_health(strategy)
            
            if health is None:
                return 1.0
            
            # 根据健康等级返回权重乘数
            level = health.health_level
            if level == "EXCELLENT":
                return 1.2
            elif level == "CRITICAL":
                return 0.5
            elif level == "POOR":
                return 0.8
            else:
                return 1.0
        except Exception as e:
            logger.debug(f"[策略健康度] {strategy} 获取失败：{e}")
            return 1.0

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
            # 安全策略：检测失败时不假设有突发新闻，避免误增权重
            return False

    # ----------------------------------------------------------
    #  P2: 评估调度器 (替代 threading.Timer)
    # ----------------------------------------------------------
    def _start_eval_scheduler(self):
        """启动评估调度后台线程 (P2 优化)"""
        def _scheduler_loop():
            while not self._eval_stop_event.is_set():
                now = time.time()
                expired_codes = []
                with self._lock:
                    for code, expiry in list(self._eval_schedule.items()):
                        if now >= expiry:
                            expired_codes.append(code)
                
                for code in expired_codes:
                    try:
                        self._evaluate(code)
                    except Exception as e:
                        import traceback
                        self._logger.error(f"[scheduler_error] {code} 评估异常: {e}\n{traceback.format_exc()}")
                
                # 每 0.5 秒检查一次
                self._eval_stop_event.wait(0.5)
        
        self._eval_thread = threading.Thread(
            target=_scheduler_loop, daemon=True, name="consensus-scheduler"
        )
        self._eval_thread.start()

    def _shutdown_eval_scheduler(self):
        """关闭评估调度器 (优雅退出)"""
        self._eval_stop_event.set()
        if self._eval_thread and self._eval_thread.is_alive():
            self._eval_thread.join(timeout=3)

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

            # P2 优化: 第一票记录到期时间，由调度器线程到期后触发评估
            if code not in self._eval_schedule:
                self._eval_schedule[code] = time.time() + self.window
                logger.info(f"[共识] {code} 窗口已开启, {self.window}s 后加权评估")

    # ----------------------------------------------------------
    #  评估
    # ----------------------------------------------------------

    @staticmethod
    def _get_vote_conviction(vote) -> float:
        """Extract intra-strategy conviction factor from a vote (0.3-1.5).

        Signal.extra may carry "conviction" set by the originating strategy.
        Default 1.0 means no adjustment.
        """
        sig = getattr(vote, "signal", None)
        if sig is not None and hasattr(sig, "extra") and isinstance(sig.extra, dict):
            c = sig.extra.get("conviction")
            if isinstance(c, (int, float)):
                return max(0.3, min(float(c), 1.5))
        return 1.0

    # ─────────────────────────────────────────────────────────
    #  P1 优化 2026-05-04: 时段参数 + 因子族 + 时间衰减 + 自适应阈值
    # ─────────────────────────────────────────────────────────

    def _get_session_params(self) -> Tuple[float, float]:
        """根据当前 HK 时间返回 (window_multiplier, threshold_multiplier)
        P1 2026-05-04: 增加 afternoon 时段 (13:00-15:00)，与 TimeGate 对齐
        """
        if not SESSION_PARAMS_ENABLED:
            return 1.0, 1.0
        try:
            import pytz
            from datetime import datetime
            hkt = pytz.timezone("Asia/Hong_Kong")
            now = datetime.now(hkt)
            hm = now.hour * 60 + now.minute  # 分钟数

            # 开盘: 09:30-10:00
            if 570 <= hm < 600:
                return SESSION_CONFIG["market_open"]
            # 午间休市: 12:00-13:00
            elif 720 <= hm < 780:
                return SESSION_CONFIG["midday"]
            # 下午盘 A股联动: 13:00-15:00
            elif 780 <= hm < 900:
                return SESSION_CONFIG["afternoon"]
            # 尾盘冲刺: 15:00-16:00
            elif 900 <= hm < 960:
                return SESSION_CONFIG["market_close"]
            # 上午盘中: 10:00-12:00
            return SESSION_CONFIG["regular"]
        except Exception:
            return 1.0, 1.0

    def _apply_factor_family_cap(
        self, votes: list, weight_details: dict, score: float
    ) -> float:
        """因子族共线性惩罚: 每族总权重不得超过 FAMILY_MAX_WEIGHT"""
        for family_name, family_members in FACTOR_FAMILIES.items():
            family_total = 0.0
            family_votes = []
            for v in votes:
                if v.strategy in family_members:
                    w = weight_details.get(v.strategy, 0.5)
                    family_total += w
                    family_votes.append((v, w))
            cap = FAMILY_MAX_WEIGHT.get(family_name, 2.0)
            if family_total > cap and len(family_votes) > 1:
                # 族内等比缩放
                scale = cap / family_total
                penalty = family_total - cap
                for v, w in family_votes:
                    old_w = weight_details[v.strategy]
                    new_w = old_w * scale
                    weight_details[v.strategy] = round(new_w, 2)
                # 从 score 中扣除超额部分 (只影响同方向)
                for v, w in family_votes:
                    if v.action == SignalAction.BUY:
                        score -= w * (1 - scale)
                    elif v.action == SignalAction.SELL:
                        score += w * (1 - scale)
                logger.debug(
                    f"[因子族/{family_name}] 总权重{family_total:.2f}>{cap:.2f}, "
                    f"缩放至×{scale:.2f}, 惩罚={penalty:.2f}"
                )
        return score

    # ── Phase 2a (2026-05-21): 因子组合黑名单 ──
    # 基于历史交易数据诊断，以下因子组合胜率极低 (<25%) 且贡献大量亏损
    # P0辩论修复 2026-05-26: 基于审计数据的策略黑名单
    # 组合黑名单 (所有因子都在active_strategies中时触发)
    FACTOR_BLACKLIST = [
        frozenset({"bb_factor", "rsi_factor", "vol_factor"}),  # 5笔 0%WR -$2,260
        frozenset({"st_factor", "rsi_factor", "bb_factor"}),  # 10笔 0%WR -$1,550
        frozenset({"st_factor", "rsi_factor", "bb_factor", "vol_factor"}),  # 含st_factor
    ]
    # 单因子黑名单 (任一因子出现即触发)
    STRATEGY_BLACKLIST = {
        "confluence_v2",  # 5笔 0%WR -$125
    }

    def _check_factor_blacklist(self, votes: list, score: float, code: str) -> float:
        """因子组合黑名单 + 单因子黑名单: 命中时完全屏蔽信号 (ADR-043)"""
        active_strategies = set(v.strategy for v in votes)

        # 1. 单因子黑名单检查
        for strategy in self.STRATEGY_BLACKLIST:
            if strategy in active_strategies:
                logger.warning(
                    f"[因子黑名单] {code} 单因子 {strategy} 被禁用 (ADR-043), "
                    f"score {score:+.2f} → 0"
                )
                return 0.0

        # 2. 组合黑名单检查
        for blacklisted in self.FACTOR_BLACKLIST:
            if blacklisted.issubset(active_strategies):
                logger.warning(
                    f"[因子黑名单] {code} 低胜率组合触发: "
                    f"{', '.join(sorted(blacklisted))} "
                    f"score {score:+.2f} → 0 (ADR-043)"
                )
                return 0.0

        return score

    def _compute_adaptive_threshold(self) -> float:
        """基于滚动分数历史的 percentile 阈值"""
        if not ADAPTIVE_THRESHOLD_ENABLED or len(self._score_history) < 20:
            return self.threshold
        import numpy as np
        scores = np.array(list(self._score_history))
        pct_val = float(np.percentile(scores, ADAPTIVE_THRESHOLD_PERCENTILE))
        adaptive = max(ADAPTIVE_THRESHOLD_MIN, min(ADAPTIVE_THRESHOLD_MAX, pct_val))
        logger.debug(
            f"[自适应阈值] percentile={ADAPTIVE_THRESHOLD_PERCENTILE} → "
            f"{pct_val:.2f}, 限制后={adaptive:.2f} (离散阈值={self.threshold})"
        )
        return adaptive

    def _apply_pnl_weight_update(self):
        """P1 优化: 基于策略实际 PnL 的贝叶斯权重更新

        每 N 笔交易后，根据各策略近期表现调整 base_weights。
        使用贝叶斯平滑: new_weight = prior * (1 - α) + observed * α
        胜率越高的策略权重逐步提升，持续亏损的策略权重衰减。
        """
        alpha = 0.15  # 学习率: 每次更新最多调整 15%
        min_weight = 0.3
        max_weight = 2.0

        updated = []
        for strategy, outcomes in self._strategy_outcomes.items():
            if len(outcomes) < 3:
                continue
            records = list(outcomes)
            wins = [r for r in records if r > 0]
            losses = [r for r in records if r < 0]
            win_rate = len(wins) / len(records) if records else 0.5
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0
            profit_factor = avg_win / avg_loss if avg_loss > 0 else 1.0

            # 综合表现分: 胜率 × 盈亏比, 归一化到 0.5~2.0
            performance = win_rate * profit_factor
            performance = max(0.5, min(2.0, performance))

            prior = self.base_weights.get(strategy, 1.0)
            new_weight = prior * (1 - alpha) + prior * performance * alpha
            new_weight = max(min_weight, min(max_weight, new_weight))
            old_weight = self.base_weights.get(strategy, prior)
            self.base_weights[strategy] = round(new_weight, 2)

            if abs(new_weight - old_weight) > 0.01:
                updated.append(
                    f"{strategy}: {old_weight:.2f}→{new_weight:.2f} "
                    f"(wr={win_rate:.0%} pf={profit_factor:.2f})"
                )

        if updated:
            logger.info(
                f"[PnL反馈] 策略权重更新 (α={alpha}, N={self._pnl_update_interval}笔): "
                + " | ".join(updated)
            )

    def _evaluate(self, code: str):
        """窗口到期，加权评分 (v3: 时间衰减 + 因子族惩罚 + 自适应阈值 + 时段参数)"""
        with self._lock:
            votes = self._ballots.pop(code, [])
            self._eval_schedule.pop(code, None)  # P2: 清理调度记录

        if not votes:
            return

        # ── P1 优化: 时间衰减去重 (替代简单取最后一票) ──
        # 窗口内越靠近末端的票权重越高，max() 取最新一票但标记衰减因子
        t_now = time.time()
        window_end = t_now
        # 找到窗口内每策略的所有票，按时间衰减取加权方向
        strategy_decayed_votes = {}
        for v in votes:
            age = t_now - v.timestamp
            decay = max(0.3, 1.0 - age / max(1, self.window))
            if v.strategy not in strategy_decayed_votes:
                strategy_decayed_votes[v.strategy] = {
                    "latest_vote": v,
                    "decay": decay,
                    "all_directions": set(),
                }
            else:
                # 取最新票，保留最大衰减
                existing = strategy_decayed_votes[v.strategy]
                if v.timestamp > existing["latest_vote"].timestamp:
                    existing["latest_vote"] = v
                    existing["decay"] = decay
            strategy_decayed_votes[v.strategy]["all_directions"].add(v.action.value)

        # 构建去重后的 vote 列表 (带衰减因子)
        unique_votes = []
        time_decay_map = {}  # strategy -> decay_factor
        for strategy, data in strategy_decayed_votes.items():
            unique_votes.append(data["latest_vote"])
            time_decay_map[strategy] = data["decay"]

        # -------------------------------------------------------
        # 信号一致性检查：策略是否在窗口内频繁反转
        # -------------------------------------------------------
        flip_count = 0
        strategy_directions: Dict[str, set] = defaultdict(set)
        for v in votes:
            logical_strat = v.strategy
            strategy_directions[logical_strat].add(v.action.value)
        for strat, directions in strategy_directions.items():
            if len(directions) > 1:
                flip_count += 1
        total_strategies = len(strategy_directions)
        # P1 优化: 放宽混乱阈值至 70% (vol_factor 已有冷却，减少误判)
        # CHAOS_THRESHOLD: SSOT strategy.consensus.chaos_threshold
        if total_strategies > 0 and flip_count / total_strategies > CHAOS_THRESHOLD:
            logger.info(
                f"[共识] {code} ⚠️ 信号混乱（{flip_count}/{total_strategies} 策略反转），放弃本窗口"
            )
            return

        regime, adx_val = self._get_market_regime(code)

        # ── P1 优化: 时段参数 ──
        sess_win_mult, sess_thr_mult = self._get_session_params()

        # 加权评分 (带时间衰减)
        score = 0.0
        weight_details = {}
        bullish_votes = []
        bearish_votes = []

        for v in unique_votes:
            w, w_details = self._calc_weight(v.strategy, regime, code)
            conviction = self._get_vote_conviction(v)
            decay = time_decay_map.get(v.strategy, 1.0)
            adjusted_w = w * conviction * decay
            weight_details[v.strategy] = round(adjusted_w, 2)

            if v.action == SignalAction.BUY:
                score += adjusted_w
                bullish_votes.append(v)
            elif v.action == SignalAction.SELL:
                score -= adjusted_w
                bearish_votes.append(v)
            # ALERT 不计分

        # ── P1 优化: 因子族共线性惩罚 ──
        score = self._apply_factor_family_cap(unique_votes, weight_details, score)

        # ── Phase 2a (2026-05-21): 因子组合黑名单 ──
        score = self._check_factor_blacklist(unique_votes, score, code)

        # Phase 2: 分层共识计算 (结构共识 vs 事件共识)
        structural_votes = []
        event_votes = []
        for v in unique_votes:
            if getattr(v.signal, 'source', SignalSource.TECHNICAL) == SignalSource.EVENT:
                event_votes.append(v)
            else:
                structural_votes.append(v)

        # --- 结构共识评分 ---
        structural_score = 0.0
        structural_bullish = []
        structural_bearish = []
        structural_strategies = []

        for v in structural_votes:
            w, _ = self._calc_weight(v.strategy, regime, code)
            conviction = self._get_vote_conviction(v)
            decay = time_decay_map.get(v.strategy, 1.0)
            cw = w * conviction * decay
            if v.action == SignalAction.BUY:
                structural_score += cw
                structural_bullish.append(v)
            elif v.action == SignalAction.SELL:
                structural_score -= cw
                structural_bearish.append(v)
            structural_strategies.append(v.strategy)

        # --- 事件共识评分 (轻量级) ---
        event_score = 0.0
        event_strategies = []
        if event_votes:
            event_score = (len([v for v in event_votes if v.action == SignalAction.BUY]) -
                           len([v for v in event_votes if v.action == SignalAction.SELL])) * 0.55
            event_strategies = [v.strategy for v in event_votes]

        # --- 融合逻辑 ---
        fusion_score = structural_score
        is_conflict = False
        conflict_details = ""

        if event_score != 0:
            tech_dir = 1 if structural_score >= 0 else -1
            evt_dir = 1 if event_score >= 0 else -1

            if structural_score != 0 and tech_dir != evt_dir:
                is_conflict = True
                conflict_details = f"结构分={structural_score:+.2f} vs 事件分={event_score:+.2f}"
                fusion_score *= 0.8
                logger.info(f"[共识/分层] {code} ⚠️ 结构与事件冲突：{conflict_details}")
            else:
                fusion_score += event_score * 0.3

        score = fusion_score

        # ── P1 优化: 记录分数到滚动历史 ──
        self._score_history.append(abs(score))

        adx_str = f"ADX={adx_val:.1f}" if adx_val else "ADX=N/A"
        session_label = "开盘" if sess_thr_mult > 1.0 else "收盘" if sess_thr_mult < 1.0 else "常规"
        logger.info(
            f"[共识] {code} 评估完毕 | 市场={regime}({adx_str}) | 时段={session_label} | "
            f"融合分={score:+.2f} (技术分={structural_score:+.2f}, 事件分={event_score:+.2f}) | "
            f"权重={weight_details}"
        )

        # 统计同向票数
        bullish_count = sum(1 for v in unique_votes if v.action == SignalAction.BUY)
        bearish_count = sum(1 for v in unique_votes if v.action == SignalAction.SELL)

        # 策略分歧度度量
        divergence = 0.0
        if bullish_count > 0 and bearish_count > 0:
            divergence = min(bullish_count, bearish_count) / max(bullish_count, bearish_count)
            div_threshold = float(DIVERGENCE_THRESHOLD)
            if divergence >= div_threshold:
                decay_factor = float(DIVERGENCE_SCORE_DECAY)
                logger.info(
                    f"[共识/分歧度] {code} BUY={bullish_count} vs SELL={bearish_count} "
                    f"分歧度={divergence:.2f} >= {div_threshold}, "
                    f"score {score:+.2f} 衰减至 {score * (1 - divergence * decay_factor):+.2f}"
                )
                score *= (1 - divergence * decay_factor)
                if DIVERGENCE_GRAY_ZONE:
                    from app.scenario.classifier import ScenarioType
                    self._scenario_cache[code] = ScenarioType.GRAY_ZONE

        MIN_AGREE_VOTES = 1

        # ── P1 优化: 动态阈值 (HealingGate + Adaptive + Session) ──
        adaptive_thr = self._compute_adaptive_threshold()
        dynamic_threshold = adaptive_thr * sess_thr_mult

        # Healing Gate: 自愈系统恢复中提高门槛 (叠加在自适应+时段之上)
        try:
            from app.self_healing.health_gate import get_health_gate
            constraints = get_health_gate().get_trading_constraints()
            if constraints["status"] == "RECOVERING":
                dynamic_threshold *= (1.0 / constraints.get("position_size_multiplier", 0.5))
                logger.warning(f"[HealingGate] 系统恢复中: 阈值提升至 {dynamic_threshold:.2f}")
        except Exception:
            pass

        # 检查同向票数
        if score >= dynamic_threshold:
            if bullish_count < MIN_AGREE_VOTES:
                logger.info(
                    f"[同向票数不足] {code} score={score:.2f}>=阈值{dynamic_threshold:.2f} "
                    f"但同向票数{bullish_count}<{MIN_AGREE_VOTES}，跳过"
                )
                score = 0

        if score <= -dynamic_threshold:
            if bearish_count < MIN_AGREE_VOTES:
                logger.info(
                    f"[同向票数不足] {code} score={score:.2f}<=负阈值{dynamic_threshold:.2f} "
                    f"但同向票数{bearish_count}<{MIN_AGREE_VOTES}，跳过"
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

        if score >= dynamic_threshold:
            # 检查策略数量
            if not check_min_strategies(bullish_votes, "BUY"):
                pass  # 不触发信号
            else:
                avg_price = sum(v.price for v in bullish_votes) / len(bullish_votes) if bullish_votes else votes[0].price
                
                # 获取资金流向情绪（BUY 方向：流入为正增益，与 SELL 对称）
                capital_sentiment = self._get_capital_flow_sentiment(code)
                
                # P1-1: 修复置信度公式 - 简化为加权平均（技术/情绪 0.5/0.5）
                # 技术项(50%): 分数项 + 共识项的线性组合
                # 分数项: score/threshold 线性映射，score=阈值时=0.50，score=2×阈值时=1.0
                score_part = min(1.0, abs(score) / self.threshold) * 0.50
                # 共识项: 同向票数/全部投票数（动态分母）
                unique_votes_buy = bullish_votes + bearish_votes
                consensus_part = min(1.0, len(bullish_votes) / max(1, len(unique_votes_buy))) * 0.25
                # 技术合计 = 分数项 + 共识项 (50% + 25% = 75%)
                # 按 0.5/0.5 比例重新归一化：技术权重 = 0.5
                technical_confidence = score_part + consensus_part  # 0.0 ~ 0.75
                # 归一化技术项到 0.0 ~ 1.0 (除以 0.75)
                # 2026-04-22: 单策略天花板从 0.5→0.7, 配合权重调整突破 0.55
                technical_part = min(1.0, technical_confidence / 0.75) * 0.7

                # P8: 因子相关性折扣 — confluence_v2内部相关因子缩减技术置信度
                _corr_discount = 1.0
                for _v in bullish_votes:
                    if _v.strategy == "confluence_v2":
                        _extra = getattr(_v.signal, 'extra', {}) or {}
                        _corr_discount = _extra.get("avg_correlation_discount", 1.0)
                        break
                technical_part *= _corr_discount

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
                # === LLM 市场研判置信度调节（2026-04-11 新增）===
                llm_part = 0.0
                _buy_llm_assessment = None
                _buy_llm_alignment = 0.0
                _buy_llm_align_factor = 1.0
                if self._llm_consensus_enabled and self._llm_analyst:
                    try:
                        # 【修复 2026-04-11】使用 SCHEDULED 触发类型以匹配调度器预分析的缓存键
                        llm_assessment = self._llm_analyst.get_cached_assessment(code, trigger_type="SCHEDULED")
                        if llm_assessment and llm_assessment.is_valid():
                            llm_confidence = llm_assessment.confidence_value
                            direction_score = llm_assessment.direction_score  # -1.0 ~ 1.0
                            # BUY 方向：direction_score 正值表示一致
                            direction_alignment = direction_score
                            # LLM 贡献 = 基础置信度 × (1 + 方向一致性调节)
                            # 一致时最高 1.3x，冲突时最低 0.5x
                            alignment_factor = 1.0 + direction_alignment * 0.3
                            alignment_factor = max(0.5, min(1.3, alignment_factor))
                            llm_part = llm_confidence * alignment_factor
                            _buy_llm_assessment = llm_assessment
                            _buy_llm_alignment = direction_alignment
                            _buy_llm_align_factor = alignment_factor
                            logger.debug(
                                f"LLM置信度调节(BUY): {code} llm_conf={llm_confidence:.2f} "
                                f"align={direction_alignment:.2f} factor={alignment_factor:.2f} "
                                f"llm_part={llm_part:.3f}"
                            )
                    except Exception as _llm_e:
                        logger.warning(f"LLM置信度调节异常(降级跳过): {_llm_e}")
                        llm_part = 0.0

                if llm_part > 0 and self._llm_consensus_enabled:
                    # 有 LLM 数据时：原有三项等比缩减，腾出 llm_weight
                    w_llm = self._llm_consensus_weight  # 0.15
                    w_remaining = 1.0 - w_llm
                    # 原比例 tech:sent:scen = 0.4:0.3:0.3，等比缩放
                    w_tech = 0.4 * w_remaining   # ~0.34
                    w_sent = 0.3 * w_remaining   # ~0.255
                    w_scen = 0.3 * w_remaining   # ~0.255
                    base_confidence = (
                        technical_part * w_tech +
                        sentiment_part * w_sent +
                        scenario_confidence * w_scen +
                        llm_part * w_llm
                    )
                else:
                    # 2026-04-22 优化: sentiment_part 始终为0 (capital flow 不可用)
                    # 将 30% 权重重新分配给 technical_part (0.4→0.7)
                    base_confidence = (
                        technical_part * 0.7 +
                        sentiment_part * 0.0 +
                        scenario_confidence * 0.3
                    )
                confidence = min(1.0, base_confidence)

                logger.info(
                    f"[共识] {code} BUY 置信度计算 | 技术项={technical_part:.2f} | "
                    f"情绪项={sentiment_part:.2f} | 场景项={scenario_confidence:.2f} | "
                    f"资金流={capital_sentiment:.2f} | LLM项={llm_part:.3f} | 最终={confidence:.2f}"
                )

                
                # 2026-04-09 修复: 添加 scenario 字段 (之前缺失导致 TypeError)
                scenario_label = ""
                if scenario_obj:
                    scenario_label = getattr(scenario_obj, 'value', str(scenario_obj))

                _buy_trace_id = f"TRC-{uuid.uuid4().hex[:12].upper()}"

                # 记录 LLM 审计（2026-04-11 新增：开盘套利阶段链路记录）
                if _buy_llm_assessment is not None:
                    self._record_llm_audit(
                        trace_id=_buy_trace_id,
                        code=code,
                        action="BUY",
                        llm_assessment=_buy_llm_assessment,
                        direction_alignment=_buy_llm_alignment,
                        alignment_factor=_buy_llm_align_factor,
                        llm_part=llm_part,
                    )

                _buy_extra: Optional[Dict[str, Any]] = None
                if _buy_trace_id in self._llm_audit_log:
                    _buy_extra = {"llm_assessment": self._llm_audit_log[_buy_trace_id]}
                
                # Phase 2: 判定信号来源
                tech_buy_count = len([v for v in structural_votes if v.action == SignalAction.BUY])
                event_buy_count = len([v for v in event_votes if v.action == SignalAction.BUY])
                if tech_buy_count > 0 and event_buy_count > 0:
                    signal_source = "HYBRID"
                elif tech_buy_count > 0:
                    signal_source = "TECHNICAL"
                else:
                    signal_source = "EVENT"

                result = ConsensusResult(
                    confidence=round(confidence, 2),
                    code=code,
                    action=SignalAction.BUY,
                    price=avg_price,
                    score=round(score, 2),
                    threshold=self.threshold,
                    market_regime=regime,
                    scenario=scenario_label,
                    adx_value=round(adx_val, 1) if adx_val else None,
                    agree_strategies=[v.strategy for v in bullish_votes],
                    oppose_strategies=[v.strategy for v in bearish_votes],
                    weight_details=weight_details,
                    reasons=[v.reason for v in bullish_votes],
                    votes=bullish_votes,
                    trace_id=_buy_trace_id,
                    divergence=round(divergence, 2),
                    source=signal_source,
                    extra=_buy_extra,
                )

        elif score <= -dynamic_threshold:
            # 检查策略数量
            if not check_min_strategies(bearish_votes, "SELL"):
                pass  # 不触发信号
            else:
                avg_price = sum(v.price for v in bearish_votes) / len(bearish_votes) if bearish_votes else votes[0].price
                
                # 获取资金流向情绪
                capital_sentiment = self._get_capital_flow_sentiment(code)
                
                # P1-1: 修复置信度公式（SELL 对称版本 - 简化为加权平均）
                # 技术项: 分数项 + 共识项 (与BUY对称)
                score_part = min(1.0, abs(score) / self.threshold) * 0.45
                unique_votes_sell = bearish_votes + bullish_votes
                consensus_part = min(1.0, len(bearish_votes) / max(1, len(unique_votes_sell))) * 0.25
                technical_confidence = score_part + consensus_part
                technical_part = min(1.0, technical_confidence / 0.7) * 0.7

                # P8: 因子相关性折扣 — confluence_v2内部相关因子缩减技术置信度
                _corr_discount = 1.0
                for _v in bearish_votes:
                    if _v.strategy == "confluence_v2":
                        _extra = getattr(_v.signal, 'extra', {}) or {}
                        _corr_discount = _extra.get("avg_correlation_discount", 1.0)
                        break
                technical_part *= _corr_discount

                # 情绪项: SELL + 负情绪 = 加分
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
                # === LLM 市场研判置信度调节（2026-04-11 新增）===
                llm_part = 0.0
                _sell_llm_assessment = None
                _sell_llm_alignment = 0.0
                _sell_llm_align_factor = 1.0
                if self._llm_consensus_enabled and self._llm_analyst:
                    try:
                        # 【修复 2026-04-11】使用 SCHEDULED 触发类型以匹配调度器预分析的缓存键
                        llm_assessment = self._llm_analyst.get_cached_assessment(code, trigger_type="SCHEDULED")
                        if llm_assessment and llm_assessment.is_valid():
                            llm_confidence = llm_assessment.confidence_value
                            direction_score = llm_assessment.direction_score  # -1.0 ~ 1.0
                            # SELL 方向：direction_score 负值表示一致，取反
                            direction_alignment = -direction_score
                            # LLM 贡献 = 基础置信度 × (1 + 方向一致性调节)
                            # 一致时最高 1.3x，冲突时最低 0.5x
                            alignment_factor = 1.0 + direction_alignment * 0.3
                            alignment_factor = max(0.5, min(1.3, alignment_factor))
                            llm_part = llm_confidence * alignment_factor
                            _sell_llm_assessment = llm_assessment
                            _sell_llm_alignment = direction_alignment
                            _sell_llm_align_factor = alignment_factor
                            logger.debug(
                                f"LLM置信度调节(SELL): {code} llm_conf={llm_confidence:.2f} "
                                f"align={direction_alignment:.2f} factor={alignment_factor:.2f} "
                                f"llm_part={llm_part:.3f}"
                            )
                    except Exception as _llm_e:
                        logger.warning(f"LLM置信度调节异常(降级跳过): {_llm_e}")
                        llm_part = 0.0

                if llm_part > 0 and self._llm_consensus_enabled:
                    # 有 LLM 数据时：原有三项等比缩减，腾出 llm_weight
                    w_llm = self._llm_consensus_weight  # 0.15
                    w_remaining = 1.0 - w_llm
                    # 原比例 tech:sent:scen = 0.4:0.3:0.3，等比缩放
                    w_tech = 0.4 * w_remaining   # ~0.34
                    w_sent = 0.3 * w_remaining   # ~0.255
                    w_scen = 0.3 * w_remaining   # ~0.255
                    base_confidence = (
                        technical_part * w_tech +
                        sentiment_part * w_sent +
                        scenario_confidence * w_scen +
                        llm_part * w_llm
                    )
                else:
                    # 2026-04-22 优化: sentiment_part 始终为0 (capital flow 不可用)
                    # 将 30% 权重重新分配给 technical_part (0.4→0.7)
                    base_confidence = (
                        technical_part * 0.7 +
                        sentiment_part * 0.0 +
                        scenario_confidence * 0.3
                    )
                confidence = min(1.0, base_confidence)

                logger.info(
                    f"[共识] {code} SELL 置信度计算 | 技术项={technical_part:.2f} | "
                    f"情绪项={sentiment_part:.2f} | 场景项={scenario_confidence:.2f} | "
                    f"资金流={capital_sentiment:.2f} | LLM项={llm_part:.3f} | 最终={confidence:.2f}"
                )

                # 2026-04-09 修复: 添加 scenario 字段 (之前缺失导致 TypeError)
                scenario_label_sell = ""
                scenario_obj_sell = self._scenario_cache.get(code)
                if scenario_obj_sell:
                    scenario_label_sell = getattr(scenario_obj_sell, 'value', str(scenario_obj_sell))

                _sell_trace_id = f"TRC-{uuid.uuid4().hex[:12].upper()}"

                # 记录 LLM 审计（2026-04-11 新增：开盘套利阶段链路记录）
                if _sell_llm_assessment is not None:
                    self._record_llm_audit(
                        trace_id=_sell_trace_id,
                        code=code,
                        action="SELL",
                        llm_assessment=_sell_llm_assessment,
                        direction_alignment=_sell_llm_alignment,
                        alignment_factor=_sell_llm_align_factor,
                        llm_part=llm_part,
                    )

                _sell_extra: Optional[Dict[str, Any]] = None
                if _sell_trace_id in self._llm_audit_log:
                    _sell_extra = {"llm_assessment": self._llm_audit_log[_sell_trace_id]}

                # Phase 2: 判定信号来源 (SELL)
                tech_sell_count = len([v for v in structural_votes if v.action == SignalAction.SELL])
                event_sell_count = len([v for v in event_votes if v.action == SignalAction.SELL])
                if tech_sell_count > 0 and event_sell_count > 0:
                    signal_source = "HYBRID"
                elif tech_sell_count > 0:
                    signal_source = "TECHNICAL"
                else:
                    signal_source = "EVENT"

                result = ConsensusResult(
                    confidence=round(confidence, 2),
                    code=code,
                    action=SignalAction.SELL,
                    price=avg_price,
                    score=round(score, 2),
                    threshold=self.threshold,
                    market_regime=regime,
                    scenario=scenario_label_sell,
                    adx_value=round(adx_val, 1) if adx_val else None,
                    agree_strategies=[v.strategy for v in bearish_votes],
                    oppose_strategies=[v.strategy for v in bullish_votes],
                    weight_details=weight_details,
                    reasons=[v.reason for v in bearish_votes],
                    votes=bearish_votes,
                    trace_id=_sell_trace_id,
                    divergence=round(divergence, 2),
                    source=signal_source,
                    extra=_sell_extra,
                )

        if result:
            # ============================================================
            # 置信度调整器 - 统一管理（2026-04-09 新增）
            # 用加法替代乘法，避免指数衰减
            # ============================================================
            from app.strategy.confidence_adjuster import ConfidenceAdjuster
            adjuster = ConfidenceAdjuster(result.confidence)
            
            # 多周期反向降权
            daily_trend = self._daily_trend_cache.get(code, "neutral")
            if daily_trend != "neutral":
                is_counter_trend = (
                    (result.action == SignalAction.BUY and daily_trend == "down") or
                    (result.action == SignalAction.SELL and daily_trend == "up")
                )
                if is_counter_trend:
                    adjuster.apply_preset(
                        "counter_trend",
                        f"短期 {result.action.value} 与日线 {daily_trend} 反向"
                    )

            # 同方向重复信号降权
            action_str = result.action.value
            last_action_info = self._last_signal_action.get(code)
            if last_action_info:
                last_action, last_ts = last_action_info
                if last_action == action_str and (time.time() - last_ts) < 300:
                    adjuster.apply_preset(
                        "duplicate_signal",
                        f"300s内同方向({action_str})重复信号"
                    )
            self._last_signal_action[code] = (action_str, time.time())

            # 信号质量降权
            quality = self._quality_score.get(code, 1.0)
            if quality < 0.4:
                logger.info(
                    f"[信号质量] {code} 质量分={quality:.2f} < 0.4，拒绝低质量信号"
                )
                return
            if quality < 0.5:
                adjuster.apply_preset(
                    "quality_very_low",
                    f"质量分={quality:.2f}"
                )
            elif quality < 1.0:
                # 用质量分比例调整 score（不改变方向，只影响最终权重感知）
                adjusted_score = round(result.score * quality, 2)
                result = dataclasses.replace(result, score=adjusted_score)
                adjuster.apply_preset(
                    "quality_low",
                    f"质量分={quality:.2f}，分数调整 {result.score:.2f}→{adjusted_score:.2f}"
                )

            # 应用最终置信度
            conf_result = adjuster.get_result()
            result = dataclasses.replace(result, confidence=conf_result.final_confidence)
            
            # 记录审计日志
            if conf_result.adjustments:
                logger.info(
                    f"[置信度调整] {code} raw={conf_result.raw_confidence:.3f} → "
                    f"final={conf_result.final_confidence:.3f} | "
                    f"调整: {', '.join(f'{a.name}({a.value:+.3f})' for a in conf_result.adjustments)}"
                )

            # ============================================================
            # 统一信号质量评分（2026-04-11 Task#15 新增）
            # 在置信度调整完成后计算，将结果附加到 ConsensusResult
            # ============================================================
            try:
                from app.signal.quality import get_default_scorer
                _scorer = get_default_scorer()
                _news_factor = self._get_news_factor(code)
                _quality_ctx = {
                    'consensus_result': result,
                    'mtf_cache': self._mtf_alignment_cache.get(code),
                    'news_factor': _news_factor,
                    'quality_score': self._quality_score.get(code),
                }
                _quality_result = _scorer.compute(_quality_ctx)
                result = dataclasses.replace(result, quality_result=_quality_result)
                logger.info(
                    f"[信号质量] {code} 等级={_quality_result.tier} "
                    f"综合分={_quality_result.composite:.3f} "
                    f"仓位系数={_quality_result.position_factor}"
                )
            except Exception as _qe:
                logger.debug(f"[信号质量] {code} 评分失败，不影响主流程: {_qe}")

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

                # AI 否决层观察 (2026-05-10 新增 — 模拟盘异步观察模式)
                self._run_veto_observer_async(code, result)

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
                f"LLM乘数={enhanced.llm_signal_impact_factor:.2f} | "
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
                "llm_signal_impact_factor": enhanced.llm_signal_impact_factor,
                "news_age_minutes": enhanced.news_age_minutes,
                "volatility_state": enhanced.volatility_state,
            })

            # P0 修复 (2026-05-17): 将 LLM 增强后的 final_score 写回 score
            # 确保 data_subscriber 构造 Signal(score=result.score) 时使用的是增强后的分数
            if hasattr(result, 'score'):
                result.score = enhanced.final_score

            return result

        except Exception as e:
            logger.debug(f"信号增强失败：{e}")
            return result  # 返回原始结果

    def _run_veto_observer_async(self, code: str, result) -> None:
        """
        AI 否决层观察器 (2026-05-10 新增 — 模拟盘异步观察模式)
        
        异步运行，不阻塞主信号流程。
        记录否决判断到 data/veto_log.jsonl，供后续分析。
        """
        try:
            # 延迟初始化
            if self._veto_observer is None:
                from app.risk.ai_veto_observer import AIVetoObserver
                from app.llm.agent_bridge import get_agent_bridge
                self._veto_observer = AIVetoObserver(agent_bridge=get_agent_bridge())
            
            # 构建市场上下文
            regime, adx = self._get_market_regime(code)
            market_ctx = {
                "vix": adx if adx else 20,  # 用 ADX 替代 VIX
                "trend": regime,
                "volatility": "high" if adx and adx > 30 else "normal",
            }
            
            # 构建信号信息
            signal = {
                "code": code,
                "direction": result.action.value if hasattr(result.action, 'value') else str(result.action),
                "strength": getattr(result, 'confidence', 0.5) * 100,
                "source": ",".join(result.agree_strategies) if hasattr(result, 'agree_strategies') else "unknown",
            }
            
            # 异步运行 (不阻塞)
            def _run_veto():
                try:
                    veto_result = self._veto_observer.evaluate(code, signal, market_ctx)
                    self._veto_observer.log(code, signal, veto_result)
                except Exception as e:
                    logger.debug(f"[VetoObserver] 异步评估失败: {e}")
            
            # 启动后台线程
            import threading
            t = threading.Thread(target=_run_veto, daemon=True, name=f"veto-{code}")
            t.start()
            
        except Exception as e:
            logger.debug(f"[VetoObserver] 初始化失败: {e}")

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
                    "adjusted": round(self._calc_weight(s, regime, code)[0], 2),
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

