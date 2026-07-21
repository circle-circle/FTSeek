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
        from app.core.db_utils import get_main_db
        db_path = str(get_main_db())
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

    def atr_provider(self) -> "ATRProvider":
        """ATR 单一真相源 (Single Source of Truth)"""
        from app.core.atr_provider import ATRProvider
        return self._get_or_create(
            "atr_provider",
            lambda: ATRProvider(
                market_data_router=self.data_provider(),
                cache_ttl_seconds=60,
            ),
        )

    def database_pool(self) -> "DatabasePool":
        """统一 SQLite 连接池 (SSOT).

        新代码应通过 DI 容器获取 DatabasePool，避免模块级全局变量。
        """
        from app.storage.db_pool import get_pool
        return self._get_or_create("database_pool", get_pool)

    def shared_kernel(self) -> "SharedKernel":
        """共享内核 (VIX/市场状态/风控参数)."""
        from app.core.shared_kernel import get_shared_kernel
        return self._get_or_create("shared_kernel", get_shared_kernel)

    # ================================================================
    #  策略层
    # ================================================================
    def strategy_registry(self) -> "StrategyRegistry":
        """策略注册中心"""
        from app.strategy.registry import get_strategy_registry
        return self._get_or_create("strategy_registry", get_strategy_registry)

    # [CLEANUP 2026-07-14] voting_coordinator() 工厂已删除 — 零调用者.
    #   scheduler 恒用 Null/Bridge/TechnicalFallbackAdapter; 真·3模型VotingCoordinator
    #   已随死投票脑 (voting_coordinator.py/voting_aggregator.py/model_agent.py) 一同清理.

    # [CLEANUP 2026-06-13] consensus_engine removed — zero calls, self-documented as disconnected

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
        # 2026-06-09 更新: 允许 ALLOW_REAL_TRADING=true 时通过实盘
        # 2026-06-24 重构 (三态共存 Phase 0): 主 pipeline broker 路由改由 AUTO_TRADE_ACCOUNT
        #   控制 (sim|real), 不再读 FUTU_TRADE_ENV (该变量已 deprecated). TRADE_ENV 仍控制
        #   DB 路由 (db_utils.is_live_mode) 不变, 与 broker 解耦.
        #   - 手动交易 (manual_order_service) 固定 REAL, 不经此路径
        #   - 自动交易 (主 pipeline) 默认 SIM, real 需 ALLOW_REAL_TRADING + (Phase2) graduation
        _auto_account = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        _allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
        if _auto_account == "real" and not _allow_real:
            import logging
            _logger = logging.getLogger("ftseek.di_container")
            _logger.critical(
                "[安全拦截] AUTO_TRADE_ACCOUNT=real 但 ALLOW_REAL_TRADING!=true, 强制降级为模拟盘"
            )
            _auto_account = "sim"
        _futu_env = 0 if _auto_account == "real" else 1
        if _auto_account == "real":
            import logging
            logging.getLogger("ftseek.di_container").warning(
                "[实盘模式] AUTO_TRADE_ACCOUNT=real, ALLOW_REAL_TRADING=true — 主 pipeline 自动交易走实盘"
            )
            # 修复6a (2026-07-20, DEB-20260720): LIVE 启动断言 — 真账户禁止 fail-open 裸奔.
            #   risk.acb_fail_mode / preflight_fail_mode 若非 closed, 风控异常会放行开仓;
            #   在真金账户上这是灾难性的. SIM 模式不进本分支, 零行为影响. 断言失败拒绝启动.
            import logging as _logging_6a
            _log6a = _logging_6a.getLogger("ftseek.di_container")
            try:
                from app.core.config_container import ConfigContainer as _CC6a
                _cc6a = _CC6a.get_instance()
                _acb_fm = str(_cc6a.get_dotted("risk.acb_fail_mode", "open")).strip().lower()
                _pf_fm = str(_cc6a.get_dotted("risk.preflight_fail_mode", "open")).strip().lower()
            except Exception as _e6a:
                _log6a.critical(f"[LIVE启动断言] 读取 fail_mode 配置失败, 拒绝以实盘启动: {_e6a}")
                raise RuntimeError("LIVE 启动断言: 无法读取 risk.*_fail_mode 配置") from _e6a
            _bad = [n for n, v in (("acb_fail_mode", _acb_fm), ("preflight_fail_mode", _pf_fm)) if v != "closed"]
            if _bad:
                _log6a.critical(
                    f"[LIVE启动断言] 实盘模式要求 {_bad} 必须为 'closed' (当前 "
                    f"acb={_acb_fm}, preflight={_pf_fm}); fail-open 在真账户会放行风控异常开仓 → 拒绝启动"
                )
                raise RuntimeError(
                    f"LIVE 启动断言失败: {_bad} 必须为 closed (实盘禁止 fail-open)"
                )

        exec_config = {
            "market_order_min_score": exec_section.get("order_type", {}).get("market_min_score", 80),
            "limit_order_max_score": exec_section.get("order_type", {}).get("limit_max_score", 60),
            "twap_order_min_value": exec_section.get("order_type", {}).get("twap_min_value", None),
            "max_retry_attempts": exec_section.get("retry", {}).get("max_attempts", 3),
            "retry_delay_seconds": exec_section.get("retry", {}).get("delay_seconds", 5),
            "max_slippage_ratio": exec_section.get("slippage", {}).get("max_ratio", 0.005),
            "idempotency_window": exec_section.get("idempotency", {}).get("window_seconds", 3600),
            "futu_host": os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            "futu_port": int(os.getenv("FUTU_OPEND_PORT", "11111")),
            "futu_trade_env": _futu_env,
            "futu_account_id": os.getenv("REAL_ACCOUNT_ID" if _futu_env == 0 else "SIMULATE_ACCOUNT_ID", ""),
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
        # [CLEANUP 2026-06-13] performance_tracker + auto_tuner injections removed
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
