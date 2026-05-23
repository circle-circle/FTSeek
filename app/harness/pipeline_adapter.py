"""
管线适配器 (PipelineAdapter) — 旧系统 scheduler 与新框架的桥接层

职责:
1. 在旧系统 scan_loop 的 _process_symbol_impl 中注入 StrategyRouter
2. 共享状态: DB连接, 持仓数据, 风控状态
3. 统一信号格式: SignalScore ↔ RouteResult 互转
4. Shadow 双写: 新旧链路结果都写入 shadow_trades 表

设计:
- 旧系统 scheduler 持有 PipelineAdapter 实例
- scan_loop 每个 symbol 处理时调用 adapter.intercept()
- adapter 根据 PipelineConfig 决定走哪条链路
- 结果统一写入 DB, 通知统一走 notifier

注入点 (不修改旧代码):
  scheduler_core.py → TradingScheduler.__init__():
    self.pipeline_adapter = PipelineAdapter(self.config)
  
  signal_pipeline.py → _process_symbol_impl():
    result = scheduler.pipeline_adapter.intercept(symbol, market_data, signal_score)
    if result and not result.allowed:
        return  # 被新框架拦截

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class InterceptResult:
    """拦截结果 — adapter 对外暴露的统一格式"""
    intercepted: bool             # 是否被新框架处理
    allowed: bool                 # 是否允许执行
    source: str                   # "legacy" | "factor" | "hybrid"
    direction: Optional[str]      # "LONG" | "SHORT" | None
    shares: Optional[int]         # 建议股数
    reason: str                   # 决策原因
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class PipelineAdapter:
    """
    管线适配器 — 连接旧系统与新框架

    使用方式:
        # 在 scheduler_core.py 的 __init__ 中:
        from app.harness.pipeline_adapter import PipelineAdapter
        self.pipeline_adapter = PipelineAdapter(self)

        # 在 signal_pipeline._process_symbol_impl 中:
        result = scheduler.pipeline_adapter.intercept(symbol, market_data)
        if result.intercepted and not result.allowed:
            logger.info(f"[{symbol}] 被新框架拦截: {result.reason}")
            return

        # 在 signal 生成后 (如果有):
        result = scheduler.pipeline_adapter.post_signal(symbol, signal_score, market_data)
        if not result.allowed:
            return  # 被新框架风控拦截
    """

    def __init__(self, scheduler=None, force_legacy: bool = False):
        self._scheduler = scheduler
        self._router = None
        self._shadow_bridge = None
        self._enabled = True

        # 无 scheduler 或 force_legacy → 默认 legacy (不注入新框架)
        if force_legacy or scheduler is None:
            logger.info("PipelineAdapter: legacy 模式 (无scheduler/强制legacy)")
            return

        # 从 scheduler 获取配置
        config = self._load_config()
        if config.master_switch.value != "legacy":
            self._init_router(config)
            logger.info(f"PipelineAdapter 启用: {config.master_switch.value}")
        else:
            logger.info("PipelineAdapter: legacy 模式, 不注入新框架")

    def _load_config(self):
        """从 unified_config 加载管线配置"""
        from app.harness.strategy_router import PipelineConfig
        try:
            import yaml
            from pathlib import Path
            config_path = Path(__file__).resolve().parent.parent.parent / "config" / "unified_config.yaml"
            if config_path.exists():
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                pipeline_cfg = cfg.get("pipeline", {})
                return PipelineConfig.from_dict(pipeline_cfg)
        except Exception as e:
            logger.warning(f"加载管线配置失败: {e}, 使用默认 legacy")
        return PipelineConfig()

    def _init_router(self, config):
        """初始化路由器"""
        from app.harness.strategy_router import StrategyRouter
        self._router = StrategyRouter(config, self._scheduler)

    @property
    def is_active(self) -> bool:
        return self._router is not None and self._enabled

    def intercept(
        self,
        symbol: str,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> InterceptResult:
        """
        信号生成前拦截 — 市场上下文门禁

        在 _process_symbol_impl 的最早阶段调用,
        如果市场上下文不允许交易, 直接跳过整个信号生成流程。

        Returns:
            InterceptResult: intercepted=True 表示新框架做了决策
        """
        if not self.is_active:
            return InterceptResult(
                intercepted=False, allowed=True,
                source="legacy", direction=None, shares=None,
                reason="PipelineAdapter 未激活",
            )

        try:
            ctx = self._router._get_market_context().get_context()
            allowed, reason = self._router._get_market_context().check_entry_allowed(symbol)

            if not allowed:
                return InterceptResult(
                    intercepted=True, allowed=False,
                    source="market_context", direction=None, shares=None,
                    reason=reason,
                    metadata={"context": ctx.to_dict()},
                )

            return InterceptResult(
                intercepted=True, allowed=True,
                source="market_context", direction=None, shares=None,
                reason="市场上下文通过",
                metadata={"context": ctx.to_dict()},
            )

        except Exception as e:
            logger.error(f"[PipelineAdapter] intercept 异常: {e}")
            return InterceptResult(
                intercepted=False, allowed=True,
                source="legacy_fallback", direction=None, shares=None,
                reason=f"异常降级: {e}",
            )

    def post_signal(
        self,
        symbol: str,
        signal_score: Any,
        market_data: Dict[str, Any],
    ) -> InterceptResult:
        """
        信号生成后处理 — 风控 + 仓位计算

        在旧链路产生 SignalScore 之后调用,
        用新框架的风控和仓位逻辑覆盖旧系统的默认值。

        Args:
            symbol: 股票代码
            signal_score: 旧系统的 SignalScore 对象
            market_data: 行情数据

        Returns:
            InterceptResult with shares/position info
        """
        if not self.is_active:
            return InterceptResult(
                intercepted=False, allowed=True,
                source="legacy", direction=None, shares=None,
                reason="PipelineAdapter 未激活",
            )

        try:
            # 1. 信号路由 (转换旧格式 → RouteResult)
            signal = self._router.route_signal(
                symbol, market_data, signal_score=signal_score
            )
            if not signal.allowed:
                return InterceptResult(
                    intercepted=True, allowed=False,
                    source=signal.source, direction=signal.direction,
                    shares=None, reason=signal.reason,
                    metadata=signal.metadata,
                )

            # 2. 风控路由
            risk_result = self._router.route_risk(symbol, signal)
            if not risk_result.allowed:
                return InterceptResult(
                    intercepted=True, allowed=False,
                    source=risk_result.source, direction=risk_result.direction,
                    shares=None, reason=risk_result.reason,
                    metadata=risk_result.metadata,
                )

            # 3. 仓位路由
            sizing_result = self._router.route_sizing(
                symbol, risk_result, market_data
            )

            return InterceptResult(
                intercepted=True, allowed=True,
                source=sizing_result.source,
                direction=sizing_result.direction,
                shares=sizing_result.metadata.get("shares"),
                reason=sizing_result.reason,
                metadata=sizing_result.metadata,
            )

        except Exception as e:
            logger.error(f"[PipelineAdapter] post_signal 异常: {e}")
            return InterceptResult(
                intercepted=False, allowed=True,
                source="legacy_fallback", direction=None, shares=None,
                reason=f"异常降级: {e}",
            )

    def post_trade(
        self,
        symbol: str,
        trade_record: Dict[str, Any],
    ):
        """
        交易完成后处理 — Shadow 双写

        在旧链路完成下单后调用,
        将交易记录同时写入新框架的 shadow_trades 表。
        """
        if not self.is_active:
            return

        try:
            from app.harness.shadow_bridge import ShadowBridge
            bridge = self._get_shadow_bridge()
            if bridge:
                bridge.record_from_legacy(trade_record)
        except Exception as e:
            logger.debug(f"[PipelineAdapter] shadow 双写失败: {e}")

    def _get_shadow_bridge(self):
        if self._shadow_bridge is None:
            try:
                from app.harness.shadow_bridge import ShadowBridge
                self._shadow_bridge = ShadowBridge()
            except Exception:
                pass
        return self._shadow_bridge

    def get_status(self) -> Dict[str, Any]:
        """获取适配器状态"""
        status = {
            "active": self.is_active,
            "enabled": self._enabled,
        }
        if self._router:
            status["router"] = self._router.get_status()
        return status

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False
