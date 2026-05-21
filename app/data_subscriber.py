
def _get_us_exit_params():
    from app.risk.dynamic_exit_manager import dynamic_exit_manager
    return dynamic_exit_manager.get_market_params('us')
"""
数据订阅器
对接 futu-api 订阅实时行情，分发给策略引擎
信号经过「加权共识引擎」评分，多策略加权确认后才推送
"""
import os
import sys
import dataclasses
import logging as stdlib_logging

# 必须在导入 futu 之前设置日志环境变量和配置
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"
os.environ["FUTU_LOG_PATH"] = "/tmp/futu_logs"

# 仅配置 futu 日志，不影响应用日志
futu_logger = stdlib_logging.getLogger("futu")
futu_logger.setLevel(stdlib_logging.CRITICAL)
# 清除 futu 日志的所有 handler，防止文件锁
futu_logger.handlers = []
futu_logger.propagate = False

import logging
import threading
import time
import queue
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Tuple, Optional, Any

from app import config
from config.config_center import get_config_center as _gcc_dsub
from app.services.trading_state_manager import get_trading_state_manager
from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.indicators import IndicatorBuffer, adx as calc_adx
from app.signal_store import signal_store
from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.consensus_engine import ConsensusEngine, ConsensusResult
# Phase 17 修复: 使用 consensus/adaptive_weights.py 版本 (engine_ref 构造函数)
# 而非 services/adaptive_weight_manager.py 版本 (base_weights 构造函数)
from app.strategy.consensus.adaptive_weights import AdaptiveWeightManager
from app.trader.executor import TradeExecutor
from app.trader.position_manager import PositionManager
from app.strategy.hk_specific import get_hk_adapter, HKMarketConfig
from app.risk.regime_params_loader import params_loader as regime_params_loader
from app.data.hsi_data import hsi_fetcher
from app.storage.signal_tracker import signal_tracker
from app.risk.circuit_breaker import CircuitBreaker  # 新增: 熔断器
from app.analytics.trade_evaluation import get_evaluator  # 新增: 交易评估器
from app.core.domain_events import get_event_bus, DomainEvent, EventType, TradeOpenedPayload, TradeClosedPayload

# ============================================================
#  Harness Pilot Phase (2026-05-02): 提取的多周期引擎 + 动态止损引擎
# ============================================================
from app.data_subscriber_engines.multi_timeframe import (
    MultiTimeframeEngine,
    MTFConfig,
    TREND_TO_ENUM,
    TREND_FILTER_SYNC,
    PERIOD_WEIGHTS_ALIGNMENT,
    MTF_WARMUP_SECONDS,
    MTF_DEFAULT_THRESHOLD,
)
from app.data_subscriber_engines.dynamic_stop_params import DynamicStopEngine, StopConfig

# ============================================================
#  P0 God Class 拆分 (2026-05-21): 统一缓存管理器
# ============================================================
from app.data_subscriber_engines.cache_manager import CacheManager

logger = logging.getLogger("ftseek.subscriber")
# 2026-04-21 修复: 移除 propagate=False，允许日志传播到 ftseek 父级 logger
# 之前 propagate=False 导致所有 INFO 日志被静默丢弃（日志黑洞）
logger.propagate = True

# 推送冷却 (同标的达成共识后冷却) - 支持动态更新
SIGNAL_COOLDOWN = int(_gcc_dsub().get('strategy.cooldown.signal_cooldown', 120))

# 共识引擎参数 (2026-05-20: 修复 key 路径 window → window_seconds 对齐 YAML)
CONSENSUS_WINDOW    = int(_gcc_dsub().get('strategy.consensus.window_seconds', 90))
CONSENSUS_THRESHOLD = float(_gcc_dsub().get('strategy.consensus.threshold', 1.5))  # 2026-05-19: 提高共识阈值 (原1.0)
CONSENSUS_ADX_TREND = float(_gcc_dsub().get('strategy.consensus.adx_trend', 25))
CONSENSUS_ADX_RANGE = float(_gcc_dsub().get('strategy.consensus.adx_range', 20))

# ============================================================
#  配置读取辅助 (P1-2 2026-04-28)
# ============================================================
_dsub_cfg: Dict[str, Any] = {}

def _dsub(key: str, default: Any) -> Any:
    """读取 data_subscriber 硬编码阈值配置"""
    if key not in _dsub_cfg:
        try:
            from config.config_center import get_config_center
            _dsub_cfg[key] = get_config_center().get(f"hardcoded_thresholds.data_subscriber.{key}", default)
        except Exception:
            _dsub_cfg[key] = default
    return _dsub_cfg[key]

# ADX 计算周期 (已从配置中心读取，模块级保留 fallback)
ADX_PERIOD = int(_dsub("adx_period", 14))


