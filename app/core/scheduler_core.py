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
from typing import Dict, Any, List, Optional, Set
from pathlib import Path
import sys

from app.utils.timezone import ET

from app.core.config_defaults import CFG

# 路径
BASE_DIR = Path(__file__).resolve().parent.parent
# P0 Fix 2026-04-23: PROJECT_ROOT is one level above BASE_DIR
PROJECT_ROOT = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from app.data.news_provider import USNewsProvider
from app.data.market_data_router import MarketDataRouter
# 2026-07-10: VotingCoordinator/VotingConfig 顶层 import 已移除.
# scheduler 不再实例化 VotingCoordinator (改用 NullVotingAdapter/BridgeVotingAdapter).
# DI 容器的 voting_coordinator() 工厂保留但仅作为 playbook fallback, 那里用延迟 import.
from app.core.models import (
    SignalScore, Direction, MarketRegime,
    Order, OrderType, OrderState, Position,
    TradeEvent, FailureRecord, FailureType, ExitReason, AlertLevel,
)
from app.core.events import EventStore
from app.storage.trade_store import TradeStore
# ADR-003 (2026-06-04): 期权已禁用 (options.enabled=false), 移除 OptionSelector 初始化
# 保留导入以兼容 position_monitor 的期权平仓代码 (处理历史残留仓位)
# from app.execution.options_selector import OptionSelector, OptionContract

logger = logging.getLogger(__name__)


