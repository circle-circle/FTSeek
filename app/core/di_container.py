"""
依赖注入容器 — DI Container

统一管理所有组件的创建、装配和生命周期。
消除 main.py 中的手动装配，解决模块间紧耦合。

使用示例:
    container = DIContainer()
    scheduler = container.build_scheduler()
    scheduler.run_forever()
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class DIContainer:
    """
    依赖注入容器

    职责：
    1. 按需创建所有组件（懒加载）
    2. 管理组件间依赖关系
    3. 确保单例（同一组件只创建一次）
    4. 提供 build_scheduler() 装配完整调度器
    """

    _instance: Optional["DIContainer"] = None

    def __new__(cls) -> "DIContainer":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._instances: Dict[str, Any] = {}
        self._base_dir = Path(__file__).resolve().parent.parent.parent
        self._yaml_path = str(self._base_dir / "config" / "unified_config.yaml")
        self._initialized = True

    def _get_or_create(self, key: str, factory):
        """懒加载：已创建则返回，否则创建并缓存"""
        if key not in self._instances:
            self._instances[key] = factory()
        return self._instances[key]

    # ================================================================
    #  核心基础设施
    # ================================================================
    def config(self) -> "ConfigContainer":
        """统一配置访问器"""
        from app.core.config_container import get_config
        return self._get_or_create("config", get_config)

    def event_store(self) -> "EventStore":
        """事件存储"""
        from app.core.events import EventStore
        db_path = str(self._base_dir / "data" / "ftseek_us.db")
        return self._get_or_create("event_store", lambda: EventStore(db_path=db_path))

    # ================================================================
    #  数据层
    # ================================================================
    def data_provider(self) -> "MarketDataRouter":
        """行情数据路由器"""
        from app.data.market_data_router import MarketDataRouter

        return self._get_or_create("data_provider", lambda: MarketDataRouter(
            twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
            alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
            cache_ttl_seconds=900,
            futu_opend_host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            futu_opend_port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
            enable_futu_lv3=os.getenv("ENABLE_FUTU_LV3", "true").lower() in ("true", "1", "yes"),
        ))

    def news_provider(self) -> "USNewsProvider":
        """新闻情绪提供商"""
        from app.data.news_provider import USNewsProvider
        return self._get_or_create("news_provider", USNewsProvider)

    def atr_provider(self) -> "ATRProvider":
        """ATR 单一真相源 (Single Source of Truth)

        所有 ATR 消费方（TrendFirst / ATRStopEngine / ExitRuleDispatcher 等）
        统一从该 Provider 读取，避免多处独立计算导致的数据不一致。
        """
        from app.core.atr_provider import ATRProvider
        return self._get_or_create(
            "atr_provider",
            lambda: ATRProvider(
                market_data_router=self.data_provider(),
                cache_ttl_seconds=60,
            ),
        )

    # ================================================================
    #  策略层
    # ================================================================
    def strategy_registry(self) -> "StrategyRegistry":
        """策略注册中心"""
        from app.strategy.registry import get_strategy_registry
        return self._get_or_create("strategy_registry", get_strategy_registry)

    def performance_tracker(self) -> "PerformanceTracker":
        """策略性能追踪器"""
        from app.strategy.performance_tracker import get_performance_tracker
        return self._get_or_create("performance_tracker", get_performance_tracker)

    def auto_tuner(self) -> "AutoTuner":
        """策略自动调优器"""
        from app.strategy.auto_tuner import AutoTuner
        return self._get_or_create("auto_tuner", AutoTuner)

    # ================================================================
    #  投票 & 共识
    # ================================================================
    def voting_coordinator(self) -> "VotingCoordinator":
        """多模型投票协调器"""
        from app.agents.voting.voting_coordinator import VotingCoordinator
        from app.agents.voting.voting_config import VotingConfig
        return self._get_or_create("voting_coordinator", lambda: VotingCoordinator(
            config=VotingConfig.get_default_config(),
            timeout_seconds=25,
        ))

    def consensus_engine(self) -> "ConsensusEngine":
        """共识决策引擎"""
        from app.decision.consensus import ConsensusEngine
        cfg = self.config()
        try:
            decision_config = cfg.get_section("decision") or cfg.get_section("DECISION_CONFIG") or {}
        except Exception:
            decision_config = {}
        return self._get_or_create("consensus_engine", lambda: ConsensusEngine(
            config={
                "STRATEGY_WEIGHTS": decision_config.get("strategy_weights",
                    decision_config.get("STRATEGY_WEIGHTS", {})),
                "STRATEGY_ORTHOGONALIZATION": cfg.get_section("STRATEGY_ORTHOGONALIZATION"),
                "COST_MODEL": cfg.get_section("COST_MODEL"),
            },
            event_store=self.event_store(),
        ))

    # ================================================================
    #  执行层
    # ================================================================
    def execution_engine(self) -> "ExecutionEngine":
        """交易执行引擎"""
        from app.execution.engine import ExecutionEngine
        cfg = self.config()
        try:
            exec_section = cfg.get_section("execution") or {}
        except Exception:
            exec_section = {}
        # P0 修复 (2026-05-11): 强制模拟盘确认，禁止自动切换实盘
        _trade_env = int(os.getenv("TRADE_ENV", "1"))
        _futu_env = int(os.getenv("FUTU_TRADE_ENV", "1"))
        if _trade_env == 0 or _futu_env == 0:
            import logging
            _logger = logging.getLogger("ftseek.di_container")
            _logger.critical("[安全拦截] 检测到实盘配置 (TRADE_ENV=%s, FUTU_TRADE_ENV=%s)，强制切换为模拟盘", _trade_env, _futu_env)
            _trade_env = 1
            _futu_env = 1

        exec_config = {
            "market_order_min_score": exec_section.get("order_type", {}).get("market_min_score", 80),
            "limit_order_max_score": exec_section.get("order_type", {}).get("limit_max_score", 60),
            "twap_order_min_value": exec_section.get("order_type", {}).get("twap_min_value", None),
            "max_retry_attempts": exec_section.get("retry", {}).get("max_attempts", 3),
            "retry_delay_seconds": exec_section.get("retry", {}).get("delay_seconds", 5),
            "max_slippage_ratio": exec_section.get("slippage", {}).get("max_ratio", 0.005),
            "idempotency_window": exec_section.get("idempotency", {}).get("window_seconds", 3600),
            "futu_host": os.getenv("OPEND_HOST", "127.0.0.1"),
            "futu_port": int(os.getenv("OPEND_PORT", "11111")),
            "futu_trade_env": _futu_env,
            "futu_account_id": os.getenv("SIMULATE_ACCOUNT_ID", ""),
            "auto_connect_broker": True,
            "brokers": [{"name": "futu", "priority": 1, "available": True}],
        }
        engine = ExecutionEngine(config=exec_config, event_store=self.event_store())
        engine.initialize()
        return self._get_or_create("execution_engine", lambda: engine)

    # ================================================================
    #  风控层
    # ================================================================
    def risk_engine(self) -> "RiskEngine":
        """风险引擎"""
        from app.risk.engine import RiskEngine
        cfg = self.config()
        try:
            risk_cfg = cfg.get_section("risk") or {}
        except Exception:
            risk_cfg = {}
        engine = RiskEngine(config={"risk": risk_cfg}, event_store=self.event_store())
        engine.initialize()
        # P0-fix 2026-05-04: 注入教训注入器，激活 LESSON 动态约束
        try:
            engine.set_lesson_injector(self.lesson_injector())
        except Exception:
            pass
        return self._get_or_create("risk_engine", lambda: engine)

    # ================================================================
    #  教训注入层
    # ================================================================
    def lesson_injector(self):
        """教训注入器"""
        from app.review.lesson_injector import LessonInjector
        return self._get_or_create("lesson_injector", lambda: LessonInjector())

    # ================================================================
    #  通知层
    # ================================================================
    def notifier(self) -> "DingTalkNotifier":
        """钉钉通知器"""
        from app.notifier.dingtalk import DingTalkNotifier
        cfg = self.config()
        try:
            svc = cfg.get_section("service") or {}
            webhook = svc.get("dingtalk", {}).get("webhook_url", "")
            secret = svc.get("dingtalk", {}).get("secret", "")
        except Exception:
            webhook = os.getenv("DINGTALK_WEBHOOK_URL_US", "")
            secret = os.getenv("DINGTALK_SECRET_US", "")
        return self._get_or_create("notifier", lambda: DingTalkNotifier(
            webhook_url=webhook,
            secret=secret,
        ))

    # ================================================================
    #  调度器装配
    # ================================================================
    def build_scheduler(self):
        """
        装配完整的 TradingScheduler

        所有依赖通过 DI 容器注入，scheduler 不再需要手动创建组件。
        """
        from app.core.scheduler_core import TradingScheduler

        scheduler = TradingScheduler(
            event_store=self.event_store(),
            execution_engine=self.execution_engine(),
            risk_engine=self.risk_engine(),
            notifier=self.notifier(),
        )

        # ── 注入增强组件 ──
        # 策略注册中心
        scheduler.strategy_registry = self.strategy_registry()
        # 性能追踪器
        scheduler.performance_tracker = self.performance_tracker()
        # 自动调优器
        scheduler.auto_tuner = self.auto_tuner()
        # DI 容器引用（供后续扩展）
        scheduler.di_container = self

        logger.info("[DIContainer] TradingScheduler 装配完成")
        return scheduler

    # ================================================================
    #  健康检查
    # ================================================================
    def health_check(self) -> Dict[str, Any]:
        """检查所有核心组件状态"""
        components = {}
        for key in ["event_store", "data_provider", "atr_provider", "execution_engine", "risk_engine"]:
            if key in self._instances:
                components[key] = "OK"
            else:
                components[key] = "NOT_CREATED"
        return {
            "status": "healthy" if all(v == "OK" for v in components.values()) else "partial",
            "components": components,
        }

    @classmethod
    def reset(cls):
        """重置容器（用于测试）"""
        cls._instance = None


# ================================================================
#  便捷函数
# ================================================================
def get_di_container() -> DIContainer:
    """获取全局 DI 容器"""
    return DIContainer()
