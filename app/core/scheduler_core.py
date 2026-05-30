"""
FTSeek-US 核心调度器 - 核心调度逻辑

包含:
- PositionStore 类
- TradingScheduler 主类框架
- 初始化和配置
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Optional
from pathlib import Path
import sys

import pytz

from app.core.config_defaults import CFG

# 路径
BASE_DIR = Path(__file__).resolve().parent.parent
# P0 Fix 2026-04-23: PROJECT_ROOT is one level above BASE_DIR
PROJECT_ROOT = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from app.data.news_provider import USNewsProvider
from app.data.market_data_router import MarketDataRouter
from app.agents.voting.voting_coordinator import VotingCoordinator
from app.agents.voting.voting_config import VotingConfig
from app.decision.consensus import ConsensusEngine
from app.core.models import (
    SignalScore, Direction, MarketRegime,
    Order, OrderType, OrderState, Position,
    TradeEvent, FailureRecord, FailureType, ExitReason, AlertLevel,
)
from app.core.events import EventStore
from app.storage.trade_store import TradeStore
from app.execution.options_selector import OptionSelector, OptionContract

logger = logging.getLogger(__name__)

# 美东时区
ET = pytz.timezone("America/New_York")


class PositionStore:
    """内存持仓管理（轻量级，无富途依赖）"""

    def __init__(self):
        self._positions: Dict[str, Position] = {}  # symbol -> Position
        # P0-4a 修复 (2026-05-03): 改用 RLock 允许 sync 线程重入
        # sync 线程获取锁后调用 add/remove (内部也获取锁) 不会死锁
        self._lock = threading.RLock()

    def add(self, symbol: str, position: Position):
        with self._lock:
            self._positions[symbol] = position

        # 自动同步动态池 (P0 修复: 传入市场前缀)
        # Task #49 (2026-05-28): 消除 silent exception, 失败记录但不中断主流程 (非关键路径)
        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool(market_prefix="US.")
            pool.add_position(symbol, metadata={
                "direction": position.direction.value if hasattr(position.direction, 'value') else str(position.direction),
                "quantity": position.quantity,
            })
        except Exception as e:
            logger.error(f"[daily_active_pool] {symbol} 添加同步失败: {e}")
            # 不中断主流程 (内存持仓已写入), 但异常向监控系统暴露

    def get(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(symbol)

    def remove(self, symbol: str):
        with self._lock:
            self._positions.pop(symbol, None)

        # 自动同步动态池 (P0 修复: 传入市场前缀)
        # Task #49 (2026-05-28): 消除 silent exception, 失败记录但不中断主流程 (非关键路径)
        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            get_daily_active_pool(market_prefix="US.").remove_position(symbol)
        except Exception as e:
            logger.error(f"[daily_active_pool] {symbol} 移除同步失败: {e}")
            # 不中断主流程 (内存持仓已移除), 但异常向监控系统暴露

    def all(self) -> List[Position]:
        with self._lock:
            return list(self._positions.values())

    def has(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._positions

    def count(self) -> int:
        with self._lock:
            return len(self._positions)


class TradingScheduler:
    """
    交易调度器主类

    负责：
    1. 美东交易时间判断
    2. 信号扫描（每 SCAN_INTERVAL_MINUTES 分钟）
    3. 持仓监控（每 MONITOR_INTERVAL_MINUTES 分钟）
    """

    # Task #46 (2026-05-28) 配置 SSOT: 类级常量仅作 fallback,
    # 实际值在 __init__ → _load_scheduler_params 内通过 ConfigKeys 从
    # unified_config.yaml 读取。如需修改, 请改 YAML, 不要改这里。
    SCAN_INTERVAL_MINUTES = 10     # fallback: scheduler.scan_interval_minutes
    MONITOR_INTERVAL_MINUTES = 0.5  # fallback: scheduler.monitor_interval_minutes
    MAX_POSITIONS = 5              # fallback: scheduler.max_positions
    _DEFAULT_MIN_SIGNAL_STRENGTH = 50    # fallback: strategies.defaults.min_strength
    _DEFAULT_MIN_CONFIDENCE = 0.5         # fallback: risk.signal_quality.min_confidence
    _DEFAULT_MIN_CONSENSUS_LEVEL = 0.20   # fallback: scheduler.min_consensus_level
    # 兼容旧引用 (保持 self.MIN_CONFIDENCE 访问可用)
    MIN_SIGNAL_STRENGTH = _DEFAULT_MIN_SIGNAL_STRENGTH
    MIN_CONFIDENCE = _DEFAULT_MIN_CONFIDENCE
    MIN_CONSENSUS_LEVEL = _DEFAULT_MIN_CONSENSUS_LEVEL

    def __init__(
        self,
        event_store: EventStore,
        execution_engine=None,
        risk_engine=None,
        notifier=None,
        watchlist: Optional[List[str]] = None,
    ):
        """
        初始化交易调度器
        
        Args:
            event_store: 事件存储
            execution_engine: 执行引擎
            risk_engine: 风控引擎
            notifier: 通知器
            watchlist: 监控股票列表
        """
        self.event_store = event_store
        self.execution_engine = execution_engine
        self.risk_engine = risk_engine
        self.notifier = notifier

        # 行情数据路由器（Futu LV3 主 → TwelveData 技术指标 → Finnhub 备 → AlphaVantage fallback）
        # P1 修复 (2026-05-04): 优先从 unified_config.yaml SSOT 读取 opend 配置
        try:
            from app.core.config import get_config
            cfg = get_config()
            futu_opend_host = cfg.get_dotted("opend.host") or os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            futu_opend_port = cfg.get_dotted("opend.port") or int(os.getenv("FUTU_OPEND_PORT", "11111"))
            enable_lv3 = cfg.get_dotted("opend.enable_lv3", True)
        except Exception:
            futu_opend_host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
            futu_opend_port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
            enable_lv3 = os.getenv("ENABLE_FUTU_LV3", "true").lower() in ("true", "1", "yes")

        self.data_router = MarketDataRouter(
            twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
            alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
            cache_ttl_seconds=900,
            futu_opend_host=futu_opend_host,
            futu_opend_port=futu_opend_port,
            enable_futu_lv3=enable_lv3,
        )
        # 保留 yahoo 属性别名，防止其他地方引用
        self.market_data = self.data_router

        # ── 分时段订阅管理器 (2026-05-01 新增) ──
        # 港股和美股共享 OpenD 1000 个槽位，各自收盘后退订释放
        self._subscription_mgr = None

        # 新闻情绪提供商：RSS + Qwen LLM（优于 Finnhub 关键词规则）
        self.news_provider = USNewsProvider()
        # 保留 news 属性别名，兼容旧引用
        self.news = self.news_provider

        # 期权选择器（SHORT 信号 → 买入 Put 期权替代卖空）
        # P1-3 修复 2026-04-26: 复用 broker_adapter 的 quote_ctx，避免浪费 OpenD LV3 槽位
        self.quote_ctx_for_options = None  # 延迟到 execution_engine.initialize() 后注入
        self.option_selector = OptionSelector(quote_ctx=None)

        # 多模型投票
        self.voting_coordinator = VotingCoordinator(
            config=VotingConfig.get_default_config(),
            timeout_seconds=25,
        )
        
        # P0 清理 2026-04-25: 移除已废弃的 ai_trading_integration
        # ai_executor 和 ai_stoploss_monitor 从未被实际调用
        # 止损/止盈由 ExitMonitor 统一处理
        self.ai_executor = None
        self.ai_stoploss_monitor = None

        # ── 从 unified_config.yaml 加载调度器运行时参数 (SSOT) ──
        self._load_scheduler_params()

        # 共识引擎
        # 从 ConfigContainer 统一读取策略权重 (替代 config_us.py 硬编码)
        from app.core.config_container import get_config as get_app_config
        app_cfg = get_app_config()
        decision_cfg = app_cfg.get_dotted("decision.strategy_weights") or {}
        self.consensus_engine = ConsensusEngine(
            config={
                "STRATEGY_WEIGHTS": decision_cfg,
                "STRATEGY_ORTHOGONALIZATION": app_cfg.get_dotted("STRATEGY_ORTHOGONALIZATION"),
                "COST_MODEL": app_cfg.get_dotted("COST_MODEL"),
            },
            event_store=event_store,
        )

        # 2026-04-15: 做空策略路由器 + 快速退出引擎
        try:
            from app.execution.short_strategy import get_short_strategy_router, load_short_config
            from app.exit.option_quick_exit import get_quick_exit_engine
            cfg = load_short_config()
            self.short_strategy_router = get_short_strategy_router(cfg)
            self.option_quick_exit = get_quick_exit_engine(cfg.get("options", {}).get("quick_exit", {}))
            logger.info("[调度器] 做空策略路由器 + 快速退出引擎已初始化")
        except Exception as e:
            logger.warning(f"[调度器] 做空策略初始化失败: {e}")
            self.short_strategy_router = None
            self.option_quick_exit = None

        # 持仓管理
        self.position_store = PositionStore()

        # Phase A (2026-05-14): 多策略协调层 (灰度, 默认 observe_only)
        # 走 strategies.coordinator.enabled 灰度开关; 关闭时无任何副作用
        self.coordinator = None
        try:
            self._init_coordinator()
        except Exception as _ce:
            logger.warning(f"[调度器·Coordinator] 初始化失败 (降级运行): {_ce}")

        # P0 2026-04-30: 跨引擎开仓互斥锁 (P1 修复 2026-05-11: 改为 RLock 防止死锁)
        # Scalping 和主管线共用此锁保护开仓临界区，防止竞态导致同标的重复开仓
        self._open_position_lock = threading.RLock()

        # P0 2026-04-20: 注入 position_store 到 RiskEngine，使仓位限制检查生效
        # 修复: 原代码中 RiskEngine 从未收到 position_store，导致仓位检查完全跳过
        if self.risk_engine:
            self.risk_engine.position_store = self.position_store
            logger.info("[调度器] position_store 已注入 RiskEngine (仓位限制检查已激活)")

        # P0 2026-04-21: 注入 capital_safety 到 RiskEngine，使总敞口计算使用实际净值
        try:
            from app.risk.capital_safety import get_capital_safety_system
            capital = get_capital_safety_system()
            if self.risk_engine:
                self.risk_engine.capital_safety = capital
                logger.info("[调度器] capital_safety 已注入 RiskEngine (总敞口计算修复)")
        except Exception as e:
            logger.warning(f"[调度器] capital_safety 注入失败 (降级运行): {e}")

        # P0-8 2026-04-26: 注入 broker_adapter 到 RiskEngine，供 broker fallback 分支按方向拆分敞口
        if self.risk_engine and self.execution_engine:
            broker = getattr(self.execution_engine, 'broker_adapter', None)
            if broker:
                self.risk_engine.broker_adapter = broker
                logger.info("[调度器] broker_adapter 已注入 RiskEngine (方向敞口拆分已激活)")

        # P1-3 2026-04-26: 复用 broker_adapter.quote_ctx 给 option_selector
        if self.execution_engine:
            ba = getattr(self.execution_engine, 'broker_adapter', None)
            if ba and getattr(ba, 'quote_ctx', None):
                self.quote_ctx_for_options = ba.quote_ctx
                self.option_selector.quote_ctx = ba.quote_ctx
                logger.info("[调度器] option_selector 已复用 broker_adapter.quote_ctx (节省 OpenD 槽位)")

        # 持久化交易记录 (F18: 走 paths.py)
        from app.core.paths import get_trade_records_db
        db_path = str(get_trade_records_db())
        self.trade_store = TradeStore(db_path)

        # P0-fix 2026-05-02: 教训注入器 (LessonInjector) — 闭环关键断链修复
        # 从 lessons DB 加载活跃教训，注入到盘前/盘中/风控各阶段
        try:
            from app.review.lesson_injector import LessonInjector
            self.lesson_injector = LessonInjector(db_path=db_path)
            active_count = len(self.lesson_injector.load_active_lessons())
            logger.info(f"[调度器] LessonInjector 已初始化 — {active_count} 条活跃教训")
        except Exception as e:
            logger.warning(f"[调度器] LessonInjector 初始化失败 (降级运行): {e}")
            self.lesson_injector = None

        # P0-fix 2026-05-02: 注入 lesson_injector 到 RiskEngine，使 PreTradeChecker 能获取教训约束
        if self.risk_engine and self.lesson_injector:
            self.risk_engine._lesson_injector = self.lesson_injector
            logger.info("[调度器] lesson_injector 已注入 RiskEngine (教训约束检查已激活)")

        # 复盘定时任务状态
        self._last_review_date: Optional[date] = None

        # Agent 桥接 (Phase 5: 盘前计划 + 盘后归因 → Hermes Agent)
        # 由 main.py 的 initialize() 注入
        self.agent_bridge = None

        # 监控股票池
        self.watchlist = watchlist or self._load_watchlist()
        # 沙盒模式: 用 sandbox universe 覆盖 watchlist (聚焦实验)
        try:
            from app.core.config_container import get_config as _gc
            _cfg = _gc()
            if _cfg.get_dotted("mode.sandbox.enabled", False):
                _univ = _cfg.get_dotted("mode.sandbox.universe", []) or []
                if _univ:
                    self.watchlist = list(_univ)
                    logger.warning(f"[SANDBOX] watchlist 覆盖为 universe: {self.watchlist}")
        except Exception as _se:
            logger.debug(f"[SANDBOX] universe 覆盖失败 (走 watchlist): {_se}")

        # P0 修复 (2026-05-12): 启动时将战斗计划标的注入动态池
        # 确保辩论/分析师产生的新标的进入轮询扫描
        self._inject_battle_plan_to_pool()

        # ── 新闻管线 V2 (2026-04-29 新增，迁移自港股) ──
        self.news_pipeline_v2 = None
        self._news_v2_running = False
        self._news_v2_thread = None
        try:
            from app.news_v2.pipeline import NewsPipelineV2
            from app.news_v2.sources.finnhub_news_source import FinnhubNewsSource
            from app.news_v2.sources.sec_edgar_source import SECEdgarSource

            # 构建 watchlist codes 和 names
            wl_codes = set()
            wl_names = set()
            for stock in self.watchlist:
                code = stock if isinstance(stock, str) else stock.get("code", "")
                name = stock.get("name", "") if isinstance(stock, dict) else ""
                if code:
                    wl_codes.add(code)
                if name:
                    wl_names.add(name)

            self.news_pipeline_v2 = NewsPipelineV2(
                watchlist_codes=wl_codes,
                watchlist_names=wl_names,
            )
            # 添加数据源 (observe 模式，零副作用)
            finnhub_src = FinnhubNewsSource(
                interval_seconds=120,
                watchlist_codes=list(wl_codes),
            )
            self.news_pipeline_v2.add_source(finnhub_src)

            sec_src = SECEdgarSource(interval_seconds=300)
            self.news_pipeline_v2.add_source(sec_src)

            # 添加 RSSHub 美股新闻源 (WSJ/Reuters/Fed)
            try:
                from app.news_v2.sources.rsshub_us_source import RSSHubUSSource
                rsshub_src = RSSHubUSSource(interval_seconds=180)
                self.news_pipeline_v2.add_source(rsshub_src)
                logger.info("[NewsV2] RSSHub 美股源已添加 (WSJ/Reuters/Fed)")
            except Exception as e:
                logger.warning("[NewsV2] RSSHub 源添加失败 (降级运行): %s", e)

            # ─── 催化辩论一手源 (2026-05-27 RFC-Catalyst) ───
            # 港交所披露易 + 证监会官网 (公司直接发布, 比二手新闻快且权威)
            try:
                from app.core.config_container import get_config as _gc_cat
                _cat_cfg = _gc_cat().get_dotted("catalyst", {}) or {}
                if isinstance(_cat_cfg, dict) and _cat_cfg.get("enabled", False):
                    _src_cfg = _cat_cfg.get("sources", {}) or {}
                    
                    # HKEX 港交所披露易
                    if _src_cfg.get("hkex", {}).get("enabled", False):
                        from app.catalyst.sources.hkex_source import HKEXSource
                        hkex_interval = int(_src_cfg["hkex"].get("interval_seconds", 120))
                        hkex_src = HKEXSource(interval_seconds=hkex_interval)
                        self.news_pipeline_v2.add_source(hkex_src)
                        logger.info("[Catalyst] HKEX 港交所源已添加 (%ds轮询)", hkex_interval)
                    
                    # CSRC 证监会官网
                    if _src_cfg.get("csrc", {}).get("enabled", False):
                        from app.catalyst.sources.csrc_source import CSRCSource
                        csrc_interval = int(_src_cfg["csrc"].get("interval_seconds", 300))
                        csrc_src = CSRCSource(interval_seconds=csrc_interval)
                        self.news_pipeline_v2.add_source(csrc_src)
                        logger.info("[Catalyst] CSRC 证监会源已添加 (%ds轮询)", csrc_interval)
            except Exception as e:
                logger.warning("[Catalyst] 一手源添加失败 (降级运行): %s", e)

            # 链路修复 (2026-05-15): 接入 CN 国产源 — 国内大陆环境优先
            # 之前问题: news_sources.cn 配置存在但 0 代码引用 (死配置)
            try:
                _cn_cfg = cfg.get_dotted("news_sources.cn", {}) or {}
                _cn_added = []

                if _cn_cfg.get("cailianpress", {}).get("enabled", False):
                    from app.news_v2.sources.cailianpress_source import CailianPressSource
                    _cls_cfg = _cn_cfg["cailianpress"]
                    cls_src = CailianPressSource(
                        interval_seconds=int(_cls_cfg.get("interval_sec", 30)),
                        keywords=_cls_cfg.get("keywords"),
                        proxy=_cls_cfg.get("proxy") or None,
                        max_per_fetch=int(_cls_cfg.get("max_per_fetch", 50)),
                    )
                    self.news_pipeline_v2.add_source(cls_src)
                    _cn_added.append("财联社")

                if _cn_cfg.get("wallstreetcn", {}).get("enabled", False):
                    from app.news_v2.sources.wallstreetcn_source import WallstreetCNSource
                    _wsc_cfg = _cn_cfg["wallstreetcn"]
                    wsc_src = WallstreetCNSource(
                        interval_seconds=int(_wsc_cfg.get("interval_sec", 60)),
                        channels=_wsc_cfg.get("channels"),
                        proxy=_wsc_cfg.get("proxy") or None,
                        max_per_channel=int(_wsc_cfg.get("max_per_channel", 20)),
                    )
                    self.news_pipeline_v2.add_source(wsc_src)
                    _cn_added.append("华尔街见闻")

                if _cn_cfg.get("eastmoney_us", {}).get("enabled", False):
                    from app.news_v2.sources.eastmoney_us_source import EastmoneyUSSource
                    _em_cfg = _cn_cfg["eastmoney_us"]
                    # symbols 默认空 → 用 watchlist 自动同步 (P1-8 已加 monitor_loop 同步)
                    em_src = EastmoneyUSSource(
                        interval_seconds=int(_em_cfg.get("interval_sec", 120)),
                        symbols=_em_cfg.get("symbols") or [],
                        proxy=_em_cfg.get("proxy") or None,
                        per_symbol_limit=int(_em_cfg.get("per_symbol_limit", 5)),
                        max_symbols_per_fetch=int(_em_cfg.get("max_symbols_per_fetch", 20)),
                    )
                    self.news_pipeline_v2.add_source(em_src)
                    _cn_added.append("东方财富")

                if _cn_added:
                    logger.info(f"[NewsV2] CN 国产源已添加: {_cn_added}")
                else:
                    logger.info("[NewsV2] CN 国产源全部 disabled (yaml news_sources.cn.*.enabled)")
            except Exception as e:
                logger.warning(f"[NewsV2] CN 源添加失败 (降级运行): {e}")

            # 挂接数据收集器 (observe 模式) + 激活地缘预筛器
            class _ObserveCollector:
                """轻量级 observe 模式收集器 — 仅记录统计"""
                def __init__(self):
                    self.stats = {"received": 0, "passed": 0, "rejected": 0}
                def record_received(self, item_dict, passed=False, reason=""):
                    self.stats["received"] += 1
                    if passed:
                        self.stats["passed"] += 1
                    else:
                        self.stats["rejected"] += 1

            collector = _ObserveCollector()
            self.news_pipeline_v2.attach_collector(collector)
            self.news_pipeline_v2._observe_collector = collector
            logger.info("[NewsV2] 数据收集器已挂接 + 地缘预筛器已激活")

            # 观察模式回调：只记录日志
            def _on_news_v2_passed(item):
                logger.info(
                    "[NewsV2-Observe] 通过 | %s | %s | score=%.2f",
                    item.source_name, item.title[:80], item.raw_score
                )

            self.news_pipeline_v2.on_news_passed = _on_news_v2_passed

            logger.info("[NewsV2] 管线已初始化 (observe 模式) | watchlist=%d", len(wl_codes))
        except Exception as e:
            logger.warning("[NewsV2] 初始化失败 (降级运行): %s", e)
            self.news_pipeline_v2 = None

        # ── 美股风控 Pipeline (2026-04-29 新增，模块化 Chain-of-Responsibility) ──
        self.us_risk_pipeline = None
        try:
            from app.risk.us_risk_pipeline import USRiskPipeline

            self.us_risk_pipeline = USRiskPipeline(
                risk_engine=None,  # risk_engine 在 _process_symbol_impl 中注入
                config={
                    "min_confidence": getattr(self, 'MIN_CONFIDENCE', type(self)._DEFAULT_MIN_CONFIDENCE),
                    "min_score": getattr(self, 'MIN_SIGNAL_STRENGTH', type(self)._DEFAULT_MIN_SIGNAL_STRENGTH),
                    "max_per_symbol": 0.25,
                    "max_total": 0.80,
                    "max_per_direction": 0.65,  # 04/30: 从50%提升, 匹配unified_config.yaml (QQQ占45%导致连续拦截)
                    "cooldown_seconds": 120,
                },
            )
            logger.info("[USRiskPipeline] 已初始化 (%d 个检查)", len(self.us_risk_pipeline.checks))
        except Exception as e:
            logger.warning("[USRiskPipeline] 初始化失败 (降级使用旧 RiskEngine): %s", e)

        # ── 财报日历 (2026-04-29 新增) ──
        self.earnings_calendar = None
        self.earnings_risk_guard = None
        try:
            from app.data.earnings_calendar import EarningsCalendar
            from app.risk.earnings_risk_guard import EarningsRiskGuard

            self.earnings_calendar = EarningsCalendar()
            self.earnings_risk_guard = EarningsRiskGuard()

            # 盘前预加载未来 7 天财报数据
            try:
                earnings_list = self.earnings_calendar.get_watchlist_earnings(days_ahead=7)
                if earnings_list:
                    # 注入 ATR 倍数到 earnings_risk_guard
                    for entry in earnings_list:
                        sector = entry.get("sector", "Unknown")
                        from app.data.earnings_calendar import get_sector_atr_multiplier
                        atr_cfg = get_sector_atr_multiplier(sector, is_earnings_period=True)
                        entry["atr_multiplier"] = atr_cfg["earnings"]
                        entry["flash_exit_multiplier"] = atr_cfg["flash_exit"]

                    self.earnings_risk_guard.update_earnings_calendar(earnings_list)
                    logger.info("[Earnings] 财报日历已加载: %d 只股票 (未来7天)", len(earnings_list))
            except Exception as e:
                logger.debug("[Earnings] 盘前预加载失败 (运行中重试): %s", e)
        except Exception as e:
            logger.warning("[Earnings] 财报模块初始化失败 (降级运行): %s", e)

        # ── VIX Monitor 全局初始化 (2026-04-29) ──
        self.vix_monitor = None
        self._last_vix_alert_level = "NORMAL"
        try:
            from app.risk.vix_monitor import VIXMonitor
            self.vix_monitor = VIXMonitor()
            logger.info("[VIX Monitor] 已初始化 (阈值: 正常<20 / 警戒<25 / 高波动<30 / 极端≥40)")
        except Exception as e:
            logger.warning("[VIX Monitor] 初始化失败 (降级使用 market_state): %s", e)

        # ── SharedKernel 初始化 (多引擎 VIX/风控参数 SSOT) ──
        try:
            from app.core.shared_kernel import init_shared_kernel
            from app.analysis.market_state import get_checker
            checker = get_checker()
            init_shared_kernel(vix_provider=checker._fetch_vix_data)
            logger.info("[SharedKernel] 已绑定 VIX 数据源 (market_state)")
        except Exception as e:
            logger.warning("[SharedKernel] 初始化失败 (将使用默认 VIX 值): %s", e)

        # ── Scalping 均值回归引擎 (2026-04-29 新增) ──
        self.scalping_engine = None
        self._scalping_thread = None
        try:
            from app.core.config_container import get_config as get_app_config
            cfg = get_app_config()
            scalping_cfg = cfg.get_dotted("scalping", {})

            if scalping_cfg.get("enabled", False):
                from app.scalping.engine import get_scalping_engine
                from app.utils.daily_active_pool import get_daily_active_pool

                pool = get_daily_active_pool(market_prefix="US.")

                # 2026-05-09: 告警防抖器 (观察期 30min, 重复 3 次升级)
                from app.observability.alert_debouncer import AlertDebouncer
                debouncer = AlertDebouncer(
                    observation_window_sec=1800,
                    escalate_threshold=3,
                )

                self.scalping_engine = get_scalping_engine(
                    execution_engine=self.execution_engine,
                    data_router=self.data_router,
                    risk_engine=self.risk_engine,
                    notifier=self.notifier,
                    daily_active_pool=pool,
                    debouncer=debouncer,
                    mode=scalping_cfg.get("mode", "observe"),
                    polling_interval=scalping_cfg.get("polling_interval_seconds", 5),
                    open_position_lock=self._open_position_lock,
                )
                # P1-A (2026-05-15): 注入 scheduler 引用 → ScalpingEngine 可调 coordinator
                self.scalping_engine.scheduler = self
                logger.info(
                    f"[Scalping] 引擎已初始化 | 模式: {scalping_cfg.get('mode', 'observe')} | scheduler 已注入"
                )
            else:
                logger.info("[Scalping] 总开关关闭 (scalping.enabled=false)")
        except Exception as e:
            logger.warning("[Scalping] 初始化失败 (降级运行): %s", e)

        # ── 智能自适应轮询控制器 (2026-04-15 新增) ──
        # 解决免费 API 限流 + 美股高波动的矛盾
        from app.data.adaptive_polling import AdaptivePollingController, StockTier
        from app.utils.watchlist_manager import get_watchlist_with_tiers
        
        # Livermore 融合 (2026-04-16): 根据股票分层和数量自适应 API 预算
        # 原则: 持仓股优先, TIER1 > TIER2, ETF 低频
        watchlist_with_tiers = get_watchlist_with_tiers()
        num_stocks = len(watchlist_with_tiers.get('us_stocks', []))

        # 自适应 API 预算: LV3 无限制 → 设置为股票总数 (2026-04-20 升级)
        # 原逻辑: 基于 TwelveData 8/min 限制设为 12
        # 新逻辑: Futu LV3 本地快照无 API 限制, 可轮询全部股票
        api_budget = num_stocks  # LV3 无限制, 覆盖全部股票
        self.polling_controller = AdaptivePollingController(api_budget_per_scan=api_budget)

        # O1 (2026-05-16): sandbox 模式下 polling_controller 也只注册 universe 内标的
        # 之前 sandbox 只改了 self.watchlist, 但 polling_controller 仍读 watchlist_us.json
        # → 5/15 实际交易 BA/CRM/NFLX/RIVN/SNDK/TME/ARM (全部 universe 外)
        # 修复: sandbox 启用时仅注册 universe ∩ watchlist 子集, 严格聚焦实验
        _sandbox_universe = set()
        try:
            from app.core.config_container import get_config as _gc
            _cfg = _gc()
            if _cfg.get_dotted("mode.sandbox.enabled", False):
                _univ = _cfg.get_dotted("mode.sandbox.universe", []) or []
                # universe 项不含 US. 前缀, 标准化
                _sandbox_universe = {
                    (s if s.startswith("US.") else f"US.{s}") for s in _univ
                }
        except Exception as _silent_e:
            logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")

        # 按 watchlist.json 中的 tier 注册到对应优先级
        tier_map = {
            'TIER1': StockTier.HIGH_VOLATILITY,  # TIER1 → P1 (10-15 分钟)
            'TIER2': StockTier.WATCHLIST,        # TIER2 → P2 (30-60 分钟)
            'ETF': StockTier.WATCHLIST,          # ETF → P2 (低频)
        }

        _registered = 0
        _filtered_by_sandbox = 0
        for stock in watchlist_with_tiers.get('us_stocks', []):
            symbol = stock.get('code', '')
            tier_str = stock.get('tier', 'TIER1')
            enabled = stock.get('enabled', True)
            if not enabled:
                continue
            # O1: sandbox 严格过滤
            if _sandbox_universe and symbol not in _sandbox_universe:
                _filtered_by_sandbox += 1
                continue
            tier = tier_map.get(tier_str, StockTier.WATCHLIST)
            self.polling_controller.register_symbol(symbol, tier)
            _registered += 1

        # ETF 作为市场基准，注册但低频轮询 (sandbox 下若 SPY/QQQ 在 universe 也算)
        for etf in watchlist_with_tiers.get('etfs', []):
            symbol = etf.get('code', '')
            if not etf.get('enabled', True):
                continue
            if _sandbox_universe and symbol not in _sandbox_universe:
                _filtered_by_sandbox += 1
                continue
            self.polling_controller.register_symbol(symbol, StockTier.WATCHLIST)
            _registered += 1

        if _sandbox_universe:
            logger.warning(
                f"[O1·SANDBOX] polling_controller 已严格过滤: 注册 {_registered} 只 "
                f"(过滤 {_filtered_by_sandbox} 只非 universe), universe={sorted(_sandbox_universe)}"
            )
        
        logger.info(
            f"[自适应轮询] 已注册 {len(self.watchlist)} 只观察股 | "
            f"API 预算={api_budget}/轮 | "
            f"TIER1={sum(1 for s in watchlist_with_tiers.get('us_stocks', []) if s.get('tier')=='TIER1')} | "
            f"ETF={len(watchlist_with_tiers.get('etfs', []))}"
        )

        # ── 分时段订阅管理器 (2026-05-01 新增) ──
        # 替代原来的 _subscribe_futu_watchlist() 直接订阅
        # 港股和美股共享槽位，港股收盘后美股才订阅，美股收盘后自动退订
        if hasattr(self.data_router, 'futu') and self.data_router.futu:
            from app.data.subscription_manager import SubscriptionManager, FutuLV3Adapter
            adapter = FutuLV3Adapter(self.data_router.futu)
            self._subscription_mgr = SubscriptionManager(
                gateway=adapter,
            )
            logger.info("[调度器] 分时段订阅管理器已初始化")
        else:
            logger.info("[调度器] Futu LV3 未启用，跳过订阅管理器")

        # 运行状态
        self.running = False
        self._scan_thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None

        # 信号冷却（避免同标的短时间重复下单）
        self._signal_cooldown: Dict[str, datetime] = {}
        self.SIGNAL_COOLDOWN_MINUTES = getattr(self, 'SIGNAL_COOLDOWN_MINUTES', 60)

        logger.info(f"[调度器] 初始化完成，监控 {len(self.watchlist)} 只股票")

    def _init_coordinator(self):
        """
        Phase A (2026-05-14): 初始化 StrategyCoordinator (灰度).

        默认 enabled=true (从 yaml 读), observe_only=true → 不阻断现有逻辑.
        当 enabled=false 时, self.coordinator 仍创建为对象但所有方法都 short-circuit.
        Phase B 把 observe_only 切到 false 后才真正强制.
        """
        try:
            from app.core.config_container import get_config as _gc
            cfg = _gc()
        except Exception:
            cfg = None

        enabled = bool(cfg.get_dotted("strategies.coordinator.enabled", False)) if cfg else False
        observe_only = bool(cfg.get_dotted("strategies.coordinator.observe_only", True)) if cfg else True
        max_total = float(cfg.get_dotted("strategies.coordinator.max_total_pct", 0.90)) if cfg else 0.90

        # T4 (2026-05-16): sandbox 模式强制 SymbolLock + SignalArbitrator enforce
        # 5/15 同标的 ARM 三笔 (692/693 SCALPING + 706 MAIN) 共占 2 桶 — 总敞口 2× 名义.
        # sandbox 是实验环境, 应该用更严的约束验证 enforce 行为, 而非沿用宽松 observe_only.
        # 主入口: 在 sandbox 下 observe_only 强制为 False (但 enabled 仍依赖 yaml).
        _sandbox_force_enforce = False
        try:
            if cfg and cfg.get_dotted("mode.sandbox.enabled", False):
                _sandbox_force_enforce = True
                observe_only = False
                logger.warning(
                    "[T4·SANDBOX] coordinator.observe_only 强制为 False — "
                    "sandbox 严格 enforce 模式 (SymbolLock + SignalArbitrator 真实阻断)"
                )
        except Exception as _silent_e:
            logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")

        # 各 bucket cap (从 yaml). B.3 (2026-05-14): bucket 而非 strategy
        # fast_lane 走 main bucket → 不在 caps 里 (避免重复加总)
        caps = {
            "main":     float(cfg.get_dotted("strategies.main.capital_pct", 0.65)) if cfg else 0.65,
            "scalping": float(cfg.get_dotted("strategies.scalping.capital_pct", 0.25)) if cfg else 0.25,
            "llm_plan": float(cfg.get_dotted("strategies.llm_plan.capital_pct", 0.0)) if cfg else 0.0,
        }
        # B.3: bucket_map — 让 fast_lane 占用计入 main bucket
        bucket_map = {
            "fast_lane": "main",
        }

        from app.coordinator import (
            CapitalAllocator, SymbolLockManager, SignalArbitrator, ExitDispatcher
        )
        from app.coordinator.strategy_coordinator import get_coordinator
        from app.core.paths import get_decisions_db

        capital = CapitalAllocator(caps=caps, max_total_pct=max_total, observe_only=observe_only,
                                   bucket_map=bucket_map)
        symlock = SymbolLockManager(observe_only=observe_only)
        arb = SignalArbitrator(audit_db_path=str(get_decisions_db()), audit_enabled=True)
        dispatcher = ExitDispatcher()
        # B.5 (2026-05-14): 注册 4 个最小 ExitPolicy (Phase A 全部委托 PositionExitMonitor)
        try:
            from app.strategy.exit_policies import (
                MainPipelineExitPolicy, ScalpingExitPolicy,
                FastLaneExitPolicy, LLMPlanExitPolicy,
            )
            dispatcher.register("main", MainPipelineExitPolicy())
            dispatcher.register("scalping", ScalpingExitPolicy())
            dispatcher.register("fast_lane", FastLaneExitPolicy())
            dispatcher.register("llm_plan", LLMPlanExitPolicy())
        except Exception as _ee:
            logger.warning(f"[调度器·Coordinator] ExitPolicy 注册失败 (降级): {_ee}")

        self.coordinator = get_coordinator(
            capital=capital, lock=symlock, arb=arb, exit_dispatcher=dispatcher,
            enabled=enabled,
        )

        # 注册 4 个 adapter (Phase A: 仅声明元数据, 不接管下单)
        try:
            from app.strategy.adapters import all_adapters
            self._strategy_adapters = all_adapters()
            logger.info(
                f"[调度器·Coordinator] 已注册 {len(self._strategy_adapters)} 个 adapter: "
                f"{[a.name + '(p=' + str(a.priority) + ')' for a in self._strategy_adapters]}"
            )
        except Exception as e:
            logger.warning(f"[调度器·Coordinator] adapter 注册失败 (降级运行): {e}")
            self._strategy_adapters = []

        # Phase A 启动 banner — 一行扫到的关键信息
        banner = [
            "═" * 70,
            "║ Coordinator Phase A — 多策略协调层启动",
            f"║   enabled       : {enabled}",
            f"║   observe_only  : {observe_only}  (true=不阻断只记录 / false=强制)",
            f"║   max_total_pct : {max_total}",
            f"║   caps          : {caps}",
            f"║   adapters      : {[(a.name, a.priority) for a in self._strategy_adapters]}",
            f"║   reconcile     : every {cfg.get_dotted('strategies.coordinator.reconcile_interval_sec', 300) if cfg else 300}s",
            f"║   audit_db      : signal_arbitration in decisions.db",
            f"║   watchdog      : scan_loop + monitor_loop 心跳监控",
            "║ 开盘验证: scripts/coordinator_status.py --all",
            "═" * 70,
        ]
        for line in banner:
            logger.info(line)

        # P1 (2026-05-14): 启动 Watchdog 心跳监控
        try:
            from app.observability.watchdog import get_watchdog
            wd = get_watchdog()
            # 根据实际调度间隔设置心跳阈值
            scan_interval = cfg.get_dotted("scheduler.scan_interval_minutes", 10) if cfg else 10
            monitor_interval = cfg.get_dotted("scheduler.monitor_interval_minutes", 0.5) if cfg else 0.5
            wd.register("scan_loop", stale_threshold_sec=scan_interval * 60 * 5)  # 5 倍间隔
            wd.register("monitor_loop", stale_threshold_sec=monitor_interval * 60 * 5)
            wd.start(check_interval_sec=60.0)
            logger.info(f"[Watchdog] 已启动 — scan={scan_interval}min, monitor={monitor_interval}min")
        except Exception as _we:
            logger.warning(f"[Watchdog] 启动失败 (不影响交易): {_we}")

        # T6 (2026-05-16): CapitalAllocator 周期性 reconcile (5min)
        # 修复 5/15 同标的多桶占用 (ARM 692/693 SCALPING + 706 MAIN 总敞口 2× 名义)
        # observe_only=true 时只记录漂移, enforce 时主动 reset_and_reconcile
        try:
            _reconcile_interval = int(cfg.get_dotted("strategies.coordinator.reconcile_interval_sec", 300)) if cfg else 300
            import threading as _th_t6
            def _t6_reconcile_loop():
                while True:
                    try:
                        _th_t6.Event().wait(_reconcile_interval)
                        # 从 PositionStore 拉真实持仓, 按 owner_strategy 分组算 pct
                        _ps = getattr(self, "position_store", None)
                        if _ps is None:
                            continue
                        _equity = 0.0
                        try:
                            from app.risk.capital_safety import get_capital_safety_system as _gcs
                            _cs = _gcs()
                            if _cs._state and _cs._state.current_equity > 0:
                                _equity = _cs._state.current_equity
                        except Exception as _silent_e:
                            logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")
                        if _equity <= 0:
                            continue
                        _by_strategy: Dict[str, float] = {}
                        for _p in _ps.all():
                            _owner = getattr(_p, "owner_strategy", None) or "main"
                            _mv = float(getattr(_p, "avg_entry_price", 0) or 0) * float(getattr(_p, "size", 0) or 0)
                            _pct = _mv / _equity if _equity > 0 else 0
                            _by_strategy[_owner] = _by_strategy.get(_owner, 0.0) + _pct
                        # 走 coordinator (reconcile 内部会校正 cap 漂移)
                        _coord = getattr(self, "coordinator", None)
                        if _coord is not None and getattr(_coord, "enabled", False):
                            try:
                                _coord.capital.reconcile_with_positions(_by_strategy)
                            except Exception as _re:
                                logger.debug(f"[T6·reconcile] capital.reconcile 失败: {_re}")
                        _log_fn = logger.info if self._is_market_open() else logger.debug
                        _log_fn(
                            f"[T6·reconcile] 资金对账完成: by_strategy={_by_strategy} equity=${_equity:,.0f}"
                        )
                    except Exception as _le:
                        logger.debug(f"[T6·reconcile_loop] 异常 (继续): {_le}")
            _t6_thread = _th_t6.Thread(target=_t6_reconcile_loop, daemon=True, name="T6CapitalReconcile")
            _t6_thread.start()
            logger.info(f"[T6·reconcile] 后台线程已启动 (interval={_reconcile_interval}s)")
        except Exception as _t6e:
            logger.warning(f"[T6·reconcile] 启动失败 (不影响交易): {_t6e}")

    def _load_scheduler_params(self):
        """
        从 unified_config.yaml 加载调度器运行时参数 (SSOT)

        替代 class-level 硬编码常量，保持默认值作为 fallback。
        参数通过 get_dotted() 从统一配置中读取，支持热重载。
        """
        try:
            from app.core.config_container import get_config as get_app_config
            cfg = get_app_config()

            # 沙盒模式 (2026-05-14, karpathy MVP): mode.sandbox.enabled=true 时覆盖参数
            self._sandbox_mode = bool(cfg.get_dotted("mode.sandbox.enabled", False))
            sandbox_overrides = (
                cfg.get_dotted("mode.sandbox.overrides", {}) or {}
            ) if self._sandbox_mode else {}

            def _sb_get(yaml_path: str, default):
                """读 yaml, sandbox 模式下走 overrides 优先"""
                if yaml_path in sandbox_overrides:
                    return sandbox_overrides[yaml_path]
                return cfg.get_dotted(yaml_path, default)

            self.SCAN_INTERVAL_MINUTES = _sb_get(
                "scheduler.scan_interval_minutes", self.SCAN_INTERVAL_MINUTES
            )
            self.MONITOR_INTERVAL_MINUTES = _sb_get(
                "scheduler.monitor_interval_minutes", self.MONITOR_INTERVAL_MINUTES
            )
            self.MAX_POSITIONS = cfg.get_dotted(
                "scheduler.max_positions", self.MAX_POSITIONS
            )
            # P0 2026-05-04: SSOT 收敛 — 从 strategies.defaults 读取 (唯一来源)
            # 回退到 scheduler.* 路径以兼容旧配置
            # Task #46 (2026-05-28): 通过 ConfigKeys 读取 SSOT 路径,
            # 默认值与 unified_config.yaml 保持一致。
            from app.core.config_keys import ConfigKeys
            self.MIN_SIGNAL_STRENGTH = _sb_get(
                ConfigKeys.MIN_EFFECTIVE_STRENGTH.value,
                cfg.get_dotted(
                    "strategies.defaults.min_strength",
                    cfg.get_dotted(
                        "scheduler.min_signal_strength",
                        self._DEFAULT_MIN_SIGNAL_STRENGTH,
                    ),
                ),
            )
            # F4 (2026-05-14): 收敛到 MinConfidenceResolver
            # 沙盒模式: 沙盒 overrides 优先于 resolver
            from app.core.min_confidence_resolver import get_min_confidence_resolver
            if self._sandbox_mode and ConfigKeys.MIN_CONFIDENCE.value in sandbox_overrides:
                self.MIN_CONFIDENCE = float(sandbox_overrides[ConfigKeys.MIN_CONFIDENCE.value])
            else:
                self.MIN_CONFIDENCE = get_min_confidence_resolver().for_phase("intraday")
            self.MIN_CONSENSUS_LEVEL = cfg.get_dotted(
                "scheduler.min_consensus_level", self._DEFAULT_MIN_CONSENSUS_LEVEL
            )
            # P0 修复 (2026-05-04): 统一信号冷却时间 SSOT
            # 原代码读取 scheduler.signal_cooldown_minutes=60 (3600秒)
            # 修复后读取 strategy.signal_cooldown=120 (秒)
            # 删除死配置: scheduler.signal_cooldown_minutes, decision.signal_timing.cooldown_seconds
            self.SIGNAL_COOLDOWN_MINUTES = _sb_get(
                "strategy.signal_cooldown", 120
            ) / 60.0  # 秒转分钟

            # 沙盒模式 banner — 显眼提醒
            if self._sandbox_mode:
                logger.warning("=" * 70)
                logger.warning("║  🧪 [SANDBOX MODE] 沙盒加速实验模式已启用")
                logger.warning(f"║  扫描={self.SCAN_INTERVAL_MINUTES}min  监控={self.MONITOR_INTERVAL_MINUTES}min")
                logger.warning(f"║  cooldown={self.SIGNAL_COOLDOWN_MINUTES*60:.0f}s  min_conf={self.MIN_CONFIDENCE:.2f}  min_strength={self.MIN_SIGNAL_STRENGTH}")
                _universe = cfg.get_dotted("mode.sandbox.universe", []) or []
                logger.warning(f"║  universe ({len(_universe)}): {_universe}")
                logger.warning("║  风控守底: kill_switch / per_trade_R / max_total_position 仍生效")
                logger.warning("║  关闭沙盒: mode.sandbox.enabled=false 重启")
                logger.warning("=" * 70)

            logger.info(
                "[调度器] 配置已加载 | scan=%smin monitor=%smin max_pos=%d "
                "min_score=%d min_conf=%.2f min_consensus=%.2f cooldown=%dmin",
                self.SCAN_INTERVAL_MINUTES, self.MONITOR_INTERVAL_MINUTES,
                self.MAX_POSITIONS, self.MIN_SIGNAL_STRENGTH,
                self.MIN_CONFIDENCE, self.MIN_CONSENSUS_LEVEL,
                self.SIGNAL_COOLDOWN_MINUTES,
            )
            # Task #46 (2026-05-28): 显式打印 SSOT 关键阈值, 便于审计配置漂移
            try:
                logger.info(
                    "[配置SSOT] min_conf=%.2f min_strength=%d max_total_pos=%.2f "
                    "main_pct=%.2f scalping_pct=%.2f stop_loss=%.3f take_profit=%.3f atr_x=%.2f",
                    self.MIN_CONFIDENCE,
                    int(self.MIN_SIGNAL_STRENGTH),
                    float(cfg.get_dotted(ConfigKeys.MAX_TOTAL_POSITION.value, 0.85)),
                    float(cfg.get_dotted(ConfigKeys.MAIN_BUDGET_PCT.value, 0.65)),
                    float(cfg.get_dotted(ConfigKeys.SCALPING_BUDGET_PCT.value, 0.20)),
                    float(cfg.get_dotted(ConfigKeys.STOP_LOSS_PCT.value, 0.045)),
                    float(cfg.get_dotted(ConfigKeys.TAKE_PROFIT_PCT.value, 0.025)),
                    float(cfg.get_dotted(ConfigKeys.ATR_MULTIPLIER.value, 2.5)),
                )
            except Exception as _e:
                logger.debug("[配置SSOT] 摘要打印失败 (不阻断): %s", _e)
        except Exception as e:
            logger.warning("[调度器] 配置加载失败，使用类默认值: %s", e)

    def _load_watchlist(self) -> List[str]:
        """从配置加载股票池 + 每日动态交易池 (P2 优化)"""
        codes = []
        try:
            from app.utils.watchlist_manager import get_stock_codes
            codes = get_stock_codes()
            logger.info(f"[调度器] 从 watchlist_us.json 加载 {len(codes)} 只基础股票")
        except Exception as e:
            logger.warning(f"[调度器] 使用 watchlist_manager 失败：{e}")
            codes = ["US.TSLA", "US.NVDA", "US.MU", "US.GOOG", "US.BABA"]

        # 合并每日动态交易池
        try:
            from app.utils.daily_active_pool import get_daily_active_pool
            pool = get_daily_active_pool()
            active_codes = pool.get_active_codes()
            
            # 过滤 US 代码
            us_active = [c for c in active_codes if c.startswith("US.")]
            
            # 合并并去重，优先保留 Pool 中的标的
            final_codes = list(set(codes + us_active))
            logger.info(f"[调度器] 合并动态池: {len(us_active)} 只活跃 -> 总计 {len(final_codes)} 只")
            return final_codes
        except Exception as e:
            logger.warning(f"[调度器] 合并动态池失败: {e}")
            return codes

    def _inject_battle_plan_to_pool(self):
        """启动时将战斗计划标的注入动态池 (2026-05-12 新增)

        盘前辩论/分析师产生的标的写入 daily_battle_plan_us.json 后,
        本方法在启动时自动将其注入到 daily_active_pool, 确保轮询扫描覆盖。

        P0 修复 (2026-05-20): sandbox 模式下过滤非 universe 标的
        """
        try:
            from app.us_pipeline.battle_plan_manager import USBattlePlanManager
            from app.utils.daily_active_pool import get_daily_active_pool

            mgr = USBattlePlanManager()
            plan = mgr.get_plan()
            if not plan or not plan.get("targets"):
                logger.debug("[调度器] 战斗计划无 targets，跳过注入")
                return

            # P0 修复 (2026-05-20): sandbox 模式下过滤非 universe 标的
            # 之前直接注入所有 targets, 导致 universe 外标的进入动态池
            _sandbox_universe = set()
            try:
                from app.core.config_container import get_config as _gc
                _cfg = _gc()
                if _cfg.get_dotted("mode.sandbox.enabled", False):
                    _univ = _cfg.get_dotted("mode.sandbox.universe", []) or []
                    _sandbox_universe = {
                        (s if s.startswith("US.") else f"US.{s}") for s in _univ
                    }
            except Exception as _silent_e:
                logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")

            if _sandbox_universe:
                # 过滤 plan targets 只保留 universe 内标的
                original_count = len(plan.get("targets", []))
                filtered_targets = [
                    t for t in plan["targets"]
                    if t.get("symbol", "") in _sandbox_universe
                ]
                filtered_count = original_count - len(filtered_targets)
                if filtered_count > 0:
                    logger.warning(
                        f"[SANDBOX·BattlePlan] 注入动态池已过滤: "
                        f"移除 {filtered_count} 只非 universe 标的, "
                        f"保留 {len(filtered_targets)} 只"
                    )
                # 创建过滤后的 plan 副本
                filtered_plan = dict(plan)
                filtered_plan["targets"] = filtered_targets
                pool = get_daily_active_pool(market_prefix="US.")
                injected = pool.inject_from_battle_plan(filtered_plan)
            else:
                pool = get_daily_active_pool(market_prefix="US.")
                injected = pool.inject_from_battle_plan(plan)

            if injected:
                logger.info(f"[调度器] 战斗计划已注入 {injected} 只新标的到动态池")
        except Exception as e:
            logger.warning(f"[调度器] 战斗计划注入动态池失败: {e}")

    def _subscribe_futu_watchlist(self):
        """
        Futu LV3 Push 订阅模式 (V4 2026-05-05: 全量 K_1M)

        V4 升级: 精简池 → 全量 QUOTE+K_1M 覆盖。
        - 30只股票+2只ETF, 全部QUOTE+K_1M = 64/1000槽位
        - 港股36槽位余量充足
        - 所有标的策略完整覆盖 (RSI+MACD+Bollinger+SuperTrend)
        - Emergency Interceptor 通过回调实时接收行情，无需轮询

        优势:
        1. 实时推送，延迟 <100ms (相比快照轮询 1-5s)
        2. 无需自适应轮询器，降低 API 调用频率
        3. 非交易时段自动退订释放槽位
        """
        if not hasattr(self.data_router, 'futu') or not self.data_router.futu:
            logger.info("[FutuLV3] 未启用，使用 HTTP API 降级模式")
            return

        # 获取分层 watchlist
        try:
            from app.utils.watchlist_manager import get_watchlist_with_tiers
            watchlist_data = get_watchlist_with_tiers()
            stocks_with_tiers = watchlist_data.get('us_stocks', []) + watchlist_data.get('etfs', [])
        except Exception:
            stocks_with_tiers = self.watchlist

        # 分层统计 (V4 2026-05-05: 全量 K_1M — 30只×2槽位=60 安全)
        tier1 = []
        tier2 = []
        etfs = []

        # P0 修复 (2026-05-20): sandbox 模式下过滤非 universe 标的
        # 之前 _subscribe_futu_watchlist 直接读 get_watchlist_with_tiers(),
        # 绕过了 __init__ 中的 sandbox universe 覆盖, 导致 TME/CRM/ORCL 等 universe 外标的被订阅
        _sandbox_universe = set()
        try:
            from app.core.config_container import get_config as _gc
            _cfg = _gc()
            if _cfg.get_dotted("mode.sandbox.enabled", False):
                _univ = _cfg.get_dotted("mode.sandbox.universe", []) or []
                _sandbox_universe = {
                    (s if s.startswith("US.") else f"US.{s}") for s in _univ
                }
        except Exception as _silent_e:
            logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")

        for s in (stocks_with_tiers if isinstance(stocks_with_tiers, list) else self.watchlist):
            if isinstance(s, dict):
                code = s.get('code', s.get('symbol', ''))
                tier = s.get('tier', 'TIER2')
            else:
                code = str(s)
                tier = 'TIER2'

            # P0 修复: sandbox 严格过滤
            if _sandbox_universe and code not in _sandbox_universe:
                continue

            if not code.startswith('US.'):
                etfs.append(code if code.startswith('US.') else f'US.{code}')
            elif tier == 'TIER1':
                tier1.append(code)
            else:
                tier2.append(code)

        if _sandbox_universe:
            logger.warning(
                f"[SANDBOX·FutuLV3] 订阅已过滤: TIER1={len(tier1)} TIER2={len(tier2)} ETF={len(etfs)} "
                f"(universe={sorted(_sandbox_universe)})"
            )

        # V6 (2026-05-20): 全量订阅 QUOTE + K_1M + K_5M + K_15M + K_30M + ORDER_BOOK + TICKER
        # 槽位计算: 30只×7 + 2ETF×2 = 214/1000 (ETF 不需要订单流/逐笔/多周期)
        all_stocks = tier1 + tier2
        if all_stocks:
            self.data_router.futu.subscribe(
                all_stocks, 
                sub_types=['QUOTE', 'K_1M', 'K_5M', 'K_15M', 'K_30M', 'ORDER_BOOK', 'TICKER']
            )
            logger.info(
                f"[FutuLV3] ✅ 全量 Push 订阅: {len(all_stocks)} 只 "
                f"(QUOTE+K_1M+K_5M+K_15M+K_30M+ORDER_BOOK+TICKER)"
            )

        # ETF 单独订阅 QUOTE + K_1M + K_15M (不需要订单流/逐笔)
        if etfs:
            self.data_router.futu.subscribe(etfs, sub_types=['QUOTE', 'K_1M', 'K_15M'])
            logger.info(f"[FutuLV3] ✅ ETF Push 订阅: {len(etfs)} 只 (QUOTE+K_1M+K_15M)")

        total_slots = (len(tier1) + len(tier2)) * 7 + len(etfs) * 3
        logger.info(
            f"[FutuLV3] ✅ Push 订阅模式已激活 | "
            f"监控 {len(self.watchlist)} 只美股 | "
            f"槽位: {total_slots}/1000 | "
            f"TIER1={len(tier1)} TIER2={len(tier2)} ETF={len(etfs)}"
        )

        # 验证 LV3 连接可用性 (单次快照测试)
        try:
            test_symbols = self.watchlist[:3] if self.watchlist else ["US.AAPL"]
            results = self.data_router.futu.get_batch_quote(test_symbols)
            if results:
                sample = list(results.values())[0]
                logger.info(
                    f"[FutuLV3] ✅ 快照验证通过 | "
                    f"测试标的: {test_symbols[0]} | "
                    f"价格: ${sample.get('price', 0):.2f} | "
                    f"数据源: {sample.get('data_source', 'unknown')}"
                )
            else:
                logger.warning("[FutuLV3] ⚠️ 快照验证返回空，将使用 Finnhub 降级模式")
        except Exception as e:
            logger.warning(f"[FutuLV3] 快照验证失败：{e}, 将使用降级模式")

    def startup_reconcile(self):
        """
        启动时持仓对账（P0 2026-05-20 新增）
        
        比对内存持仓 (PositionStore) 与数据库持仓 (open_positions)，
        修复服务重启后的状态不一致问题。
        
        策略:
        - DB 有、内存无 → 从 DB 恢复到内存
        - 内存有、DB 无 → 从内存同步到 DB
        - 两边都有 → 以 DB 为准（DB 是 SSOT）
        """
        logger.info("[调度器] 🔍 开始启动持仓对账...")
        
        try:
            # 1. 获取 DB 持仓
            db_positions = []
            if hasattr(self, 'trade_store') and self.trade_store:
                db_positions = self.trade_store.load_open_positions() or []
            db_symbols = {p['symbol'] for p in db_positions}
            
            # 2. 获取内存持仓
            mem_positions = {p.symbol: p for p in self.position_store.all()} if self.position_store else {}
            mem_symbols = set(mem_positions.keys())
            
            # 3. 对账逻辑
            # DB 有但内存无 → 恢复到内存 (跳过期权，由 position_sync 处理)
            for db_pos in db_positions:
                symbol = db_pos['symbol']
                if symbol in mem_symbols:
                    continue
                if '26' in symbol or symbol.endswith('P') or symbol.endswith('C'):
                    # 期权由 position_sync 单独处理
                    continue
                # TODO: 恢复股票持仓到 PositionStore
                logger.debug(f"[调度器] 跳过股票持仓恢复：{symbol} (待实现)")
            
            # 内存有但 DB 无 → 同步到 DB（可能是内存中未持久化的新持仓）
            for symbol in mem_symbols - db_symbols:
                mem_pos = mem_positions[symbol]
                if hasattr(self, 'trade_store') and self.trade_store:
                    self.trade_store.upsert_open_position(mem_pos)
                    logger.info(f"[调度器] ✅ 从内存同步到 DB: {symbol}")
            
            # 4. 日志汇总
            logger.info(
                f"[调度器] ✅ 持仓对账完成 | "
                f"DB={len(db_symbols)} | 内存={len(mem_symbols)} | "
                f"差异={len(db_symbols ^ mem_symbols)}"
            )
            
        except Exception as e:
            logger.error(f"[调度器] ❌ 持仓对账失败：{e} | 降级运行", exc_info=True)

    def get_lv3_health_status(self) -> Dict[str, Any]:
        """
        获取 LV3 健康状态 (2026-04-17 新增)

        Returns:
            LV3 健康状态字典
        """
        if hasattr(self.data_router, 'futu') and self.data_router.futu:
            return self.data_router.futu.get_health_status()
        return {
            "provider": "futu_lv3",
            "enabled": False,
            "reason": "未启用或初始化失败",
        }

    def run_forever(self):
        """启动两个后台线程：信号扫描 + 持仓监控"""
        self.running = True
        logger.info("[调度器] 服务启动")

        # 从 DB 恢复上次未平仓持仓（重启保护，含股票持仓 + 期权持仓）
        self._restore_positions_from_db()

        # P0 2026-05-20: 启动持仓对账（内存 vs DB 双向比对）
        self.startup_reconcile()

        # Phase A (2026-05-14): 持仓恢复后立即 reconcile coordinator
        # 防止重启后账本/锁为空导致首次 reserve 时误判余量
        try:
            if self.coordinator is not None and self.coordinator.enabled:
                self.coordinator.reconcile(self.position_store)
                _snap = self.coordinator.capital.snapshot()
                logger.info(
                    f"[调度器·Coordinator] 启动回灌完成: capital={_snap.by_strategy} "
                    f"locks={self.coordinator.lock.all_locked()}"
                )
        except Exception as _ce:
            logger.warning(f"[调度器·Coordinator] 启动回灌失败: {_ce}")

        # 信号扫描线程
        self._scan_thread = threading.Thread(
            target=self._scan_loop, name="SignalScanLoop", daemon=True
        )
        self._scan_thread.start()

        # 持仓监控线程
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="PositionMonitorLoop", daemon=True
        )
        self._monitor_thread.start()

        # LLM 策略引擎线程 (2026-04-16 新增)
        self._llm_thread = threading.Thread(
            target=self._llm_strategy_loop, name="LLMStrategyLoop", daemon=True
        )
        self._llm_thread.start()

        # 新闻管线 V2 线程 (2026-04-29 新增，observe 模式)
        if self.news_pipeline_v2:
            self._news_v2_thread = threading.Thread(
                target=self._news_v2_loop, name="NewsV2Loop", daemon=True
            )
            self._news_v2_thread.start()
            logger.info("[NewsV2] 后台采集线程已启动")

        # VIX 监控线程 (2026-04-29 新增)
        if self.vix_monitor:
            self._vix_thread = threading.Thread(
                target=self._vix_monitor_loop, name="VIXMonitorLoop", daemon=True
            )
            self._vix_thread.start()
            logger.info("[VIX Monitor] 后台监控线程已启动")

        # Scalping 均值回归线程 (2026-04-29 新增)
        if self.scalping_engine:
            self.scalping_engine.start()
            # 注意: start() 可能启动等待线程(盘前)或直接启动主循环(盘中)
            # 实际启动状态以 ScalpingEngine 日志为准

        # 若启动时正处于交易时间，立即触发一次扫描（不等 30 分钟冷启动）
        if self._is_market_open():
            logger.info("[调度器] 交易时间内启动，立即触发首次信号扫描")
            t = threading.Thread(
                target=self._run_signal_scan,
                name="ImmediateScan",
                daemon=True,
            )
            t.start()

        # ── 分时段订阅管理器启动 (2026-05-01 新增) ──
        # 替代原来的 _ensure_futu_disconnected_during_hk() 5分钟轮询
        if self._subscription_mgr:
            # 根据当前时段决定：立即订阅 / 等待自动循环激活
            if self._subscription_mgr.should_be_subscribed():
                logger.info("[分时调度] 当前为美股盘前/交易时段，立即订阅 watchlist...")
                self._subscription_mgr.ensure_subscribed(self.watchlist)
            else:
                reason = "港股交易时段" if self._subscription_mgr.is_hk_trading() else "美股已收盘"
                logger.info(f"[分时调度] 当前{reason}，暂不订阅 (自动循环将在盘前激活)")

            # 启动自动循环 — 盘前自动订阅，港股时段/美股收盘自动退订
            self._subscription_mgr.start_auto_loop(
                codes_callback=lambda: list(self.watchlist),
                interval=60,
            )
        else:
            # 兼容旧逻辑：无订阅管理器时使用原来的方式
            if self._is_hk_trading_hours():
                logger.info(
                    "[分时调度] 启动时处于港股交易时段，FTSeek-US 暂缓连接 OpenD"
                )
            else:
                self._ensure_futu_disconnected_during_hk()

        # 等待
        _last_hk_check = 0.0
        _hk_check_interval = 300  # 每 5 分钟检查一次港股时段
        while self.running:
            # 2026-04-20: 每 5 分钟检查一次港股交易时段，自动断开/恢复 Futu 连接
            # 2026-05-01: 若 SubscriptionManager 已启动，跳过此检查（自动循环已处理）
            if not self._subscription_mgr:
                now_ts = time.time()
                if now_ts - _last_hk_check >= _hk_check_interval:
                    self._ensure_futu_disconnected_during_hk()
                    _last_hk_check = now_ts
            time.sleep(5)

    def stop(self):
        """停止服务"""
        logger.info("停止 FTSeek-US 服务...")
        self.running = False

        # ── 停止分时段订阅管理器 (2026-05-01 新增) ──
        if self._subscription_mgr:
            self._subscription_mgr.stop_auto_loop()
            if self._subscription_mgr.subscribed_count > 0:
                logger.info(
                    f"[分时调度] 停止时退订 {self._subscription_mgr.subscribed_count} 只美股标的..."
                )
                self._subscription_mgr.unsubscribe_all()

        # P0 修复 (2026-04-25): 优雅关闭后台线程，避免 systemd SIGKILL
        threads_to_join = [
            getattr(self, '_scan_thread', None),
            getattr(self, '_monitor_thread', None),
            getattr(self, '_llm_thread', None),
            getattr(self, '_news_v2_thread', None),
            getattr(self, '_vix_thread', None),
        ]
        # P0 修复 (2026-04-30 10:30): 缩短 join timeout (5线程×timeout),
        # 为后续 Futu close + config cleanup + os._exit 留出余量 (systemd TimeoutStopSec=120s).
        # P2 修复 (2026-05-02): LLM 线程单独给 10s (LLM API 调用可达 120s, 但通常 10-30s 完成),
        # 其他线程保持 2s. 总超时: 4×2s + 10s = 18s << 120s.
        for t in threads_to_join:
            if t and t.is_alive():
                t_timeout = 10 if t.name == "LLMStrategyLoop" else 2
                t.join(timeout=t_timeout)
                if t.is_alive():
                    logger.warning(f"[调度器] 线程 {t.name} 未能在 {t_timeout}s 内退出，跳过")

        # 停止 Scalping 引擎
        if self.scalping_engine:
            self.scalping_engine.stop()
            logger.info("[Scalping] 引擎已停止")

        # C5 归因 (2026-05-13): 确保归因数据在关闭时刷新
        try:
            from app.analysis.strategy_attribution import get_attribution
            get_attribution()._flush_daily_attribution()
            logger.info("[调度器] 归因数据已刷新")
        except Exception as _silent_e:
            logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")
        # P0 修复 (2026-04-30): 关闭 Futu 连接设超时，防止网络调用阻塞 shutdown
        if hasattr(self, 'quote_ctx_for_options') and self.quote_ctx_for_options:
            try:
                import threading
                def _close_ctx():
                    try:
                        self.quote_ctx_for_options.close()
                    except Exception as e:
                        logger.debug("[调度器] 关闭期权行情上下文失败: %s", e)
                t = threading.Thread(target=_close_ctx, daemon=True)
                t.start()
                t.join(timeout=3)
            except Exception as e:
                logger.debug("[调度器] 期权行情上下文关闭线程异常: %s", e)
        logger.info("[调度器] 服务停止")

    def trigger_immediate_scan(self, symbol: str, reason: str = "event"):
        """
        立即触发对特定标的的扫描 (用于新闻/突发事件驱动)
        """
        if self.polling_controller:
            logger.info(f"⚡ [即时扫描] {symbol} 触发原因: {reason}")
            self.polling_controller.force_poll(symbol, reason)
        else:
            logger.warning(f"[即时扫描] {symbol} 轮询控制器未初始化")

    # P0-3 新增 2026-04-26: 暴露 close_position 实例方法，供 EmergencyInterceptor 紧急清仓调用
    def close_position(self, symbol: str, reason: str = "MANUAL") -> dict:
        """
        手动/紧急平仓单只标的

        Args:
            symbol: 股票代码 (e.g. "US.TSLA")
            reason: 平仓原因描述

        Returns:
            {"success": bool, "detail": str}
        """
        from app.core.models import ExitReason
        from app.pipeline.order_executor import _close_position

        if not self.position_store.has(symbol):
            return {"success": False, "detail": f"no position for {symbol}"}

        pos = self.position_store.get(symbol)

        # 尝试获取实时价格，失败则使用入场价
        price = pos.avg_entry_price
        try:
            ctx = self.data_router.get_full_market_context(symbol)
            if ctx and ctx.get("price", 0) > 0:
                price = ctx["price"]
        except Exception as _silent_e:
            logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")

        try:
            _close_position(self, pos, price, ExitReason.EMERGENCY)
            logger.info(f"[紧急平仓] {symbol} 成功 (reason={reason})")
            return {"success": True, "detail": reason}
        except Exception as e:
            logger.error(f"[紧急平仓] {symbol} 失败: {e}")
            return {"success": False, "detail": str(e)}

    def force_scan(self, ignore_market_hours: bool = False):
        """
        手动触发一次信号扫描（不等待 30 分钟间隔）。

        Args:
            ignore_market_hours: True = 跳过交易时间检查（用于测试 / 盘前盘后模拟）
        """
        if not self.running:
            logger.warning("[force_scan] 调度器未运行，无法触发扫描")
            return

        if not ignore_market_hours and not self._is_market_open():
            logger.warning("[force_scan] 当前非交易时间，若需强制扫描请传 ignore_market_hours=True")
            return

        logger.info(f"[force_scan] 手动触发信号扫描 ignore_market_hours={ignore_market_hours}")
        t = threading.Thread(
            target=self._run_signal_scan,
            name="ForceScan",
            daemon=True,
        )
        t.start()

    # 以下方法委托到 app.pipeline.* 模块和 scheduler_triggers.py
    # 此处仅为类型提示和文档
    def _scan_loop(self):
        """信号扫描主循环 - 在 signal_pipeline.py 中实现"""
        from app.pipeline.signal_pipeline import scan_loop
        scan_loop(self)

    def _run_signal_scan(self):
        """遍历股票池，生成并执行交易信号 - 在 signal_pipeline.py 中实现"""
        from app.pipeline.signal_pipeline import run_signal_scan
        run_signal_scan(self)

    def _process_symbol(self, symbol: str, vix: float, spy_context: Dict[str, Any], market_sentiment: float):
        """处理单只股票：数据→投票→决策→下单 - 在 signal_pipeline.py 中实现"""
        from app.pipeline.signal_pipeline import process_symbol
        process_symbol(self, symbol, vix, spy_context, market_sentiment)

    def _monitor_loop(self):
        """持仓监控主循环 - 在 position_monitor.py 中实现"""
        from app.pipeline.position_monitor import monitor_loop
        monitor_loop(self)

    def _is_market_open(self) -> bool:
        """判断美东交易时间（09:30 - 16:00 周一到周五） - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import is_market_open
        return is_market_open()

    def _is_hk_trading_hours(self) -> bool:
        """判断当前是否为港股交易时段 (北京时间)

        港股交易时段:
        - 上午: 09:30 - 12:00
        - 下午: 13:00 - 16:00
        - 盘前预热: 08:30 - 09:30 (连接占用)

        港股交易期间 FTSeek-US 应断开 OpenD 连接，释放槽位给 FTSeek 港股系统。
        """
        # P1 修复 (2026-05-28): 显式使用北京时间，不依赖服务器本地时区
        HKT = pytz.timezone("Asia/Hong_Kong")
        now = datetime.now(HKT)
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()

        if weekday >= 5:
            return False

        t = hour * 60 + minute
        # 08:30 - 16:00 (含盘前预热)
        return 510 <= t <= 960  # 08:30=510, 16:00=960

    def _ensure_futu_disconnected_during_hk(self):
        """港股交易时段自动断开 Futu 连接，释放 OpenD 槽位

        2026-04-23 升级：动态订阅管理
        - 港股交易时段 (08:30-16:00 CST)：美股退订释放槽位
        - 美股交易时段 (21:30-04:00 CST)：美股 Push 订阅
        - 非交易时段：退订释放槽位
        """
        if self.data_router is None or not hasattr(self.data_router, 'futu') or self.data_router.futu is None:
            return

        is_hk_hours = self._is_hk_trading_hours()
        is_us_hours = self._is_market_open()

        # 港股时段且美股已连接 → 断开
        if is_hk_hours and self.data_router.futu.is_connected:
            logger.info("[分时调度] 港股交易时段，FTSeek-US 退订并断开 Futu 连接，释放 OpenD 槽位")
            try:
                self.data_router.futu.unsubscribe_all()
            except Exception as _silent_e:
                logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")
            self.data_router.futu.disconnect()

        # 美股时段且未订阅 → 重新订阅
        elif is_us_hours and not is_hk_hours and not self.data_router.futu._subscribed_symbols:
            logger.info("[分时调度] 美股交易时段，重新订阅 Push 行情")
            try:
                self._subscribe_futu_watchlist()
            except Exception as e:
                logger.error(f"[分时调度] 美股订阅失败: {e}")

    def _restore_positions_from_db(self):
        """服务重启后从 DB 快照恢复未平仓持仓到内存 - 在 position_monitor.py 中实现"""
        from app.pipeline.position_monitor import restore_positions_from_db
        restore_positions_from_db(self)

    def _is_in_cooldown(self, symbol: str) -> bool:
        """检查信号冷却 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import is_in_cooldown
        return is_in_cooldown(self, symbol)

    def _set_cooldown(self, symbol: str):
        """设置信号冷却 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import set_cooldown
        set_cooldown(self, symbol)

    def _check_daily_review(self):
        """每日 16:05 ET 触发复盘推送 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import check_daily_review
        check_daily_review(self)
    
    def _check_weekly_review(self):
        """每周五 16:10 ET 触发周报推送 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import check_weekly_review
        check_weekly_review(self)
    
    def _check_monthly_review(self):
        """每月 1 日 10:00 ET 触发月报推送 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import check_monthly_review
        check_monthly_review(self)
    
    def _llm_strategy_loop(self):
        """LLM 策略引擎循环 - 按市场阶段自适应调度"""
        from app.pipeline.llm_pipeline import run_llm_strategy_scan, run_after_hours_llm_scan, run_premarket_planning, run_open_checker, run_open_price_refresh
        from .scheduler_triggers import get_market_phase, MarketPhase
        import time

        logger.info("[LLM策略] 策略引擎循环已启动 (按市场阶段自适应)")
        last_trading_run = None
        last_afterhours_run = None

        while self.running:
            try:
                phase = get_market_phase()
                now = datetime.now(ET)

                if phase == MarketPhase.TRADING:
                    # 🚨 盘中休眠：禁止主动扫描，LLM 进入待机状态
                    # LLM 仅作为异步风控员响应触发请求
                    for _ in range(60):
                        if not self.running:
                            return
                        time.sleep(1)
                    continue

                elif phase == MarketPhase.PRE_MARKET:
                    # P2 修复 (2026-04-19): 节假日跳过
                    try:
                        from app.utils.holiday_calendar import get_holiday_calendar
                        cal = get_holiday_calendar()
                        today_et = datetime.now(ET).date()
                        if not cal.is_trading_day("US", today_et):
                            logger.debug("[LLM策略] 今日美股节假日，跳过盘前计划")
                            if last_afterhours_run is None or (now - last_afterhours_run).total_seconds() >= 7200:
                                last_afterhours_run = now  # 重置计时器
                            time.sleep(60)  # 可中断休眠
                            for _ in range(59):
                                if not self.running:
                                    return
                                time.sleep(1)
                            continue
                    except Exception as e:
                        logger.debug(f"[LLM策略] 节假日检查异常 (继续): {e}")

                    # 盘前: 生成作战计划 (每日一次)
                    run_premarket_planning(self)

                    # P0 修复: 开盘确认器 — run_open_checker 内部自带时间窗口 + 日期幂等
                    # 无需外部 guard，函数自行判断 09:15-09:29 ET 窗口并每日执行一次
                    run_open_checker(self)

                    # 开盘价刷新: 09:30:00-09:31:00 ET 用真实开盘价刷新 confirmed_price + SL/TP
                    run_open_price_refresh(self)

                    # 盘前: 每2小时采集新闻 + LLM 推荐 (不执行交易)
                    if last_afterhours_run is None or (now - last_afterhours_run).total_seconds() >= 7200:
                        logger.info("[LLM策略] 盘前 → 新闻采集 + LLM 推荐")
                        run_after_hours_llm_scan(self, phase=phase)
                        last_afterhours_run = now

                    # 2026-04-24 关闭: 盘前观察模式已彻底移除 (Phase 1 dead code deleted)
                    # 原因: Futu LV3 盘前无数据，轮询无意义，观察模式已退役。
                    # 盘前阶段仅保留: 作战计划生成 (08:30-09:25) + 新闻采集 + LLM 推荐

                elif phase == MarketPhase.AFTER_HOURS:
                    # 盘后: 处理盘中排队的 LLM 审计
                    self._process_deferred_audit_queue()
                    
                    # 盘后: 每2小时采集新闻 + LLM 推荐 (不执行交易)
                    if last_afterhours_run is None or (now - last_afterhours_run).total_seconds() >= 7200:
                        logger.info("[LLM策略] 盘后夜盘 → 新闻采集 + LLM 推荐")
                        run_after_hours_llm_scan(self, phase=phase)
                        last_afterhours_run = now

                elif phase == MarketPhase.OVERNIGHT:
                    # 深夜: 每6小时采集新闻 + LLM 推荐 (最低频率)
                    if last_afterhours_run is None or (now - last_afterhours_run).total_seconds() >= 21600:
                        logger.info("[LLM策略] 深夜 → 低频新闻 + LLM 推荐")
                        run_after_hours_llm_scan(self, phase=MarketPhase.OVERNIGHT)
                        last_afterhours_run = now

                elif phase == MarketPhase.WEEKEND:
                    # 周末: 完全休眠，不请求任何 API
                    logger.debug("[LLM策略] 周末休市 → 休眠")
                    # 重置计数器，周一重新计算
                    last_trading_run = None
                    last_afterhours_run = None
                    # 重置日级标志
                    self._premarket_plan_executed_today = False
                    self._open_checker_executed_today = False

                time.sleep(60)  # 每分钟检查阶段变化 (可中断)
                for _ in range(59):
                    if not self.running:
                        return
                    time.sleep(1)
            except Exception as e:
                logger.error(f"[LLM策略] 循环异常: {e}", exc_info=True)
                # 可中断休眠，避免 shutdown 阻塞
                for _ in range(120):
                    if not self.running:
                        return
                    time.sleep(1)
    
    def _check_position_snapshot(self):
        """持仓快照任务 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import check_position_snapshot
        check_position_snapshot(self)

    def _check_position_reconcile(self):
        """Task #49 (2026-05-28): 每日 09:30 ET 持仓对账任务 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import check_position_reconcile
        check_position_reconcile(self)


    def _news_v2_loop(self):
        """新闻管线 V2 后台采集循环 (observe 模式，零副作用)
        P0-5 修复 (2026-05-03): 持久化 event loop，不再每 180s 创建/销毁
        """
        import asyncio
        
        logger.info("[NewsV2] 管线采集循环已启动 (interval=180s, observe模式)")
        self._news_v2_running = True
        
        # P0-5: 创建一次 event loop 并复用
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while self._news_v2_running and self.running:
                try:
                    if self.news_pipeline_v2:
                        passed = loop.run_until_complete(self.news_pipeline_v2.run_once())
                        if passed:
                            logger.info("[NewsV2] 本轮通过 %d 条新闻", len(passed))
                except Exception as e:
                    logger.warning("[NewsV2] 采集异常: %s", e)
                
                # 等待 180 秒
                for _ in range(180):
                    if not self._news_v2_running or not self.running:
                        return
                    time.sleep(1)
        finally:
            loop.close()
        
        logger.info("[NewsV2] 管线采集循环已停止")

    def _vix_monitor_loop(self):
        """VIX 监控后台循环 — 每 5 分钟检查一次，级别变化时推送告警"""
        logger.info("[VIX Monitor] 监控循环已启动 (interval=300s)")

        while self.running:
            try:
                if self.vix_monitor:
                    vix_data = self.vix_monitor.check_vix_level()
                    vix = vix_data.get("vix")
                    level = vix_data.get("level", "UNKNOWN")

                    if vix is not None:
                        _log_fn = logger.info if self._is_market_open() else logger.debug
                        _log_fn("[VIX] 当前值=%.1f | 级别=%s | %s", vix, level, vix_data.get("message", ""))

                        # 级别变化时推送告警
                        if level != self._last_vix_alert_level and level in ("WARNING", "HIGH", "CRITICAL"):
                            self._last_vix_alert_level = level
                            if self.notifier:
                                try:
                                    self.notifier.send_text(
                                        f"[VIX 警报] {vix_data['message']}\n"
                                        f"建议: {vix_data.get('recommendation', 'N/A')}"
                                    )
                                except Exception as _silent_e:
                                    logger.warning(f"[silent_except:scheduler_core.py] {_silent_e}")
            except Exception as e:
                logger.debug("[VIX Monitor] 监控异常: %s", e)

            # 等待 300 秒 (可中断)
            for _ in range(300):
                if not self.running:
                    return
                time.sleep(1)

        logger.info("[VIX Monitor] 监控循环已停止")


    def _process_deferred_audit_queue(self):
        """处理盘中排队的 LLM 审计 (盘后阶段执行)"""
        queue = getattr(self, '_deferred_audit_queue', None)
        if not queue:
            return
        
        count = len(queue)
        logger.info("[LLM 审计] 开始处理盘后延迟审计队列 | 排队=%d", count)
        
        llm_engine = getattr(self, 'llm_engine', None)
        if not llm_engine:
            logger.debug("[LLM 审计] LLM 引擎不可用，跳过延迟队列")
            self._deferred_audit_queue = []
            return
        
        processed = 0
        for item in queue[:10]:  # 最多处理 10 条，防止过载
            try:
                symbol = item.get("symbol", "")
                logger.debug("[LLM 审计] 处理延迟审计 | %s", symbol)
                # 简化版审计: 仅传入 symbol，不传完整 context (避免数据过期)
                if hasattr(llm_engine, 'audit_position_async'):
                    llm_engine.audit_position_async(symbol, {"symbol": symbol})
                processed += 1
            except Exception as e:
                logger.warning("[LLM 审计] 延迟审计失败 | %s: %s", item.get("symbol", "?"), e)
        
        # 清空队列
        remaining = count - processed
        self._deferred_audit_queue = self._deferred_audit_queue[10:]
        
        logger.info(
            "[LLM 审计] 延迟队列处理完成 | 处理=%d | 剩余=%d",
            processed, remaining
        )