class PositionStore:
    """内存持仓管理（轻量级，无富途依赖）"""

    def __init__(self):
        self._positions: Dict[str, Position] = {}  # symbol -> Position
        # P0-4a 修复 (2026-05-03): 改用 RLock 允许 sync 线程重入
        # sync 线程获取锁后调用 add/remove (内部也获取锁) 不会死锁
        self._lock = threading.RLock()
        # P0 修复 (2026-06-20): pending 集合防止重复开仓
        # broker_sync 5s debounce 窗口内, 多个 signal_pipeline 线程可能同时
        # 检查 has() 返回 False, 导致重复开仓. pending 即时锁定 symbol,
        # 订单提交/失败后释放. 纯内存操作, 零 IO 开销.
        self._pending: Set[str] = set()

    def add(self, symbol: str, position: Position) -> bool:
        # Critical #3 修复 (2026-06-02):
        # 内存写入 + daily_active_pool 同步必须在同一锁内, 否则
        # scan_loop / monitor_loop 并发调用时可能出现:
        #   1) 内存与动态池/JSON 文件不一致
        #   2) daily_active_pool.save() 与 remove() 交叉导致条目丢失
        # 锁顺序: PositionStore._lock → DailyActivePool._lock
        # (DailyActivePool 不会回调 PositionStore, 因此不存在循环依赖)
        with self._lock:
            existing = self._positions.get(symbol)
            if existing is not None and existing.direction != position.direction:
                logger.critical(
                    f"[PositionStore] {symbol} 方向冲突: "
                    f"existing={existing.direction}, incoming={position.direction} — 拒绝覆盖"
                )
                return False
            self._positions[symbol] = position
            self._pending.discard(symbol)  # 开仓成功，清除pending标记

            # 自动同步动态池 (P0 修复: 传入市场前缀)
            # Task #49 (2026-05-28): 消除 silent exception, 失败记录但不中断主流程 (非关键路径)
            try:
                from app.utils.daily_active_pool import get_daily_active_pool
                pool = get_daily_active_pool(market_prefix="US.")
                pool.add_position(symbol, metadata={
                    "direction": position.direction.value if hasattr(position.direction, 'value') else str(position.direction),
                    "quantity": position.size,
                })
            except Exception as e:
                logger.error(f"[daily_active_pool] {symbol} 添加同步失败: {e}")
                # 不中断主流程 (内存持仓已写入), 但异常向监控系统暴露
        return True

    def get(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return self._positions.get(symbol)

    def remove(self, symbol: str):
        # Critical #3 修复 (2026-06-02):
        # 内存移除与动态池移除原子化, 避免持仓闪断窗口期被其他线程读取到
        # "已平仓但仍在动态池" 的不一致状态.
        with self._lock:
            self._positions.pop(symbol, None)
            self._pending.discard(symbol)  # 清理 pending 状态（安全起见）

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

    def managed(self) -> List[Position]:
        """系统管理的持仓 (exit_monitor / signal_pipeline / drawdown 使用)
        
        源头隔离 (2026-06-11): 通过 is_strategy_managed() 判断,
        手动/legacy/unknown 持仓自动排除, 消费者无需各自查 DB 过滤。

        幽灵仓防护: 由 position_sync 的 broker-first 机制在源头阻止幽灵仓进入
        PositionStore (Futu 无此持仓 → 不同步到内存)。DB 层 is_anomalous 标记
        仅用于 Dashboard (data_service.py) 和审计, 不影响交易引擎内存视图。
        """
        from app.positions import is_strategy_managed
        with self._lock:
            return [p for p in self._positions.values() if is_strategy_managed(p)]

    def manual(self) -> List[Position]:
        """手动持仓 (dashboard 展示 / 审计使用)"""
        from app.positions import get_position_source, PositionSource
        with self._lock:
            return [p for p in self._positions.values()
                    if get_position_source(p) == PositionSource.MANUAL]

    def managed_symbols(self) -> set:
        """系统管理的持仓 symbol 集合 (signal_pipeline 快速查找)"""
        from app.positions import is_strategy_managed
        with self._lock:
            return {sym for sym, p in self._positions.items() if is_strategy_managed(p)}

    def has(self, symbol: str) -> bool:
        """检查是否已有持仓或pending开仓"""
        with self._lock:
            return symbol in self._positions or symbol in self._pending

    def mark_pending(self, symbol: str):
        """标记symbol为pending开仓状态"""
        with self._lock:
            self._pending.add(symbol)

    def clear_pending(self, symbol: str):
        """清除symbol的pending状态"""
        with self._lock:
            self._pending.discard(symbol)

    def count(self) -> int:
        with self._lock:
            return len(self._positions)


def _warn_strength_key_divergence(cfg, effective_key: str, legacy_keys: tuple) -> None:
    """[DEB-fullchain #18] 旧强度键若声明与生效键不同的值 → 启动告警 (不改变生效值)."""
    try:
        eff = cfg.get_dotted(effective_key, None)
        if eff is None:
            return
        for key in legacy_keys:
            val = cfg.get_dotted(key, None)
            if val is not None and float(val) != float(eff):
                logger.warning(
                    f"[config-diff] {key}={val} 与生效键 {effective_key}={eff} 不一致 "
                    f"(以生效键为准, 旧键为死键 — 配置宣称与实际不符家族 #18)"
                )
    except Exception as e:
        logger.debug(f"[config-diff] 强度键分歧检查失败(忽略): {e}")


def _insider_push_dedup_path() -> Path:
    """内部人集群推送事件去重账本路径 (走统一 data 目录, 不依赖 CWD)."""
    try:
        from app.core.paths import get_data_dir
        return Path(str(get_data_dir())) / "insider_push_dedup.json"
    except Exception:
        return Path("data") / "insider_push_dedup.json"


def _insider_push_seen(key: str, path: Optional[Path] = None) -> bool:
    """该集群事件是否已推送过 (只读).

    键 = "<symbol>:<first_buy_date>:<latest_buy_date>" — 与 pure/insider.py 的
    cluster 指纹同形; 同一集群在 30 天窗口内跨日只推一次 (2026-09-21 用户裁:
    原实现只有"当日已扫"哨兵, 无跨日去重 ⇒ 同一事件每天盘前重复推送).
    """
    import json as _json
    p = path or _insider_push_dedup_path()
    try:
        if not p.exists():
            return False
        seen = _json.loads(p.read_text(encoding="utf-8")) or {}
        return isinstance(seen, dict) and key in seen
    except Exception:
        return False


def _insider_push_mark(key: str, date_str: str, path: Optional[Path] = None) -> None:
    """记录该集群事件已推送 (原子写; 顺带清理 >120 天旧键防无限增长)."""
    import json as _json
    p = path or _insider_push_dedup_path()
    try:
        seen = {}
        if p.exists():
            seen = _json.loads(p.read_text(encoding="utf-8")) or {}
            if not isinstance(seen, dict):
                seen = {}
    except Exception:
        seen = {}
    try:
        _cut = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=120)).strftime("%Y-%m-%d")
        seen = {k: v for k, v in seen.items() if isinstance(v, str) and v >= _cut}
    except Exception:
        logger.warning("signal cooldown filter failed", exc_info=True)
    seen[key] = date_str
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / (p.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(seen, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        logger.warning("signal cooldown write failed", exc_info=True)


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
    SCAN_INTERVAL_MINUTES = 5      # ADR-005: 10→5min, 加速数据采集
    MONITOR_INTERVAL_MINUTES = 0.5  # fallback: scheduler.monitor_interval_minutes
    # 2026-07-21: playbook 退出监控独立快循环默认值 (实例值由 _sb_get 覆盖; 配置加载失败时类属性兜底)
    PLAYBOOK_EXIT_FAST_LOOP_ENABLED = True
    PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC = 20
    # 2026-09-15: 盘中自动执行轮询间隔 (原 2290 行硬编码 300 → 参数化; 用户裁"执行"落 180s).
    #   实测 (9/14 日志): 单轮耗时 p50=1s/p90=9s, 间隔 p50=310s; 入场价漂移中位 0.15% (~费用线量级),
    #   缩短间隔主要省"门禁解除→进场"的响应延迟. 回滚: YAML 键改回 300.
    PLAYBOOK_AUTO_EXECUTE_INTERVAL_SEC = 180
    MAX_POSITIONS = 12             # fallback: scheduler.max_positions (同步 YAML 2026-06-03: 5→12)
    _DEFAULT_MIN_SIGNAL_STRENGTH = 50    # fallback: strategies.defaults.min_strength
    _DEFAULT_MIN_CONFIDENCE = 0.70        # 2026-06-13: fail-safe 统一 YAML SSOT
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
        # 2026-07-11: 注册为进程级共享单例, 全进程 (regime/position_sync/
        # order_executor 等) 复用此 Futu 连接, 消除 per-cycle 重建 + 线程泄漏
        from app.data.market_data_router import set_market_data_router
        set_market_data_router(self.data_router)
        # 保留 yahoo 属性别名，防止其他地方引用
        self.market_data = self.data_router

        # ── 分时段订阅管理器 (2026-05-01 新增) ──
        # 港股和美股共享 OpenD 1000 个槽位，各自收盘后退订释放
        self._subscription_mgr = None

        # 新闻情绪提供商：RSS + Qwen LLM（优于 Finnhub 关键词规则）
        self.news_provider = USNewsProvider()
        # 保留 news 属性别名，兼容旧引用
        self.news = self.news_provider

        # 决策适配器 (scheduler.voting_coordinator 是历史兼容字段名).
        # 旧 VotingCoordinator 超时配置已不在生产决策路径.
        # 2026-07-09: bridge模式 — 如果opportunity_scan_YYYYMMDD.json存在,
        # 2026-07-09: 移除VotingCoordinator fallback。
        # 根因: 3模型投票方向准确率47%≈随机(DeepSeek 88%偏空/qwen 88%偏多),
        #       bridge不存在时fallback到投票=用噪声替代信号。
        # 新逻辑: bridge存在 → BridgeVotingAdapter; bridge缺失 → technical/none。
        # deep_analysis skill负责盘前生成bridge文件(cron CST 21:00/ET 09:00)。
        self.voting_coordinator = None  # 先置空, 下面按bridge状态赋值

        # bridge覆盖: 尝试加载今日bridge文件
        _bridge_loaded = False
        try:
            from app.playbook.pipeline import _try_load_bridge
            _bridge = _try_load_bridge()
            if _bridge is not None:
                _bridge_n = len(_bridge._candidates)
                if _bridge_n < 3:
                    logger.warning(f"[scheduler] bridge加载成功但只有{_bridge_n}个候选(<3), 信号覆盖可能不足")
                else:
                    logger.info(f"[scheduler] bridge文件加载成功 ({_bridge_n}候选), decision_adapter → BridgeVotingAdapter")
                self.voting_coordinator = _bridge
                _bridge_loaded = True
        except Exception as _bridge_err:
            logger.warning(f"[scheduler] bridge加载失败: {_bridge_err}")

        # bridge不存在 → 按配置 fallback (2026-07-11 双脑清理)
        # playbook.fallback_when_no_bridge: technical → TechnicalFallbackAdapter (确定性兜底)
        #                                    none      → NullVotingAdapter (全 SKIP, 保守)
        if not _bridge_loaded:
            _fb_mode = "technical"
            try:
                from app.core.config_container import get_config as _gc_fb
                _fb_mode = str(_gc_fb().get_dotted("playbook.fallback_when_no_bridge", "technical")).lower()
            except Exception:
                logger.warning("fallback config read failed", exc_info=True)
            if _fb_mode == "technical":
                from app.playbook.pipeline import TechnicalFallbackAdapter
                self.voting_coordinator = TechnicalFallbackAdapter()
                logger.warning(
                    "[scheduler] bridge文件不存在 → TechnicalFallbackAdapter (确定性 technical 兜底). "
                    "deep_analysis cron应在盘前ET 09:00生成bridge文件. "
                    "如需回退全SKIP, 设 playbook.fallback_when_no_bridge=none"
                )
            else:
                from app.playbook.pipeline import NullVotingAdapter
                self.voting_coordinator = NullVotingAdapter()
                logger.warning(
                    "[scheduler] bridge文件不存在 → NullVotingAdapter (全SKIP). "
                    "deep_analysis cron应在盘前ET 09:00生成bridge文件."
                )
        
        # P0 清理 2026-04-25: 移除已废弃的 ai_trading_integration
        # ai_executor 和 ai_stoploss_monitor 从未被实际调用
        # 止损/止盈由 ExitMonitor 统一处理
        self.ai_executor = None
        self.ai_stoploss_monitor = None

        # ── 从 unified_config.yaml 加载调度器运行时参数 (SSOT) ──
        self._load_scheduler_params()

        # 共识引擎
        # [CLEANUP 2026-06-13] ConsensusEngine removed — zero calls, self-documented as disconnected

        # 做空策略路由器 (2026-07-01: 期权快速退出引擎已删除, 自动交易不做期权)
        try:
            from app.execution.short_strategy import get_short_strategy_router, load_short_config
            cfg = load_short_config()
            self.short_strategy_router = get_short_strategy_router(cfg)
            logger.info("[调度器] 做空策略路由器已初始化")
        except Exception as e:
            logger.warning(f"[调度器] 做空策略初始化失败: {e}")
            self.short_strategy_router = None

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
        # 主管线与 ORB 辅线共用此锁保护开仓临界区，防止同标的重复开仓
        self._open_position_lock = threading.RLock()

        # P0 2026-04-20: 注入 position_store 到 RiskEngine，使仓位限制检查生效
        # 修复: 原代码中 RiskEngine 从未收到 position_store，导致仓位检查完全跳过
        if self.risk_engine:
            self.risk_engine.position_store = self.position_store
            logger.info("[调度器] position_store 已注入 RiskEngine (仓位限制检查已激活)")

        # P1 2026-06-06: 注入 position_store 到 ExecutionEngine，替代 Futu API 方向冲突检查
        # 根因: engine.submit_order() 每次入场都 position_list_query(refresh_cache=True)
        #       50-200ms 网络延迟 × 每单，position_store 内存查询 <1ms
        if self.execution_engine:
            self.execution_engine.position_store = self.position_store
            logger.info("[调度器] position_store 已注入 ExecutionEngine (方向冲突检查走内存)")

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
        # C2 退役 (DEB-feedback-loop, 2026-07-31 用户裁): 产出零消费者 (on_news_passed
        #   只打日志, immediate_scan enabled=false 配置级死亡全史零触发), 采集能力被
        #   UE 自有 finnhub 链 (llm_universe_scan clue_finnhub_news) 覆盖 → 默认关停.
        #   保留 app.news_v2.models (UE 引用 UnifiedNewsItemV2 数据模型). 改 true 复活.
        _nv2_retired = False
        try:
            from app.core.config import get_config as _gc_nv2
            _nv2_retired = not bool(_gc_nv2().get_dotted("news_v2.enabled", True))
        except Exception:
            logger.warning("news v2 config read failed", exc_info=True)
        try:
            if _nv2_retired:
                raise RuntimeError("NewsV2 已退役 (news_v2.enabled=false, DEB-20260731 C2)")
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
                    # P2b (2026-06-16): 同时添加裸代码 (去掉 US. 前缀)
                    # 根因: 新闻文本中出现 "AAPL" 而非 "US.AAPL", 导致 relevance=0
                    if code.startswith("US."):
                        bare = code[3:]
                        if bare:
                            wl_codes.add(bare)
                if name:
                    wl_names.add(name)

            # P2b (2026-06-16): 从 Futu 查询公司名称注入 watchlist_names
            # 根因: self.watchlist 是纯字符串列表, 无公司名 → wl_names 永远为空
            try:
                _adapter = getattr(getattr(self, "execution_engine", None), "broker_adapter", None)
                if _adapter and hasattr(_adapter, "get_stock_names"):
                    _names = _adapter.get_stock_names(list(wl_codes))
                    if _names:
                        wl_names.update(_names.values())
                        logger.info(f"[NewsV2] 从 Broker 注入 {len(_names)} 个公司名")
            except Exception as _name_err:
                logger.debug(f"[NewsV2] 公司名注入跳过: {_name_err}")

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
            # 2026-06-01: 修复 'dict' has no get_dotted —
            #   line 223 cfg = load_short_config() 把外层 cfg 覆盖成 plain dict, 此处显式重新拿 ConfigContainer
            try:
                from app.core.config import get_config as _gc_news
                _news_cfg = _gc_news()
                _cn_cfg = _news_cfg.get_dotted("news_sources.cn", {}) or {}
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

            # P1 (2026-06-30): 突发新闻驱动盘中即时扫描
            # 只有当新闻 urgency == IMMEDIATE 且通过质量门时才会触发
            self.news_pipeline_v2.on_immediate = self._on_news_v2_immediate

            # P0 (2026-06-26): 盘中停止定时采集；突发新闻应走主动推送，轮询仅在盘前/盘后运行。
            try:
                from app.core.config_container import get_config as _news_cfg_getter
                self._news_v2_skip_market_hours = bool(
                    _news_cfg_getter().get_dotted("news_v2.skip_market_hours", True)
                )
            except Exception:
                self._news_v2_skip_market_hours = True

            # P1 (2026-06-30): 突发新闻即时扫描的冷却字典
            self._news_immediate_cooldowns: Dict[str, float] = {}

            logger.info("[NewsV2] 管线已初始化 (observe 模式) | watchlist=%d | skip_market_hours=%s",
                        len(wl_codes), self._news_v2_skip_market_hours)
        except Exception as e:
            if _nv2_retired:
                logger.info("[NewsV2] 已退役 (news_v2.enabled=false) — 产出零消费者, DEB-20260731 C2")
            else:
                logger.warning("[NewsV2] 初始化失败 (降级运行): %s", e)
            self.news_pipeline_v2 = None
            self._news_v2_skip_market_hours = True

        # ── 财报日历 (2026-04-29 新增) ──
        # 注: 原 USRiskPipeline (app/risk/us_risk_pipeline.py + app/risk/checks/) 已于
        # 2026-06-20 删除 — 实例化后 .run() 从未被调用, 实际风控走 order_executor 内联门。
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

        # ── RFC-075 ORB 辅线引擎: 2026-06-01 上线 → **2026-09-20 退役** ──
        # 用户裁「Playbook 成为唯一自动交易下单管线, 其他管线都是历史」⇒ app/orb/ 整体删除
        # (当年 orb.enabled=false, 最后成交 2026-06-22)。ORB 曾**直连** execution_engine
        # (不经 order_executor 闸), 故关闭靠模块退役而非改闸。回滚: git revert 本批。

        # ── 智能自适应轮询控制器 (2026-04-15 新增) ──
        # 解决免费 API 限流 + 美股高波动的矛盾
        from app.data.adaptive_polling import AdaptivePollingController, StockTier
        from app.utils.watchlist_manager import get_watchlist_with_tiers
        
        # Livermore 融合 (2026-04-16): 根据股票分层和数量自适应 API 预算
        # 原则: 持仓股优先, TIER1 > TIER2, ETF 低频
        watchlist_with_tiers = get_watchlist_with_tiers()
        num_stocks = len(watchlist_with_tiers.get('us_stocks', []))
        num_etfs = len([e for e in watchlist_with_tiers.get('etfs', []) if e.get('enabled', True)])

        # 自适应 API 预算: LV3 无限制 → 设置为股票+ETF总数
        # P0 (2026-06-05): 原 budget=num_stocks 不含 ETF，导致 ETF 被个股挤掉永远扫描不到
        # 修复: budget 包含可交易 ETF 数量，确保 HIGH_VOL 层 ETF 能进入轮询批次
        api_budget = num_stocks + num_etfs  # LV3 无限制, 覆盖全部标的
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

        # ETF 注册: 可交易ETF用P1优先级, 基准ETF(SPY/QQQ)保持P2
        _benchmark_etfs = {"US.SPY", "US.QQQ"}
        for etf in watchlist_with_tiers.get('etfs', []):
            symbol = etf.get('code', '')
            if not etf.get('enabled', True):
                continue
            if _sandbox_universe and symbol not in _sandbox_universe:
                _filtered_by_sandbox += 1
                continue
            # P0 (2026-06-04): 可交易ETF (XLF/XLY/XLK/XLI/XLE/IWM) 提升到 P1
            # 它们有独立交易信号, 30min轮询太慢会错过入场窗口
            if symbol in _benchmark_etfs:
                self.polling_controller.register_symbol(symbol, StockTier.WATCHLIST)
            else:
                self.polling_controller.register_symbol(symbol, StockTier.HIGH_VOLATILITY)
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
        self._playbook_exit_thread: Optional[threading.Thread] = None  # 2026-07-21 独立快循环

        # 信号冷却（避免同标的短时间重复下单）
        self._signal_cooldown: Dict[str, datetime] = {}
        self.SIGNAL_COOLDOWN_MINUTES = getattr(self, 'SIGNAL_COOLDOWN_MINUTES', 60)

        # P1 (2026-06-02): 从 DB 恢复信号冷却状态 — 防止重启后冷却丢失
        self._restore_signal_cooldown_from_db()

        logger.info(f"[调度器] 初始化完成，监控 {len(self.watchlist)} 只股票")

    def _restore_signal_cooldown_from_db(self):
        """从 trade_records DB 恢复最近平仓记录到 _signal_cooldown

        解决: 系统频繁重启导致内存态 _signal_cooldown 清空 → 重启后立即重新入场
        """
        try:
            import sqlite3
            from app.core.db_utils import get_main_db
            from datetime import timedelta
            from app.utils.timezone import now_et
            db_path = get_main_db()
            if not db_path.exists():
                return
            cooldown_min = getattr(self, 'SIGNAL_COOLDOWN_MINUTES', 60)
            cutoff = (now_et() - timedelta(minutes=cooldown_min)).isoformat()
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT symbol, MAX(exit_time) FROM trade_records "
                    "WHERE exit_time IS NOT NULL AND exit_time > ? GROUP BY symbol",
                    (cutoff,)
                ).fetchall()
            for sym, exit_ts in rows:
                try:
                    restored = datetime.fromisoformat(exit_ts)
                    if restored.tzinfo is None:
                        from app.utils.timezone import to_et
                        restored = to_et(restored)
                    self._signal_cooldown[sym] = restored
                except (ValueError, TypeError):
                    logger.warning("cooldown restore failed", exc_info=True)
            if rows:
                logger.info(
                    f"[Cooldown·DB恢复] 从 trade_records 恢复 {len(rows)} 个标的冷却 "
                    f"(窗口={cooldown_min}min): {[r[0] for r in rows]}"
                )
        except Exception as e:
            logger.debug(f"[Cooldown·DB恢复] 失败 (不影响启动): {e}")

    def _init_coordinator(self):
        """
        初始化 StrategyCoordinator 资金/锁账本 (灰度).

        默认 enabled=true (从 yaml 读), observe_only=true → 不阻断现有逻辑.
        当 enabled=false 时, self.coordinator 仍创建为对象但所有方法都 short-circuit.
        """
        try:
            from app.core.config_container import get_config as _gc
            cfg = _gc()
        except Exception:
            cfg = None

        enabled = bool(cfg.get_dotted("strategies.coordinator.enabled", False)) if cfg else False
        observe_only = bool(cfg.get_dotted("strategies.coordinator.observe_only", True)) if cfg else True
        max_total = float(cfg.get_dotted("strategies.coordinator.max_total_pct", 0.80)) if cfg else 0.80

        # T4 (2026-05-16): sandbox 模式强制 SymbolLock + SignalArbitrator enforce
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
        # fast_lane / trend_first 走 main bucket → 不在 caps 里 (避免重复加总)
        caps = {
            "main":     float(cfg.get_dotted("strategies.main.capital_pct", 0.80)) if cfg else 0.80,
            "llm_plan": float(cfg.get_dotted("strategies.llm_plan.capital_pct", 0.0)) if cfg else 0.0,
        }
        # B.3: bucket_map — 让 fast_lane / trend_first 占用计入 main bucket
        # Task #1 (2026-05-31): trend_first 也明确映射到 main 桶, 与 yaml capital_source: main 对齐
        bucket_map = {
            "fast_lane":   "main",
            "trend_first": "main",
        }

        from app.coordinator import (
            CapitalAllocator, SymbolLockManager, SignalArbitrator, ExitDispatcher
        )
        from app.coordinator.strategy_coordinator import get_coordinator
        from app.core.paths import get_decisions_db

        # P1-7 (2026-06-12): enable persistent capital ledger
        capital = CapitalAllocator(caps=caps, max_total_pct=max_total, observe_only=observe_only,
                                   bucket_map=bucket_map, db_path=str(get_decisions_db()))
        symlock = SymbolLockManager(observe_only=observe_only)
        arb = SignalArbitrator(audit_db_path=str(get_decisions_db()), audit_enabled=True)
        dispatcher = ExitDispatcher()
        # B.5 (2026-05-14): 注册 3 个最小 ExitPolicy (Phase A 全部委托 PositionExitMonitor)
        try:
            from app.strategy.exit_policies import (
                MainPipelineExitPolicy,
                FastLaneExitPolicy, LLMPlanExitPolicy,
            )
            dispatcher.register("main", MainPipelineExitPolicy())
            dispatcher.register("fast_lane", FastLaneExitPolicy())
            dispatcher.register("llm_plan", LLMPlanExitPolicy())
        except Exception as _ee:
            logger.warning(f"[调度器·Coordinator] ExitPolicy 注册失败 (降级): {_ee}")

        self.coordinator = get_coordinator(
            capital=capital, lock=symlock, arb=arb, exit_dispatcher=dispatcher,
            enabled=enabled,
        )

        # Coordinator 启动 banner — 一行扫到的关键信息
        banner = [
            "═" * 70,
            "║ Coordinator Phase A — 多策略协调层启动",
            f"║   enabled       : {enabled}",
            f"║   observe_only  : {observe_only}  (true=不阻断只记录 / false=强制)",
            f"║   max_total_pct : {max_total}",
            f"║   caps          : {caps}",
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
            scan_interval = cfg.get_dotted("scheduler.scan_interval_minutes", 3) if cfg else 3
            monitor_interval = cfg.get_dotted("scheduler.monitor_interval_minutes", 0.5) if cfg else 0.5
            wd.register("scan_loop", stale_threshold_sec=scan_interval * 60 * 5)  # 5 倍间隔
            wd.register("monitor_loop", stale_threshold_sec=monitor_interval * 60 * 5)
            # 2026-07-16 (H1): playbook 退出监控跑在 _llm_strategy_loop 内, 此前无看门狗覆盖 —
            #   若 dual.close_position 阻塞, 整个循环卡死 → 退出静默停摆, 止损失效.
            #   基础节奏 60s, 但盘后 LLM scan 可能耗时数分钟, 阈值放宽到 600s.
            # 2026-09-03 (D2(b), DEB-20260903-intraday-chain-audit §四): 600s 硬编码 → 配置化,
            #   **默认值不变** (不放宽检出)。放宽阈值会把真卡死的 heal 延迟同步放大 3×
            #   (watchdog._heal_multiplier=3.0), 故本次不改默认值, 只开出可回滚的旋钮;
            #   误报由 D2(a) 的轮内护航消除 (见 _wd_open_round_heartbeat)。
            _llm_loop_stale_sec = 600.0
            try:
                _llm_loop_stale_sec = float(
                    cfg.get_dotted("scheduler.watchdog_llm_loop_stale_sec", 600)
                ) if cfg else 600.0
            except Exception:
                _llm_loop_stale_sec = 600.0
            wd.register("llm_strategy_loop", stale_threshold_sec=_llm_loop_stale_sec)
            wd.start(check_interval_sec=60.0)
            logger.info(
                f"[Watchdog] 已启动 — scan={scan_interval}min, monitor={monitor_interval}min, "
                f"llm_strategy_loop={_llm_loop_stale_sec:.0f}s"
            )
        except Exception as _we:
            logger.warning(f"[Watchdog] 启动失败 (不影响交易): {_we}")

        # T6 (2026-05-16, 禁用 2026-06-18): CapitalAllocator 周期性 reconcile
        # 根因修复: T6 线程与 strategy_coordinator.reconcile() 双重 reconcile
        # T6 从 PositionStore 按 owner_strategy 分组 → 算出 0 或 0.643
        # strategy_coordinator 从完整持仓分类 auto/legacy/manual → 算出另一个值
        # 两者交替执行导致账本在 0↔0.643 之间震荡
        # 修复: 删除 T6，只保留 strategy_coordinator.reconcile() 作为唯一 reconcile 入口
        # (strategy_coordinator 已有完整持仓分类 + reconcile 逻辑，T6 是冗余)

    # ── D2(a): 长轮心跳护航 (2026-09-03, DEB-20260903-intraday-chain-audit §四) ──────────────
    # 病灶: `_llm_strategy_loop` 只在 while 顶部 ping 一次, 而单轮迭代实测可达 ~58min —
    #   2026-09-03 盘前轮 20:02:28→21:01:09 CST (57.7min) · 2026-09-01 同窗 20:02:28→21:00:12
    #   (57.7min) · 2026-08-22 盘后轮 04:00→04:30+ CST — 全部 ≫ 600s 阈值 → 连报 stale →
    #   heal 整服重启 (08-22 04:30:44 / 09-03 21:31:38, 后者 = 开盘后 90s 起 36s 盲窗)。
    #   〔归因勘误〕耗时的**不是**"盘前新闻采集 + 多标的 LLM 推荐": `llm_pushes.
    #   after_hours_scan_enabled=false` → run_after_hours_llm_scan 进门即返回。真实长步在
    #   盘前/盘后的数据采集链 (定桶逐标的 ATR / 宏观日历影子 / 富途通道影子全量拉取),
    #   这些步骤自身零进度日志零心跳 → 日志上表现为 20-32min 静默段。
    # 修法: 非 TRADING 阶段整轮护航 (轮内每 120s 补 ping), 受两重收敛避免变成"永不检出":
    #   ① **预算** (scheduler.watchdog_long_round_budget_sec, 默认 2700s): 超预算即停 ping →
    #      交回 watchdog 判定。无脑定时 ping 会让真卡死永远检不出 = 静默死亡
    #      (见 DEB-20260831-silent-death-hardening), 故必须有预算。
    #   ② **阶段**: 轮次跨到 TRADING 立即停 ping + 交回 watchdog (盘前轮次跨开盘意味着
    #      `run_playbook_auto_execute` (入场) 被阻塞, 真卡死检出归 watchdog 权威,
    #      09-03 那次误重启恰好解了阻塞, 不能把这条恢复路径挡掉)。
    #      2026-10-09 两阶段化 (用户裁「请优化调整」): 跨界先 WARNING, 宽限
    #      (scheduler.watchdog_cross_open_grace_sec, 默认 120s) 内未收轮才 ERROR+critical —
    #      10-09 实证慢轮跨开盘 0.2s 自解, 即时推送是纯噪音; 真卡死检出延迟 0s→grace。
    #   TRADING 阶段**不护航**: 入场执行在同一循环内, 卡死要紧检出 + heal;
    #   出场另有独立快循环 (`playbook_exit_loop`, 阈值 120s), 完全不受本改动影响。
    # 线程卫生 (同日 D3 教训): 全程最多 1 个护航线程, 每轮顶部结算 + join(5s), 停机随
    #   self.running 自终止 → 不累积。回滚: scheduler.watchdog_long_round_enabled=false + 重启。
    _WD_LOOP_NAME = "llm_strategy_loop"
    _WD_ROUND_INTERVAL_SEC = 120.0

    def _wd_ping(self, name: Optional[str] = None) -> None:
        """补一次 watchdog 心跳 (永不抛 — 心跳本身不能成为新的故障点)。"""
        try:
            from app.observability.watchdog import get_watchdog
            get_watchdog().ping(name or self._WD_LOOP_NAME)
        except Exception as _e:
            logger.debug(f"[Watchdog] 补心跳失败 (不影响循环): {_e}")

    def _wd_round_cfg(self):
        """读护航配置 (每次现读 → 跟随配置热重载): (enabled, budget_sec)。"""
        _enabled, _budget = True, 2700.0
        try:
            from app.core.config_container import get_config as _gc_wd
            _cfg = _gc_wd()
            _enabled = bool(_cfg.get_dotted("scheduler.watchdog_long_round_enabled", True))
            _budget = float(_cfg.get_dotted("scheduler.watchdog_long_round_budget_sec", 2700))
        except Exception as _e:
            logger.debug(f"[Watchdog·护航] 读配置失败, 用默认 (enabled=True,budget=2700): {_e}")
        return _enabled, _budget

    def _wd_open_round_heartbeat(self, phase) -> None:
        """为当前轮次开心跳护航 (仅非 TRADING / 非 WEEKEND 阶段; 永不抛)。"""
        try:
            self._wd_close_round_heartbeat()  # 幂等: 绝不叠加线程
            from .scheduler_triggers import MarketPhase
            if phase is None or phase in (MarketPhase.TRADING, MarketPhase.WEEKEND):
                return
            _enabled, _budget = self._wd_round_cfg()
            if not _enabled:
                return
            _t0 = time.time()
            _stop = threading.Event()
            _phase_name = getattr(phase, "value", str(phase))
            _state: Dict[str, Any] = {"pings": 0, "stopped_reason": None}

            def _keepalive():
                while self.running and not _stop.wait(self._WD_ROUND_INTERVAL_SEC):
                    _elapsed = time.time() - _t0
                    if _budget > 0 and _elapsed > _budget:
                        _state["stopped_reason"] = f"预算耗尽 ({_elapsed:.0f}s>{_budget:.0f}s)"
                        logger.error(
                            f"[Watchdog·护航] {_phase_name} 轮次已跑 {_elapsed:.0f}s > 预算 "
                            f"{_budget:.0f}s → 停止补心跳, 交回 watchdog 判定 (可能真卡死)"
                        )
                        return
                    try:
                        from .scheduler_triggers import (
                            get_market_phase as _gmp_wd, MarketPhase as _MP_wd,
                        )
                        if _gmp_wd() == _MP_wd.TRADING:
                            if _state.get("crossed_at") is None:
                                # 2026-10-09 降噪 (用户裁「请优化调整」): 跨开盘不再即时推送 —
                                #   10-09 实证: 121s 慢轮跨开盘后 0.2s 自解, 🚨 属误打扰。
                                #   两阶段: 停 ping 交回 watchdog 不变 (真卡死仍有 600s stale
                                #   兜底); 跨界先 WARNING, 宽限内未收轮才升级 ERROR+critical —
                                #   09-03「入场被阻塞必须可见」的检出能力保留, 延迟 0s→grace。
                                _state["crossed_at"] = time.time()
                                _state["stopped_reason"] = f"跨入 TRADING (已跑 {_elapsed:.0f}s)"
                                try:
                                    from app.core.config_container import get_config as _gc_gr
                                    _grace = float(_gc_gr().get_dotted(
                                        "scheduler.watchdog_cross_open_grace_sec", 120))
                                except Exception:
                                    _grace = 120.0
                                _state["grace"] = _grace
                                logger.warning(
                                    f"[Watchdog·护航] {_phase_name} 轮次跨开盘仍未结束 "
                                    f"(已跑 {_elapsed:.0f}s) → 停止补心跳, 交回 watchdog; "
                                    f"宽限 {_grace:.0f}s 内未收轮将升级 critical"
                                )
                            else:
                                # 显式 None 判定: grace=0 是合法配置 (即时升级/回滚阀),
                                #   不得用 `or` 兜底 (0 is falsy 会把 0 错成 120)。
                                _grace_now = _state.get("grace")
                                if _grace_now is None:
                                    _grace_now = 120.0
                                if time.time() - _state["crossed_at"] >= float(_grace_now):
                                    _msg = (
                                        f"🚨 [Watchdog·护航] {_phase_name} 轮次跨开盘 "
                                        f"≥{float(_grace_now):.0f}s 仍未收轮 "
                                        f"→ 判真卡死: 入场执行被阻塞"
                                    )
                                    logger.error(_msg)
                                    try:
                                        from app.utils.critical_alerts import send_critical
                                        send_critical(_msg, async_mode=True)
                                    except Exception as _se:
                                        logger.debug(f"[Watchdog·护航] critical 上送失败: {_se}")
                                    return
                            # 跨界后不再补 ping (交回 watchdog), 留在循环内等收轮/宽限升级
                            continue
                    except Exception as _pe:
                        logger.debug(f"[Watchdog·护航] 阶段判定失败 → 继续护航 (不误停): {_pe}")
                    self._wd_ping()
                    _state["pings"] += 1

            _th = threading.Thread(target=_keepalive, daemon=True, name="WdRoundHeartbeat")
            _th.start()
            # getattr 读法: 本属性只在循环期存在, 不在 __init__ 里占位 (测试桩无需构造它)
            self._wd_round_keeper = (_th, _stop, _t0, _phase_name, _state)
        except Exception as _we:
            logger.debug(f"[Watchdog·护航] 开启失败 (不影响循环): {_we}")

    def _wd_close_round_heartbeat(self) -> None:
        """结算本轮护航: 停线程 + 落轮次耗时遥测 (慢轮次可见, 取代过去的误报 stale)。"""
        _keeper = getattr(self, "_wd_round_keeper", None)
        if not _keeper:
            return
        self._wd_round_keeper = None
        _th, _stop, _t0, _phase_name, _state = _keeper
        _stop.set()
        try:
            _th.join(timeout=5.0)
        except Exception as _e:
            logger.debug(f"[Watchdog·护航] 护航线程 join 失败: {_e}")
        _dur = time.time() - _t0
        if _dur > 600 or _state.get("stopped_reason"):
            _why = f"; 提前停止: {_state['stopped_reason']}" if _state.get("stopped_reason") else ""
            logger.warning(
                f"[Watchdog·护航] {_phase_name} 轮次耗时 {_dur:.0f}s "
                f"(补心跳 {_state['pings']} 次{_why}) — 慢轮次, 非卡死"
            )

    # ── D4(a): 盘前步骤级取消 (2026-09-03, DEB-20260903-intraday-chain-audit §六) ────────────
    # 实况: 2026-09-03 盘前轮次 ET 09:01:09 进 run_playbook_generate, 到 09:31:38 仍未完成 →
    #   开盘后 90 秒内 TRADING 分支的 run_playbook_auto_execute (入场触发/re-price/下单)
    #   **一次都没跑**; 当天是 watchdog 误重启 (09:31:38) 意外打断盘前轮次才恢复了入场。
    # 修法: 盘前分支每个**非必需**步骤前查一次阶段, 已开盘就 `continue` 回轮顶 → 重新取 phase
    #   进 TRADING 分支, 把"跨界阻塞 = 剩余四步之和"收敛成"= 当前正在跑的那一步"。
    # 跳过零损失 (逐个核过调用点): generate / morning_push / topup_supplement /
    #   normalize_vol_buckets 在 TRADING 分支都有**同名 catch-up 调用**且各自幂等
    #   (scheduler_core.py TRADING 分支: generate→morning_push→premarket_invalidation→topup→
    #   normalize→auto_execute); 宏观日历影子是 record-only 且自带 06:00-09:29 ET 窗口门
    #   (跨开盘本就立即返回)。generate 本身**不跳** — 它是入场的供给前提, 跳了也只是换到
    #   TRADING 分支跑同一件事, 不省时间。
    # 残余 (转 D4(c) 另案): 单步内部不可中断 → 若长尾出在"跨开盘时正在跑的那一步"
    #   (09-03 正是 generate 跑 30.5min 未完成), 本修法救不了, 仍需重活外移 worker 线程。
    def _premarket_overrun(self, step_name: str) -> bool:
        """盘前轮次是否已跨开盘 (True → 调用方应跳过剩余盘前步骤, continue 回轮顶)。

        阶段判定异常 → False (保守: 维持原步骤顺序, 不因守卫故障跳步骤)。
        每轮至多触发一次 (触发即 continue → 下一轮进 TRADING 分支, 不再回到盘前分支)。
        """
        try:
            from .scheduler_triggers import get_market_phase as _gmp_po, MarketPhase as _MP_po
            if _gmp_po() != _MP_po.TRADING:
                return False
        except Exception:
            return False
        logger.warning(
            f"[盘前跨界] 市场已开盘但盘前轮次未收尾 → 跳过剩余盘前步骤 (下一站 {step_name}), "
            f"回轮顶进 TRADING 分支恢复入场执行 (D4(a), DEB-20260903 §六)"
        )
        try:
            from app.utils.critical_alerts import send_critical
            send_critical(
                f"⚠️ [盘前跨界] 盘前轮次跨开盘未收尾 → 已跳过剩余盘前步骤 (从 {step_name} 起), "
                f"转入 TRADING 分支恢复入场。若本告警反复出现或入场仍延迟, 说明某个盘前单步长尾 "
                f"(单步不可中断, 需 D4(c) 重活外移)",
                async_mode=True,
            )
        except Exception:
            logger.warning("premarket cross-open alert failed", exc_info=True)
        return True

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
            # 2026-07-21: playbook 退出监控独立快循环 (单宿主开关+间隔)
            self.PLAYBOOK_EXIT_FAST_LOOP_ENABLED = _sb_get(
                "scheduler.playbook_exit_fast_loop_enabled", self.PLAYBOOK_EXIT_FAST_LOOP_ENABLED
            )
            self.PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC = int(_sb_get(
                "scheduler.playbook_exit_fast_loop_interval_sec", self.PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC
            ))
            self.PLAYBOOK_AUTO_EXECUTE_INTERVAL_SEC = int(_sb_get(
                "scheduler.playbook_auto_execute_interval_sec", self.PLAYBOOK_AUTO_EXECUTE_INTERVAL_SEC
            ))
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
            # [DEB-fullchain #18 2026-08-16] 死键分歧告警: 生效键恒为 SSOT, 但旧键若仍
            #   声明不同值说明配置意图漂移 (宣称与实际不符家族), 启动时显性告警不静默。
            _warn_strength_key_divergence(cfg, ConfigKeys.MIN_EFFECTIVE_STRENGTH.value,
                                          ("strategies.defaults.min_strength",
                                           "scheduler.min_signal_strength"))
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
                "strategy.signal_cooldown", 60
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
                    "main_pct=%.2f stop_loss=%.3f take_profit=%.3f atr_x=%.2f",
                    self.MIN_CONFIDENCE,
                    int(self.MIN_SIGNAL_STRENGTH),
                    float(cfg.get_dotted(ConfigKeys.MAX_TOTAL_POSITION.value, 0.80)),
                    float(cfg.get_dotted(ConfigKeys.MAIN_BUDGET_PCT.value, 0.80)),
                    float(cfg.get_dotted(ConfigKeys.STOP_LOSS_PCT.value, 0.045)),
                    float(cfg.get_dotted(ConfigKeys.TAKE_PROFIT_PCT.value, 0.025)),
                    float(cfg.get_dotted(ConfigKeys.ATR_MULTIPLIER.value, 2.5)),
                )
            except Exception as _e:
                logger.debug("[配置SSOT] 摘要打印失败 (不阻断): %s", _e)
        except Exception as e:
            logger.warning("[调度器] 配置加载失败，使用类默认值: %s", e)

    def _get_subscription_codes(self) -> List[str]:
        """应订集合 (每60s被自动订阅循环调用)。

        P-2b (2026-08-01 订阅动态化, DEB-20260724 §7 用户裁决):
          应订集合 = 基准(roles.benchmark) ∪ 当日有效指令标的 ∪ playbook 未平仓
                     ∪ 主线 PositionStore 安全网; 静态 watchlist 降为可配置
                     (subscription.static_watchlist_enabled, 默认 false — 其唯一真实消费者
                      主线 scan_loop 已静默, DEB §7.5)。
          指令盘前生成后 60s 内被增量订阅; 收盘由现有时段机制 unsubscribe_all 全退,
          次日回调自然重建。回滚: subscription.dynamic_enabled=false。
        """
        try:
            from app.core.config_container import get_config as _gc_sub
            _sub_cfg = _gc_sub().get_dotted("subscription", {}) or {}
        except Exception:
            _sub_cfg = {}
        if not bool(_sub_cfg.get("dynamic_enabled", True)):
            return self._legacy_subscription_codes()

        codes = set()
        # ① 基准 (P-1 真源: roles.benchmark; 兜底 SPY/QQQ)
        try:
            from app.utils.watchlist_manager import get_symbols_by_role as _by_role
            _bench = _by_role("benchmark") or ["US.SPY", "US.QQQ"]
        except Exception:
            _bench = ["US.SPY", "US.QQQ"]
        codes.update(_bench)
        # ② 静态 watchlist (可配置, 默认关)
        if bool(_sub_cfg.get("static_watchlist_enabled", False)):
            codes.update(self.watchlist)
        # ③ 当日有效指令标的 (真正在交易的, 取价从快照升级 push 缓存)
        try:
            codes.update(self._today_instruction_symbols())
        except Exception as e:
            logger.warning(f"[订阅·动态] 当日指令标的查询失败 (降级不并入): {e}")
        # ④ playbook 未平仓 (退出监控高频取价)
        try:
            codes.update(self._playbook_open_symbols())
        except Exception as e:
            logger.warning(f"[订阅·动态] playbook 未平仓查询失败 (降级不并入): {e}")
        # ⑤ 主线 PositionStore (既有安全网: playbook 仓不入此 store, 通常为空;
        #    若未来恢复主线持仓, 自动让其走 path 2.5 免配额取价)
        try:
            if self.position_store:
                for p in self.position_store.all():
                    sym = p.symbol
                    if sym and sym.startswith("US."):
                        codes.add(sym)
        except Exception as e:
            logger.debug(f"[订阅] 合并持仓标的失败(降级): {e}")
        # 可观测性: 集合变化时落一条组成日志 (60s 轮询, 不变不刷屏)
        if codes != getattr(self, "_last_sub_set", None):
            self._last_sub_set = set(codes)
            logger.info(f"[订阅·动态] 应订集合 {len(codes)} 只 (基准{len(_bench)} + 指令/持仓/主线并集)")
        return list(codes)

    def _legacy_subscription_codes(self) -> List[str]:
        """P-2b 回滚路径 = 2026-08-01 前实现: watchlist ∪ 主线 PositionStore。"""
        codes = set(self.watchlist)
        try:
            if self.position_store:
                for p in self.position_store.all():
                    sym = p.symbol
                    if sym and sym.startswith("US."):
                        codes.add(sym)
        except Exception as e:
            logger.debug(f"[订阅] 合并持仓标的失败(降级只用watchlist): {e}")
        return list(codes)

    def _today_instruction_symbols(self) -> set:
        """当日 (ET) 有效方向指令的去重标的 (BUY/SELL_SHORT, 只读)。"""
        import sqlite3 as _sq
        from app.core.paths import get_trade_records_db
        from app.utils.timezone import now_et
        today = now_et().strftime("%Y-%m-%d")
        conn = _sq.connect(f"file:{get_trade_records_db()}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM playbook_instructions "
                "WHERE date=? AND direction IN ('BUY','SELL_SHORT')", (today,)).fetchall()
            return {r[0] for r in rows if r[0]}
        finally:
            conn.close()

    def _playbook_open_symbols(self) -> set:
        """playbook 未平仓标的 (只读, 与 is_playbook_open_position 同口径)。"""
        import sqlite3 as _sq
        from app.core.paths import get_trade_records_db
        conn = _sq.connect(f"file:{get_trade_records_db()}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM trade_records "
                "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                "AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)"
            ).fetchall()
            return {r[0] for r in rows if r[0]}
        finally:
            conn.close()

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

        [ADR-012] 旧作战计划轨道已废弃冻结 (2026-07-13 discovery管线停用); 注入默认下线,
        避免冻结标的污染扫描池。复活: feature_flags.legacy_battle_plan_enabled=true + 重启。
        """
        try:
            from app.core.config_container import get_config as _gc0
            if not _gc0().get_dotted("feature_flags.legacy_battle_plan_enabled", False):
                logger.info("[调度器] 旧作战计划轨道已下线 (legacy_battle_plan_enabled=false), 跳过注入")
                return
        except Exception:
            return

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

        # [ADR-013] LV3 股票推送订阅类型改为可配置精简集 (默认 QUOTE+K_1M).
        #   历史 V6 全量订阅 7 类 → 每笔成交多周期 kline 推送, SDK 逐条建 DataFrame, 接收线程满载一核。
        #   K_5M/K_15M/K_30M/ORDER_BOOK/TICKER 无有效消费者, 收敛后零交易链路影响。
        from app.data.futu_lv3_provider import _configured_stock_sub_types
        _stock_sub_types = _configured_stock_sub_types()
        all_stocks = tier1 + tier2
        if all_stocks:
            self.data_router.futu.subscribe(
                all_stocks,
                sub_types=_stock_sub_types
            )
            logger.info(
                f"[FutuLV3] ✅ Push 订阅: {len(all_stocks)} 只 "
                f"({'+'.join(_stock_sub_types)})"
            )

        # ETF 单独订阅 QUOTE + K_1M + K_15M (不需要订单流/逐笔)
        if etfs:
            self.data_router.futu.subscribe(etfs, sub_types=['QUOTE', 'K_1M', 'K_15M'])
            logger.info(f"[FutuLV3] ✅ ETF Push 订阅: {len(etfs)} 只 (QUOTE+K_1M+K_15M)")


        total_slots = (len(tier1) + len(tier2)) * len(_stock_sub_types) + len(etfs) * 3
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
                # 2026-07-14: 跳过 playbook_sim 仓位 — position_sync 正确跳过它们 (line 1069),
                #   若此处恢复到 position_store 会导致一致性校验报幽灵仓
                # 2026-07-16 P0 (重启误平根因): open_positions 的 owner_strategy/strategy_id
                #   在重启时可能尚未回填 → 字段判定不可靠, 统一用 is_playbook_open_position.
                from app.playbook.pipeline import is_playbook_open_position
                if is_playbook_open_position(symbol):
                    continue
                # 恢复股票持仓到 PositionStore (2026-06-12 实现)
                try:
                    direction = Direction.SHORT if db_pos.get("direction", "").upper() == "SHORT" else Direction.LONG
                    try:
                        regime = MarketRegime(db_pos.get("market_regime", "RANGE"))
                    except (ValueError, KeyError):
                        regime = MarketRegime.RANGE

                    entry_time_str = db_pos.get("entry_time", "")
                    if entry_time_str:
                        try:
                            from datetime import datetime as _dt
                            entry_time = _dt.fromisoformat(str(entry_time_str))
                        except (ValueError, TypeError):
                            from app.utils.timezone import now_et as _now_et
                            entry_time = _now_et()
                    else:
                        from app.utils.timezone import now_et as _now_et
                        entry_time = _now_et()

                    position = Position(
                        strategy_id=db_pos.get("strategy_id", "") or "main_pipeline",
                        signal_id=db_pos.get("signal_id", ""),
                        symbol=symbol,
                        direction=direction,
                        size=float(db_pos.get("size", 0)),
                        avg_entry_price=float(db_pos.get("avg_entry_price", 0)),
                        initial_sl=float(db_pos.get("initial_sl", 0) or 0),
                        initial_tp=float(db_pos.get("initial_tp", 0) or 0),
                        current_sl=float(db_pos.get("current_sl", 0) or 0),
                        take_profit_levels=db_pos.get("take_profit_levels", []),
                        time_stop_hours=int(db_pos.get("time_stop_hours", 72) or 72),
                        market_regime=regime,
                    )
                    position.entry_time = entry_time
                    position.trail_state = db_pos.get("trail_state", {}) or {}
                    position.owner_strategy = (
                        db_pos.get("owner_strategy")
                        or db_pos.get("strategy_id")
                        or "main"
                    )
                    if db_pos.get("position_id"):
                        position.position_id = db_pos["position_id"]
                    if db_pos.get("trace_id"):
                        position.trace_id = db_pos["trace_id"]

                    # 异常仓位标记: 允许恢复但阻止自动平仓
                    if db_pos.get("is_anomalous"):
                        position.metadata = position.metadata or {}
                        position.metadata["_close_blocked"] = True
                        position.metadata["_close_blocked_reason"] = "DB is_anomalous=1"

                    # P0 (2026-06-14): 从 trail_state_store 恢复 current_sl
                    # open_positions.current_sl 可能过期 (ExitMonitor 只更新内存),
                    # trail_state_store 每 60s 持久化一次, 数据更新
                    try:
                        from app.risk.trail_state_store import get_trail_state_store
                        _ts = get_trail_state_store().load(symbol)
                        if _ts:
                            _ts_sl = float(_ts.get("current_sl", 0) or 0)
                            if _ts_sl > 0:
                                position.current_sl = _ts_sl
                            _ts_isl = float(_ts.get("initial_sl", 0) or 0)
                            if _ts_isl > 0 and position.initial_sl <= 0:
                                position.initial_sl = _ts_isl
                            # 恢复 trail_state dict (peak prices etc.)
                            for _k in ("highest_price", "lowest_price", "prev_scan_price",
                                       "last_trigger_time", "entry_time_ts", "entry_session"):
                                if _k in _ts:
                                    position.trail_state[_k] = _ts[_k]
                    except Exception as _ts_err:
                        logger.debug(f"[调度器·对账] trail_state 恢复跳过 {symbol}: {_ts_err}")

                    self.position_store.add(symbol, position)
                    logger.info(
                        f"[调度器·对账] ✅ 恢复股票持仓 {symbol} {direction.value} "
                        f"{int(position.size)}股 @ ${position.avg_entry_price:.2f} "
                        f"SL=${position.current_sl:.2f}"
                    )
                except Exception as restore_err:
                    logger.warning(f"[调度器·对账] ❌ 恢复 {symbol} 失败: {restore_err}")
            
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

        # B3 (2026-07-20): 交易环境横幅 — 启动时醒目打印账户路由与风控 fail 模式,
        #   防止切实盘时配置组合被忽视 (LIVE + fail-open 的硬拦截在 di_container, 此处仅可见性).
        try:
            import os as _os_b3
            from app.core.config_container import get_config as _get_cfg_b3
            _cfg_b3 = _get_cfg_b3()
            _acct_b3 = _os_b3.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
            _allow_b3 = _os_b3.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
            _acb_b3 = _cfg_b3.get_dotted("risk.acb_fail_mode", "closed")
            _pf_b3 = _cfg_b3.get_dotted("risk.preflight_fail_mode", "open")
            _exec_b3 = _cfg_b3.get_dotted("playbook.execute_mode", "futu_simulate")
            _banner_log = logger.warning if _acct_b3 == "real" else logger.info
            _banner_log(
                "=" * 20 + " [交易环境] " + "=" * 20 + "\n"
                f"  账户路由 AUTO_TRADE_ACCOUNT={_acct_b3} | ALLOW_REAL_TRADING={_allow_b3}\n"
                f"  playbook.execute_mode={_exec_b3}\n"
                f"  risk.acb_fail_mode={_acb_b3} | risk.preflight_fail_mode={_pf_b3}"
                + (" ⚠️ 实盘要求均为 closed" if _acct_b3 == "real" else " (SIM 期 open 合理)")
                + "\n" + "=" * 52
            )
        except Exception as _b3_err:
            logger.warning(f"[调度器] 交易环境横幅打印失败 (不影响启动): {_b3_err}")

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

        # RFC-078 批E (2026-09-09): 实盘成交实时推送订阅 (record-only, 真钱成交秒级落账+飞书提醒).
        #   自建常驻 US+HK 双 ctx (被动接收无需解锁, 不碰下单 ctx); REAL-only 过滤防 sim
        #   同账户 SIMULATE 成交污染; 异常全收口 — 订阅失败不阻断主服务 (每日 sync cron 兜底).
        #   回滚: .env.us FTSEEK_LIVE_DEAL_PUSH=0 + 重启.
        try:
            from app.web_dashboard.live_deal_push import start_live_deal_push
            if start_live_deal_push():
                logger.info("[调度器] 实盘成交推送订阅已启动 (RFC-078 批E)")
        except Exception as _ldp_err:
            logger.warning(f"[调度器] 实盘成交推送启动失败 (不影响主链路, 每日 sync 兜底): {_ldp_err}")

        # 信号扫描线程 (循环内部自带 WEEKEND/OVERNIGHT 守卫)
        self._scan_thread = threading.Thread(
            target=self._scan_loop, name="SignalScanLoop", daemon=True
        )
        self._scan_thread.start()

        # 持仓监控线程 (循环内部自带 WEEKEND/OVERNIGHT 守卫)
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="PositionMonitorLoop", daemon=True
        )
        self._monitor_thread.start()

        # LLM 策略引擎线程 (循环内部自带 WEEKEND 守卫)
        self._llm_thread = threading.Thread(
            target=self._llm_strategy_loop, name="LLMStrategyLoop", daemon=True
        )
        self._llm_thread.start()

        # 新闻管线 V2 线程 (循环内部自带 sleep_if_market_closed 守卫)
        if self.news_pipeline_v2:
            self._news_v2_thread = threading.Thread(
                target=self._news_v2_loop, name="NewsV2Loop", daemon=True
            )
            self._news_v2_thread.start()
            logger.info("[NewsV2] 后台采集线程已启动")

        # VIX 监控线程 (循环内部自带 sleep_if_market_closed 守卫)
        if self.vix_monitor:
            self._vix_thread = threading.Thread(
                target=self._vix_monitor_loop, name="VIXMonitorLoop", daemon=True
            )
            self._vix_thread.start()
            logger.info("[VIX Monitor] 后台监控线程已启动")

        # ManualPositionGuard 线程 (循环内部自带 WEEKEND/OVERNIGHT 守卫)
        self._manual_guard_thread = threading.Thread(
            target=self._manual_guard_loop, name="ManualGuardLoop", daemon=True
        )
        self._manual_guard_thread.start()
        logger.info("[ManualGuard] 手动仓位风险监控线程已启动")

        # 内部人集群买入每日扫描线程 (循环内部自带 sleep_if_market_closed 守卫)
        self._insider_scan_thread = threading.Thread(
            target=self._insider_scan_loop, name="InsiderScanLoop", daemon=True
        )
        self._insider_scan_thread.start()
        logger.info("[InsiderScan] 每日内部人集群扫描线程已启动")

        # Playbook 退出监控独立快循环线程 (2026-07-21)
        #   单宿主: enabled 时 llm_strategy_loop 内旧调用点自动跳过 (防并发重复平仓)
        if self.PLAYBOOK_EXIT_FAST_LOOP_ENABLED:
            self._playbook_exit_thread = threading.Thread(
                target=self._playbook_exit_fast_loop, name="PlaybookExitFastLoop", daemon=True
            )
            self._playbook_exit_thread.start()
            logger.info(f"[PlaybookExit] 独立快循环线程已启动 (interval={self.PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC}s)")


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
                logger.info("[分时调度] 当前为美股盘前/交易时段，立即订阅应订集合 (P-2b 动态)...")
                from app.data.futu_lv3_provider import _configured_active_set_sub_types
                self._subscription_mgr.ensure_subscribed(
                    self._get_subscription_codes(),
                    sub_types=_configured_active_set_sub_types(),
                )
            else:
                reason = "港股交易时段" if self._subscription_mgr.is_hk_trading() else "美股已收盘"
                logger.info(f"[分时调度] 当前{reason}，暂不订阅 (自动循环将在盘前激活)")

            # 启动自动循环 — 盘前自动订阅，港股时段/美股收盘自动退订
            self._subscription_mgr.start_auto_loop(
                codes_callback=self._get_subscription_codes,
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


        # 2026-08-27 退役⑦: C5 跨策略归因 jsonl (attribution_daily.jsonl) 已删 —
        # 主线/ORB 仅 sim 研究 (策略隔离 2026-07-15) 后 main_total_trades 恒 0,
        # 全仓无消费方 (仅 .hermes 审计文档提及). 回放历史保留在 jsonl 文件本身.
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

    def _manual_guard_loop(self):
        """手动仓位风险监控主循环 - 在 manual_position_guard.py 中实现"""
        from app.risk.manual_position_guard import manual_guard_loop
        manual_guard_loop(self)

    def _is_market_open(self) -> bool:
        """判断美东交易时间（09:30 - 16:00 周一到周五） - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import is_market_open
        return is_market_open()

    def _is_hk_trading_hours(self) -> bool:
        """判断当前是否为港股交易时段 (北京时间)

        港股已停止自动交易 (2026-06-11)，始终返回 False。
        原逻辑: 08:30-16:00 CST 期间 FTSeek-US 断开 OpenD 释放槽位。
        如需恢复港股，取消下面的 return False 注释。
        """
        # 港股已停用
        return False
        # --- 以下为原始逻辑，恢复港股时启用 ---
        # HKT = pytz.timezone("Asia/Hong_Kong")
        # now = datetime.now(HKT)
        # hour = now.hour
        # minute = now.minute
        # weekday = now.weekday()
        # if weekday >= 5:
        #     return False
        # t = hour * 60 + minute
        # return 510 <= t <= 960

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

    # 2026-08-22 audit-fix A3: 影子脚本模块缓存. 原先 _llm_strategy_loop 每轮循环
    # (TRADING 60s / 盘后各分支) 对 ~12 个 scripts/*_shadow*.py 全量重执行
    # spec_from_file_location+exec_module — 脚本的节流/幂等全在磁盘状态文件或 DB,
    # 重执行只重复付出 import+模块级初始化成本, 零收益. 改为进程内缓存: 首次加载
    # 成功后复用同一 module 对象. 注意: 运行期修改 scripts/*.py 不再热生效, 需重启
    # (与主链路 import 语义一致); 加载失败不缓存, 下轮自动重试 (与原行为相同).
    def _load_script_module(self, mod_name: str):
        cache = getattr(self, "_script_mod_cache", None)
        if cache is None:
            cache = self._script_mod_cache = {}
        if mod_name in cache:
            return cache[mod_name]
        import importlib.util
        from pathlib import Path as _Path
        _script_path = _Path(__file__).resolve().parents[2] / "scripts" / f"{mod_name}.py"
        _spec = importlib.util.spec_from_file_location(mod_name, str(_script_path))
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        cache[mod_name] = _mod
        return _mod

    def _llm_strategy_loop(self):
        """LLM 策略引擎循环 - 按市场阶段自适应调度"""
        from app.pipeline.llm_pipeline import run_after_hours_llm_scan, run_open_checker, run_open_price_refresh
        from .scheduler_triggers import get_market_phase, MarketPhase
        import time

        logger.info("[LLM策略] 策略引擎循环已启动 (按市场阶段自适应)")
        last_trading_run = None

        # 2026-07-16: last_afterhours_run 持久化 — 修复"每次重启即重推盘前/盘后LLM推荐".
        #   原为纯内存变量, 重启复位 None → 循环首圈命中 is-None 立即重推(不管刚推过).
        #   现启动时从 data/llm_scan_state.json 载回上次时间, 距上次不足窗口则不重推.
        import json as _json
        from pathlib import Path as _Path
        _llm_scan_state = _Path(__file__).resolve().parents[2] / "data" / "llm_scan_state.json"

        _persist_state = {"last": "<unset>"}

        def _persist_ah(ts):
            if _persist_state["last"] == ts:
                return
            try:
                with open(_llm_scan_state, "w", encoding="utf-8") as _f:
                    _json.dump({"last_afterhours_run": ts.isoformat() if ts else None}, _f)
                _persist_state["last"] = ts
            except Exception as _pe:
                # 2026-09-10 debug→warning: 并非"不影响" — 写不进 last_afterhours_run,
                #   下次重启即复位 None → 首圈命中 is-None 重推盘前/盘后 LLM 推荐,
                #   正是下方 2026-07-16 那段修复要防的形态。fail 必须可见可计数。
                logger.warning(f"[LLM策略] scan 时间持久化失败 (重启将重推LLM推荐): {_pe}", exc_info=True)

        last_afterhours_run = None
        try:
            if _llm_scan_state.exists():
                _saved = _json.load(open(_llm_scan_state, encoding="utf-8")).get("last_afterhours_run")
                if _saved:
                    last_afterhours_run = datetime.fromisoformat(_saved)
                    logger.info(f"[LLM策略] 载回上次LLM推荐时间: {_saved} (重启不重推)")
        except Exception as _le:
            # 2026-09-10 debug→warning: 载入失败按"无记录"处理 → 重启即重推, 同上必须可见。
            logger.warning(f"[LLM策略] scan 时间载入失败 (视为无记录, 重启将重推LLM推荐): {_le}", exc_info=True)
        # 2026-10-18: WEEKEND 分支每 60s 调 _persist_ah(None), 原实现每轮重写 JSON;
        #   记录已落盘值, 值未变则零写入。
        _persist_state["last"] = last_afterhours_run

        while self.running:
            # D2(a): 结算上一轮护航 (停线程 + 落轮次耗时遥测) 再补心跳。原实现只有这一处 ping,
            #   而单轮迭代实测 ~58min ≫ 阈值 600s → 误判 stale → heal 整服重启。
            self._wd_close_round_heartbeat()
            self._wd_ping()
            try:
                phase = get_market_phase()
                now = datetime.now(ET)
                # D2(a): 非盘中阶段为本轮开"预算受限"的心跳护航; TRADING 阶段不护航 (紧检出)
                self._wd_open_round_heartbeat(phase)

                if phase == MarketPhase.TRADING:
                    # Catch-up: 盘前因重启/超时错过生成时的补救 (DB幂等, 已有指令则跳过)
                    try:
                        from app.playbook.pipeline import run_playbook_generate
                        run_playbook_generate(self)
                    except Exception as _e:
                        logger.warning(f"[playbook] TRADING catch-up生成异常 (已隔离): {_e}")

                    # 2026-07-11: 独立盘前指令推送 (不依赖生成内联, 持久化幂等+重试)
                    try:
                        from app.playbook.pipeline import run_playbook_morning_push
                        run_playbook_morning_push(self)
                    except Exception as _e:
                        logger.warning(f"[playbook] TRADING 盘前指令推送异常 (已隔离): {_e}")

                    # 盘前失效评估 (record-only): 窗口 09:35-09:59 ET 在开盘之后,
                    # 必须挂 TRADING 分支 (挂 PRE_MARKET 永不触发). 函数内窗口+每日幂等 guard.
                    try:
                        from app.playbook.premarket_invalidation import run_check as _pm_inv
                        _pm_inv(self)
                    except Exception as _e:
                        logger.debug(f"[playbook] 盘前失效评估异常 (已隔离): {_e}")

                    # 2026-07-24 (用户裁决): topup 盘中窗 (默认 10:30-11:30 ET) — 实时价重评
                    #   回调窗, 捕捉当日跌进窗的标的; 盘前窗命中 PRE_MARKET 分支的同一调用。
                    #   函数自带双窗口时间门/分窗幂等/共享当日上限, 此处无条件调用安全。
                    try:
                        from app.playbook.pipeline import run_playbook_topup_supplement
                        run_playbook_topup_supplement(self)
                    except Exception as _e:
                        logger.warning(f"[playbook] TRADING topup 补量异常 (已隔离): {_e}")

                    # 2026-07-17: 定桶 catch-up — 盘中重启/盘中新增指令会错过 PRE_MARKET 定桶,
                    #   此处幂等补标 (已落桶的行跳过), 保证下单审计能读到 vol_bucket。
                    try:
                        from app.playbook.pipeline import run_playbook_normalize_vol_buckets
                        run_playbook_normalize_vol_buckets(self)
                    except Exception as _e:
                        logger.warning(f"[playbook] TRADING 定桶 catch-up 异常 (已隔离): {_e}")

                    # Playbook: 盘中自动执行 triggered 指令 (OKR Week 1: 数据积累)
                    # 每 N 秒检查一次是否有新的 triggered 指令需要自动执行
                    # 2026-07-10: 从30分钟降到5分钟 — 分批建仓指令依赖价格触及入场区间,
                    #   30分钟间隔可能错过最佳入场窗口(如日内闪崩回调后快速反弹)
                    # 2026-09-15: 硬编码 300 → self.PLAYBOOK_AUTO_EXECUTE_INTERVAL_SEC (默认 180,
                    #   配置键 scheduler.playbook_auto_execute_interval_sec). 用户裁"执行": 缩短
                    #   门禁解除→进场的响应延迟. 单轮耗时实测 p50=1s/p90=9s, 调用余量充足.
                    if not hasattr(self, '_playbook_auto_execute_last_run'):
                        self._playbook_auto_execute_last_run = None
                    
                    if (self._playbook_auto_execute_last_run is None or 
                        (now - self._playbook_auto_execute_last_run).total_seconds() >= self.PLAYBOOK_AUTO_EXECUTE_INTERVAL_SEC):
                        try:
                            from app.playbook.pipeline import run_playbook_auto_execute
                            run_playbook_auto_execute(self)
                            self._playbook_auto_execute_last_run = now
                        except Exception as _e:
                            logger.warning(f"[Playbook] 盘中自动执行异常 (已隔离): {_e}")

                    # 盘中再入场扫描 (2026-08-06 用户指示: sim 直上线快速验真):
                    #   当日非硬亏平仓标的 回踩VWAP+slope转正+量比/spread 确认 → 重武装指令走全门链.
                    #   函数内 300s 节流 + 10:00-13:55 ET 窗口 + sim-only 守卫. 回滚: intraday_reentry.enabled=false.
                    try:
                        from app.playbook.intraday_reentry import run_scan as _reentry_scan
                        _reentry_scan(self)
                    except Exception as _e:
                        logger.warning(f"[Playbook] 盘中再入场扫描异常 (已隔离): {_e}")

                    # Phase 1 (2026-06-24): futu_simulate 真实模拟仓位退出监控 (broker 平仓)
                    # 2026-07-21: 独立快循环启用时跳过 (单宿主约束, 防双线程并发重复平仓;
                    #   DB outcome 幂等兜底仍在, 双保险). 回滚: 配置 enabled=false 即恢复此路径.
                    if not self.PLAYBOOK_EXIT_FAST_LOOP_ENABLED:
                        try:
                            from app.playbook.pipeline import monitor_playbook_exits
                            monitor_playbook_exits(self)
                        except Exception as _e:
                            logger.warning(f"[Playbook] sim exit monitor 异常 (已隔离): {_e}")

                    # 富途未利用通道影子采集 (DEB-20260812 主张A/B, record-only):
                    #   capital_flow 分钟资金流 + derivative_unusual 期权大单异动落库,
                    #   攒样供 8/24 同窗裁决 (增量 IC / 大单流领先假说). 函数内节流
                    #   300s + 按 (symbol, item_time)/事件哈希去重. 零执行路径改动.
                    #   回滚: futu_channels.shadow_collect_enabled=false + 重启.
                    try:
                        _mod = self._load_script_module("futu_channel_shadow_collect")
                        _mod.run_collect(self, phase="TRADING")
                    except Exception as _e:
                        logger.warning(f"[LLM策略] futu通道影子采集异常 (已隔离): {_e}", exc_info=True)

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
                                _persist_ah(now)
                            time.sleep(60)  # 可中断休眠
                            for _ in range(59):
                                if not self.running:
                                    return
                                time.sleep(1)
                            continue
                    except Exception as e:
                        # 2026-09-10 debug→warning + exc_info: 这是 fail-open — 节假日判定抛异常
                        #   即直接落入下方交易分支 (开盘确认器/生成/推送)。按 DEB-20260721 R4 共识
                        #   "fail-open 失明必须 WARNING + 可计数", 原 debug 在 INFO 级下零可见。
                        logger.warning(f"[LLM策略] 节假日检查异常 (fail-open 继续): {e}", exc_info=True)

                    # P0 修复: 开盘确认器 — run_open_checker 内部自带时间窗口 + 日期幂等
                    # 无需外部 guard，函数自行判断 09:15-09:29 ET 窗口并每日执行一次
                    run_open_checker(self)

                    # 开盘价刷新: 09:30:00-09:31:00 ET 用真实开盘价刷新 confirmed_price + SL/TP
                    run_open_price_refresh(self)

                    # 盘前: 每2小时采集新闻 + LLM 推荐 (不执行交易)
                    if last_afterhours_run is None or (now - last_afterhours_run).total_seconds() >= 7200:
                        logger.debug("[LLM策略] 盘前 → 新闻采集 + LLM 推荐 (关停时函数内静默)")
                        run_after_hours_llm_scan(self, phase=phase)
                        last_afterhours_run = now
                        _persist_ah(now)

                    # 2026-04-24 关闭: 盘前观察模式已彻底移除 (Phase 1 dead code deleted)
                    # 原因: Futu LV3 盘前无数据，轮询无意义，观察模式已退役。
                    # 盘前阶段仅保留: 作战计划生成 (08:30-09:25) + 新闻采集 + LLM 推荐

                    # Playbook Layer 0+1 盘前生成 (每日幂等, 自带节假日/时间窗口 guard)
                    try:
                        from app.playbook.pipeline import run_playbook_generate
                        run_playbook_generate(self)
                    except Exception as _e:
                        # 2026-07-04: debug→warning — 盘前生成失败会导致当日无作战计划, 必须可见
                        logger.warning(f"[LLM策略] playbook 盘前生成异常 (已隔离): {_e}", exc_info=True)

                    # 2026-07-24 (用户裁决): 扩容补量 — 当日指令 < 目标下限时从流动性池走正式链补足.
                    #   函数内自带双窗口时间门(盘前08:45-09:30/盘中10:30-11:30 ET)/分窗幂等/开关, 此处无条件调用安全.
                    # D4(a): 已跨开盘 → 跳过 (TRADING 分支有同名 catch-up, 幂等; 详见 _premarket_overrun)。
                    #   诚实标注: 本步自带 08:45-09:30/10:30-11:30 ET 双窗口门, 跨开盘本就立即返回 →
                    #   此守卫是**防御性**的 (窗口若被放宽仍护住入场), 不是今日实测的收益点。
                    if self._premarket_overrun("topup_supplement"):
                        continue
                    try:
                        from app.playbook.pipeline import run_playbook_topup_supplement
                        run_playbook_topup_supplement(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] playbook 补量异常 (已隔离): {_e}", exc_info=True)

                    # 2026-07-17: 盘前波动率定桶落库 (DA/UE 直写指令补 vol_bucket/atr_pct 到主表)
                    # D4(a): 本步实测最长 (09-03 达 31.4min, 逐标的 ATR) → 跨开盘必须让位给入场
                    if self._premarket_overrun("normalize_vol_buckets"):
                        continue
                    try:
                        from app.playbook.pipeline import run_playbook_normalize_vol_buckets
                        run_playbook_normalize_vol_buckets(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] playbook 盘前定桶异常 (已隔离): {_e}", exc_info=True)

                    # 2026-07-11: 独立盘前指令推送 (不依赖生成内联, 持久化幂等+重试)
                    if self._premarket_overrun("morning_push"):
                        continue
                    try:
                        from app.playbook.pipeline import run_playbook_morning_push
                        run_playbook_morning_push(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] playbook 盘前指令推送异常 (已隔离): {_e}", exc_info=True)

                    # 宏观日历影子采集 (2026-08-12, financial-data-query 数据源接入):
                    # 蚂蚁API盘前拉8个美国核心宏观指标最新值/披露日, jsonl落库.
                    # 函数内自带时间窗口(06:00-09:29 ET)+每日幂等. record-only 不进决策.
                    # 回滚: macro_calendar.shadow_collect_enabled=false + 重启.
                    # D4(a): 实测单次 26.7min (8 指标外部 API, 无逐指标日志), 但**窗口门在入口**
                    #   (06:00-09:29 ET) → 跨开盘后本就立即返回, 此守卫同属防御性; 真正的风险是
                    #   "09:29 前开始、跑过开盘"这一段, 单步内部不可中断 → 属 D4(c) 残余。
                    if self._premarket_overrun("macro_calendar_shadow"):
                        continue
                    try:
                        _mod_mc = self._load_script_module("macro_calendar_shadow_collect")
                        _mod_mc.run_collect(self, phase="PRE_MARKET")
                    except Exception as _e:
                        logger.warning(f"[LLM策略] 宏观日历影子采集异常 (已隔离): {_e}", exc_info=True)

                elif phase == MarketPhase.AFTER_HOURS:
                    # 盘后: 处理盘中排队的 LLM 审计
                    self._process_deferred_audit_queue()

                    # 富途通道影子·盘后收尾采集 (DEB-20260812): 盘后序列才完整,
                    # 每日 1 次全量拉取 (函数内按日幂等). 回滚同 TRADING 挂载.
                    try:
                        _mod = self._load_script_module("futu_channel_shadow_collect")
                        _mod.run_collect(self, phase="AFTER_HOURS")
                    except Exception as _e:
                        logger.warning(f"[LLM策略] futu通道影子盘后采集异常 (已隔离): {_e}", exc_info=True)
                    
                    # 盘后: 每2小时采集新闻 + LLM 推荐 (不执行交易)
                    if last_afterhours_run is None or (now - last_afterhours_run).total_seconds() >= 7200:
                        logger.debug("[LLM策略] 盘后夜盘 → 新闻采集 + LLM 推荐 (关停时函数内静默)")
                        run_after_hours_llm_scan(self, phase=phase)
                        last_afterhours_run = now
                        _persist_ah(now)

                    # Playbook Layer 3 每日盘后追踪 (每日幂等)
                    try:
                        from app.playbook.pipeline import run_playbook_track, run_playbook_weekly
                        run_playbook_track(self)
                        run_playbook_weekly(self)  # 周级幂等, 仅周五实际出周报
                    except Exception as _e:
                        # 2026-07-04: debug→warning — 原debug级别在INFO日志中不可见,
                        # 导致盘后追踪静默失败(AMD/BABA holding仓位未被EOD平仓)
                        logger.warning(f"[LLM策略] playbook 盘后追踪异常 (已隔离): {_e}", exc_info=True)

                    # MFE_STALL 入场侧影子打标 (2026-08-12, record-only): 8/11诊断
                    #   发现 news_positive 触发 100%出现在 STALL 笔 (6/6) — 攒样验证
                    #   (n≥30 再裁门禁), 零执行路径改动. 回滚: 配置键 false + 重启.
                    try:
                        _mod = self._load_script_module("stall_entry_shadow_tag")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] stall影子打标异常 (已隔离): {_e}", exc_info=True)

                    # 入场去同步化影子打标 (2026-08-12, record-only): 8/11 实锤 21秒
                    #   5笔齐射 (禁触窗解除瞬间同轮倾泻) 4亏1赢; 历史分散入场同区
                    #   间 +$487. 打 volley 聚类 + 时段分区标签攒样, n≥30 再裁单轮
                    #   限流/最小间隔门. 零执行路径改动. 回滚: 配置键 false + 重启.
                    try:
                        _mod = self._load_script_module("entry_sync_shadow_tag")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] 入场同步影子打标异常 (已隔离): {_e}", exc_info=True)

                    # 盘后出场影子监控 (2026-08-27, 用户动议·record-only): 水下隔夜仓
                    #   盘后延续修复观测 (touch_entry/touch_be/touch_tp) + red_cut_1pct
                    #   唯一决策臂 (深红≥1%收盘割), 19:50+ 实时预评, 触发构造限价+
                    #   fill_outside_rth 载荷验证链路不发单, 实际平仓后配对结算 delta.
                    #   回放裁决 scripts/ah_exit_replay.py (n=12: 静态TP/SL臂 0/12 死单;
                    #   水下入夜盘后触及净盈利垫 0/11 — 修复引擎=次日RTH非盘后).
                    #   回滚: playbook.ah_exit_shadow_enabled=false + 重启.
                    try:
                        _mod_ah = self._load_script_module("ah_exit_shadow")
                        _mod_ah.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] AH出场影子异常 (已隔离): {_e}", exc_info=True)

                    # 陈旧锚影子打标 (2026-08-12 Plan B, record-only): DA锚价 vs 盘前价
                    #   对照暴露陈旧 (AVGO锚低4.3%/HON锚高3.7%); drift门/extreme_gap/出带
                    #   影子已覆盖深度场景, 真实缺口=趋势逃逸锚陈旧。用 trigger_miss 每轮
                    #   观测价重建指令当日价格路径, 打陈旧度标签+结局, n≥30 再裁硬门或
                    #   接 entry_reference。零执行路径改动。回滚: 配置键 false + 重启。
                    try:
                        _mod = self._load_script_module("stale_anchor_shadow_tag")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] 陈旧锚影子打标异常 (已隔离): {_e}", exc_info=True)

                    # 隔夜持仓×新指令冲突影子打标 (2026-08-16, record-only):
                    # W_WEEK 引入多夜持仓后, 隔夜股次日可能被 DA/UE 再选中。执行段已有
                    # 方向感知防御 (pipeline L3244: 同向拦截/反向放行), 本影子攒观测样本
                    # (冲突频率/翻转结局/候选浪费率), n≥30 再裁是否加候选端排除。
                    try:
                        _mod = self._load_script_module("conflict_shadow_tag")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] 持仓冲突影子打标异常 (已隔离): {_e}", exc_info=True)

                    # 簇浓度影子打标 (2026-08-27, 组合风控P2, record-only): 日开仓帽16/
                    #   并发帽12全是笔数维度无结构维度。手工簇映射+占位区间重放, 每笔开仓
                    #   记同簇并发数 (任意cap可离线重切) → data/cluster_concentration_shadow.jsonl.
                    #   首跑18日: 最大单簇并发仅5, cap敏感性 3/4/5/6 全部无正收益证据
                    #   (cap3拦10笔合计+$39=拦了反亏) — 攒n≥30日再裁. 回滚: 配置键 false + 重启.
                    try:
                        _mod = self._load_script_module("cluster_concentration_shadow")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] 簇浓度影子打标异常 (已隔离): {_e}", exc_info=True)

                    # 空头转化漏斗影子打标 (2026-08-16, record-only): SELL_SHORT指令
                    #   8/7后断崖, 真实断点=LLM分析层转化率低 (8/14轮35只→看空仅1)。
                    #   解析DA stderr日志评级分布+当日指令对照→data/short_funnel_shadow.jsonl。
                    #   n≥10交易日再裁分层gate。回滚: 配置键 false + 重启。
                    try:
                        _mod = self._load_script_module("short_funnel_shadow_tag")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] 空头漏斗影子打标异常 (已隔离): {_e}", exc_info=True)

                    # 缺口语义影子打标 (2026-08-17, DEB-20260817, record-only):
                    #   A3票型驱动冻结(gap_filler零区分度), 但gap-bucket级语义(市场级)
                    #   有实证: 小缺口等回踩/big缺口别等。打 wait/chase 判定 + A1开盘
                    #   5min中枢重锚反事实 → data/gap_semantics_shadow.jsonl, 按指令幂等。
                    #   n≥60 再裁 gap 语义门。回滚: 配置键 false + 重启。
                    try:
                        _mod = self._load_script_module("gap_semantics_shadow_tag")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] 缺口语义影子打标异常 (已隔离): {_e}", exc_info=True)

                    # band波动率化影子打标 (2026-08-17, DEB-20260817, record-only):
                    #   扁平band全池一套(entry_band.py自认不合理), 用SL同源atr分桶口径算
                    #   反事实band, 对照成交价质量/死锚/时滞 → data/band_vol_shadow.jsonl。
                    #   回填补读: 成交中位偏离0.000ATR, 病症暂不成立, 影子留作漂移监控。
                    #   回滚: 配置键 false + 重启。
                    try:
                        _mod = self._load_script_module("band_vol_shadow_tag")
                        _mod.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] band波动率化影子打标异常 (已隔离): {_e}", exc_info=True)

                    # 2026-10-18 空跑治理: 日K刷新已移出 AFTER_HOURS 主循环 —
                    #   原单轮扫描 400 个文件阻塞 658s, 占满盘后窗。现由独立 cron
                    #   (双季 04:05/05:05 CST, 脚本内 ET 16:00 守卫) 运行, 执行级日门
                    #   与失败重试语义不变。回滚 = 恢复本调用块 + 删 crontab 两行。

                elif phase == MarketPhase.OVERNIGHT:
                    # 盘后出场影子终评 (2026-08-27, record-only): AH 20:00 收后用完结
                    #   数据把 live_preliminary 行升级 final + 补漏评 + 结算.
                    #   回滚同 AFTER_HOURS 挂载 (playbook.ah_exit_shadow_enabled=false).
                    try:
                        _mod_ah = self._load_script_module("ah_exit_shadow")
                        _mod_ah.run_check(self)
                    except Exception as _e:
                        logger.warning(f"[LLM策略] AH出场影子(夜)异常 (已隔离): {_e}", exc_info=True)

                    # 深夜: 每6小时采集新闻 + LLM 推荐 (最低频率)
                    if last_afterhours_run is None or (now - last_afterhours_run).total_seconds() >= 21600:
                        logger.debug("[LLM策略] 深夜 → 低频新闻 + LLM 推荐 (关停时函数内静默)")
                        run_after_hours_llm_scan(self, phase=MarketPhase.OVERNIGHT)
                        last_afterhours_run = now
                        _persist_ah(now)

                elif phase == MarketPhase.WEEKEND:
                    # 周末: 完全休眠，不请求任何 API
                    logger.debug("[LLM策略] 周末休市 → 休眠")
                    # 重置计数器，周一重新计算
                    last_trading_run = None
                    last_afterhours_run = None
                    _persist_ah(None)
                    # 重置日级标志
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

        # D2(a): 循环退出 (停机) 时结算最后一轮护航 — 不留守护线程 (线程卫生)
        self._wd_close_round_heartbeat()

    def _check_position_snapshot(self):
        """持仓快照任务 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import check_position_snapshot
        check_position_snapshot(self)

    def _check_position_reconcile(self):
        """Task #49 (2026-05-28): 每日 09:30 ET 持仓对账任务 - 在 scheduler_triggers.py 中实现"""
        from .scheduler_triggers import check_position_reconcile
        check_position_reconcile(self)


    def _extract_symbols_from_news(self, item) -> set:
        """从新闻条目提取 watchlist 内的相关股票代码 (P1 2026-06-30)

        1. 优先使用 item.related_stocks
        2. 通过标题+内容关键词匹配常见公司名/股票代码
        3. 过滤到 self.watchlist
        """
        watchlist = set()
        for stock in self.watchlist:
            if isinstance(stock, str):
                watchlist.add(stock)
            elif isinstance(stock, dict):
                code = stock.get("code", "")
                if code:
                    watchlist.add(code)

        candidates = set()

        # 1. 结构化关联标的
        for raw in getattr(item, "related_stocks", None) or []:
            candidates.add(raw if raw.startswith("US.") else f"US.{raw}")

        # 2. 关键词匹配 (公司名/常见简称)
        text = f"{item.title} {item.content}".lower()
        company_ticker_map = {
            "apple": "US.AAPL", "aapl": "US.AAPL", "苹果": "US.AAPL",
            "microsoft": "US.MSFT", "msft": "US.MSFT", "微软": "US.MSFT",
            "google": "US.GOOGL", "alphabet": "US.GOOGL", "googl": "US.GOOGL",
            "amazon": "US.AMZN", "amzn": "US.AMZN", "亚马逊": "US.AMZN",
            "nvidia": "US.NVDA", "nvda": "US.NVDA", "英伟达": "US.NVDA",
            "tesla": "US.TSLA", "tsla": "US.TSLA", "特斯拉": "US.TSLA",
            "meta": "US.META", "facebook": "US.META",
            "amd": "US.AMD", "超威": "US.AMD",
            "baba": "US.BABA", "alibaba": "US.BABA", "阿里": "US.BABA",
            "pdd": "US.PDD", "pinduoduo": "US.PDD", "拼多多": "US.PDD",
            "jd": "US.JD", "京东": "US.JD",
            "nio": "US.NIO", "蔚来": "US.NIO",
            "xpev": "US.XPEV", "xpeng": "US.XPEV", "小鹏": "US.XPEV",
            "li auto": "US.LI", "理想": "US.LI",
            "futu": "US.FUTU", "富途": "US.FUTU",
            "tigr": "US.TIGR", "老虎": "US.TIGR",
            "netflix": "US.NFLX", "nflx": "US.NFLX",
            "qualcomm": "US.QCOM", "高通": "US.QCOM",
            "broadcom": "US.AVGO", "博通": "US.AVGO",
            "micron": "US.MU", "美光": "US.MU",
            "arm": "US.ARM",
        }
        for name, ticker in company_ticker_map.items():
            if name in text:
                candidates.add(ticker)

        # 3. 只保留在 watchlist 里的标的
        return candidates & watchlist

    def _on_news_v2_immediate(self, item):
        """突发新闻回调：触发相关标的的即时信号扫描 (P1 2026-06-30)

        链路：突发新闻 → 提取标的 → 风控/时段/冷却检查 → force_poll →
              现有信号扫描流水线 (因子/LLM/风控/下单)
        """
        try:
            from app.core.config_container import get_config as _get_cfg
            from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskState
            from app.core.scheduler_triggers import get_market_phase, MarketPhase

            cfg = _get_cfg().get_dotted("news_v2.immediate_scan", {}) or {}
            if not cfg.get("enabled", False):
                return

            # 1. 紧急程度过滤
            min_urgency = cfg.get("min_urgency", "IMMEDIATE")
            urgency_order = {"LOW": 0, "NORMAL": 1, "HIGH": 2, "IMMEDIATE": 3}
            item_urgency = getattr(item, "urgency", "NORMAL") or "NORMAL"
            if urgency_order.get(item_urgency, 0) < urgency_order.get(min_urgency, 3):
                return

            # 2. 标题黑名单过滤
            title_lower = item.title.lower()
            for bad in cfg.get("title_blacklist", []):
                if bad.lower() in title_lower:
                    logger.debug("[NewsV2-Immediate] 命中黑名单跳过: %s | %s", bad, item.title[:60])
                    return

            # 3. 时段检查
            phase = get_market_phase()
            if cfg.get("require_trading_hours", True) and phase != MarketPhase.TRADING:
                logger.debug("[NewsV2-Immediate] 非盘中时段，跳过: %s | phase=%s", item.title[:60], phase)
                return

            # 4. 全局风险状态检查 (RISK_OFF / HALT 禁止新开仓)
            risk_state = RiskStateMachine().get_state()
            if risk_state in (RiskState.RISK_OFF, RiskState.HALT):
                logger.warning("[NewsV2-Immediate] 风险状态 %s，禁止新开仓: %s", risk_state, item.title[:60])
                return

            # 5. 提取 watchlist 内相关标的
            symbols = self._extract_symbols_from_news(item)
            if not symbols:
                logger.debug("[NewsV2-Immediate] 无 watchlist 关联标的: %s", item.title[:60])
                return

            # 6. 冷却检查 + 触发扫描
            cooldown_sec = int(cfg.get("cooldown_seconds", 300))
            now_ts = time.time()
            cooldowns = getattr(self, "_news_immediate_cooldowns", {})
            for symbol in symbols:
                last_triggered = cooldowns.get(symbol, 0)
                if now_ts - last_triggered < cooldown_sec:
                    logger.debug("[NewsV2-Immediate] %s 冷却中，跳过", symbol)
                    continue

                cooldowns[symbol] = now_ts
                logger.info(
                    "[NewsV2-Immediate] ⚡ 突发新闻触发即时扫描 | %s | %s | urgency=%s",
                    symbol, item.title[:60], item_urgency
                )
                self.trigger_immediate_scan(symbol, reason=f"breaking_news:{item.source_name}")

            self._news_immediate_cooldowns = cooldowns

        except Exception as e:
            logger.warning("[NewsV2-Immediate] 处理异常: %s", e)

    def _news_v2_loop(self):
        """新闻管线 V2 后台采集循环 (observe 模式，零副作用)
        P0-5 修复 (2026-05-03): 持久化 event loop，不再每 180s 创建/销毁
        P0 (2026-06-26): 盘中 (09:30-16:00 ET) 跳过定时采集，突发新闻走主动推送。
        P1 (2026-06-30): 按市场阶段动态调整轮询频率，避免 OVERNIGHT 空跑 API。
        """
        import asyncio
        from app.core.scheduler_triggers import (
            get_market_phase, MarketPhase, sleep_if_market_closed, interruptible_sleep
        )

        self._news_v2_running = True
        skip_market_hours = getattr(self, "_news_v2_skip_market_hours", True)

        # P1: 阶段化采集频率，避免非交易时段空跑
        phase_intervals = {
            MarketPhase.TRADING: (180, "盘中跳过定时采集，突发新闻走主动推送" if skip_market_hours else "盘中正常采集"),
            MarketPhase.PRE_MARKET: (1800, "盘前降频采集"),
            MarketPhase.AFTER_HOURS: (1800, "盘后降频采集"),
            MarketPhase.OVERNIGHT: (900, "深夜休市，新闻管线休眠"),
            MarketPhase.WEEKEND: (300, "周末/假日，新闻管线休眠"),
        }

        logger.info("[NewsV2] 管线采集循环已启动 (phase-aware, skip_market_hours=%s)", skip_market_hours)

        # P0-5: 创建一次 event loop 并复用
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while self._news_v2_running and self.running:
                phase = get_market_phase()
                interval, phase_msg = phase_intervals.get(phase, (180, "默认采集"))

                try:
                    # 第二层防线: 服务跨午夜进入假日时，运行时守卫
                    if sleep_if_market_closed(300, lambda: self._news_v2_running and self.running):
                        continue

                    # P1: 深夜休市直接跳过 API 调用，避免空跑
                    if phase == MarketPhase.OVERNIGHT:
                        logger.debug("[NewsV2] %s，跳过本轮 (interval=%ds)", phase_msg, interval)
                        if interruptible_sleep(interval, lambda: self._news_v2_running and self.running):
                            return
                        continue

                    # P1: 周末/假日由 sleep_if_market_closed 守卫，理论上不会到达；保留兜底
                    if phase == MarketPhase.WEEKEND:
                        if interruptible_sleep(interval, lambda: self._news_v2_running and self.running):
                            return
                        continue

                    # P0 (2026-06-26): 盘中跳过定时轮询，仅盘前/盘后/周末采集
                    if skip_market_hours and phase == MarketPhase.TRADING:
                        logger.info("[NewsV2] %s，等待下一轮 (interval=%ds)", phase_msg, interval)
                        if interruptible_sleep(interval, lambda: self._news_v2_running and self.running):
                            return
                        continue

                    if self.news_pipeline_v2:
                        passed = loop.run_until_complete(self.news_pipeline_v2.run_once())
                        if passed:
                            logger.info("[NewsV2] 本轮通过 %d 条新闻", len(passed))
                except Exception as e:
                    logger.warning("[NewsV2] 采集异常: %s", e)

                # 等待 180 秒
                if interruptible_sleep(interval, lambda: self._news_v2_running and self.running):
                    return
        finally:
            loop.close()

        logger.info("[NewsV2] 管线采集循环已停止")

    def _vix_monitor_loop(self):
        """VIX 监控后台循环 — 每 5 分钟检查一次，级别变化时推送告警"""
        logger.info("[VIX Monitor] 监控循环已启动 (interval=300s)")

        while self.running:
            try:
                # 第二层防线: 服务跨午夜进入假日时，运行时守卫
                from app.core.scheduler_triggers import sleep_if_market_closed
                if sleep_if_market_closed(300, lambda: self.running):
                    continue

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

    def _playbook_exit_fast_loop(self):
        """Playbook 退出监控独立快循环 (2026-07-21).

        背景: 此前 monitor_playbook_sim_exits 捎带在 llm_strategy_loop 一串任务末尾,
        节奏 ~60-108s 且受前序任务慢/卡牵连, 无独立存活监控.
        本循环: 市场开放时每 PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC (默认20s) 调用一次;
        配合函数内批量取价 (Futu Push缓存优先, 近零成本), 端到端退出延迟 ~25s.
        单宿主: 启用时 llm_strategy_loop 旧调用点跳过. 回滚: 配置 enabled=false 恢复旧宿主.
        """
        logger.info(f"[PlaybookExit] 快循环已启动 (interval={self.PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC}s)")
        try:
            from app.observability.watchdog import get_watchdog
            get_watchdog().register("playbook_exit_loop", stale_threshold_sec=120)
        except Exception as _wd_e:
            logger.debug(f"[PlaybookExit] watchdog 注册失败 (降级运行): {_wd_e}")

        while self.running:
            try:
                try:
                    from app.observability.watchdog import get_watchdog
                    get_watchdog().ping("playbook_exit_loop")
                except Exception:
                    logger.warning("watchdog ping failed", exc_info=True)
                # [DEB-20260902 E0 · 2026-09-02 裁决实施] 外部心跳 touch — 与内部 watchdog ping 同点:
                #   循环活 = 心跳新; 冻结 (9/1 GIL 形态) → mtime 停滞 → 外部 cron 监督
                #   (scripts/freeze_sentinel.py, 420s = 内部 heal 360s + 60s 余量裁决值)
                #   py-spy 取证 + restart + 告警。休市期循环仍迭代 → 心跳仍新, 监督侧
                #   以 TRADING 相位自行闸门。回滚: 删本块 (+ 删 cron 行 + 删脚本)。
                try:
                    Path("/run/ftseek-heartbeat").touch()
                except Exception as _hb_e:
                    logger.debug(f"[PlaybookExit] heartbeat touch 失败 (哨兵侧可见): {_hb_e}")
                # 市场关闭时休眠 (照抄 VIX 样板: sleep_if_market_closed 返回 True=已睡到开市)
                from app.core.scheduler_triggers import sleep_if_market_closed
                if sleep_if_market_closed(self.PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC, lambda: self.running):
                    continue

                from app.playbook.pipeline import monitor_playbook_exits
                monitor_playbook_exits(self)

                # ── [RFC-141] 入场延迟确认门 sweep (D2' forward) ──
                #   挂这里而非 180s 主循环: 影子只裁过 2min/5min 两条腿
                #   (DELAYS_MIN=(2,5)), 主循环 180s ⇒ 挂主循环 = 静默落地一个
                #   3-5min 的未预注册变体。本循环 interval=10s ⇒ 确认时点 =
                #   arm+delay_sec 后首次 sweep, 误差 ≤10s。
                #   门关闭时 _PENDING 恒空 ⇒ 立即 return, 零开销零行为变更。
                try:
                    from app.playbook.entry_confirm_gate import (
                        sweep_entry_confirmations as _ec_sweep, pending_count as _ec_n)
                    if _ec_n() > 0:
                        from app.playbook.db import route_playbook_db as _ec_route
                        from app.utils.timezone import now_et as _ec_now
                        _ec_r = _ec_sweep(self, _ec_route("sim"), _ec_now())
                        if any(_ec_r.get(k) for k in ("confirmed", "abandoned", "expired")):
                            logger.info(f"[EntryConfirm] sweep: {_ec_r}")
                except Exception as _ec_sweep_err:
                    logger.warning(f"[EntryConfirm] sweep 异常 (已隔离): {_ec_sweep_err}", exc_info=True)

            except Exception as _e:
                logger.warning(f"[PlaybookExit] 快循环异常 (已隔离): {_e}")

            # 可中断 sleep (照抄 VIX 样板)
            for _ in range(max(1, int(self.PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC))):
                if not self.running:
                    return
                time.sleep(1)

        logger.info("[PlaybookExit] 快循环已停止")

    def _insider_scan_loop(self):
        """内部人集群买入每日扫描 — 每天盘前 1 次, SEC EDGAR Form 4 数据

        扫描 watchlist + ORB universe (~40 symbols), 发现集群买入信号时:
          1. 记入日志
          2. 通知推送 (如有 notifier)
          3. 信号在下次 signal_stage.process() 时被 insider 策略自然消费
        """
        logger.info("[InsiderScan] 每日扫描循环已启动 (interval=4h)")

        # 持久化 scanned_today 到文件，避免调度器重启后 _scanned_today 丢失导致重复扫描
        from pathlib import Path
        # DEB-20260903 #34: 哨兵路径用统一数据目录 (原 Path("data") 相对路径依赖 CWD,
        #   CWD 非仓库根时哨兵写错位置 → 重启后重复扫描)
        try:
            from app.core.paths import get_data_dir
            _sentinel_file = Path(str(get_data_dir())) / ".insider_scanned_today"
        except Exception:
            _sentinel_file = Path("data") / ".insider_scanned_today"

        while self.running:
            try:
                # 假日守卫
                from app.core.scheduler_triggers import sleep_if_market_closed
                if sleep_if_market_closed(3600, lambda: self.running):
                    continue

                # 每日只扫一次 (ET 06:00-09:30 盘前之间触发, 给盘前留时间)
                from app.utils.timezone import now_et
                _now_scan = now_et()
                today = _now_scan.strftime("%Y-%m-%d")
                hour = _now_scan.hour
                _minute = _now_scan.minute

                # 每天 ET 06:00-09:30 (盘前) 之间执行一次扫描
                # DEB-20260903 #34: 原 hour<6 or hour>=10 允许 09:30-09:59 (开盘后)仍在窗口内,
                #   与注释"盘前 06:00-09:30"不符. 收紧为 06:00 <= t < 09:30.
                _in_window = (hour >= 6) and (hour < 9 or (hour == 9 and _minute < 30))
                if not _in_window:
                    # 等待 1 小时后再次检查
                    for _ in range(3600):
                        if not self.running:
                            return
                        time.sleep(1)
                    continue

                # 文件持久化: 读取上次扫描日期
                _scanned_today = ""
                if _sentinel_file.exists():
                    try:
                        _scanned_today = _sentinel_file.read_text().strip()
                    except Exception:
                        logger.warning("insider sentinel read failed", exc_info=True)

                if today == _scanned_today:
                    # 等待 1 小时后再次检查
                    for _ in range(3600):
                        if not self.running:
                            return
                        time.sleep(1)
                    continue

                # 收集扫描列表: watchlist
                # 2026-09-24: 移除 orb.universe 死读 — ORB 已于 RFC-090 (90de5a1c7) 退役,
                # unified_config.yaml 的 orb: 节已整段删除, 此后 get_dotted 恒回退 [] ⇒
                # 该块为零效果隐形开关 (config reverse census 点名)。InsiderScan 人群
                # 自 orb 节删除起即 = watchlist, 本次清理零行为变更。
                symbols_to_scan = list(set(self.watchlist))

                logger.info(f"[InsiderScan] 开始扫描 {len(symbols_to_scan)} 只标的...")

                from app.data.sec_form4_provider import get_sec_form4_provider
                provider = get_sec_form4_provider()
                clusters = provider.scan_watchlist(symbols_to_scan)

                if clusters:
                    logger.info(f"[InsiderScan] 🟢 发现 {len(clusters)} 个集群买入信号!")
                    _scan_date = _now_scan.strftime("%Y-%m-%d")
                    for c in clusters:
                        # 事件年龄 (便于区分新事件 vs 陈旧集群; 原正文只有 days_span=集群跨度,
                        #   易被误读为"距今 Nd"). 2026-09-21 用户裁.
                        try:
                            _age_days = (
                                _now_scan.date()
                                - datetime.strptime(c.latest_buy_date, "%Y-%m-%d").date()
                            ).days
                        except Exception:
                            _age_days = None
                        _age_txt = (
                            f"最近买入 {c.latest_buy_date} ({_age_days}天前)"
                            if _age_days is not None else f"最近买入 {c.latest_buy_date}"
                        )
                        msg = (
                            f"🟢 内部人集群买入: {c.symbol}\n"
                            f"   {c.rationale}\n"
                            f"   Score: {c.score:.2f} | {_age_txt}"
                        )
                        logger.info(f"[InsiderScan] {msg}")
                        # 事件级去重 (跨日持久化): 同一集群只推一次, 防 30 天窗口内每天重推.
                        _evt_key = f"{c.symbol}:{c.first_buy_date}:{c.latest_buy_date}"
                        if _insider_push_seen(_evt_key):
                            logger.info(
                                f"[InsiderScan] {c.symbol} 同一集群事件已推送, 跳过 (键 {_evt_key})"
                            )
                            continue
                        if self.notifier:
                            try:
                                self.notifier.send_text(msg)
                                _insider_push_mark(_evt_key, _scan_date)
                            except Exception as _e:
                                logger.debug(f"[InsiderScan] 通知失败(下次续推): {_e}")
                        else:
                            _insider_push_mark(_evt_key, _scan_date)
                else:
                    logger.info(f"[InsiderScan] 扫描完成, 未发现集群买入信号")

                # 扫描成功后写入 sentinel (修复: 原来在扫描前写, 进程被kill则标记已扫描但实际未扫)
                try:
                    _sentinel_file.parent.mkdir(parents=True, exist_ok=True)
                    _sentinel_file.write_text(today)
                except Exception:
                    logger.warning("insider scan sentinel write failed", exc_info=True)

            except Exception as e:
                logger.warning(f"[InsiderScan] 扫描异常: {e}")

            # 等待 1 小时后再次检查是否到扫描时间
            for _ in range(3600):
                if not self.running:
                    return
                time.sleep(1)

        logger.info("[InsiderScan] 扫描循环已停止")


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
        _failed_items = []
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
                # DEB-20260904 #149: 失败条目保留待下轮重试 —
                # 原无条件切片丢弃前 10 条, 失败审计永不重试
                _failed_items.append(item)
        
        # 失败条目 + 未处理条目留待下轮
        self._deferred_audit_queue = _failed_items + self._deferred_audit_queue[10:]
        
        logger.info(
            "[LLM 审计] 延迟队列处理完成 | 处理=%d | 剩余=%d",
            processed, len(self._deferred_audit_queue)
        )