class DataSubscriber:
    """富途行情订阅管理器 (加权共识模式)"""

    def __init__(self, strategies: List[BaseStrategy], notifier: DingTalkNotifier, 
                 llm_analyst=None, trade_executor=None):
        self.strategies = strategies
        self.notifier = notifier
        self._quote_ctx = None
        self._running = False
        self._subscribed = False  # 动态订阅状态标记 (True=已订阅, False=已退订)

        # 【2026-04-21 修复】接受 llm_analyst 和 trade_executor 参数
        # llm_analyst: LLM市场研判引擎 (由 main.py 注入)
        # trade_executor: 统一交易执行器 (消除双实例导致的风控状态不一致)
        self._llm_analyst = llm_analyst

        # 【2026-05-02 Phase P0】Shadow Mode for market_hours.py validation
        # Runs new shared market_hours logic alongside existing _is_trading_hours(),
        # logs discrepancies but always returns OLD result (production-safe).
        # Config flag: market_hours.shadow_mode (default: False)
        self._market_hours_shadow = None
        self._init_market_hours_shadow()

        # P2 状态管理器 (2026-05-19): 集中管理 15+ 个状态字典
        self._state_manager = get_trading_state_manager()
        logger.info("[TradingStateManager] 已注入到 DataSubscriber")

        # 推送冷却: code -> last_push_time
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()

        # 2026-03-27 新增: 止损冷却机制，防止频繁触发
        # 止损冷却: code -> last_stop_loss_time
        self._stop_loss_cooldown: Dict[str, float] = {}
        # 止损失败计数: code -> int（用于递增冷却）
        self._stop_loss_fail_count: Dict[str, int] = {}

        # 追踪止盈缓存: code -> 追踪止损价（Task2 新增）
        self._trailing_stop_cache: Dict[str, float] = {}
        # 追踪止盈触发时间缓存: code -> timestamp（用于重开仓冷却判断）
        self._trailing_exit_time: Dict[str, float] = {}

        # code 级别交易锁：防止同一标的 _on_consensus 并发执行（Task3）
        self._trade_locks: Dict[str, threading.Lock] = {}

        # 开仓原子锁: code -> timestamp - 防止同一标的在同一秒内重复开仓
        # 2026-03-25 修复：解决 quote+kline handler 同时触发导致重复开仓的问题
        self._open_position_ts: Dict[str, float] = {}  # code -> last open timestamp
        self._open_lock = threading.Lock()  # 保护 _open_position_ts 的写入

        # 持仓期间峰值价格缓存: code -> peak_price（Task2 移动止损使用）
        self._peak_price_cache: Dict[str, float] = {}

        # Flash Exit 闪崩防御 (P1 2026-05-06)
        # 记录每个标的上一次快照: {code: (price, timestamp)}
        self._flash_snapshots: Dict[str, Tuple[float, float]] = {}
        self._flash_exit_enabled: bool = True
        self._flash_atr_mult: float = 1.0       # ATR 倍数阈值
        self._flash_window_seconds: float = 60.0  # 检测窗口

        # Pre-Close Exit 盘末退出 (P1 2026-05-06)
        self._pre_close_exit_enabled: bool = True
        self._pre_close_window_minutes: int = 15  # 距收盘触发窗口

        # 加载退出增强配置 (P1 2026-05-06)
        self._load_exit_enhance_config()

        # 软止损预警冷却字典 (2026-04-07 新增)
        # 结构：{code: last_warning_timestamp} - 防止每秒多次预警
        # P0 2026-05-21: 迁移至 KlineProcessor，此处保留向后兼容代理
        self.soft_stop_warnings: Dict[str, float] = {}
        
        # P0 2026-05-21: KlineProcessor (延迟初始化，首次使用时创建)
        self._kline_processor = None

        # 行情心跳：记录最后一次收到行情数据的时间（用于断线检测）
        self._last_quote_time: float = time.time()
        self._reconnect_lock = threading.Lock()

        # K 线缓冲: 为每个 code 维护滑动窗口, 用于计算 ADX
        self._kline_buffers: Dict[str, IndicatorBuffer] = defaultdict(lambda: IndicatorBuffer(maxlen=200))

        # 【P0 2026-04-21 修复】异步行情处理队列
        # 回调线程（futu SDK CallbackExecutor）中不能做耗时操作（DB查询/交易执行/策略计算），
        # 否则会导致回调线程阻塞，后续所有行情回调堆积在队列中无法消费。
        # 修复方案：handler 只更新心跳 + 入队，由独立工作线程处理重型逻辑。
        self._quote_queue = queue.Queue(maxsize=50)     # P0 2026-05-04: Per-code 去重后降至标的数
        # P0 2026-05-21: 双队列优先机制 — 持仓股优先处理（止损/止盈安全）
        self._priority_kline_queue = queue.Queue(maxsize=500)  # 持仓股 K线，优先处理
        self._kline_queue = queue.Queue(maxsize=1000)   # P0 2026-05-21: 非持仓股 K线，从 200 提升至 1000
        self._quote_worker: Optional[threading.Thread] = None
        # P0 2026-05-04: Per-code 报价去重 — 队列只存 code, 数据在 dict
        self._latest_quotes: Dict[str, dict] = {}
        self._pending_codes: set = set()
        self._quote_lock = threading.Lock()
        self._kline_worker: Optional[threading.Thread] = None
        # P0 2026-05-20: Per-(code,k_type) K线去重 — 队列只存 key, 数据在 dict
        # 同一 (code, k_type) 多次推送只保留最新值，队列中不重复入队
        self._latest_klines: Dict[Tuple[str, str], dict] = {}
        self._pending_kline_keys: set = set()
        self._kline_lock = threading.Lock()
        self._worker_running = False

        # 多周期趋势缓存 (2026-03-25 新增)
        # 结构: {code: {"K_1M": trend, "K_3M": trend, ...}}
        # trend: "up" / "down" / "neutral"
        self._multi_timeframe_trends: Dict[str, Dict[str, str]] = defaultdict(lambda: {
            "K_1M": "neutral",
            "K_3M": "neutral",
            "K_5M": "neutral",
            "K_15M": "neutral", 
            "K_30M": "neutral",
            "K_60M": "neutral",
            "K_DAY": "neutral"
        })
        # 多周期 K 线缓存 (每个周期独立)
        self._kline_buffers_by_period: Dict[str, Dict[str, IndicatorBuffer]] = defaultdict(lambda: {
            "K_1M": IndicatorBuffer(maxlen=100),
            "K_3M": IndicatorBuffer(maxlen=100),
            "K_5M": IndicatorBuffer(maxlen=100),
            "K_15M": IndicatorBuffer(maxlen=100),
            "K_30M": IndicatorBuffer(maxlen=100),
            "K_60M": IndicatorBuffer(maxlen=100),
            "K_DAY": IndicatorBuffer(maxlen=100),
        })

        # 多周期趋势更新追踪 (区分 "neutral=默认值" vs "neutral=计算后中性")
        # {(code, period)} — 至少成功更新过一次趋势的 period
        self._trends_updated: set = set()

        # MTF 冷启动宽限期截止时间 (epoch seconds)
        self._mtf_warmup_until: float = time.time() + MTF_WARMUP_SECONDS

        # 交易执行器和持仓管理器
        # 【2026-04-21 修复】优先使用注入的 trade_executor，避免双实例
        if trade_executor is not None:
            self.trade_executor = trade_executor
        else:
            self.trade_executor = TradeExecutor()
        self.position_manager = PositionManager()
        
        # P0 2026-05-21: 注册开仓回调 — 消除循环依赖
        # position_manager 开仓时通知 data_subscriber 订阅新标的
        self.position_manager.register_on_position_opened(self._on_position_opened_callback)

        # 【P0 修复 2026-05-04】注入共享 Trade Context 到 PositionSync，避免频繁创建/销毁连接
        try:
            from app.trader.position_sync import get_position_sync
            # 确保 TradeExecutor 的 context 已初始化
            if hasattr(self.trade_executor, '_get_trd_ctx'):
                hk_ctx = self.trade_executor._get_trd_ctx('HK')
                get_position_sync(market='HK', trade_context=hk_ctx)
                logger.info("[PositionSync] 已注入共享 HK Trade Context")
                
                us_ctx = self.trade_executor._get_trd_ctx('US')
                get_position_sync(market='US', trade_context=us_ctx)
                logger.info("[PositionSync] 已注入共享 US Trade Context")
        except Exception as e:
            logger.warning(f"[PositionSync] 注入共享 Trade Context 失败: {e}")

        # 如果有 LLM 分析师，注入到共识引擎
        if self._llm_analyst is not None:
            try:
                self._consensus.set_llm_analyst(self._llm_analyst)
                logger.info("LLM分析师已注入到共识引擎")
            except Exception as e:
                logger.warning(f"LLM分析师注入共识引擎失败: {e}")

        # 【新增 2026-04-09】分层策略管理器集成
        self.layered_manager = None
        try:
            from app.strategy.layered_integration import get_layered_strategy_manager
            self.layered_manager = get_layered_strategy_manager()
            # 注入K线缓存引用，解决技术增强层K线数据为空的问题
            self.layered_manager._kline_cache = self._kline_buffers_by_period
            logger.info("分层策略管理器已集成到数据订阅器 (K线缓存已注入)")
        except Exception as e:
            logger.warning(f"分层策略管理器集成失败：{e}")

        # 新闻采集器引用 (用于事件驱动层)
        self._news_collector = None
        try:
            from app.news_push.collector import get_collector as get_news_collector
            self._news_collector = get_news_collector()
            logger.info("新闻采集器已集成到数据订阅器")
        except Exception as e:
            logger.warning(f"新闻采集器集成失败：{e}")

        # 加权共识引擎
        self._consensus = ConsensusEngine(
            window_seconds=CONSENSUS_WINDOW,
            threshold=CONSENSUS_THRESHOLD,
            adx_trend=CONSENSUS_ADX_TREND,
            adx_range=CONSENSUS_ADX_RANGE,
            on_consensus=self._on_consensus,
            allow_short=False,  # 2026-04-08: 模拟盘禁止做空
        )

        # 自适应权重管理器（根据历史盈亏自动调整策略权重）
        self._adaptive_weight_manager = AdaptiveWeightManager(self._consensus)
        logger.info("自适应权重管理器已初始化")
        
        # 【新增 2026-04-02】新闻情绪缓存 - 避免频繁调用LLM API
        # 结构: {code: (sentiment_score, timestamp)}
        self._news_sentiment_cache: Dict[str, Tuple[float, float]] = {}
        self._news_cache_ttl = 1800  # 缓存有效期 30 分钟

        # P1-2: MarketRegimeDetector（7态市场状态检测，接入共识引擎）
        from app.strategy.market_regime import MarketRegimeDetector
        self._regime_detector = MarketRegimeDetector()
        logger.info("MarketRegimeDetector 已初始化")

        # Phase 1: 场景识别系统集成（2026-04-09）
        try:
            from app.scenario import get_scenario_classifier, get_scenario_router, get_scenario_memory
            self._scenario_classifier = get_scenario_classifier()
            self._scenario_router = get_scenario_router()
            self._scenario_memory = get_scenario_memory()
            logger.info("场景识别系统已集成 (Classifier/Router/Memory)")
            # 注入场景路由器到共识引擎
            self._consensus.set_scenario_router(self._scenario_router)
        except Exception as e:
            logger.warning(f"场景识别系统集成失败：{e}")
            self._scenario_classifier = None
            self._scenario_router = None
            self._scenario_memory = None
        
        # 【Harness Pilot 2026-05-02】初始化提取的多周期引擎 + 动态止损引擎
        self._mtf_engine = MultiTimeframeEngine()
        self._stop_engine = DynamicStopEngine()
        
        # 港股特性适配器 (2026-03-15 新增)
        self.hk_adapter = get_hk_adapter(HKMarketConfig(
            t0_trading=True,
            no_price_limit=True,
            short_bias_factor=1.2,
        ))
        logger.info("港股特性适配器已初始化")

        # 【新增 2026-04-06】质量管理器 - 巴菲特理念实现
        # 包含：质量评分器、持仓周期管理器、资金水位管理器、止盈止损管理器、估值计算器
        self._init_quality_managers()
        
        # 熔断器 (新增 2026-03-17)
        self.circuit_breaker = CircuitBreaker(
            reject_limit=8,       # 2026-04-22: 5→8 减少误触发
            pause_duration=300,   # 2026-04-22: 600→300 缩短熔断时间到5分钟
            drawdown_limit=0.15,  # 最大回撤 15%
        )
        logger.info("熔断器已初始化")
        
        # 市场状态参数加载器 (2026-03-17 新增)
        self.params_loader = regime_params_loader
        self.hsi_fetcher = hsi_fetcher
        self._current_regime = 'default'
        self._last_regime_check = 0
        logger.info("市场状态参数加载器已初始化")

        # ============================================================
        #  Phase 6: 注入 6 个聚焦引擎 (依赖倒置)
        #  目标：逐步将状态字典委托给引擎管理，最终瘦身为薄编排层
        # ============================================================
        self._init_engines()

    def _init_engines(self):
        """初始化 6 个聚焦引擎 (Phase 6)"""
        try:
            from app.engine import (
                ExitStateEngine, CooldownManager,
                MarketDataStore, IndicatorCache, FutuBridge
            )

            # Phase 6: 读取配置开关
            try:
                from config.config_center import get_config_center as _gcc
                self._exit_delegation_enabled = _gcc().get('engine.exit_delegation', False)
            except Exception:
                self._exit_delegation_enabled = False

            # FutuBridge: SDK 连接管理
            self._futu_bridge = FutuBridge(
                opend_host=config.OPEND_HOST,
                opend_port=config.OPEND_PORT,
                subscribe_types=_gcc_dsub().get('strategy.subscribe_types', ["QUOTE", "K_1M", "K_5M", "K_15M"]),
                heartbeat_timeout=180,
            )

            # MarketDataStore: 行情数据 CQRS
            self._market_data_store = MarketDataStore(
                kline_maxlen=200,
                mtf_warmup_seconds=300,
            )

            # ExitStateEngine: 止损/止盈/闪崩/追踪
            self._exit_engine = ExitStateEngine(
                trade_executor=self.trade_executor,
                position_manager=self.position_manager,
                notifier=self.notifier,
                consensus_engine=None,  # 稍后注入
            )

            # CooldownManager: 信号/止损/标的冷却
            self._cooldown_manager = CooldownManager()

            # IndicatorCache: ADX/ATR/峰值计算
            self._indicator_cache = IndicatorCache(
                market_data_store=self._market_data_store,
                consensus_engine=None,  # 稍后注入
                adx_period=ADX_PERIOD,
            )

            # 注入依赖
            self._futu_bridge.set_market_data_store(self._market_data_store)

            logger.info(
                f"[DataSubscriber] 6 个聚焦引擎已初始化: "
                f"FutuBridge, MarketDataStore, ExitStateEngine, "
                f"CooldownManager, IndicatorCache "
                f"| exit_delegation={self._exit_delegation_enabled} "
                f"| flash_exit_delegation={self._exit_engine is not None}"
            )

        except Exception as e:
            logger.warning(f"[DataSubscriber] 引擎初始化失败 (回退到原始模式): {e}")
            self._futu_bridge = None
            self._market_data_store = None
            self._exit_engine = None
            self._cooldown_manager = None
            self._indicator_cache = None

        # Phase 10A: 初始化 ExitExecutor 服务
        try:
            from app.services.exit_executor import ExitExecutor
            self._exit_executor = ExitExecutor(
                position_manager=self.position_manager,
                trade_executor=self.trade_executor,
                notifier=self.notifier,
                consensus_engine=self._consensus,
                scenario_cache=getattr(self, '_scenario_cache', {}),
            )
            logger.info("[DataSubscriber] ExitExecutor 服务已初始化")
        except Exception as e:
            logger.warning(f"[DataSubscriber] ExitExecutor 初始化失败: {e}")
            self._exit_executor = None
        # Phase 10B: 初始化 ConsensusExecutor 服务
        try:
            from app.services.consensus_executor import ConsensusExecutor
            self._consensus_executor = ConsensusExecutor(
                trade_executor=self.trade_executor,
                notifier=self.notifier,
                position_manager=self.position_manager,
                signal_tracker=signal_tracker,
                evaluator=get_evaluator(),
                circuit_breaker=self.circuit_breaker,
                adaptive_weight_manager=self._adaptive_weight_manager,
                consensus_engine=self._consensus,
                scenario_cache=getattr(self, '_scenario_cache', {}),
            )
            logger.info("[DataSubscriber] ConsensusExecutor 服务已初始化")
        except Exception as e:
            logger.warning(f"[DataSubscriber] ConsensusExecutor 初始化失败：{e}")
            self._consensus_executor = None

        # Phase 15: 初始化 KlineExitChecker 服务
        try:
            from app.services.kline_exit_checker import KlineExitChecker
            self._kline_exit_checker = KlineExitChecker(
                position_manager=self.position_manager,
                exit_executor=self._exit_executor,
                consensus_engine=self._consensus,
                peak_price_cache=self._peak_price_cache,
                trailing_stop_cache=self._trailing_stop_cache,
                soft_stop_warnings=self.soft_stop_warnings,
                kline_buffers=self._kline_buffers,
                get_atr_fn=self._get_atr,
            )
            logger.info("[DataSubscriber] KlineExitChecker 服务已初始化")
        except Exception as e:
            logger.warning(f"[DataSubscriber] KlineExitChecker 初始化失败：{e}")
            self._kline_exit_checker = None

        # Phase 16: 初始化 SubscriptionManager 服务
        try:
            from app.services.subscription_manager import SubscriptionManager
            import threading
            self._subscription_manager = SubscriptionManager(
                quote_ctx=self._quote_ctx,
                config=config,
                market_data_store=self._market_data_store if hasattr(self, '_market_data_store') else None,
                kline_buffers_by_period=self._kline_buffers_by_period,
                kline_buffers=self._kline_buffers,
                update_multi_timeframe_trend_fn=self._update_multi_timeframe_trend,
                get_subscribe_codes_fn=lambda: self._get_subscribe_codes(),
                is_trading_hours_fn=lambda: self._is_trading_hours(),
            )
            self._subscription_manager._reconnect_lock = threading.Lock()
            logger.info("[DataSubscriber] SubscriptionManager 服务已初始化")
        except Exception as e:
            logger.warning(f"[DataSubscriber] SubscriptionManager 初始化失败：{e}")
            self._subscription_manager = None

    # =========================================================================
    #  Phase 6: 引擎委托属性 (向后兼容，逐步迁移)
    #  这些属性允许现有代码通过 self._xxx 访问引擎状态，同时保持向后兼容
    # =========================================================================

    def _get_quote_ctx(self):
        if self._quote_ctx is None:
            import logging as stdlib_logging
            
            # 配置 futu 日志到临时目录，避免文件锁冲突
            futu_logger = stdlib_logging.getLogger("futu")
            futu_logger.setLevel(stdlib_logging.ERROR)
            
            # 移除现有的文件 handler
            for handler in futu_logger.handlers[:]:
                if isinstance(handler, stdlib_logging.FileHandler):
                    futu_logger.removeHandler(handler)
            
            try:
                from futu import OpenQuoteContext
                self._quote_ctx = OpenQuoteContext(
                    host=config.OPEND_HOST, port=config.OPEND_PORT
                )
                logger.info("行情上下文已创建")
            except Exception as e:
                logger.error(f"创建行情上下文失败：{e}")
                raise
        return self._quote_ctx

    # ============================================================
    #  事件驱动入口（供 news_push/collector 调用）
    # ============================================================
    def on_news_event(self, news_list: list):
        """
        新闻事件入口 - 供新闻采集器调用，触发事件驱动策略

        Args:
            news_list: 新闻列表 [{'id', 'title', 'content', 'source', 'related_stocks'}]
        """
        if not self.layered_manager:
            return

        try:
            layered_signals = self.layered_manager.process_news(news_list)
            if layered_signals:
                for ls in layered_signals:
                    # 将事件驱动信号直接送入共识引擎
                    self._consensus.submit(ls.signal)
                    logger.info(
                        f"[事件驱动] {ls.signal.code} 信号已提交共识 | "
                        f"优先级={ls.priority} | 置信度={ls.confidence:.2f}"
                    )
        except Exception as e:
            logger.warning(f"[事件驱动] 处理新闻事件失败: {e}")

    def on_us_overnight_data(self, us_data: list):
        """
        美股隔夜数据入口 - 供 FTSeek-US 或外部数据源调用，触发跨市场套利策略

        Args:
            us_data: 美股隔夜数据列表 [{'code', 'name', 'close_price', 'change_pct'}]
        """
        if not self.layered_manager:
            return

        try:
            layered_signals = self.layered_manager.analyze_us_overnight(us_data)
            if layered_signals:
                for ls in layered_signals:
                    self._consensus.submit(ls.signal)
                    logger.info(
                        f"[跨市场套利] {ls.signal.code} 信号已提交共识 | "
                        f"优先级={ls.priority} | 置信度={ls.confidence:.2f}"
                    )
        except Exception as e:
            logger.warning(f"[跨市场套利] 处理美股数据失败: {e}")

    def _get_current_cooldown(self) -> int:
        """获取当前冷却时间 (支持动态更新)"""
        # 每 5 分钟检查一次市场状态
        if time.time() - self._last_regime_check > 300:
            self._update_regime_params()
        
        # 从参数加载器获取当前冷却时间
        current_params = self.params_loader.get_current_params()
        return current_params.get('signal_cooldown', SIGNAL_COOLDOWN)

    def _get_adaptive_cooldown(self, code: str, last_trade_pnl: float = None) -> int:
        """P4 fix (2026-04-26): 自适应冷却 — 基于波动率、市场状态、交易结果。

        - 高ATR: 冷却×1.5; 低ATR: 冷却×0.7
        - 亏损后: 冷却×2.0 (防止情绪化追单)
        - 盈利后: 冷却×0.7 (趋势可能延续, 允许更快再入场)
        - 最终钳位: [60, 600] 秒
        """
        base = self._get_current_cooldown()

        # 波动率调整
        try:
            atr_pct = self._get_atr_percent(code)
            if atr_pct > 0.04:
                base = int(base * 1.5)
            elif atr_pct < 0.01:
                base = int(base * 0.7)
        except Exception:
            pass

        # 交易结果调整
        if last_trade_pnl is not None:
            if last_trade_pnl < 0:
                base = int(base * 2.0)
            elif last_trade_pnl > 0:
                base = int(base * 0.7)

        return max(60, min(base, 600))
    
    def _update_regime_params(self):
        """更新市场状态参数"""
        try:
            # 获取恒指数据和市场状态
            hsi_data = self.hsi_fetcher.get_hsi_data()
            if hsi_data is None:
                return
            
            # 计算恒指 ADX
            adx = self.hsi_fetcher.calculate_hsi_adx()
            trend = self.hsi_fetcher.get_hsi_trend()
            
            # 判断市场状态
            if adx:
                if adx > 30:
                    regime = 'strong_trend_up' if trend == 'up' else 'strong_trend_down'
                elif adx > 20:
                    regime = 'weak_trend_up' if trend == 'up' else 'weak_trend_down'
                else:
                    regime = 'range_bound'
            else:
                regime = 'default'
            
            # 如果状态变化，更新参数
            if regime != self._current_regime:
                self._current_regime = regime
                params = self.params_loader.get_params(regime)
                
                logger.info(f"市场状态变更：{regime}")
                logger.info(f"  冷却时间：{params['signal_cooldown']}秒")
                logger.info(f"  置信度：{params['min_confidence']}")
                logger.info(f"  仓位：{params['max_single_position']*100:.0f}%")
                
                # 推送钉钉通知
                try:
                    from app.notifier.dingtalk import DingTalkNotifier
                    notifier = DingTalkNotifier()
                    message = f"📊 市场状态变更提醒\n\n状态：{params['description']}\n冷却：{params['signal_cooldown']}秒\n置信度：{params['min_confidence']}\n仓位：{params['max_single_position']*100:.0f}%"
                    # notifier.send_text(message)
                except (ImportError, Exception):
                    pass

        except Exception as e:
            logger.debug(f"更新市场状态参数失败：{e}")

    def _init_quality_managers(self):
        """
        初始化质量管理器 - 巴菲特理念实现

        包含:
        - 质量评分器 (QualityScorer): 基本面因子评分
        - 持仓周期管理器 (HoldingPeriodManager): 质量决定的持仓期限
        - 资金水位管理器 (WaterLevelManager): 动态风险调整
        - 止盈止损管理器 (ProfitLossManager): 三层止盈架构
        - 估值计算器 (ValuationCalculator): DCF 估值
        """
        try:
            from app.factors import (
                get_quality_scorer,
                get_holding_period_manager,
                get_water_level_manager,
                get_profit_loss_manager,
                get_valuation_calculator,
            )

            # 初始化各管理器
            self.quality_scorer = get_quality_scorer()
            self.holding_period = get_holding_period_manager()
            # SSOT: Capital Adaptive
            from config.config_center import get_config_center
            initial_capital = get_config_center().get('capital.fixed_amount', 1000000)
            self.water_level = get_water_level_manager(initial_capital=initial_capital)
            self.profit_loss = get_profit_loss_manager(water_level_manager=self.water_level)
            self.valuation = get_valuation_calculator()

            logger.info("质量管理器已初始化 (质量评分器、持仓周期管理器、资金水位管理器、止盈止损管理器、估值计算器)")
        except Exception as e:
            logger.warning(f"质量管理器初始化失败：{e}")
            self.quality_scorer = None
            self.holding_period = None
            self.water_level = None
            self.profit_loss = None
            self.valuation = None

    # ── Exit Enhance Config (P1 2026-05-06) ─────────────────────────────

    def _load_exit_enhance_config(self):
        """加载 Flash Exit / Pre-Close Exit 配置 (从 unified_config.yaml)"""
        try:
            from config.config_center import get_config_center
            cc = get_config_center()

            # Flash Exit
            flash = cc.get('hk_exit_enhance.flash_exit', {})
            if flash:
                self._flash_exit_enabled = flash.get('enabled', True)
                self._flash_atr_mult = flash.get('atr_multiplier', 1.0)
                self._flash_window_seconds = flash.get('window_seconds', 60)
                logger.info(
                    f"[Flash Exit] 已加载配置: enabled={self._flash_exit_enabled}, "
                    f"atr_mult={self._flash_atr_mult}, window={self._flash_window_seconds}s"
                )

            # Pre-Close Exit
            prec = cc.get('hk_exit_enhance.pre_close_exit', {})
            if prec:
                self._pre_close_exit_enabled = prec.get('enabled', True)
                self._pre_close_window_minutes = prec.get('window_minutes', 15)
                logger.info(
                    f"[Pre-Close Exit] 已加载配置: enabled={self._pre_close_exit_enabled}, "
                    f"window={self._pre_close_window_minutes}min"
                )

        except Exception as e:
            logger.warning(f"[Exit Enhance] 配置加载失败，使用默认值: {e}")

    # ── Market Hours Shadow Mode (2026-05-02 Phase P0) ─────────────────────

    def _init_market_hours_shadow(self):
        """
        Initialize Shadow Mode validator for market_hours.py.

        Runs new shared module logic alongside existing implementation,
        logs discrepancies but returns OLD result (production-safe).
        """
        try:
            from app.shared.market_hours import MarketHoursValidator, MarketType, is_trading_hours

            # CRITICAL: Reference the IMPL method directly, NOT _is_trading_hours.
            # _is_trading_hours is the wrapper that checks shadow mode.
            # If we reference _is_trading_hours here, we get infinite recursion:
            #   check() → old_fn() → _is_trading_hours() → shadow.check() → old_fn() → ...
            self._market_hours_shadow = MarketHoursValidator(
                market=MarketType.HK,
                old_fn=lambda: self._is_trading_hours_impl(),
                new_fn=lambda: is_trading_hours(MarketType.HK),
            )
            logger.info("✅ Market Hours Shadow Mode initialized (shared/market_hours.py)")
        except ImportError as e:
            logger.debug(f"market_hours.py not available, Shadow Mode disabled: {e}")
        except Exception as e:
            logger.warning(f"Market Hours Shadow Mode init failed (non-blocking): {e}")

    def _is_trading_hours(self) -> bool:
        """
        检查当前是否为港股交易时间

        港股交易时间:
        - 上午: 09:30 - 12:00
        - 下午: 13:00 - 16:00

        Returns:
            True: 在交易时间内
            False: 非交易时间 (收盘后、开盘前、周末)

        【2026-05-02】If Shadow Mode is active, delegates to validator.
        """
        # Shadow Mode: run both old and new logic, return OLD result
        if self._market_hours_shadow is not None:
            return self._market_hours_shadow.check()

        # Direct call (original logic preserved)
        return self._is_trading_hours_impl()

    def _is_trading_hours_impl(self) -> bool:
        """
        Original trading hours implementation.
        Extracted for Shadow Mode comparison.
        """
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()

        # 周末不交易
        if weekday >= 5:  # 5=周六, 6=周日
            return False

        current_time = hour * 60 + minute

        # 上午交易时间: 09:30 - 12:00 (570 - 720 分钟)
        morning_start = 9 * 60 + 30   # 09:30 = 570
        morning_end = 12 * 60         # 12:00 = 720

        # 下午交易时间: 13:00 - 16:00 (780 - 960 分钟)
        afternoon_start = 13 * 60     # 13:00 = 780
        afternoon_end = 16 * 60       # 16:00 = 960

        if morning_start <= current_time < morning_end:
            return True
        if afternoon_start <= current_time < afternoon_end:
            return True

        return False

    def _is_high_quality_trading_hours(self) -> bool:
        """
        检查是否为高质量交易时间（排除开收盘噪音时段）

        排除时段：
        - 开盘冲击期: 09:30 - 09:45 （流动性差、价格剧烈波动）
        - 收盘撮合期: 15:45 - 16:00 （做市商平仓、价格失真）

        Returns:
            True: 信号质量可靠的交易时段
            False: 噪音较高的时段
        """
        if not self._is_trading_hours():
            return False

        now = datetime.now()
        current_time = now.hour * 60 + now.minute

        # 排除开盘冲击期（09:30 - 09:45）— SSOT: strategy.time_gate.open_buffer
        open_buffer_start = 9 * 60 + 30   # 09:30
        open_buffer_end   = 9 * 60 + 45   # 09:45

        # 排除收盘撮合期（15:45 - 16:00）— SSOT: strategy.time_gate.close_buffer
        from config.config_center import get_config_center as _gcc_cb
        _cb_cfg = _gcc_cb().get('strategy.time_gate', {})
        _cb_s = _cb_cfg.get('close_buffer_start', '15:45')
        _cb_e = _cb_cfg.get('close_buffer_end', '16:00')
        close_buffer_start = int(_cb_s.split(':')[0]) * 60 + int(_cb_s.split(':')[1])
        close_buffer_end = int(_cb_e.split(':')[0]) * 60 + int(_cb_e.split(':')[1])

        if open_buffer_start <= current_time < open_buffer_end:
            return False  # 开盘波动期，信号噪音大
        if close_buffer_start <= current_time < close_buffer_end:
            return False  # 收盘撮合期，价格失真

        return True
    
    def _get_trading_period_info(self) -> Dict[str, any]:
        """
        P2 2026-04-27: 返回当前交易时段信息 (分时段质量门)
        
        Returns:
            {
                'period': 'open_noise' | 'main' | 'close_noise' | 'out_of_hours',
                'description': str,
            }
        """
        now = datetime.now()
        if now.weekday() >= 5:
            return {'period': 'out_of_hours', 'description': '周末非交易时段'}
        
        current_time = now.hour * 60 + now.minute
        
        # 上午交易: 09:30-12:00
        if 570 <= current_time < 720:
            if current_time < 578:  # 09:30-09:38 (从 15 分钟缩短到 8 分钟)
                return {'period': 'open_noise', 'description': '开盘噪音段 (09:30-09:38)'}
            return {'period': 'main', 'description': '主交易段 (上午)'}
        
        # 下午交易: 13:00-16:00
        if 780 <= current_time < 960:
            if current_time >= 945:  # 15:45-16:00 收盘撮合
                return {'period': 'close_noise', 'description': '收盘撮合段 (15:45-16:00)'}
            return {'period': 'main', 'description': '主交易段 (下午)'}
        
        return {'period': 'out_of_hours', 'description': '非交易时段'}
    
    def _is_cooled_down(self, code: str) -> bool:
        """检查是否冷却完成 - 2026-03-17 优化：只在推送成功后才更新冷却时间"""
        now = time.time()
        cooldown = self._get_current_cooldown()  # 使用动态冷却时间
        
        with self._cooldown_lock:
            last_time = self._cooldown.get(code, 0)
            if now - last_time < cooldown:
                return False
            # 注意：不在这里更新冷却时间，等推送成功后再更新
            return True

    # ----------------------------------------------------------
    #  ADX 实时更新
    # ----------------------------------------------------------
    def _update_adx(self, code: str, kline: dict):
        """每根 K 线到达时更新 ADX 并注入共识引擎"""
        buf = self._kline_buffers[code]
        buf.push_kline(kline)

        if buf.size < ADX_PERIOD * 2 + 5:
            return  # 数据不够

        adx_val = calc_adx(
            list(buf.high), list(buf.low), list(buf.close), ADX_PERIOD
        )
        if adx_val is not None:
            self._consensus.update_adx(code, adx_val)

        # P1-2: 同步更新 MarketRegimeDetector 并调整共识阈值
        try:
            self._regime_detector.update_data(code, kline)
            regime = self._regime_detector.detect(code)
            if regime is not None:
                # 高波动市场：提高共识阈值，降低假信号
                if regime.volatility_state.value == 'high':
                    self._consensus.threshold = 2.0
                else:
                    self._consensus.threshold = CONSENSUS_THRESHOLD  # 恢复默认 1.5
                # 日线趋势注入（用于多周期对齐降权）
                trend_map = {
                    'strong_up': 'up', 'weak_up': 'up',
                    'strong_down': 'down', 'weak_down': 'down',
                }
                daily_trend = trend_map.get(regime.trend_direction.value, 'neutral')
                self._consensus.update_daily_trend(code, daily_trend)
                logger.debug(
                    f"[市场状态] {code} {regime.regime_type.value} | "
                    f"趋势={daily_trend} | 阈值={self._consensus.threshold:.1f}"
                )
        except Exception as _e:
            logger.debug(f"[市场状态] {code} 检测异常: {_e}")

        # 注意：场景识别已移至 MyKlineHandler 中在 K_5M/K_15M 触发（新问题 3 修复）
        # 理由：1M 噪声大，应使用中周期 K 线进行场景识别

    # ----------------------------------------------------------
    #  ATR 波动率计算 (港股适配用)
    # ----------------------------------------------------------
    def _get_atr_percent(self, code: str) -> float:
        """计算 ATR 波动率百分比 (用于港股适配)
        
        使用 Wilder 风格的 True Range 均值计算，避免用整体价格区间导致 ATR 估算过小。
        True Range = max(High, PrevClose) - min(Low, PrevClose)
        ATR = 14根K线的TR均值 / 当前价
        """
        buf = self._kline_buffers[code]
        if buf.size < 15:
            return 0.02  # 默认 2%
        
        closes = list(buf.close)
        highs  = list(buf.high)
        lows   = list(buf.low)
        
        # 取最近14根K线计算True Range
        tr_list = []
        n = min(14, len(highs))
        for i in range(-n, 0):
            prev_close = closes[i - 1] if abs(i - 1) <= len(closes) else closes[0]
            tr = max(highs[i], prev_close) - min(lows[i], prev_close)
            if tr > 0:
                tr_list.append(tr)
        
        if not tr_list:
            return 0.02
        
        current_price = closes[-1]
        atr = sum(tr_list) / len(tr_list)
        atr_pct = atr / current_price if current_price > 0 else 0.02
        # 设置合理上下界（0.3% ~ 8%），避免极端值
        return max(0.003, min(atr_pct, 0.08))

    def _get_atr_from_period(self, code: str, k_type: str, period: int = 14) -> float:
        """从指定周期K线缓冲区计算 ATR 百分比（2026-03-25 新增）

        通过多周期K线（15M/30M/60M/日线）获取更稳定的ATR，用于止损宽度自适应。

        Args:
            code:   股票代码
            k_type: K线周期 "K_15M" / "K_30M" / "K_60M" / "K_DAY"
            period: ATR 计算周期，默认14

        Returns:
            float: ATR 百分比 (0.003 ~ 0.15)
        """
        try:
            buf = self._kline_buffers_by_period[code].get(k_type)
            if buf is None or buf.size < period + 1:
                return self._get_atr_percent(code)  # fallback 到 1M

            closes = list(buf.close)
            highs  = list(buf.high)
            lows   = list(buf.low)

            tr_list = []
            n = min(period, len(highs) - 1)
            for i in range(-n, 0):
                prev_close = closes[i - 1] if (len(closes) + i - 1) >= 0 else closes[0]
                tr = max(highs[i], prev_close) - min(lows[i], prev_close)
                if tr > 0:
                    tr_list.append(tr)

            if not tr_list:
                return self._get_atr_percent(code)

            current_price = closes[-1]
            if current_price <= 0:
                return 0.02
            atr_val = sum(tr_list) / len(tr_list)
            atr_pct = atr_val / current_price
            # 各周期 ATR 上下界有所不同（2026-03-25 补全 1M/3M/5M）
            bounds = {
                "K_1M":  (0.001, 0.04),   # 1分钟：波动极小
                "K_3M":  (0.002, 0.05),   # 3分钟
                "K_5M":  (0.002, 0.05),   # 5分钟
                "K_15M": (0.003, 0.06),
                "K_30M": (0.004, 0.08),
                "K_60M": (0.005, 0.10),
                "K_DAY": (0.008, 0.15),
            }
            lo, hi = bounds.get(k_type, (0.003, 0.10))
            return max(lo, min(atr_pct, hi))
        except Exception as e:
            logger.debug(f"[多周期ATR] {code} {k_type} 计算失败: {e}")
            return self._get_atr_percent(code)

    def _get_preferred_atr_percent(self, code: str) -> float:
        """根据动态止损配置选择最优ATR周期计算ATR百分比（2026-03-25 新增）

        优先级：dynamic_exit_manager 中配置的 preferred_kline → 60M → 30M → 1M fallback
        """
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            preferred = _dem.current_params.get('atr_stop', {}).get('preferred_kline', 'K_60M')
            return self._get_atr_from_period(code, preferred)
        except Exception:
            return self._get_atr_from_period(code, 'K_60M')

    def _get_atr(self, code: str) -> float:
        """计算 ATR 绝对值（用于退出引擎，2026-03-25 升级：使用多周期优先ATR）"""
        buf = self._kline_buffers[code]
        if buf.size < 5:
            return 0.0
        current_price = buf.close[-1]
        atr_pct = self._get_preferred_atr_percent(code)
        return current_price * atr_pct

    def _update_trailing_stop(self, code: str, current_price: float, position) -> None:
        """
        激活/上移追踪止盈止损价（Task2 新增）

        规则：
        - 多头：盈利 >= 1% 时激活；追踪距离 = 2×ATR；只上移不下移
        - 空头：盈利 >= 1% 时激活；追踪距离 = 2×ATR；只下移不上移
        """
        if position is None:
            return
        try:
            entry = position.entry_price
            if entry <= 0:
                return
            # 2026-03-25 升级：优先使用 preferred_kline 的ATR（默认60M），追踪更稳定
            atr_pct = self._get_preferred_atr_percent(code)
            # 获取动态止损配置中的ATR倍数
            try:
                from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                trail_atr_mult = _dem.current_params.get('atr_stop', {}).get('multiplier', 2.0)
            except Exception:
                trail_atr_mult = 2.0
            trail_gap = current_price * atr_pct * trail_atr_mult

            _trail_atr_mult = float(_dsub("trailing_stop_atr_mult", 3.0))
            _trail_activate = float(_dsub("trailing_stop_activate_pct", 0.02))

            if position.action == 'BUY':
                pnl_pct = (current_price - entry) / entry
                if pnl_pct >= _trail_activate:
                    new_trail = current_price - trail_gap * (_trail_atr_mult / 2.0)
                    old_trail = self._trailing_stop_cache.get(code, 0.0)
                    if new_trail > old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%}, gap={trail_gap*(_trail_atr_mult/2.0):.3f})"
                        )
            elif position.action == 'SELL':
                pnl_pct = (entry - current_price) / entry
                if pnl_pct >= _trail_activate:
                    new_trail = current_price + trail_gap * (_trail_atr_mult / 2.0)
                    old_trail = self._trailing_stop_cache.get(code, float('inf'))
                    if new_trail < old_trail:
                        self._trailing_stop_cache[code] = new_trail
                        logger.debug(
                            f"[追踪止盈-空头] {code} trail_stop={new_trail:.3f} "
                            f"(pnl={pnl_pct:.2%}, ATR%={atr_pct:.2%}, gap={trail_gap*1.5:.3f})"
                        )
        except Exception as e:
            logger.debug(f"[追踪止盈] 更新异常: {e}")

    # ----------------------------------------------------------
    #  多周期趋势判断 (2026-03-25 新增，2026-03-25 升级)
    # ----------------------------------------------------------
    def _update_multi_timeframe_trend(self, code: str, k_type: str, kline: dict):
        """更新多周期趋势状态（2026-05-02 Harness Pilot: 委托给 MultiTimeframeEngine）
        
        原代码 L783-992 已提取到 app/data_subscriber/multi_timeframe.py
        此方法保留 side effects（趋势缓存更新、共识同步、趋势过滤器同步）
        """
        try:
            # 委托给引擎计算趋势方向
            trend = self._mtf_engine.update_trend(
                code=code,
                k_type=k_type,
                kline=kline,
                kline_buffers=self._kline_buffers_by_period,
                trends=self._multi_timeframe_trends,
                consensus=self._consensus,
                trend_filter=None,  # trend_filter sync handled below
            )
            if trend is None:
                return  # 数据不足

            old_trend = self._multi_timeframe_trends[code][k_type]
            self._multi_timeframe_trends[code][k_type] = trend
            self._trends_updated.add((code, k_type))

            if trend != old_trend:
                # 趋势变化时的同步逻辑（保留原有 side effects）
                self._sync_trend_change(code, k_type, trend)
                
        except Exception as e:
            logger.debug(f"[多周期趋势] {code} {k_type} 更新失败: {e}")
    
    def _sync_trend_change(self, code: str, k_type: str, trend: str):
        """趋势变化时的同步：共识引擎 + 趋势过滤器（从 _update_multi_timeframe_trend 提取）"""
        # 日线趋势同步到共识引擎
        if k_type == "K_DAY":
            self._consensus.update_daily_trend(code, trend)
        
        # K_60M / K_3M / K_5M 同步到趋势过滤器
        sync_info = TREND_FILTER_SYNC.get(k_type)
        if sync_info:
            method_name, target = sync_info
            try:
                from app.risk.trend_filter import get_trend_filter, Trend
                trend_filter = get_trend_filter()
                trend_enum = getattr(Trend, TREND_TO_ENUM.get(trend, "RANGE"))
                getattr(trend_filter, method_name)(code, trend_enum)
                logger.debug(f"[趋势同步] {code} {k_type}趋势 {trend} 已同步到{target}")
            except Exception as e:
                logger.debug(f"[趋势同步] {code} {k_type}同步失败: {e}")
    
    def _get_mtf_alignment_score(self, code: str, action: str) -> tuple:
        """计算多周期对齐分数（0.0 ~ 1.0）— 委托给 MultiTimeframeEngine，自动排除冷启动未更新周期"""
        updated = {p for (c, p) in self._trends_updated if c == code}
        return self._mtf_engine.get_alignment_score(code, action, self._multi_timeframe_trends, updated_periods=updated)

    def _get_multi_timeframe_signal(self, code: str, action: str) -> tuple:
        """获取多周期趋势确认 — 委托给 MultiTimeframeEngine"""
        updated = {p for (c, p) in self._trends_updated if c == code}
        return self._mtf_engine.get_signal(
            code, action, self._multi_timeframe_trends, consensus=self._consensus, updated_periods=updated
        )
    
    def _calculate_mtf_resonance(self, code: str, action: str) -> float:
        """计算多周期共振度 — 委托给 MultiTimeframeEngine"""
        return self._mtf_engine.calculate_resonance(code, action, self._multi_timeframe_trends)
    
    def _get_dynamic_stop_params(self, code: str) -> dict:
        """动态调整止损止盈参数 — 委托给 DynamicStopEngine（2026-05-02 Harness Pilot）"""
        from app.scenario.classifier import ScenarioType

        scenario_type = None
        if self._scenario_memory:
            current_scenario = self._scenario_memory.get_current_scenario(code)
            if current_scenario:
                scenario_type = current_scenario.value

        return self._stop_engine.get_params(
            code=code,
            trends=self._multi_timeframe_trends,
            scenario_type=scenario_type,
        )

    # ----------------------------------------------------------
    #  新闻情绪仓位加成 (Task4)
    # ----------------------------------------------------------
    def _get_news_sentiment_boost(self, code: str, action) -> float:
        """返回新闻情绪加成倍率 (0.7 ~ 1.3)
        
        【优化 2026-04-02】增加缓存机制，避免频繁调用LLM API
        缓存有效期：30分钟
        条件：需要 >= 2 条近 3 小时新闻，情绪分数绝对值 > 0.3
        """
        try:
            from app.factors.news_integration import get_news_sentiment_analyzer
            
            # 检查缓存（30分钟有效）
            now = time.time()
            if code in self._news_sentiment_cache:
                score, cached_time = self._news_sentiment_cache[code]
                if now - cached_time < self._news_cache_ttl:
                    # 缓存有效，直接使用
                    return self._apply_sentiment_boost(score, action, code)
            
            # 缓存失效或不存在，重新分析
            analyzer = get_news_sentiment_analyzer()
            result = analyzer.analyze_stock(code, lookback_hours=3, max_news=10)
            score = result.get('sentiment_score', 0.0)
            news_count = result.get('news_count', 0)
            
            if news_count < 2:
                # 新闻不足，不调整，但仍然缓存
                self._news_sentiment_cache[code] = (0.0, now)
                return 1.0
            
            # 更新缓存
            self._news_sentiment_cache[code] = (score, now)
            
            return self._apply_sentiment_boost(score, action, code)
            
        except Exception as e:
            logger.debug(f"[新闻加成] 获取情绪分数异常: {e}")
            return 1.0
    
    def _apply_sentiment_boost(self, score: float, action, code: str) -> float:
        """应用情绪加成倍率（内部方法）"""
        if action == SignalAction.BUY and score > 0.3:
            logger.debug(f"[新闻加成] {code} BUY 正面新闻 score={score:.2f} → 1.3x")
            return 1.3   # 正面新闻加仓
        elif action == SignalAction.BUY and score < -0.3:
            logger.debug(f"[新闻加成] {code} BUY 负面新闻 score={score:.2f} → 0.7x")
            return 0.7   # 负面新闻减仓
        elif action == SignalAction.SELL and score < -0.3:
            logger.debug(f"[新闻加成] {code} SELL 负面新闻 score={score:.2f} → 1.3x")
            return 1.3   # 负面新闻做空加仓
        elif action == SignalAction.SELL and score > 0.3:
            logger.debug(f"[新闻加成] {code} SELL 正面新闻 score={score:.2f} → 0.7x")
            return 0.7   # 正面新闻做空减仓
        
        return 1.0

    # ----------------------------------------------------------
    #  共识达成回调
    # ----------------------------------------------------------

    # 2026-04-27 P0 修复: 删除此重复定义。_get_mtf_alignment_score 已在 L949 定义
    # 返回 (score, detail) 元组，被 _get_multi_timeframe_signal (L1046) 依赖解包。
    # 原 L1357 的 float 返回值覆盖了 L949 的 tuple 返回值 → cannot unpack non-iterable float object
    # → MultiTimeframeCheck 全部异常 → Pipeline 全部拒绝 → 零成交

    def _on_consensus(self, result: ConsensusResult):
        """推送共识信号 (修改版) - 接入决策缓冲层"""
        code = result.code

        # code 级别非阻塞锁：防止同一标的并发执行（网络抖动/重复回调）
        lock = self._trade_locks.setdefault(code, threading.Lock())
        if not lock.acquire(blocking=False):
            logger.debug(f"[幂等] {code} 上一笔交易仍在执行，跳过本次共识回调")
            return
        try:
            self._on_consensus_inner(result)
        finally:
            lock.release()

    def _on_consensus_inner(self, result: ConsensusResult):
        """_on_consensus 实际逻辑（已由外层持有 code 锁）- 2026-04-09 重构: 风控Pipeline化"""
        code = result.code
        trace_id = getattr(result, 'trace_id', 'N/A')

        # ============================================================
        #  Phase 1: 风控 Pipeline 检查（替代原来分散的 20+ 项 if-return）
        # ============================================================
        from app.risk.risk_pipeline import SignalContext, init_risk_pipeline

        # 先确定交易类型（Pipeline 部分检查需要）
        position = self.position_manager.get_position(code)
        trade_type = self._determine_trade_type(position, result.action)

        ctx = SignalContext(
            consensus_result=result,
            trace_id=trace_id,
            trade_type=trade_type,
        )

        # P1 2026-04-27: 预加载统一数据快照，Pipeline 各检查复用
        # P0 2026-05-04: try/except 保护 — preload_snapshots 未在 SignalContext 实现
        try:
            ctx.preload_snapshots(self)
        except AttributeError:
            pass

        # 初始化/获取 Pipeline（首次调用时注入 subscriber 依赖）
        try:
            from app.core.subscriber_adapter import DataSubscriberAdapter
            _adapter = DataSubscriberAdapter(self)
            pipeline = init_risk_pipeline(deps={
                "trading_hours": _adapter,       # ITradingHoursProvider
                "exit_state": _adapter,          # IExitStateProvider
                "cooldown": _adapter,            # ICooldownProvider
                "circuit_breaker": _adapter,     # ICircuitBreakerProvider
                "quality": _adapter,             # IQualityProvider
                "market_data": _adapter,         # IMarketDataProvider
                "position_query": _adapter,      # IPositionQueryProvider
            })
        except Exception:
            from app.risk.risk_pipeline import get_risk_pipeline
            pipeline = get_risk_pipeline(deps={})

        # 执行 Pipeline
        pipeline_result = pipeline.evaluate(ctx)

        # 记录信号（无论通过与否，都需记录以支持健康看板）
        base_signal = result.votes[0].signal
        action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
        signal_id = signal_tracker.record_signal(
            code=code,
            strategy=f"共识({'+'.join(result.agree_strategies)})",
            action=action_str,
            price=result.price,
            quantity=base_signal.quantity,
            confidence=result.confidence,
            consensus_score=result.score,
            strategies=result.agree_strategies,
            market_regime=result.market_regime,
            adx=result.adx_value,
            trace_id=trace_id,
        )
        logger.debug(f"[{trace_id}] [{code}] 信号记录：{signal_id[:8]}... | 置信度={result.confidence:.2f}")

        # Pipeline 未通过，记录拒绝原因
        if not pipeline_result.passed:
            signal_tracker.record_execution(
                signal_id, executed=False,
                reject_reason=pipeline_result.reject_code
            )
            logger.info(
                f"[风控Pipeline] [{trace_id}] {code} 被 [{pipeline_result.reject_check}] 拒绝: "
                f"{pipeline_result.reject_reason}"
            )
            return

        # ============================================================
        #  Phase 2: 执行层（Pipeline 通过后的业务逻辑）
        # ============================================================
        # 多周期趋势对齐度检查 (P2 fix 2026-04-26)
        # 冷启动优化 2026-05-04: 动态权重 + 宽限期 + 比例阈值
        mtf_alignment, mtf_detail = self._get_mtf_alignment_score(code, result.action)

        # 计算有效阈值: 可用周期越少，阈值成比例降低
        updated = {p for (c, p) in self._trends_updated if c == code}
        all_weights = PERIOD_WEIGHTS_ALIGNMENT
        available_weight = sum(all_weights.get(p, 0) for p in updated)
        available_weight = max(available_weight, 0.05)  # 至少 5% 防止除零
        effective_threshold = MTF_DEFAULT_THRESHOLD * min(available_weight, 1.0)
        # 绝对下限 0.10 — 低于此值无意义
        effective_threshold = max(effective_threshold, 0.10)

        # 冷启动宽限期检查
        in_warmup = time.time() < self._mtf_warmup_until
        mtf_penalty = 1.0  # 默认无惩罚
        if in_warmup:
            warmup_remaining = int(self._mtf_warmup_until - time.time())
            logger.info(
                f"[MTF宽容] {code} 冷启动宽限期 (剩余{warmup_remaining}s), "
                f"对齐度={mtf_alignment:.0%}, 可用周期={len(updated)}/7, "
                f"跳过MTF拒绝"
            )
            # Fix A: 宽限期内真正跳过拒绝
        elif mtf_alignment < 0.10:
            # Fix B: 硬拒绝 → 软拦截 (降权而非 return)
            mtf_penalty = max(0.3, mtf_alignment / 0.10)  # 0%→0.3x, 5%→0.8x, 9%→0.9x
            logger.info(
                f"[MTF降权] {code} 多周期对齐度={mtf_alignment:.0%} < 0.10, "
                f"信号降权至 {mtf_penalty:.0%}"
            )
        elif mtf_alignment < effective_threshold:
            logger.info(
                f"[MTF软放行] {code} 多周期对齐度={mtf_alignment:.0%} < "
                f"有效阈值={effective_threshold:.0%} (软拦截: 记录但不拒绝)"
            )
        elif len(updated) < 7:
            logger.info(
                f"[MTF部分] {code} 对齐度={mtf_alignment:.0%} >= "
                f"有效阈值={effective_threshold:.0%} (仅{len(updated)}/7周期可用), 放行"
            )
        # 从 SignalContext 中提取 Pipeline 计算的各种系数
        quality_boost = ctx.quality_boost
        _concentration_discount = ctx.concentration_discount
        mtf_confirmed = ctx.mtf_confirmed
        mtf_boost = ctx.mtf_boost
        mtf_reason = ctx.mtf_reason

        strategies_str = " + ".join(result.agree_strategies)
        n_agree = len(result.agree_strategies)

        # 构造合并推送的 reason
        consensus_reason = "\n".join(
            f"  ▸ {v.strategy}(权重{result.weight_details.get(v.strategy, '?')}): {v.reason}"
            for v in result.votes
        )

        confidence = result.confidence * mtf_penalty
        if mtf_penalty < 1.0:
            logger.info(
                f"[MTF降权生效] {code} 置信度 {result.confidence:.0%} → {confidence:.0%} (惩罚={mtf_penalty:.0%})"
            )

        consensus_signal = Signal(
            code=code,
            name=base_signal.name,
            action=result.action,
            price=result.price,
            quantity=base_signal.quantity,
            reason=consensus_reason,
            strategy=f"共识({strategies_str})",
            extra={
                "consensus": True,
                "score": result.score,
                "final_score": getattr(result, 'final_score', result.score),
                "threshold": result.threshold,
                "market_regime": result.market_regime,
                "adx": result.adx_value,
                "quality_boost": getattr(getattr(result, 'quality_result', None), 'position_factor', 1.0),
                "timeliness": getattr(result, 'timeliness_factor', 1.0),
                "llm_signal_impact_factor": getattr(result, 'llm_signal_impact_factor', 1.0),
                "agree_count": n_agree,
                "total_count": n_agree + len(result.oppose_strategies),
                "strategies": result.agree_strategies,
                "oppose": result.oppose_strategies,
                "weights": result.weight_details,
                "confidence": round(confidence, 2),
                "mtf_penalty": round(mtf_penalty, 2),
            },
        )

        # 港股特性适配（Pipeline 中只做了基础过滤，这里做实际适配）
        if code.startswith("HK."):
            atr_pct = self._get_atr_percent(code)
            consensus_signal = self.hk_adapter.process_signal(consensus_signal, atr_pct)
            if consensus_signal is None:
                logger.info(f"[港股适配] {code} 信号被过滤 (T+0 冲突/置信度不足/流动性限制)")
                signal_tracker.record_execution(signal_id, executed=False, reject_reason='hk_adapter')
                return

        # 空头建仓说明
        if position is None and result.action == SignalAction.SELL:
            logger.info(f"[空头建仓] {code} 无持仓，SELL信号将建立空头 (CASH账户自动卖空)")

        # 已有同向持仓时，尝试顺势加仓
        if trade_type == 'HOLD':
            added = self._try_add_position(code, position, result, consensus_signal, signal_id)
            if not added:
                action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
                logger.info(
                    f"[持仓保护] {code} 已有 {position.action} 持仓，"
                    f"不满足加仓条件，拒绝信号 {action_str} (分数={result.score:+.2f})"
                )
                signal_tracker.record_execution(signal_id, executed=False, reject_reason='existing_position')
            return

        # 新闻情绪倍率 x 分散折扣
        news_boost = self._get_news_sentiment_boost(code, result.action)
        combined_boost = news_boost * _concentration_discount
        if combined_boost != 1.0:
            logger.info(
                f"[仓位调节] {code} 新闻={news_boost:.1f}x 分散={_concentration_discount:.1f}x "
                f"合并={combined_boost:.2f}x 注入 extra"
            )
            consensus_signal = dataclasses.replace(
                consensus_signal,
                extra={**(consensus_signal.extra or {}), 'news_boost': combined_boost}
            )

        # 开仓原子锁 - 防止 quote/kline 两个 handler 同时触发导致同标的重复开仓
        if trade_type == 'OPEN':
            _open_cooldown_sec = 5.0
            with self._open_lock:
                _last_open_ts = self._open_position_ts.get(code, 0)
                _now_ts = time.time()
                if _now_ts - _last_open_ts < _open_cooldown_sec:
                    logger.warning(
                        f"[开仓去重] {code} {_open_cooldown_sec:.0f}s 内已执行过开仓 "
                        f"(距上次 {_now_ts - _last_open_ts:.1f}s)，拒绝重复信号"
                    )
                    signal_tracker.record_execution(signal_id, executed=False, reject_reason='open_dedup')
                    return
                # 预占时间戳，阻止并发仓
                self._open_position_ts[code] = _now_ts

        # === P0 修复 2026-05-08 ===

        # Fix #1: 单只股票日内交易次数监控（模拟盘放宽，记录超限不拦截）
        if trade_type == 'OPEN':
            try:
                from config.config_center import get_config_center
                _rl = get_config_center().get('risk_limits', {})
                _max_symbol_trades = _rl.get('max_symbol_trades_per_day', 3)
            except Exception:
                _max_symbol_trades = 3
            if hasattr(self, 'position_manager') and self.position_manager:
                _today_symbol_count = self.position_manager.get_today_trade_count(code)
                if _today_symbol_count >= _max_symbol_trades:
                    logger.warning(
                        f"[日内频次警告] {code} 今日已交易 {_today_symbol_count} 次 "
                        f"(上限: {_max_symbol_trades} 次/日/股) — 模拟盘放行，记录数据"
                    )

        # Fix #3: 开仓间隔监控（模拟盘软拦截 — 只警告不阻止）
        if trade_type == 'OPEN':
            try:
                from config.config_center import get_config_center
                _rl = get_config_center().get('risk_limits', {})
                _min_open_interval = _rl.get('min_open_interval_sec', 300)  # 默认 5 分钟
                _interval_mode = _rl.get('min_open_interval_mode', 'warn')  # warn | block
            except Exception:
                _min_open_interval, _interval_mode = 300, 'warn'
            _last_global_open = getattr(self, '_last_global_open_ts', 0)
            _now_ts = time.time()
            if _last_global_open > 0 and (_now_ts - _last_global_open) < _min_open_interval:
                _elapsed = int(_now_ts - _last_global_open)
                logger.warning(
                    f"[开仓间隔警告] {_min_open_interval}s 内已开仓 (距上次 {_elapsed}s)，"
                    f"{code} — 模拟盘{'仅记录' if _interval_mode == 'warn' else '拦截'}"
                )
                if _interval_mode == 'block':
                    signal_tracker.record_execution(
                        signal_id, executed=False,
                        reject_reason=f'open_interval_blocked ({_elapsed}s < {_min_open_interval}s)'
                    )
                    return
            self._last_global_open_ts = _now_ts

        # === 修复结束 ===

        # Phase 13: 委托给 ConsensusExecutor 服务 (已提取并测试通过)
        if self._consensus_executor is not None:
            return self._consensus_executor.execute_consensus(
                code=code,
                consensus_signal=consensus_signal,
                result=result,
                position=position,
                trade_type=trade_type,
                signal_id=signal_id,
                strategies_str=strategies_str,
            )

        # 3. 执行交易

        # 3. 执行交易
        trade_result = self.trade_executor.execute(consensus_signal, trade_type)
        
        if not trade_result.success:
            logger.warning(f"交易失败：{trade_result.message}")
            # 记录执行失败 (P0 修复 2026-05-05: try/except 保护)
            try:
                signal_tracker.record_execution(signal_id, executed=False, reject_reason=trade_result.message)
            except Exception as _rec_e:
                logger.warning(f"[执行记录] 写入失败状态 signal_tracker 异常: {_rec_e}")
            # 记录到熔断器 (新增 2026-03-17)
            self.circuit_breaker.record_reject(trade_result.message)
            return  # 不推送
        
        # 4. 准备推送数据
        push_data = {
            'signal': consensus_signal,
            'trade_result': trade_result,
            'position': position,
            'consensus_result': result
        }
        
        # 5. 记录执行成功 (移到推送之前，确保记录) 2026-03-19 修复
        # 2026-03-20 修复：TradeResult 无 execution_price 属性，改用 consensus_signal.price
        # P0 修复 2026-05-05: try/except 保护 — 记录失败绝不能阻断推送/领域事件/Obsidian
        try:
            signal_tracker.record_execution(
                signal_id,
                executed=True,
                execution_price=consensus_signal.price,
                execution_time=datetime.now().isoformat()
            )
        except Exception as _rec_e:
            logger.warning(f"[执行记录] 写入 signal_tracker 失败 (不影响推送): {_rec_e}")
        
        # 5.5 记录交易评估 (新增: 用于后续复盘分析)
        try:
            evaluator = get_evaluator()
            if trade_type == 'OPEN':
                # 开仓时记录初始信息
                evaluator.record_trade({
                    'trade_id': signal_id,
                    'code': code,
                    'action': result.action.value if hasattr(result.action, 'value') else str(result.action),
                    'entry_time': datetime.now().isoformat(),
                    'entry_price': consensus_signal.price,
                    'quantity': consensus_signal.quantity,
                    'trigger_strategy': strategies_str,
                    'trigger_reason': f"共识分数={result.score:+.2f}",
                    'confidence': result.confidence,
                    'consensus_score': result.score,
                    'market_regime': result.market_regime
                })
            else:
                # 平仓时更新评估
                if position:
                    evaluator.record_exit(
                        trade_id=signal_id,
                        exit_price=consensus_signal.price,
                        exit_time=datetime.now().isoformat(),
                        realized_pnl=trade_result.pnl or 0
                    )
        except Exception as e:
            logger.warning(f"记录交易评估失败: {e}")

        # 5.6 开仓成功后立即写入动态止损止盈价到 DB（Task6 - 2026-03-25）
        # _get_dynamic_stop_params 基于七周期加权计算，止损止盈与 yaml 保持一致
        if trade_type == 'OPEN':
            try:
                # SSOT: Market-specific exit params
                is_us = code.startswith('US.')
                if is_us:
                    us_params = _get_us_exit_params()
                    dyn = {
                        'stop_loss_pct': -abs(us_params.get('stop_loss', 0.015)),
                        'take_profit_pct': abs(us_params.get('take_profit', 0.04)),
                        **us_params
                    }
                else:
                    dyn = self._get_dynamic_stop_params(code)
                entry_px = consensus_signal.price
                # 做多：止损低于成本；做空：止损高于成本
                is_long = (result.action.value if hasattr(result.action, 'value') else str(result.action)) == 'BUY'
                sl_price = round(entry_px * (1 + dyn["stop_loss_pct"]), 4)     # stop_loss_pct < 0
                tp_price = round(entry_px * (1 + dyn["take_profit_pct"]), 4)   # take_profit_pct > 0
                if not is_long:
                    # 做空时止损/止盈方向相反
                    sl_price = round(entry_px * (1 - dyn["stop_loss_pct"]), 4)
                    tp_price = round(entry_px * (1 - dyn["take_profit_pct"]), 4)
                self.position_manager.update_stop_loss_take_profit(code, sl_price, tp_price)
                logger.info(
                    f"[动态止损] {code} 开仓@{entry_px} | "
                    f"止损={sl_price}({dyn['stop_loss_pct']:.1%}) | "
                    f"止盈={tp_price}({dyn['take_profit_pct']:.1%}) | "
                    f"regime={dyn['regime']}"
                )
            except Exception as _dyn_e:
                logger.warning(f"[动态止损] {code} 写入失败: {_dyn_e}")

        # 共识平仓回填 signal_tracker pnl（2026-03-24 修复：健康看板策略层数据）
        if trade_type == 'CLOSE' and trade_result.success and trade_result.pnl is not None:
            try:
                signal_tracker.record_exit(
                    signal_id=signal_id,
                    exit_price=consensus_signal.price,
                    exit_time=datetime.now().isoformat(),
                    pnl=trade_result.pnl,
                    pnl_pct=(
                        trade_result.pnl / (position.entry_price * position.quantity)
                        if position and position.entry_price > 0 and position.quantity > 0
                        else None
                    )
                )
            except Exception as _e:
                logger.debug(f"[signal_tracker] 共识平仓回填失败: {_e}")
        
        # 6. 推送 (包装在try中，避免推送失败影响记录)
        try:
            action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
            if trade_type == 'OPEN':
                self.notifier.send_open_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 开仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id}"
                )
                # 领域事件 — 开仓
                get_event_bus().emit(DomainEvent(
                    type=EventType.TRADE_OPENED,
                    data=TradeOpenedPayload(
                        code=code,
                        market='US' if code.startswith('US.') else 'HK',
                        price=getattr(trade_result, 'price', 0) or 0,
                        quantity=getattr(trade_result, 'quantity', 0) or 0,
                        action='BUY' if hasattr(trade_result, 'action') and str(getattr(trade_result, 'action', '')).endswith('BUY') else 'SELL',
                        regime=getattr(result, 'regime', 'consensus') if result else 'consensus',
                        entry_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        signal_id=str(signal_id) if signal_id else '',
                        signal_trace_id=getattr(signal_id, 'signal_id', '') if signal_id else '',
                    ),
                    correlation_id=str(signal_id) if signal_id else None,
                ))
            else:
                self.notifier.send_close_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 平仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id} | 盈亏：{trade_result.pnl:.2f}"
                )
                # 领域事件 — 平仓
                entry_px = getattr(position, 'entry_price', 0) if position else 0
                exit_px = getattr(trade_result, 'price', 0) or 0
                qty = getattr(position, 'quantity', 0) if position else 0
                pnl_val = getattr(trade_result, 'pnl', 0) or 0
                pnl_pct = (pnl_val / (entry_px * qty)) if entry_px and qty else 0
                get_event_bus().emit(DomainEvent(
                    type=EventType.TRADE_CLOSED,
                    data=TradeClosedPayload(
                        code=code,
                        market='US' if code.startswith('US.') else 'HK',
                        entry_time=getattr(position, 'entry_time', '') if position else '',
                        exit_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        entry_price=entry_px,
                        exit_price=exit_px,
                        quantity=qty,
                        pnl=pnl_val,
                        pnl_pct=pnl_pct,
                        strategy=getattr(result, 'regime', 'consensus') if result else 'consensus',
                        action='BUY' if hasattr(trade_result, 'action') and str(getattr(trade_result, 'action', '')).endswith('BUY') else 'SELL',
                        exit_reason=getattr(trade_result, 'reason', '') if hasattr(trade_result, 'reason') else '',
                        signal_id=str(signal_id) if signal_id else '',
                        signal_trace_id=getattr(signal_id, 'signal_id', '') if signal_id else '',
                        stop_loss=getattr(position, 'stop_loss', None) if position else None,
                        take_profit=getattr(position, 'take_profit', None) if position else None,
                        highest_price=getattr(position, 'highest_price', None) if position else None,
                        lowest_price=getattr(position, 'lowest_price', None) if position else None,
                    ),
                    correlation_id=str(signal_id) if signal_id else None,
                ))
                # 平仓成功后，记录交易结果供自适应权重学习
                if trade_result.pnl is not None:
                    # 获取当前场景 (Phase 1: 场景识别结果记录)
                    current_scenario = None
                    if hasattr(self, '_scenario_cache') and code in self._scenario_cache:
                        scenario_obj = self._scenario_cache[code]
                        if scenario_obj:
                            current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)

                    self._adaptive_weight_manager.record_trade(
                        agree_strategies=result.agree_strategies,
                        pnl=trade_result.pnl,
                        market_regime=result.market_regime,
                        scenario=current_scenario,
                        adx_value=result.adx_value or 0.0,
                    )
                    # Task1: 同步更新共识引擎信号质量分 (传入 scenario)
                    self._consensus.record_signal_outcome(
                        code=code,
                        action=result.action,
                        pnl=trade_result.pnl,
                        market_regime=result.market_regime,
                        scenario=current_scenario,  # Phase 1: 传入场景标签
                        adx_value=result.adx_value or 0.0,
                    )
        except Exception as e:
            logger.error(f"推送通知失败: {e}")
        
        # 7. 更新冷却时间 - P4 fix: 使用自适应冷却
        with self._cooldown_lock:
            last_pnl = trade_result.pnl if trade_result and trade_result.pnl is not None else None
            adaptive_cd = self._get_adaptive_cooldown(code, last_pnl)
            self._cooldown[code] = time.time()
            logger.debug(
                f"[{code}] 自适应冷却={adaptive_cd}s "
                f"(pnl={last_pnl}, regime={self._current_regime})"
            )

    def _determine_trade_type(self, position, action: SignalAction) -> str:
        """判断是开仓、平仓还是持仓不动
        
        返回值：
          'OPEN'  - 无持仓，执行开仓
          'CLOSE' - 有反向持仓，执行平仓
          'HOLD'  - 已有同向持仓，尝试加仓（条件不足时拒绝）
        """
        if position is None:
            return 'OPEN'
        if position.action == 'BUY' and action == SignalAction.SELL:
            return 'CLOSE'
        if position.action == 'SELL' and action == SignalAction.BUY:
            return 'CLOSE'
        # 同向信号且已有持仓：进入加仓判断
        return 'HOLD'

    # ----------------------------------------------------------
    #  顺势加仓逻辑
    # ----------------------------------------------------------
    # 2026-05-17: 加仓参数全部从 unified_config.yaml 加载 (strategy.add_position.*)
    # 旧硬编码已删除，防止 SSOT 漂移

    def _get_add_position_cfg(self) -> dict:
        """从 SSOT 加载加仓参数"""
        from config.config_center import get_config_center as _gcc_ap
        return _gcc_ap().get('strategy.add_position', {
            'min_score': 1.5, 'min_confidence': 0.70, 'min_price_pct': 0.005,
            'min_hold_minutes': 15, 'max_times_per_day': 1, 'qty_ratio': 0.5,
        })

    def _try_add_position(self, code: str, position, result, consensus_signal, signal_id: str) -> bool:
        """
        尝试顺势加仓。满足全部条件时执行加仓并返回 True，否则返回 False。

        条件：
        1. 共识得分 >= _ADD_SCORE_MIN (1.5)
        2. 置信度 >= _ADD_CONFIDENCE_MIN (0.70)
        3. 当前价 > 持仓均价 × (1 + 0.005)（顺势上涨才加）
        4. 持仓时间 >= _ADD_HOLD_MIN_MINUTES (15 分钟)
        5. 今日加仓次数 < _ADD_MAX_TIMES_PER_DAY (1)
        """
        try:
            cfg = self._get_add_position_cfg()
            # 1. 得分检查
            if result.score < cfg['min_score']:
                logger.debug(f"[加仓拒绝] {code} 得分={result.score:.2f} < {cfg['min_score']}")
                return False

            # 2. 置信度检查
            if result.confidence < cfg['min_confidence']:
                logger.debug(f"[加仓拒绝] {code} 置信度={result.confidence:.2f} < {cfg['min_confidence']}")
                return False

            # 3. 价格须高于均价（顺势）
            entry_price = position.entry_price
            if entry_price > 0 and result.price < entry_price * (1 + cfg['min_price_pct']):
                logger.debug(
                    f"[加仓拒绝] {code} 当前价={result.price:.3f} 未超过均价*{1+cfg['min_price_pct']}="
                    f"{entry_price * (1 + cfg['min_price_pct']):.3f}"
                )
                return False

            # 4. 持仓时间检查
            from datetime import datetime as _dt
            entry_time_str = getattr(position, 'entry_time', None)
            if entry_time_str:
                et = _dt.fromisoformat(entry_time_str) if isinstance(entry_time_str, str) else entry_time_str
                hold_minutes = (_dt.now() - et).total_seconds() / 60
                if hold_minutes < cfg['min_hold_minutes']:
                    logger.debug(
                        f"[加仓拒绝] {code} 持仓仅 {hold_minutes:.1f}min < {cfg['min_hold_minutes']}min"
                    )
                    return False

            # 5. 今日加仓次数检查
            today_add_count = self.position_manager.get_today_add_count(code)
            if today_add_count >= cfg['max_times_per_day']:
                logger.info(
                    f"[加仓拒绝] {code} 今日已加仓 {today_add_count} 次，"
                    f"上限={cfg['max_times_per_day']}"
                )
                return False

            # 6. 计算加仓数量（原信号量的50%，最小100股）
            add_qty = max(100, int(consensus_signal.quantity * cfg['qty_ratio']))

            # 7. 构造加仓信号（注入 is_add_position 标记供 executor 识别）
            add_signal = dataclasses.replace(
                consensus_signal,
                quantity=add_qty,
                extra={**consensus_signal.extra, 'is_add_position': True}
            )

            # 8. 执行加仓下单（走 OPEN 流程，但带 ADD 标记）
            trade_result = self.trade_executor.execute(add_signal, 'OPEN')
            if not trade_result.success:
                logger.warning(f"[加仓失败] {code} 下单失败: {trade_result.message}")
                return False

            # 9. 更新持仓均价 + 止盈止损（合并到原有 OPEN 仓位）
            self.position_manager.add_to_position(
                code=code,
                add_qty=add_qty,
                add_price=result.price,
                add_time=datetime.now().isoformat(),
                signal_id=signal_id
            )

            # 10. 记录加仓流水（is_add=1 用于统计今日加仓次数）
            self.position_manager.record_add_position(
                code=code,
                add_qty=add_qty,
                add_price=result.price,
                add_time=datetime.now().isoformat(),
                signal_id=signal_id
            )

            # 11. 记录信号执行
            signal_tracker.record_execution(
                signal_id, executed=True,
                execution_price=result.price,
                execution_time=datetime.now().isoformat()
            )

            logger.info(
                f"[顺势加仓] {code} +{add_qty}股 @ {result.price:.3f} | "
                f"得分={result.score:+.2f} | 置信度={result.confidence:.2f} | "
                f"原均价={entry_price:.3f} | 今日第{today_add_count + 1}次 | "
                f"订单={trade_result.order_id}"
            )

            # 12. 钉钉推送加仓通知
            try:
                action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
                self.notifier.send_text(
                    f"[顺势加仓] {code} {action_str} +{add_qty}股 @ {result.price:.3f}\n"
                    f"原均价={entry_price:.3f} | 得分={result.score:+.2f} | 今日第{today_add_count + 1}次"
                )
            except Exception:
                pass

            return True

        except Exception as e:
            logger.error(f"[加仓异常] {code}: {e}", exc_info=True)
            return False

    def _execute_stop_loss(self, code: str, current_price: float):
        """
        执行止损平仓 (2026-03-23 优化: 确保整手数量)
        2026-03-27 修复: 添加止损冷却机制，防止频繁触发
        2026-05-15 优化: 冷却时间从 5s 延长到 30s，减少重复触发
        2026-05-15 修复: 修复 _get_lot_size() 不存在导致 AttributeError 的 bug
        2026-05-15 优化: 统一冷却锁管理，消除双路径竞争

        Phase 6: 委托给 ExitStateEngine (配置驱动，默认关闭)
        """
        # Phase 6: 配置驱动委托给引擎
        if getattr(self, '_exit_delegation_enabled', False) and self._exit_engine is not None:
            position = self.position_manager.get_position(code)
            if position is not None:
                return self._exit_engine.execute_stop_loss(
                    code=code,
                    current_price=current_price,
                    position=position,
                    signal=None,
                    trade_type="STOP_LOSS",
                )
        # Phase 10A: 委托给 ExitExecutor 服务
        if self._exit_executor is not None:
            return self._exit_executor.execute_stop_loss(code, current_price)
        # 止损冷却递增：按失败次数增加冷却时间
        if not hasattr(self, '_stop_loss_fail_count'):
            self._stop_loss_fail_count = {}
        
        fail_count = self._stop_loss_fail_count.get(code, 0)
        # 2026-05-16: 迁移至 SSOT (unified_config.yaml → risk_limits.stop_loss_cooldown_schedule)
        from config.config_center import get_config_center as _gcc_cooldown
        COOLDOWN_SCHEDULE = _gcc_cooldown().get('risk_limits.stop_loss_cooldown_schedule', [30, 60, 120, 300, 300])
        cooldown_seconds = COOLDOWN_SCHEDULE[min(fail_count, len(COOLDOWN_SCHEDULE) - 1)]
        
        now = time.time()
        last_stop_time = self._stop_loss_cooldown.get(code, 0)
        if now - last_stop_time < cooldown_seconds:
            logger.info(
                f"[止损冷却] {code} 冷却中，距离上次止损 {now - last_stop_time:.1f}s < {cooldown_seconds}s (失败次数={fail_count})"
            )
            return  # 仍在冷却中
        
        # P0 修复 (2026-04-23): 跳过已标记为 BAD_ORDER 的标的
        bad_orders = getattr(self, '_bad_orders', {})
        if code in bad_orders:
            bo = bad_orders[code]
            logger.warning(
                f"[止损跳过] {code} 已标记为 BAD_ORDER (失败{bo['fail_count']}次: {bo['error'][:60]})"
            )
            return
        
        # P0 修复 (2026-05-19): 用线程安全的原子锁保护 _stop_loss_inflight
        # 根因: 旧代码 check-then-act 存在竞态窗口，两个线程可能同时通过检查
        if not hasattr(self, '_stop_loss_inflight'):
            self._stop_loss_inflight = {}
        if not hasattr(self, '_stop_loss_inflight_lock'):
            self._stop_loss_inflight_lock = threading.Lock()

        with self._stop_loss_inflight_lock:
            if self._stop_loss_inflight.get(code, False):
                logger.debug(f"[止损跳过] {code} 已有止损线程正在执行，跳过重复触发")
                return
            self._stop_loss_inflight[code] = True
        
        position = self.position_manager.get_position(code)
        if position is None:
            return
        
        # 获取每手股数 (2026-05-07 P0 修复: 动态获取而非配置默认值)
        # P0 修复 (2026-05-15): 修复 _get_lot_size() 不存在导致 AttributeError 的 bug
        from app.config import DEFAULT_ORDER_QTY_BY_CODE, HK_LOT_SIZES
        lot_size = HK_LOT_SIZES.get(code) or DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        
        # 碎股处理: 如果持仓不足一手，以实际持仓数量平仓 (2026-05-07 P0 修复)
        close_qty = position.quantity
        if close_qty < lot_size:
            logger.warning(
                f"[止损碎股处理] {code} 持仓{close_qty}股不足一手({lot_size}股)，"
                f"以实际数量平仓 (若 Futu 拒绝将标记为 BAD_ORDER)"
            )
        elif close_qty % lot_size != 0:
            # 向上取整到整手
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size
            logger.warning(
                f"[止损数量调整] {code} 原数量={position.quantity}，调整为整手={close_qty}"
            )
        
        logger.info(
            f"[止损执行] {code} 持仓={position.quantity}股 | 每手={lot_size}股 | "
            f"平仓={close_qty}股 | 入场价={position.entry_price} | 当前价={current_price}"
        )
        
        # 创建止损信号 (P1 修复 2026-05-21: 传递 trace_id)
        from app.strategy.base import Signal, SignalAction
        stop_loss_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"止损触发 (盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%)",
            strategy="止损",
            trace_id=getattr(position, 'trace_id', '') or '',
            extra={
                "stop_loss": True,
                "entry_price": position.entry_price,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price,
                "exit_reason": "stop_loss"
            }
        )
        
        logger.info(f"[止损尝试] {code} | 数量={close_qty}股 | 每手={lot_size}股")
        
        # P0 修复 (2026-04-23): 验证 Futu 实际持仓，避免幽灵仓导致无效下单
        # position_sync 的 _futu_positions 缓存是 Futu 真实持仓的唯一可信源
        # 2026-04-24 修复: 模拟盘 (TRADE_ENV=1) 不应验证 Futu 实际持仓，
        # 本地 DB 就是唯一真实源。Futu 模拟盘无实际成交，验证永远失败。
        try:
            if config.TRADE_ENV == 1:
                logger.debug(f"[止损验证] {code} 模拟盘跳过 Futu 持仓验证 (TRADE_ENV=1)")
            else:
                from app.trader.position_sync import get_position_sync
                ps = get_position_sync()
                futu_pos = ps._futu_positions.get(code)
                if futu_pos is None or futu_pos.quantity <= 0:
                    # Futu 无此持仓 → 可能是幽灵仓，也可能是刚卖出尚未同步
                    # P1 修复 (2026-05-16): 先用 current_price 平仓保留 P&L，避免 entry=exit 丢失盈亏
                    logger.warning(
                        f"[止损跳过] {code} Futu 无实际持仓 (本地有 {close_qty}股)，"
                        f"以当前价 {current_price:.2f} 平仓 (防 P&L 丢失)"
                    )
                    try:
                        self.position_manager.close_position(code, current_price, datetime.now().isoformat())
                        logger.info(f"[持仓修复] {code} 已平仓 (P&L 已记录)")
                    except Exception as _e:
                        logger.error(f"[持仓修复] {code} 清理失败: {_e}")
                    self._stop_loss_fail_count.pop(code, None)
                    self._stop_loss_cooldown.pop(code, None)
                    self._trailing_stop_cache.pop(code, None)
                    self._peak_price_cache.pop(code, None)
                    return
        except Exception as _e:
            logger.debug(f"[止损Futu验证] {code} 检查异常 (降级放行): {_e}")
        
        # P0 修复 (2026-04-23): 止损执行放入后台线程，不阻塞 Worker
        # 避免 Futu API 慢响应/失败时阻塞整个行情处理管线
        # 2026-05-17 P0: 用 in-flight 锁防止重复触发，冷却改为线程完成后设置
        # 2026-05-19 P0: 已在上方原子锁中设置 inflight=True，此处直接启动线程
        threading.Thread(
            target=self._do_execute_stop_loss,
            args=(code, stop_loss_signal, 'CLOSE'),
            daemon=True,
            name=f"StopLoss_{code}",
        ).start()
    
    def _do_execute_stop_loss(self, code: str, signal, trade_type: str):
        """在后台线程中执行止损平仓 (P0 修复: 不阻塞 Worker)
        2026-05-17 P0: 完成后清除 in-flight 锁，防止线程泄漏
        """
        try:
            # 确保 position 检查也在 try 中，失败时也能清理 inflight
            position = self.position_manager.get_position(code)
            if position is None:
                logger.warning(f"[止损跳过] {code} 线程启动后持仓已被清除")
                return
            
            trade_result = self.trade_executor.execute(signal, trade_type)
            current_price = signal.price
            
            if trade_result.success:
                logger.info(
                    f"[止损成功] {code} | 入场价={position.entry_price} | "
                    f"出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
                )
                # 清理相关状态（成功执行后自动清理，防止残留）
                self._stop_loss_fail_count.pop(code, None)
                self._stop_loss_cooldown.pop(code, None)
                self._trailing_stop_cache.pop(code, None)
                self._peak_price_cache.pop(code, None)
                logger.debug(f"[状态清理] {code} 止损相关缓存已清理")
                try:
                    current_scenario = None
                    if hasattr(self, '_scenario_cache') and code in self._scenario_cache:
                        scenario_obj = self._scenario_cache[code]
                        current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)
                    self._consensus.record_signal_outcome(
                        code=code, action=position.action,
                        pnl=trade_result.pnl or 0.0, scenario=current_scenario,
                    )
                except Exception as _e:
                    logger.debug(f"[信号质量] 止损记录异常: {_e}")
                try:
                    self.notifier.send_close_position_message(
                        signal=signal, trade_result=trade_result,
                        position=position, consensus_result=None
                    )
                except Exception as e:
                    logger.warning(f"[止损推送失败] {code}: {e}")
                # 领域事件 — 止损平仓
                pnl_rate = (current_price - position.entry_price) / position.entry_price if position.entry_price else 0
                get_event_bus().emit(DomainEvent(
                    type=EventType.TRADE_CLOSED,
                    data=TradeClosedPayload(
                        code=code, market='HK',
                        entry_time=position.entry_time,
                        exit_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        entry_price=position.entry_price,
                        exit_price=current_price,
                        quantity=position.quantity,
                        pnl=trade_result.pnl or 0.0,
                        pnl_pct=pnl_rate,
                        strategy='止损',
                        action=position.action,
                        exit_reason='stop_loss',
                        tags=['stop_loss', 'risk_exit'],
                        stop_loss=position.stop_loss,
                        take_profit=position.take_profit,
                        highest_price=getattr(position, 'highest_price', None),
                        lowest_price=getattr(position, 'lowest_price', None),
                    ),
                    correlation_id=getattr(position, 'signal_id', None) or None,
                ))
            else:
                error_msg = trade_result.message
                logger.error(f"[止损失败] {code}: {error_msg}")
                self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
                fail_count = self._stop_loss_fail_count[code]
                
                if fail_count >= 5:
                    if not hasattr(self, '_bad_orders'):
                        self._bad_orders = {}
                    self._bad_orders[code] = {
                        'error': error_msg, 'fail_count': fail_count,
                        'last_fail_time': time.time(),
                        'position_id': getattr(position, 'id', 'unknown'),
                    }
                    logger.error(
                        f"[止损严重警告] {code} 连续{fail_count}次止损失败，"
                        f"已标记为 BAD_ORDER 并停止重试！最后错误: {error_msg}"
                    )
                    try:
                        from app.notification import send_dingtalk_message
                        send_dingtalk_message(
                            f"🚨 P0 止损严重警告: {code} 连续{fail_count}次止损失败，"
                            f"已停止自动重试！错误: {error_msg}\n请立即人工平仓！"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[止损后台执行异常] {code}: {e}")
        finally:
            # 2026-05-17 P0: 无论成功/失败/异常，都清除 in-flight 锁
            # 2026-05-19 P0: 使用锁保护清除操作 + 设置冷却时间戳
            if hasattr(self, '_stop_loss_inflight_lock'):
                with self._stop_loss_inflight_lock:
                    inflight = getattr(self, '_stop_loss_inflight', {})
                    inflight.pop(code, None)
            else:
                inflight = getattr(self, '_stop_loss_inflight', {})
                inflight.pop(code, None)

            # 2026-05-19: 修复 _stop_loss_cooldown 死代码 — 设置冷却时间戳防止频繁重试
            self._stop_loss_cooldown[code] = time.time()

    def _execute_take_profit(self, code: str, current_price: float):
        """
        执行止盈平仓 (2026-03-23 优化: 确保整手数量)

        Args:
            code: 股票代码
            current_price: 当前价格

        Phase 6: 委托给 ExitStateEngine (配置驱动，默认关闭)
        """
        # Phase 6: 配置驱动委托给引擎
        if getattr(self, '_exit_delegation_enabled', False) and self._exit_engine is not None:
            position = self.position_manager.get_position(code)
            if position is not None:
                return self._exit_engine.execute_take_profit(
                    code=code,
                    current_price=current_price,
                    position=position,
                    signal=None,
                )
        # Phase 14: 委托给 ExitExecutor 服务
        if self._exit_executor is not None:
            return self._exit_executor.execute_take_profit(code, current_price)
        position = self.position_manager.get_position(code)
        if position is None:
            return

        # 2026-05-19: 修复 _trailing_exit_time 死代码 — 设置触发时间戳供冷却检查使用
        # TrailingCooldownCheck 依赖此时间戳防止短期内重新开仓
        self._trailing_exit_time[code] = time.time()
        
        # 获取每手股数配置
        from app.config import DEFAULT_ORDER_QTY_BY_CODE
        lot_size = DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        
        # 确保平仓数量为整手
        close_qty = position.quantity
        if close_qty % lot_size != 0:
            close_qty = ((close_qty + lot_size - 1) // lot_size) * lot_size
            logger.warning(
                f"[止盈数量调整] {code} 原数量={position.quantity}，调整为整手={close_qty}"
            )
        
        # 创建止盈信号 (P1 修复 2026-05-21: 传递 trace_id)
        from app.strategy.base import Signal, SignalAction
        take_profit_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"止盈触发 (盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%)",
            strategy="止盈",
            trace_id=getattr(position, 'trace_id', '') or '',
            extra={
                "take_profit": True,
                "entry_price": position.entry_price,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price,
                "exit_reason": "take_profit"
            }
        )
        
        logger.info(f"[止盈尝试] {code} | 数量={close_qty}股 | 每手={lot_size}股")
        
        # P0 修复 (2026-04-23): 同样验证 Futu 实际持仓
        # 2026-04-24 修复: 模拟盘 (TRADE_ENV=1) 跳过 Futu 持仓验证
        try:
            if config.TRADE_ENV == 1:
                logger.debug(f"[止盈验证] {code} 模拟盘跳过 Futu 持仓验证 (TRADE_ENV=1)")
            else:
                from app.trader.position_sync import get_position_sync
                ps = get_position_sync()
                futu_pos = ps._futu_positions.get(code)
                if futu_pos is None or futu_pos.quantity <= 0:
                    # Futu 无此持仓 → 可能是幽灵仓，也可能是刚卖出尚未同步
                    # P1 修复 (2026-05-16): 先用 current_price 平仓保留 P&L
                    logger.warning(
                        f"[止盈跳过] {code} Futu 无实际持仓，"
                        f"以当前价 {current_price:.2f} 平仓 (防 P&L 丢失)"
                    )
                    try:
                        position_manager.close_position(code, current_price, datetime.now().isoformat())
                        logger.info(f"[持仓修复] {code} 已平仓 (P&L 已记录)")
                    except Exception as _e:
                        logger.error(f"[持仓修复] {code} 清理失败: {_e}")
                    return
        except Exception as _e:
            logger.debug(f"[止盈Futu验证] {code} 检查异常 (降级放行): {_e}")
        
        # 执行平仓
        trade_result = self.trade_executor.execute(take_profit_signal, 'CLOSE')
        
        if trade_result.success:
            logger.info(
                f"[止盈成功] {code} | 入场价={position.entry_price} | "
                f"出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            # 清理追踪止盈缓存
            self._trailing_stop_cache.pop(code, None)
            self._peak_price_cache.pop(code, None)
            # 记录到信号质量管理（止盈路径）
            # 记录到信号质量管理（未知路径，传入 scenario）
            try:
                # 获取当前场景
                current_scenario = None
                if hasattr(self, '_scenario_cache') and code in self._scenario_cache:
                    scenario_obj = self._scenario_cache[code]
                    current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)

                self._consensus.record_signal_outcome(
                    code=code,
                    action=position.action,
                    pnl=trade_result.pnl or 0.0,
                    scenario=current_scenario,  # Phase 1: 传入场景标签
                )
            except Exception as _e:
                logger.debug(f"[信号质量] 记录异常：{_e}")
                logger.debug(f"[信号质量] 止盈记录异常: {_e}")
            # 推送止盈通知
            try:
                self.notifier.send_close_position_message(
                    signal=take_profit_signal,
                    trade_result=trade_result,
                    position=position,
                    consensus_result=None
                )
            except Exception as e:
                logger.warning(f"[止盈推送失败] {code}: {e}")
            # 领域事件 — 止盈平仓
            pnl_rate = (current_price - position.entry_price) / position.entry_price
            get_event_bus().emit(DomainEvent(
                type=EventType.TRADE_CLOSED,
                data=TradeClosedPayload(
                    code=code, market='HK',
                    entry_time=position.entry_time,
                    exit_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    entry_price=position.entry_price,
                    exit_price=current_price,
                    quantity=position.quantity,
                    pnl=trade_result.pnl or 0.0,
                    pnl_pct=pnl_rate,
                    strategy='止盈',
                    action=position.action,
                    exit_reason='take_profit',
                    tags=['take_profit', 'profit_exit'],
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    highest_price=getattr(position, 'highest_price', None),
                    lowest_price=getattr(position, 'lowest_price', None),
                ),
                correlation_id=getattr(position, 'signal_id', None) or None,
            ))
            # 记录交易评估 (新增)
            try:
                evaluator = get_evaluator()
                trade_id = f"TP-{code}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                pnl_rate = (current_price - position.entry_price) / position.entry_price
                evaluator.record_trade({
                    'trade_id': trade_id,
                    'code': code,
                    'action': position.action,
                    'entry_time': position.entry_time,
                    'entry_price': position.entry_price,
                    'quantity': close_qty,
                    'trigger_strategy': '追踪止盈',
                    'trigger_reason': f"盈利 {pnl_rate*100:.2f}% 触发止盈线"
                })
                evaluator.record_exit(trade_id, current_price, datetime.now().isoformat(), trade_result.pnl)
            except Exception as e:
                logger.debug(f"[止盈评估记录失败] {code}: {e}")
            # 回填 signal_tracker pnl（2026-03-24 修复：健康看板策略层数据）
            try:
                signal_tracker.record_exit_by_code(
                    code=code,
                    exit_price=current_price,
                    pnl=trade_result.pnl or 0.0,
                    exit_reason='take_profit',
                )
            except Exception as e:
                logger.debug(f"[signal_tracker] 止盈回填失败: {e}")
        else:
            logger.error(f"[止盈失败] {code}: {trade_result.message}")

    def _execute_partial_take_profit(self, code: str, current_price: float, ratio: float):
        """
        执行分批止盈平仓 (2026-03-24 优化: 整手对齐 + 追踪止盈场景至少留1手)
        
        整手对齐规则（港股）：
        1. 按 ratio 计算原始平仓量，向下取整到整手
        2. 若取整后不足一手：
           a. 总持仓 > 1手：改为平仓恰好1手（保留剩余继续跟趋势）
           b. 总持仓 == 1手：退化为全仓平仓
        3. 平仓后剩余量必须 >= 1手，否则改为全仓平仓
        
        Args:
            code: 股票代码
            current_price: 当前价格
            ratio: 平仓比例 (0.6 = 60% = 追踪止盈场景)
        """
        # Phase 14: 委托给 ExitExecutor 服务
        if self._exit_executor is not None:
            return self._exit_executor.execute_partial_take_profit(code, current_price, ratio)
        position = self.position_manager.get_position(code)
        if position is None:
            return
        
        # 优先使用 HK_LOT_SIZES，fallback 到 DEFAULT_ORDER_QTY_BY_CODE
        from app.config import DEFAULT_ORDER_QTY_BY_CODE, HK_LOT_SIZES
        lot_size = HK_LOT_SIZES.get(code) or DEFAULT_ORDER_QTY_BY_CODE.get(code, 100)
        
        total_qty = position.quantity
        
        # 计算平仓数量 - 向下取整到整手
        raw_close_qty = int(total_qty * ratio)
        close_qty = (raw_close_qty // lot_size) * lot_size
        
        if close_qty < lot_size:
            # 不足一手：若总持仓 > 1手，退为平仓1手；否则全平
            if total_qty >= lot_size * 2:
                close_qty = lot_size
                logger.info(
                    f"[分批止盈调整] {code} 按{ratio:.0%}计算={raw_close_qty}股 不足一手({lot_size}股)，"
                    f"改为平仓最少1手={lot_size}股，保留{total_qty - lot_size}股跟趋势"
                )
            elif total_qty >= lot_size:
                # 只有1手，全平
                close_qty = total_qty
                ratio = 1.0
                logger.info(
                    f"[分批止盈调整] {code} 仅1手持仓，退化为全仓平仓"
                )
            else:
                # 持仓不足一手：尝试全仓平仓 (2026-05-07 P0 修复)
                logger.warning(
                    f"[分批止盈碎股处理] {code} 持仓{total_qty}股不足一手({lot_size}股)，"
                    f"尝试全仓平仓"
                )
                close_qty = total_qty
                ratio = 1.0
        else:
            # 检查剩余数量是否满足整手
            remaining_qty = total_qty - close_qty
            if remaining_qty > 0 and remaining_qty < lot_size:
                # 剩余不足一手：尝试减少平仓量以保留整手剩余
                adjusted_close = total_qty - lot_size
                if adjusted_close >= lot_size:
                    old_close = close_qty
                    close_qty = (adjusted_close // lot_size) * lot_size
                    logger.info(
                        f"[分批止盈调整] {code} 剩余{remaining_qty}股不足一手，"
                        f"平仓量从{old_close}→{close_qty}股，保留{total_qty - close_qty}股整手剩余"
                    )
                else:
                    # 调整后平仓量也不足一手，全平
                    close_qty = total_qty
                    ratio = 1.0
                    logger.info(
                        f"[分批止盈调整] {code} 无法保留整手剩余，改为全仓平仓"
                    )
        
        remaining_qty = total_qty - close_qty if ratio < 1.0 else 0
        logger.info(
            f"[分批止盈尝试] {code} 持仓={total_qty}股 | "
            f"平仓={close_qty}股({close_qty/total_qty:.0%}) | 每手={lot_size}股 | "
            f"剩余={remaining_qty}股"
        )
        
        # 创建分批止盈信号 (P1 修复 2026-05-21: 传递 trace_id)
        from app.strategy.base import Signal, SignalAction
        partial_signal = Signal(
            code=code,
            name=position.code,
            action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
            price=current_price,
            quantity=close_qty,
            reason=f"分批止盈 ({ratio:.0%}) 盈亏={((current_price - position.entry_price) / position.entry_price * 100):.2f}%",
            strategy="分批止盈",
            trace_id=getattr(position, 'trace_id', '') or '',
            extra={
                "partial_take_profit": True,
                "ratio": ratio,
                "entry_price": position.entry_price,
                "pnl_rate": (current_price - position.entry_price) / position.entry_price,
                "exit_reason": "partial_take_profit"
            }
        )
        
        # 执行部分平仓
        if ratio >= 1.0:
            trade_result = self.trade_executor.execute(partial_signal, 'CLOSE')
        else:
            trade_result = self.trade_executor.execute_partial_close(partial_signal, ratio)
        
        if trade_result.success:
            logger.info(
                f"[分批止盈成功] {code} | 平仓{close_qty}股({ratio:.0%}) | "
                f"入场价={position.entry_price} | 出场价={current_price} | 盈亏={trade_result.pnl:.2f}"
            )
            
            # P1 修复: 分批止盈后，立即更新剩余仓位的止损价到保本价
            # 防止剩余利润回撤，实现"无风险持仓"
            remaining_qty = total_qty - close_qty
            if remaining_qty > 0:
                # 计算保本止损价 (Entry + 1.5% buffer to cover fees/slippage)
                # 这里复用状态机的逻辑
                try:
                    from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                    # 如果配置了 breakeven_stop 的 buffer，优先使用
                    be_cfg = _dem.get_breakeven_stop_params()
                    fee_buffer = be_cfg.get('fee_buffer', 0.015) # 默认 1.5%
                except Exception:
                    fee_buffer = 0.015

                if position.action == 'BUY':
                    new_sl = position.entry_price * (1 + fee_buffer)
                else:
                    new_sl = position.entry_price * (1 - fee_buffer)
                
                # 只有当新止损优于旧止损时才更新 (例如在盈利状态下，新止损应高于原止损)
                # 做多时: 新 SL (Entry*1.015) > 原 SL (Entry*0.97) -> 更新
                if position.action == 'BUY' and new_sl > position.stop_loss:
                    self.position_manager.update_stop_loss_take_profit(code, new_sl, position.take_profit)
                    logger.info(
                        f"[风控升级] {code} 分批止盈后止损上移至 {new_sl:.2f} (保本+{fee_buffer:.1%})"
                    )
                elif position.action == 'SELL' and new_sl < position.stop_loss:
                    self.position_manager.update_stop_loss_take_profit(code, new_sl, position.take_profit)
                    logger.info(
                        f"[风控升级] {code} 分批止盈后止损下移至 {new_sl:.2f} (保本-{fee_buffer:.1%})"
                    )
            # 记录到信号质量管理（分批止盈路径）
            # 记录到信号质量管理（未知路径，传入 scenario）
            try:
                # 获取当前场景
                current_scenario = None
                if hasattr(self, '_scenario_cache') and code in self._scenario_cache:
                    scenario_obj = self._scenario_cache[code]
                    current_scenario = scenario_obj.value if hasattr(scenario_obj, 'value') else str(scenario_obj)

                self._consensus.record_signal_outcome(
                    code=code,
                    action=position.action,
                    pnl=trade_result.pnl or 0.0,
                    scenario=current_scenario,  # Phase 1: 传入场景标签
                )
            except Exception as _e:
                logger.debug(f"[信号质量] 记录异常：{_e}")
                logger.debug(f"[信号质量] 分批止盈记录异常: {_e}")
            # 回填 signal_tracker pnl（2026-03-24 修复：健康看板策略层数据）
            try:
                signal_tracker.record_exit_by_code(
                    code=code,
                    exit_price=current_price,
                    pnl=trade_result.pnl or 0.0,
                    exit_reason='trailing_stop',
                )
            except Exception as e:
                logger.debug(f"[signal_tracker] 分批止盈回填失败: {e}")
            # 领域事件 — 分批止盈平仓
            pnl_rate = (current_price - position.entry_price) / position.entry_price
            get_event_bus().emit(DomainEvent(
                type=EventType.TRADE_CLOSED,
                data=TradeClosedPayload(
                    code=code, market='HK',
                    entry_time=position.entry_time,
                    exit_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    entry_price=position.entry_price,
                    exit_price=current_price,
                    quantity=close_qty,
                    pnl=trade_result.pnl or 0.0,
                    pnl_pct=pnl_rate,
                    strategy=f'分批止盈({ratio:.0%})',
                    action=position.action,
                    exit_reason='partial_take_profit',
                    tags=['partial_take_profit', 'profit_exit'],
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    highest_price=getattr(position, 'highest_price', None),
                    lowest_price=getattr(position, 'lowest_price', None),
                ),
                correlation_id=getattr(position, 'signal_id', None) or None,
            ))
        else:
            # 失败时尝试全仓平仓
            logger.warning(
                f"[分批止盈失败] {code}: {trade_result.message}，尝试全仓平仓"
            )
            full_signal = Signal(
                code=code,
                name=position.code,
                action=SignalAction.SELL if position.action == 'BUY' else SignalAction.BUY,
                price=current_price,
                quantity=position.quantity,
                reason=f"分批止盈失败后全仓平仓",
                strategy="分批止盈兜底",
                trace_id=getattr(position, 'trace_id', '') or '',
                extra={
                    "partial_take_profit": True,
                    "fallback": True,
                    "entry_price": position.entry_price,
                    "exit_reason": "partial_take_profit_fallback"
                }
            )
            retry_result = self.trade_executor.execute(full_signal, 'CLOSE')
            if retry_result.success:
                logger.info(f"[分批止盈兜底成功] {code} | 全仓平仓{position.quantity}股")
            else:
                logger.error(f"[分批止盈兜底失败] {code}: {retry_result.message}")


    # ----------------------------------------------------------
    #  【P0 2026-04-21 修复】异步行情处理工作线程
    #  将耗时的止损/止盈/策略计算从回调线程移到后台线程，
    #  防止阻塞 futu SDK 的 CallbackExecutor 导致回调堆积。
    # ----------------------------------------------------------
    def _start_workers(self):
        """启动异步处理工作线程"""
        self._worker_running = True
        self._quote_worker = threading.Thread(
            target=self._quote_worker_loop, daemon=True, name="QuoteWorker"
        )
        self._quote_worker.start()
        self._kline_worker = threading.Thread(
            target=self._kline_worker_loop, daemon=True, name="KlineWorker"
        )
        self._kline_worker.start()
        logger.info("异步行情处理工作线程已启动")

    def _stop_workers(self):
        """停止异步处理工作线程"""
        self._worker_running = False
        # Send sentinel values to unblock queue.get()
        try:
            self._quote_queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self._kline_queue.put_nowait(None)
        except queue.Full:
            pass

    def _quote_worker_loop(self):
        """Quote 数据处理工作循环 (v2: per-code 去重, 队列只存 code)"""
        logger.info("[QuoteWorker] 启动")
        while self._worker_running:
            try:
                code = self._quote_queue.get(timeout=2)
            except queue.Empty:
                continue
            if code is None:
                break
            try:
                with self._quote_lock:
                    quote = self._latest_quotes.pop(code, None)
                    self._pending_codes.discard(code)
                if quote is not None:
                    self._process_quote_data(code, quote)
            except Exception as e:
                logger.error(f"[QuoteWorker] 处理异常: {e}", exc_info=True)
        logger.info("[QuoteWorker] 退出")

    def _kline_worker_loop(self):
        """K 线数据处理工作循环 (v3: 双队列优先 — 持仓股优先处理)"""
        logger.info("[KlineWorker] 启动 (双队列优先模式)")
        while self._worker_running:
            key = None
            # 优先消费持仓股队列
            try:
                key = self._priority_kline_queue.get(timeout=0.1)
            except queue.Empty:
                # 优先队列空，消费普通队列
                try:
                    key = self._kline_queue.get(timeout=2)
                except queue.Empty:
                    continue
            if key is None:
                break
            try:
                with self._kline_lock:
                    kline = self._latest_klines.pop(key, None)
                    self._pending_kline_keys.discard(key)
                if kline is not None:
                    code, k_type = key
                    self._process_kline_data(code, kline, k_type)
            except Exception as e:
                logger.error(f"[KlineWorker] 处理异常：{e}", exc_info=True)
        logger.info("[KlineWorker] 退出")

    def _process_quote_data(self, code: str, quote: dict):
        """处理单个 Quote 数据 (v2: 轻量化 — 只更新价格+缓存, 退出逻辑在 kline worker)

        P0 2026-05-04: 移除与 _process_kline_data 重复的退出检查
        (止损/止盈/软止损预警), 只保留:
        - DB 价格同步
        - 峰值价格缓存 (供动态退出管理器使用)
        - 持仓漂移检测
        - on_quote 策略
        """
        current_price = quote.get('last_price', 0) or quote.get('last_done', 0)
        if current_price <= 0:
            return

        pos_now = self.position_manager.get_position(code)

        # 同步更新 DB 中的 current_price
        if pos_now:
            self.position_manager.update_price(code, current_price)

        # 更新峰值价格缓存 (供 dynamic_exit_manager 在 kline worker 中使用)
        if pos_now:
            if code not in self._peak_price_cache:
                self._peak_price_cache[code] = current_price
            else:
                if pos_now.action == 'BUY':
                    self._peak_price_cache[code] = max(
                        self._peak_price_cache[code], current_price
                    )
                else:
                    self._peak_price_cache[code] = min(
                        self._peak_price_cache[code], current_price
                    )
            # P2 修复 (2026-05-21): 同步写入 DB，避免重启后峰值丢失
            if pos_now.action == 'BUY' and current_price > self._peak_price_cache.get(code, 0):
                self.position_manager.update_peak_price(code, highest_price=current_price)
            elif pos_now.action == 'SELL' and current_price < self._peak_price_cache.get(code, float('inf')):
                self.position_manager.update_peak_price(code, lowest_price=current_price)
        else:
            self._peak_price_cache.pop(code, None)

        # P7 fix (2026-04-26): 持仓漂移检测 — 浮亏超 3% 时触发策略权重惩罚
        if pos_now:
            try:
                pnl_pct = (current_price - pos_now.entry_price) / pos_now.entry_price
                if pos_now.action == 'SELL':
                    pnl_pct = -pnl_pct
                if pnl_pct < -0.03:
                    scaled = -0.05 * (abs(pnl_pct) / 0.03)
                    self._adaptive_weight_manager.apply_drift_penalty(code, scaled)
            except Exception as _dp_e:
                logger.debug(f"[漂移检测] {code} 异常: {_dp_e}")

        # =================================================================
        #  Flash Exit — Tick 级闪崩防御 (P1 2026-05-06)
        #  在 Push 回调中检测 60s 内反向波动，优先级高于 K_1M 粒度的退出检查
        #  Phase 6: 委托给 ExitStateEngine (配置驱动)
        # =================================================================
        if pos_now and self._flash_exit_enabled:
            if self._exit_engine is not None:
                # 使用引擎版本
                self._exit_engine.check_flash_exit(
                    code=code,
                    current_price=current_price,
                    position=pos_now,
                )
            else:
                # 回退到原始实现
                self._check_flash_exit(
                    code=code,
                    current_price=current_price,
                    position=pos_now,
                )

        # 策略计算 — 只调用 on_quote 策略
        for strategy in self.strategies:
            if strategy.name in ('price_break', 'fusion_v3'):
                try:
                    sig = strategy.on_quote(code, quote)
                    if sig:
                        self._process_signal(sig)
                except Exception as e:
                    logger.debug(f"[策略 on_quote] {code} {strategy.name} 异常: {e}")

    def _check_flash_exit(self, code: str, current_price: float, position):
        """Flash Exit — 60s 内反向波动 >= ATR * multiplier 时立即平仓 (P1 2026-05-06)

        在 Push 回调 (毫秒级) 中检测，弥补 K_1M 粒度无法覆盖的闪崩缺口。
        参考美股 exit_policy.yaml flash_exit 规则。
        """
        # Phase 14: 委托给 ExitExecutor 服务
        if self._exit_executor is not None:
            return self._exit_executor.check_flash_exit(
                code, current_price, position, self._get_atr)
        if not self._flash_exit_enabled:
            return

        now_ts = time.time()

        # 获取 ATR
        atr = self._get_atr(code)
        if atr <= 0:
            # ATR 不可用 → 降级到固定 3%
            atr = current_price * 0.03

        # 获取上次快照
        prev_price, prev_ts = self._flash_snapshots.get(code, (current_price, now_ts))
        self._flash_snapshots[code] = (current_price, now_ts)

        # 超过窗口 → 重置
        if (now_ts - prev_ts) > self._flash_window_seconds:
            return

        # 计算反向波动
        direction = position.action  # 'BUY' or 'SELL'
        if direction == 'BUY':
            adverse = prev_price - current_price
        else:
            adverse = current_price - prev_price

        if adverse <= 0:
            return

        threshold = atr * self._flash_atr_mult
        if adverse < threshold:
            return

        # 触发 Flash Exit
        logger.critical(
            f"🚨 [Flash Exit] {code} 方向={direction} | "
            f"{self._flash_window_seconds:.0f}s 内反向波动={adverse:.4f} ({adverse/atr:.1f}x ATR) "
            f">= 阈值={threshold:.4f} ({self._flash_atr_mult}x ATR) | "
            f"prev_price={prev_price:.4f} → current={current_price:.4f}"
        )

        # 立即执行止损
        try:
            self._execute_stop_loss(code, current_price)
        except Exception as e:
            logger.error(f"[Flash Exit] {code} 执行失败: {e}", exc_info=True)

        # 清理缓存
        self._peak_price_cache.pop(code, None)
        self._flash_snapshots.pop(code, None)

    def _get_kline_processor(self):
        """
        懒加载 KlineProcessor（延迟初始化，首次使用时创建）
        
        Returns:
            KlineProcessor 实例
        """
        if self._kline_processor is None:
            from app.data_subscriber_engines.kline_processor import KlineProcessor
            from app.data_subscriber_engines.cache_manager import CacheManager
            
            # 创建 CacheManager（如果尚未创建）
            if not hasattr(self, '_cache_manager') or self._cache_manager is None:
                self._cache_manager = CacheManager()
            
            # 创建 KlineProcessor，注入依赖
            self._kline_processor = KlineProcessor(
                position_manager=self.position_manager,
                cache=self._cache_manager,
                strategies=self._strategies,
                consensus_engine=self._consensus,
                kline_exit_checker=self._kline_exit_checker,
                kline_buffers=self._kline_buffers,
                regime_detector=self._regime_detector,
                scenario_classifier=getattr(self, '_scenario_classifier', None),
                scenario_memory=getattr(self, '_scenario_memory', None),
                layered_manager=self.layered_manager,
                get_atr_fn=getattr(self, '_get_atr', None),
                execute_stop_loss_fn=self._execute_stop_loss,
                execute_take_profit_fn=self._execute_take_profit,
                execute_partial_take_profit_fn=self._execute_partial_take_profit,
                soft_stop_warnings=self.soft_stop_warnings,  # 向后兼容
                # Phase 3c: 多周期趋势相关依赖
                mtf_engine=getattr(self, '_mtf_engine', None),
                kline_buffers_by_period=getattr(self, '_kline_buffers_by_period', {}),
                multi_timeframe_trends=getattr(self, '_multi_timeframe_trends', {}),
                trends_updated=getattr(self, '_trends_updated', set()),
                sync_trend_change_fn=getattr(self, '_sync_trend_change', None),
            )
            logger.info("[KlineProcessor] 延迟初始化完成（依赖注入）")
        
        return self._kline_processor

    def _process_kline_data(self, code: str, kline: dict, k_type: str):
        """处理单个 K 线数据（在工作线程中运行）"""
        # 提取当前价格 (用于止损/止盈检查)
        current_price = kline.get('close', 0) or kline.get('last_price', 0)
        
        # 多周期趋势更新
        self._update_multi_timeframe_trend(code, k_type, kline)

        # 更新 K 线 buffer
        self._kline_buffers[code].push_kline(kline)

        # 分层策略管理器
        if self.layered_manager:
            try:
                self.layered_manager.update_kline_cache(code, k_type, kline)
            except Exception as e:
                logger.debug(f"[分层策略] K线缓存更新异常: {e}")

        # MarketRegimeDetector 接入多周期数据
        if k_type in ("K_15M", "K_30M", "K_60M"):
            try:
                self._regime_detector.update_data(code, kline)
            except Exception as e:
                logger.debug(f"[regime多周期] {code} {k_type} 更新失败: {e}")

        # =================================================================
        #  P0 修复: 禁用重复的旧追踪止盈/保本/分批逻辑，防止与状态机冲突
        # 状态机 (check_exit_priority) 现已全权处理所有退出逻辑
        # =================================================================
        
        # 旧逻辑: 独立追踪止盈 (已移至状态机)
        # if current_price > 0:
        #     pos_now = self.position_manager.get_position(code)
        #     self._update_trailing_stop(code, current_price, pos_now)
        #     trail_price = self._trailing_stop_cache.get(code, 0)
        #     ...

        # 旧逻辑: 保本止损 (已移至状态机)
        # if self.position_manager.check_breakeven_stop(code, current_price):
        #     self.position_manager.update_breakeven_stop(code)

        # 旧逻辑: 分批止盈 (已移至状态机)
        # partial_ratio = self.position_manager.check_partial_take_profit(code, current_price)
        # if partial_ratio > 0: ...

        # Phase 15: 委托给 KlineExitChecker 服务
        if self._kline_exit_checker is not None:
            if self._kline_exit_checker.check_exits(
                code=code,
                current_price=current_price,
                k_type=k_type,
                execute_stop_loss_fn=self._execute_stop_loss,
                execute_take_profit_fn=self._execute_take_profit,
                execute_partial_take_profit_fn=self._execute_partial_take_profit,
            ):
                return

        # =================================================================
        # P0 修复 (2026-05-19): 删除重复的 inline 止损/止盈逻辑
        # 根因：KlineExitChecker (L2810-2819) 已全权处理止损/止盈，
        #       但 L2821-2878 仍然执行 inline 硬止损和止盈，导致多退出系统并发冲突
        # 影响：同一持仓可能被多个系统同时触发退出，止损/止盈价格不一致，日志混乱
        # 修复：删除 inline 逻辑，KlineExitChecker 成为唯一退出执行入口
        # =================================================================

        # P0 2026-05-21: 软止损预警委托给 KlineProcessor
        # 原逻辑：2919-2934 行 inline 实现
        # 新逻辑：委托给 KlineProcessor._check_soft_stop_warning()
        if current_price > 0:
            pos_now = self.position_manager.get_position(code)
            if pos_now:
                processor = self._get_kline_processor()
                processor._check_soft_stop_warning(code, current_price, pos_now)

        # P0 2026-05-21: 动态退出逻辑委托给 KlineProcessor
        # 原逻辑：2929-3025 行 inline 实现
        # 新逻辑：委托给 KlineProcessor._check_dynamic_exit()
        if k_type == "K_1M" and pos_now:
            processor = self._get_kline_processor()
            if processor._check_dynamic_exit(code, current_price, pos_now):
                return  # 触发了退出，直接返回

            # P6 fix: 时间止损 + 放量异常退出 (从 _process_quote_data 迁移)
            if k_type == "K_1M" and pos_now:
                time_stop_enabled = False
                try:
                    from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem_ts
                    time_stop_cfg = _dem_ts.current_params.get('time_stop', {})
                    time_stop_enabled = time_stop_cfg.get('enabled', False)
                    if time_stop_enabled:
                        max_hold_minutes = time_stop_cfg.get('max_hold_minutes', 480)
                        entry_time_str = getattr(pos_now, 'entry_time', None)
                        if entry_time_str:
                            from datetime import datetime as _dt_exit
                            et = _dt_exit.fromisoformat(entry_time_str.replace(' ', 'T')) if isinstance(entry_time_str, str) else entry_time_str
                            hold_minutes = (_dt_exit.now() - et).total_seconds() / 60
                            pnl_pct = (current_price - pos_now.entry_price) / pos_now.entry_price
                            if pos_now.action == 'SELL':
                                pnl_pct = -pnl_pct
                            if hold_minutes > max_hold_minutes and abs(pnl_pct) < 0.01:
                                logger.info(f"[时间止损] {code} 持仓{hold_minutes:.0f}分钟超限, 强制平仓")
                                self._execute_take_profit(code, current_price)
                                self._trailing_stop_cache.pop(code, None)
                                self._peak_price_cache.pop(code, None)
                                return
                except Exception as _ts_e:
                    # 2026-05-19: 时间止损异常必须记录，防止仓位保护失效
                    logger.warning(f"[时间止损] {code} 检查异常: {_ts_e}")

                # 放量异常退出
                if self._kline_buffers[code].size >= 25:
                    try:
                        vols = list(self._kline_buffers[code].volume)
                        if len(vols) >= 20:
                            avg_vol_20 = sum(vols[-21:-1]) / 20
                            current_vol = vols[-1]
                            pnl_pct = (current_price - pos_now.entry_price) / pos_now.entry_price
                            if pos_now.action == 'SELL':
                                pnl_pct = -pnl_pct
                            if current_vol > avg_vol_20 * 3.0 and pnl_pct > 0.03:
                                logger.info(f"[放量止盈] {code} 量比={current_vol/avg_vol_20:.1f}x, 部分止盈50%")
                                self._execute_partial_take_profit(code, current_price, 0.5)
                    except Exception as _vol_e:
                        # 2026-05-19: 放量止盈异常记录，避免静默失败
                        logger.warning(f"[放量止盈] {code} 检查异常: {_vol_e}")

        # 更新 ADX
        self._update_adx(code, kline)

        # 场景识别（多周期）
        if k_type in ("K_5M", "K_15M") and self._scenario_classifier is not None:
            try:
                buf = self._kline_buffers[code]
                if buf.size >= 30:
                    klines_for_scenario = []
                    closes = list(buf.close)
                    highs = list(buf.high)
                    lows = list(buf.low)
                    opens = list(buf.open)
                    volumes = list(buf.volume)
                    n = min(100, len(closes))
                    for i in range(-n, 0):
                        klines_for_scenario.append({
                            'open': opens[i], 'high': highs[i],
                            'low': lows[i], 'close': closes[i], 'volume': volumes[i],
                        })
                    if klines_for_scenario:
                        scenario_result = self._scenario_classifier.classify(code, klines_for_scenario)
                        if self._scenario_memory is not None:
                            smoothed_scenario = self._scenario_memory.update(
                                code, scenario_result.scenario_type, scenario_result.confidence
                            )
                            self._consensus.update_scenario(code, smoothed_scenario)
                            logger.info(
                                f"[场景分类/多周期] {code} {k_type} {smoothed_scenario.value} | "
                                f"置信度={scenario_result.confidence:.2f}"
                            )
            except Exception as e:
                logger.debug(f"[场景分类/多周期] {code} {k_type} 异常：{e}")

        # 策略计算（仅 K_1M）
        if k_type == "K_1M":
            for strategy in self.strategies:
                try:
                    # P0 修复 2026-05-04: 注入 shared_buffer 供因子策略计算指标
                    # IndicatorBuffer 存储的是分列的 deque，需转换为策略需要的 list[dict] 格式
                    shared_buffer = []
                    if code in self._kline_buffers:
                        buf = self._kline_buffers[code]
                        opens, highs, lows, closes, volumes = list(buf.open), list(buf.high), list(buf.low), list(buf.close), list(buf.volume)
                        shared_buffer = [
                            {"open": o, "high": h, "low": l, "close": c, "volume": v}
                            for o, h, l, c, v in zip(opens, highs, lows, closes, volumes)
                        ]
                    sig = strategy.on_kline(code, kline, k_type, shared_buffer=shared_buffer)
                    if sig:
                        self._process_signal(sig)
                except Exception as e:
                    logger.debug(f"[策略 on_kline] {code} {strategy.name} 异常: {e}")

    def _process_signal(self, signal: Signal):
        if signal is None:
            return
        self._consensus.submit(signal)

    def _on_position_opened_callback(self, code: str, source: str):
        """
        开仓回调 — 新持仓开仓时动态订阅行情
        
        Args:
            code: 股票代码
            source: 来源标识 (e.g., SOURCE_POSITION)
        """
        logger.info(f"[PositionOpened] 新持仓 {code} (source={source}) — 动态订阅行情")
        try:
            # 订阅单个标的的行情和 K 线
            self.subscribe(codes=[code])
        except Exception as e:
            logger.warning(f"[PositionOpened] {code} 动态订阅失败: {e}")

    # ----------------------------------------------------------
    #  行情订阅
    # ----------------------------------------------------------
    def subscribe(self, codes=None):
        """
        行情订阅 - 2026-04-22 升级为动态池驱动

        Args:
            codes: 可选，显式指定订阅列表。None 时从动态池获取。
        """
        # Phase 16: SubscriptionManager 因 quote_ctx 延迟创建问题暂不委托
        # quote_ctx 在 __init__ 时为 None，SubscriptionManager 捕获 None 后无法工作
        # TODO: 待 SubscriptionManager 支持 lazy quote_ctx 后启用
        # if self._subscription_manager is not None:
        #     return self._subscription_manager.subscribe(codes)

        from futu import SubType

        ctx = self._get_quote_ctx()

        sub_type_map = {
            "QUOTE":  SubType.QUOTE,
            "K_1M":   SubType.K_1M,
            "K_3M":   SubType.K_3M,
            "K_5M":   SubType.K_5M,
            "K_15M":  SubType.K_15M,
            "K_30M":  SubType.K_30M,
            "K_60M":  SubType.K_60M,
            "K_DAY":  SubType.K_DAY,
        }

        # 获取订阅标的列表
        if codes is None:
            subscribe_list = self._get_subscribe_codes()
        else:
            subscribe_list = codes
        
        # 过滤港股
        subscribe_list = [c for c in subscribe_list if c.startswith("HK.")]
        
        sub_types = [sub_type_map[st] for st in config.SUBSCRIBE_TYPES if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        logger.info(f"开始订阅 {len(subscribe_list)} 只港股标的, 类型={[str(s) for s in sub_types]}")
        success_count = 0
        fail_count = 0
        for code in subscribe_list:
            for st in sub_types:
                ret, msg = ctx.subscribe([code], [st])
                if ret == 0:
                    success_count += 1
                    logger.debug(f"订阅成功: {code} -> {st}")
                else:
                    fail_count += 1
                    if "无权限" in str(msg) or "no permission" in str(msg).lower():
                        logger.warning(f"订阅跳过(无权限): {code} -> {st} | {msg}")
                    else:
                        logger.error(f"订阅失败: {code} -> {st}: {msg}")
        logger.info(f"订阅完成: 成功={success_count}, 失败={fail_count}, 总计={success_count+fail_count}")
        self._subscribed = True
        
        # Phase 1: 启动预热 — 从 Futu 拉取历史 K 线填充 MTF 缓存
        # 解决重启后 MTF 对齐度为 0 导致无法交易的问题
        self._warm_up_buffers()


    def _warm_up_buffers(self):
        """
        Phase 1: 启动预热 (Warm-up) — 从 Futu 拉取历史 K 线填充 MTF 缓存
        
        目标:
            系统重启或隔夜后，内存中的 K 线 Buffer 为空。
            本方法会拉取最近 3 天的 K 线数据并推入 Buffer，
            从而立即初始化多周期趋势 (_multi_timeframe_trends)。
        
        策略:
            1. 获取 Watchlist
            2. 对每个标的请求 K_60M 和 K_DAY (最影响 MTF 对齐度)
            3. 少量 K_1M (辅助短期趋势)
            4. 限制并发和频率，防止触发 OpenD Limit
        """
        import time
        from datetime import datetime, timedelta
        from futu import KLType, SubType
        
        watchlist = _gcc_dsub().get('strategy.watch_list_hk', [])
        if not watchlist:
            logger.warning("[预热] 无 Watchlist，跳过预热")
            return

        try:
            pool_codes = self._get_subscribe_codes()
            if pool_codes:
                watchlist = pool_codes
        except Exception:
            pass

        logger.info(f"[预热] 开始拉取 {len(watchlist)} 只股票历史数据...")
        
        # 定义需要的 K 线类型和数量
        # 重点关注长周期，因为它们恢复最慢
        warmup_targets = [
            (KLType.K_60M, 50),
            (KLType.K_DAY, 60),
            (KLType.K_15M, 100),
        ]
        
        success_count = 0
        fail_count = 0

        for code in watchlist:
            try:
                for ktype, count in warmup_targets:
                    end_time = datetime.now().strftime('%Y-%m-%d')
                    start_time = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')
                    
                    ret, data, _ = self._quote_ctx.request_history_kline(
                        code, start=start_time, end=end_time, ktype=ktype, autype='qfq'
                    )
                    if ret == 0 and data is not None and not data.empty:
                        # 将数据推入 Buffer
                        # 注意: request_history_kline 返回的是 DataFrame
                        # _process_kline_data 需要 dict 格式，但这里我们直接填充 buffer
                        
                        # Determine the correct key for the buffer
                        key = ktype.name if hasattr(ktype, 'name') else str(ktype)
                        if key.startswith('KLType.'):
                            key = key.split('.')[-1]
                        
                        buffer = self._kline_buffers_by_period[code][key]
                        
                        for _, row in data.iterrows():
                            # 构造 kline dict 以复用 push_kline 逻辑
                            kline = {
                                'time_key': row['time_key'],
                                'open': row['open'],
                                'high': row['high'],
                                'low': row['low'],
                                'close': row['close'],
                                'volume': row['volume'],
                            }
                            buffer.push_kline(kline)
                            
                        # 触发趋势更新
                        # 手动调用一次更新，使用最后一条数据
                        last_row = data.iloc[-1]
                        kline_last = {
                            'time_key': last_row['time_key'],
                            'open': last_row['open'],
                            'high': last_row['high'],
                            'low': last_row['low'],
                            'close': last_row['close'],
                            'volume': last_row['volume'],
                        }
                        self._update_multi_timeframe_trend(code, key, kline_last)
                        
                        success_count += 1
                    
                    time.sleep(0.1) # 简单限流
            except Exception as e:
                fail_count += 1
                logger.warning(f"[预热] {code} 失败: {e}")

        logger.info(f"[预热] 完成: 成功填充 {success_count} 组数据, {fail_count} 组失败")
        logger.info("[预热] MTF 缓存已初始化，策略可随时响应")


    def unsubscribe_all(self):
        """
        取消所有港股行情订阅 (2026-04-23 新增)
        用于港股收盘后释放富途 OpenD 槽位。
        """
        # Phase 16: SubscriptionManager 因 quote_ctx 延迟创建问题暂不委托
        # if self._subscription_manager is not None:
        #     return self._subscription_manager.unsubscribe_all()

        from futu import SubType

        if not self._quote_ctx:
            logger.debug("[动态订阅] 无活跃连接，无需退订")
            return

        # 获取当前订阅的代码列表
        try:
            codes = self._get_subscribe_codes()
            codes = [c for c in codes if c.startswith("HK.")]
        except Exception as e:
            logger.warning(f"[动态订阅] 获取代码列表失败: {e}")
            codes = _gcc_dsub().get('strategy.watch_list_hk', [])

        if not codes:
            logger.debug("[动态订阅] 无代码，无需退订")
            return

        sub_type_map = {
            "QUOTE":  SubType.QUOTE,
            "K_1M":   SubType.K_1M,
            "K_3M":   SubType.K_3M,
            "K_5M":   SubType.K_5M,
            "K_15M":  SubType.K_15M,
            "K_30M":  SubType.K_30M,
            "K_60M":  SubType.K_60M,
            "K_DAY":  SubType.K_DAY,
        }
        sub_types = [sub_type_map[st] for st in config.SUBSCRIBE_TYPES if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        logger.info(f"开始退订 {len(codes)} 只港股标的 (收盘释放槽位)...")
        success_count = 0
        fail_count = 0

        for code in codes:
            for st in sub_types:
                try:
                    ret, msg = self._quote_ctx.unsubscribe([code], [st])
                    if ret == 0:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    logger.warning(f"[退订] {code} {st} 异常: {e}")

        self._subscribed = False
        logger.info(f"[动态订阅] 退订完成: 成功={success_count}, 失败={fail_count} | 槽位已释放")

    def _setup_handlers(self):
        from futu import StockQuoteHandlerBase, CurKlineHandlerBase

        # 【P0 2026-04-21 修复】轻量化 handler — 只更新心跳 + 入队
        # 重型逻辑（止损/止盈/策略计算）移到独立工作线程，
        # 防止阻塞 futu SDK 的 CallbackExecutor 导致回调堆积。
        # 【P0 2026-05-04 修复】Per-code 去重: 队列只存 code, 最新报价在 _latest_quotes dict
        # 同一标的多次推送只保留最新一条, 队列深度从数千降至标的数 (≤50)
        subscriber = self

        class MyQuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_str):
                # 心跳更新（必须在回调线程中快速执行）
                subscriber._last_quote_time = time.time()
                try:
                    ret, data = super().on_recv_rsp(rsp_str)
                    if ret != 0:
                        return
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        quote = row.to_dict()
                        with subscriber._quote_lock:
                            # 更新最新报价 (覆盖旧值)
                            subscriber._latest_quotes[code] = quote
                            # 同 code 已在队列中 → 跳过, worker 取时会拿到最新 quote
                            if code not in subscriber._pending_codes:
                                subscriber._pending_codes.add(code)
                                try:
                                    subscriber._quote_queue.put_nowait(code)
                                except queue.Full:
                                    subscriber._pending_codes.discard(code)
                                    logger.warning(f"[QuoteHandler] 队列已满，丢弃 {code} 行情")
                except Exception as e:
                    logger.error(f"[QUOTE回调异常] {e}", exc_info=True)

        class MyKlineHandler(CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_str):
                # 心跳更新
                subscriber._last_quote_time = time.time()
                try:
                    ret, data = super().on_recv_rsp(rsp_str)
                    if ret != 0:
                        return
                    for _, row in data.iterrows():
                        code = row.get("code", "")
                        kline = row.to_dict()
                        k_type = kline.get("k_type", "K_1M")
                        key = (code, k_type)
                        # P0 2026-05-21: 双队列优先 — 持仓股入优先队列，非持仓股入普通队列
                        with subscriber._kline_lock:
                            subscriber._latest_klines[key] = kline
                            if key not in subscriber._pending_kline_keys:
                                subscriber._pending_kline_keys.add(key)
                                # 判断是否持仓
                                is_holding = subscriber.position_manager.get_position(code) is not None
                                target_queue = subscriber._priority_kline_queue if is_holding else subscriber._kline_queue
                                queue_name = "priority" if is_holding else "normal"
                                try:
                                    target_queue.put_nowait(key)
                                except queue.Full:
                                    logger.warning(f"[KlineHandler] {queue_name}队列已满，丢弃 {code} {k_type}")
                except Exception as e:
                    logger.error(f"[KLINE 回调异常] {e}", exc_info=True)

        ctx = self._get_quote_ctx()
        self._quote_handler = MyQuoteHandler()
        self._kline_handler = MyKlineHandler()
        ctx.set_handler(self._quote_handler)
        ctx.set_handler(self._kline_handler)
        logger.info("行情回调 handler 已设置 (quote + kline，异步处理模式)")

    def _reconnect(self):
        """
        断线重连 (优化版 - 2026-03-27): 优先复用现有连接
        
        优化逻辑:
        1. 先测试现有连接是否可用
        2. 仅在网络真正断开时重建
        3. 重新订阅而非重新创建连接
            
            # 1. 优先尝试复用现有连接 (2026-03-27 新增)
            if self._quote_ctx is not None:
                try:
                    # 测试连接是否有效
                    ret, data = self._quote_ctx.get_basic_info("HK.00700", num=1)
                    if ret == 0:
                        logger.info("[重连] 现有连接仍然有效，仅重新订阅行情...")
                        self._resubscribe()
                        self._setup_handlers()
                        self._last_quote_time = time.time()
                        logger.info("[重连] 重新订阅成功，行情已恢复")
                        return
                    else:
                        logger.warning(f"[重连] 现有连接测试失败：{data}，需要重建")
                except Exception as test_error:
                    logger.warning(f"[重连] 现有连接测试异常：{test_error}，需要重建")
            
            # 2. 现有连接不可用，关闭并重建
            logger.info("[重连] 现有连接不可用，开始重建新连接...")
        """
        # Phase 16: SubscriptionManager 因 quote_ctx 延迟创建问题暂不委托
        # if self._subscription_manager is not None:
        #     return self._subscription_manager.reconnect()

        if not self._reconnect_lock.acquire(blocking=False):
            logger.warning("[重连] 已有重连任务进行中，跳过")
            return
        try:
            logger.info("[重连] 检测到行情断线，开始自动重连...")
            # 关闭旧连接（强制释放 Futu SDK 内部 socket）
            try:
                if self._quote_ctx is not None:
                    self._quote_ctx.close()
            except Exception:
                pass
            finally:
                self._quote_ctx = None

            # 等待 3 秒让 OpenD 清理旧会话
            time.sleep(2)

            # 重建连接 → 重订阅 → 重设 handler（指数退避，最多 5 次）
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    self.subscribe()        # 内部会设置 _subscribed = True
                    self._setup_handlers()
                    self._last_quote_time = time.time()
                    logger.info(f"[重连] 第 {attempt} 次重连成功，行情已恢复")
                    return
                except Exception as e:
                    logger.error(f"[重连] 第 {attempt} 次重连失败: {e}")
                    # 清除上次失败的 ctx，下次重建
                    try:
                        if self._quote_ctx is not None:
                            self._quote_ctx.close()
                    except Exception:
                        pass
                    self._quote_ctx = None
                    time.sleep(min(5 * attempt, 30))  # 最长等 30 秒

            logger.error("[重连] 达到最大重试次数，行情断线未恢复，请人工检查")
        finally:
            self._reconnect_lock.release()

    def start(self):
        self._running = True
        try:
            self.subscribe()
            self._setup_handlers()
            self._start_workers()  # P0: 启动异步处理工作线程
            logger.info(
                f"数据订阅已启动 | 共识窗口={CONSENSUS_WINDOW}s | "
                f"触发阈值={CONSENSUS_THRESHOLD} | "
                f"ADX趋势>{CONSENSUS_ADX_TREND} 震荡<{CONSENSUS_ADX_RANGE} | "
                f"推送冷却={SIGNAL_COOLDOWN}s"
            )
        except Exception as e:
            logger.error(f"数据订阅启动异常：{e}")
            raise

        def cleanup_loop():
            # 心跳检测：连续超过 180 秒无行情数据，判定断线并重连
            HEARTBEAT_TIMEOUT = 180  # 秒
            queue_check_counter = 0
            sync_counter = 0  # 订阅同步计数器
            while self._running:
                time.sleep(60)
                signal_store.cleanup_expired()
                self._cleanup_cooldown()
                
                # [旁路新闻信号] 仅在交易时段注入，非交易时间跳过 (2026-04-25 P1)
                if self._is_trading_hours():
                    self._poll_news_signals(signal_store)

                # ============================================================
                # 【动态订阅管理】2026-04-23 新增：根据交易时段自动订阅/退订
                # ============================================================
                is_market_open = self._is_trading_hours()
                
                if is_market_open and not self._subscribed:
                    # 进入交易时段且未订阅 → 重新订阅
                    logger.info("[动态订阅] 检测到交易时段，启动订阅...")
                    try:
                        self.subscribe()
                    except Exception as e:
                        logger.error(f"[动态订阅] 订阅失败: {e}")
                        
                elif not is_market_open and self._subscribed:
                    # 离开交易时段且已订阅 → 退订释放槽位
                    logger.info("[动态订阅] 检测到非交易时段，退订释放槽位...")
                    try:
                        self.unsubscribe_all()
                    except Exception as e:
                        logger.error(f"[动态订阅] 退订失败: {e}")

                # 每 10 分钟同步一次订阅与动态池 (仅交易时段)
                sync_counter += 1
                if sync_counter >= 10 and is_market_open:
                    sync_counter = 0
                    try:
                        self._sync_subscription()
                    except Exception as e:
                        logger.warning(f"[订阅同步] 异常: {e}")

                # 每 5 分钟检查一次队列深度 (仅交易时段)
                queue_check_counter += 1
                if queue_check_counter >= 5:
                    queue_check_counter = 0
                    if is_market_open:
                        q_depth = self._quote_queue.qsize()
                        k_depth = self._kline_queue.qsize()
                        if q_depth > 0 or k_depth > 0:
                            logger.warning(
                                f"[队列监控] 交易时段队列堆积: "
                                f"quote_queue={q_depth} | kline_queue={k_depth}"
                            )
                        else:
                            logger.debug(f"[队列监控] 队列正常 (quote=0, kline=0)")

                # 仅在交易时段内做心跳检测（非交易时段本来就没有行情）
                if is_market_open:
                    elapsed = time.time() - self._last_quote_time
                    if elapsed > HEARTBEAT_TIMEOUT:
                        logger.warning(
                            f"[心跳] {elapsed:.0f}s 未收到行情数据，"
                            f"疑似断线，启动重连..."
                        )
                        threading.Thread(
                            target=self._reconnect, daemon=True
                        ).start()

        t = threading.Thread(target=cleanup_loop, daemon=True)
        t.start()

    def _cleanup_cooldown(self):
        now = time.time()
        with self._cooldown_lock:
            expired = [k for k, t in self._cooldown.items() if now - t > SIGNAL_COOLDOWN * 2]
            for k in expired:
                del self._cooldown[k]

    # ----------------------------------------------------------
    # 【新增 2026-04-06】质量管理器辅助方法
    # ----------------------------------------------------------

    def _check_quality_and_valuation(self, code: str, price: float) -> Optional[Dict]:
        """
        检查质量和估值 (巴菲特理念)

        流程:
        1. 质量评分 - 过滤垃圾股 (JUNK)
        2. 水位检查 - 确定是否允许交易
        3. 估值检查 - 确保有安全边际

        Args:
            code: 股票代码
            price: 当前价格

        Returns:
            质量检查结果字典，或 None (检查失败)
        """
        try:
            # 1. 获取基本面数据
            fundamentals = self._get_fundamentals(code)
            if not fundamentals:
                logger.warning(f"[质量检查] {code} 基本面数据不可用")
                return None

            # 2. 质量评分
            quality = self.quality_scorer.calculate(code, fundamentals)
            if not quality:
                logger.warning(f"[质量检查] {code} 质量评分失败")
                return None

            logger.info(f"[质量检查] {code} 质量评分：{quality.score} ({quality.class_.value})")

            # 3. 过滤垃圾股
            if quality.class_.value == 'JUNK':
                return {'reject': True, 'reason': f'质量评级为 JUNK (得分:{quality.score})'}

            # 4. 水位检查
            can_trade, reason = self.water_level.can_trade(quality.class_.value)
            if not can_trade:
                return {'reject': True, 'reason': f'水位限制：{reason}'}

            # 5. 估值检查
            valuation = self.valuation.calculate(code, price, fundamentals)
            if not valuation:
                logger.warning(f"[质量检查] {code} 估值计算失败")
                return None

            logger.info(f"[估值检查] {code} 估值信号：{valuation.signal.value} (安全边际:{valuation.margin_of_safety_pct:.1%})")

            # 6. 过滤高估股票
            if valuation.signal.value in ('SELL', 'STRONG_SELL'):
                return {'reject': True, 'reason': f'估值过高：{valuation.notes}'}

            return {
                'reject': False,
                'quality_class': quality.class_.value,
                'quality_score': quality.score,
                'valuation_signal': valuation.signal.value,
            }
        except Exception as e:
            logger.warning(f"[质量检查] {code} 异常：{e}")
            return None

    def _get_fundamentals(self, code: str) -> Optional[Dict]:
        """
        获取基本面数据 (模拟数据，实际需要从 API 获取)

        Args:
            code: 股票代码

        Returns:
            基本面数据字典
        """
        try:
            if '00700' in code:  # 腾讯 - 优质股
                return {
                    'roe_5y_avg': 25.0, 'debt_ratio': 0.25, 'operating_cashflow': 1e9,
                    'gross_margin': 0.45, 'margin_trend': 0.05, 'revenue_growth_3y': 0.20,
                    'eps': 20.0, 'pe_ttm': 25.0, 'growth_rate': 0.15, 'book_value': 100.0,
                }
            elif '09988' in code:  # 阿里 - 良好股
                return {
                    'roe_5y_avg': 15.0, 'debt_ratio': 0.35, 'operating_cashflow': 5e8,
                    'gross_margin': 0.35, 'margin_trend': 0.02, 'revenue_growth_3y': 0.12,
                    'eps': 15.0, 'pe_ttm': 18.0, 'growth_rate': 0.10, 'book_value': 80.0,
                }
            elif '03690' in code:  # 美团 - 良好股
                return {
                    'roe_5y_avg': 12.0, 'debt_ratio': 0.40, 'operating_cashflow': 3e8,
                    'gross_margin': 0.30, 'margin_trend': 0.03, 'revenue_growth_3y': 0.25,
                    'eps': 8.0, 'pe_ttm': 30.0, 'growth_rate': 0.20, 'book_value': 50.0,
                }
            else:  # 默认 - 一般股
                return {
                    'roe_5y_avg': 10.0, 'debt_ratio': 0.50, 'operating_cashflow': 1e8,
                    'gross_margin': 0.25, 'margin_trend': 0.0, 'revenue_growth_3y': 0.08,
                    'eps': 5.0, 'pe_ttm': 15.0, 'growth_rate': 0.08, 'book_value': 40.0,
                }
        except Exception as e:
            logger.warning(f"[基本面数据] {code} 获取失败：{e}")
            return None

    def _get_subscribe_codes(self) -> List[str]:
        """
        获取需要订阅的标的列表 (2026-04-22 新增)
        从动态池获取，空池时回退到静态 watchlist
        """
        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="HK.")
            active_codes = pool.get_active_codes()
            
            # 过滤港股
            active_codes = [c for c in active_codes if c.startswith("HK.")]
            
            if active_codes:
                breakdown = pool.get_source_breakdown()
                logger.info(
                    f"[动态池订阅] 从动态池获取 {len(active_codes)} 只港股标的 | "
                    f"来源: {breakdown}"
                )
                return active_codes
        except Exception as e:
            logger.warning(f"[动态池订阅] 获取失败: {e}，回退到静态 watchlist")
        
        # 空池回退: 使用配置中的静态 watchlist
        fallback = _gcc_dsub().get('strategy.watch_list_subscribe', _gcc_dsub().get('strategy.watch_list_hk', []))
        logger.warning(f"[动态池订阅] 动态池为空，回退到静态 watchlist ({len(fallback)} 只)")
        return fallback

    def _sync_subscription(self):
        """
        同步订阅与动态池 (2026-04-22 新增)
        定时调用，当动态池变化时调整订阅
        """
        try:
            if not self._quote_ctx or not self._running:
                return
            
            from app.utils.daily_active_pool import get_daily_active_pool
            from futu import SubType
            
            pool = get_daily_active_pool(market_prefix="HK.")
            active_codes = [c for c in pool.get_active_codes() if c.startswith("HK.")]
            
            if not active_codes:
                logger.debug("[订阅同步] 动态池为空，跳过")
                return
            
            # 简化策略: 持仓股 (POSITION) 用全类型，其他仅 QUOTE
            position_codes = set()
            other_codes = set()
            
            entries = pool.get_entries()
            for entry in entries:
                if not entry.code.startswith("HK."):
                    continue
                if entry.is_expired():
                    continue
                if entry.source == "POSITION":
                    position_codes.add(entry.code)
                else:
                    other_codes.add(entry.code)
            
            # 订阅持仓股 (全类型)
            for code in position_codes:
                for st_name in config.SUBSCRIBE_TYPES:
                    try:
                        st = getattr(SubType, st_name, None)
                        if st:
                            self._quote_ctx.subscribe(code, [st])
                    except Exception as e:
                        logger.debug(f"[订阅同步] {code} {st_name} 失败: {e}")
            
            # 订阅其他标的 (仅 QUOTE)
            for code in other_codes:
                try:
                    self._quote_ctx.subscribe(code, [SubType.QUOTE])
                except Exception as e:
                    logger.debug(f"[订阅同步] {code} QUOTE 失败: {e}")
            
            total = len(position_codes) + len(other_codes)
            logger.info(
                f"[订阅同步] 完成 | 持仓={len(position_codes)} (全类型) | "
                f"其他={len(other_codes)} (仅QUOTE) | 总计={total}"
            )
            
        except Exception as e:
            logger.warning(f"[订阅同步] 失败: {e}")

    def _resubscribe(self):
        """
        重新订阅行情 (不创建新连接) - 2026-03-27 新增
        2026-04-22 升级: 使用动态池替代静态 watchlist
        """
        try:
            if self._quote_ctx is None:
                logger.error("[重新订阅] 行情上下文为 None")
                return
            
            from futu import SubType
            codes = self._get_subscribe_codes()
            # 过滤港股
            codes = [c for c in codes if c.startswith("HK.")]
            
            for code in codes:
                try:
                    # 订阅报价
                    self._quote_ctx.subscribe(code, [SubType.QUOTE])
                    # 订阅 K 线
                    self._quote_ctx.subscribe(code, [SubType.K_1M])
                    logger.debug(f"[重新订阅] {code} 订阅成功")
                except Exception as e:
                    logger.warning(f"[重新订阅] {code} 订阅失败：{e}")
            
            logger.info(f"[重新订阅] 完成 ({len(codes)} 只标的)")
            
        except Exception as e:
            logger.error(f"[重新订阅] 失败：{e}")

    def _poll_news_signals(self, signal_store):
        """
        [旁路] 读取新闻监听器注入的信号 (2026-04-24 新增)
        零侵入现有代码，仅从共享 SQLite 表读取并注入 signal_store
        """
        try:
            import sqlite3
            import time as _time
            from app.storage.repository import get_db as _pool_get_db  # B3-13 连接池

            # P0 修复: 自动清理超过 2 小时的 pending news_event 信号
            try:
                import os as _os
                _data_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), 'data')
                ftseek_db = sqlite3.connect(_os.path.join(_data_dir, 'ftseek.db'))
                ftseek_db.execute("""
                    UPDATE signals SET status = 'expired', expire_at = datetime('now')
                    WHERE strategy = 'news_event' AND status = 'pending'
                      AND created_at < datetime('now', '-2 hours')
                """)
                expired = ftseek_db.total_changes
                ftseek_db.commit()
                ftseek_db.close()
                if expired > 0:
                    logger.info(f"[信号清理] 清理了 {expired} 个过期 news_event 信号 (>2h)")
            except Exception as e:
                logger.debug(f"[信号清理] 过期清理失败: {e}")

            db_path = _os.path.join(_data_dir, 'news_signals.db')
            with _pool_get_db(db_path) as conn:
                cursor = conn.execute("""
                    SELECT id, code, action, confidence, priority, reason 
                    FROM news_signals 
                    WHERE consumed = 0 AND priority >= 3
                    ORDER BY priority DESC, created_at DESC
                    LIMIT 3
                """)
                
                injected = 0
                # [P2 修复] 冷却集合：本轮已注入的 (code, action)，防止同批重复
                _injected_this_round = set()
                
                for row in cursor.fetchall():
                    sid, code, action, conf, priority, reason = row
                    
                    # 港股系统只接收港股信号 (HK.)，过滤 US.* (美股由 FTSeek-US 处理)
                    if not code.startswith("HK."):
                        # 标记为非港股信号为已消费，避免堆积
                        conn.execute("UPDATE news_signals SET consumed = 1 WHERE id = ?", (sid,))
                        continue
                    
                    # [P2 修复] 同批去重：同一 code+action 本轮只注入一次
                    dedup_key = (code, action)
                    if dedup_key in _injected_this_round:
                        conn.execute("UPDATE news_signals SET consumed = 1 WHERE id = ?", (sid,))
                        continue
                    
                    from app.strategy.base import Signal, SignalAction
                    # 将置信度融入 reason 字符串
                    full_reason = f"{reason} (置信度: {conf:.0%})"
                    
                    signal = Signal(
                        code=code,
                        action=SignalAction.BUY if action == "BUY" else SignalAction.SELL,
                        price=0.0,
                        quantity=0,
                        reason=full_reason,
                        strategy="news_event",
                        priority=priority,
                        timestamp=_time.time()
                    )
                    
                    signal_store.add(signal)
                    logger.info(f"📡 [旁路] 新闻信号已注入主系统: {code} {action} (conf={conf:.2f})")
                    
                    conn.execute("UPDATE news_signals SET consumed = 1 WHERE id = ?", (sid,))
                    _injected_this_round.add(dedup_key)
                    injected += 1
                
                # B3-13：commit/close 由 with 块收尾自动处理
            if injected > 0:
                logger.info(f"📡 [旁路] 本轮注入 {injected} 个新闻信号")
        except Exception as e:
            logger.debug(f"[旁路] 新闻信号轮询失败: {e}")


    def stop(self):
        self._running = False
        self._stop_workers()  # P0: 停止异步处理工作线程
        if self._quote_ctx:
            self._quote_ctx.close()
            self._quote_ctx = None
            logger.info("行情上下文已关闭")

    def get_snapshot(self, codes=None):
        ctx = self._get_quote_ctx()
        codes = codes or config.WATCH_LIST
        ret, data = ctx.get_market_snapshot(codes)
        return data if ret == 0 else None
