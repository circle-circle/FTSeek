"""
策略路由器 (StrategyRouter) — 新旧链路无缝切换的核心枢纽

辩论收敛设计:
- 双层切换: 全局 master_switch (legacy/factor/hybrid) + 模块级 override
- 每个环节 (信号/风控/退出/仓位) 可独立选择链路
- 旧链路 = 生产代码直接调用 (零修改)
- 新链路 = 因子框架 + 统计门禁 + 动态风控
- Hybrid = 混合 (如: 新信号 + 旧退出)

配置 (unified_config.yaml):
  pipeline:
    master_switch: legacy     # legacy | factor | hybrid | shadow_only
    overrides:
      signal: null            # null = follow master, or: legacy/factor
      risk: null
      exit: null
      sizing: null

数据流:
  StrategyRouter
    ├─ route_signal()    → SignalScore (旧: LLM投票, 新: FactorAssembly)
    ├─ route_risk()      → RiskDecision (旧: RiskEngine, 新: MarketContext+DynamicRisk)
    ├─ route_exit()      → ExitDecision (旧: position_exit_monitor, 新: DynamicRiskExit)
    └─ route_sizing()    → PositionSize (旧: fixed%, 新: ATR+Edge)

版本: 1.0 | 2026-05-23
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================
#  Enums & Config
# ============================================================
class PipelineMode(str, Enum):
    LEGACY = "legacy"          # 完全使用旧链路
    FACTOR = "factor"          # 完全使用新因子框架
    HYBRID = "hybrid"          # 混合模式 (per-module override)
    SHADOW_ONLY = "shadow_only"  # 仅shadow对比, 不真实下单


@dataclass
class PipelineConfig:
    """管线配置 — 从 unified_config.yaml 加载"""
    master_switch: PipelineMode = PipelineMode.LEGACY
    signal_override: Optional[str] = None   # legacy | factor | None
    risk_override: Optional[str] = None
    exit_override: Optional[str] = None
    sizing_override: Optional[str] = None

    def resolve(self, module: str) -> str:
        """解析某模块实际使用的链路"""
        override = getattr(self, f"{module}_override", None)
        if override:
            return override
        if self.master_switch == PipelineMode.HYBRID:
            # hybrid 默认: signal=factor, risk=factor, exit=legacy, sizing=factor
            defaults = {
                "signal": "factor",
                "risk": "factor",
                "exit": "legacy",
                "sizing": "factor",
            }
            return defaults.get(module, "legacy")
        return self.master_switch.value

    @classmethod
    def from_dict(cls, d: Dict) -> PipelineConfig:
        master = d.get("master_switch", "legacy")
        overrides = d.get("overrides", {})
        return cls(
            master_switch=PipelineMode(master),
            signal_override=overrides.get("signal"),
            risk_override=overrides.get("risk"),
            exit_override=overrides.get("exit"),
            sizing_override=overrides.get("sizing"),
        )


# ============================================================
#  Route Result (统一返回格式)
# ============================================================
@dataclass
class RouteResult:
    """路由结果 — 统一新旧链路的输出格式"""
    allowed: bool = False           # 是否允许执行
    direction: Optional[str] = None  # "LONG" | "SHORT" | None
    source: str = "legacy"          # "legacy" | "factor"
    strength: float = 0.0          # 信号强度 0-100
    confidence: float = 0.0        # 置信度 0-1
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


# ============================================================
#  StrategyRouter — 核心路由器
# ============================================================
class StrategyRouter:
    """
    策略路由器 — 新旧链路的唯一入口

    设计原则:
    1. 零修改旧代码 — 旧链路通过 adapter 函数调用
    2. 统一接口 — 返回 RouteResult 无论走哪条链路
    3. 审计日志 — 每次路由决策记录 source + reason
    4. 安全降级 — 新链路异常时自动 fallback 到旧链路
    """

    def __init__(self, config: PipelineConfig, scheduler=None):
        self.config = config
        self._scheduler = scheduler
        self._audit_log: List[Dict] = []

        # 延迟初始化的组件
        self._market_context = None
        self._factor_assembly = None
        self._dynamic_risk = None

        logger.info(f"StrategyRouter 初始化: master={config.master_switch.value}")

    # ──────────────────────────────────────
    #  延迟加载组件
    # ──────────────────────────────────────
    def _get_market_context(self):
        if self._market_context is None:
            from app.harness.market_context import MarketContextProvider
            self._market_context = MarketContextProvider()
        return self._market_context

    def _get_factor_assembly(self):
        if self._factor_assembly is None:
            from app.factors.assembly import FactorAssembly
            self._factor_assembly = FactorAssembly()
        return self._factor_assembly

    def _get_dynamic_risk(self):
        if self._dynamic_risk is None:
            from app.exit.dynamic_risk import DynamicRiskExit
            self._dynamic_risk = DynamicRiskExit()
        return self._dynamic_risk

    # ──────────────────────────────────────
    #  信号路由
    # ──────────────────────────────────────
    def route_signal(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        **kwargs
    ) -> RouteResult:
        """
        信号生成路由

        Args:
            symbol: 股票代码
            market_data: 行情数据 (OHLCV + 技术指标)

        Returns:
            RouteResult with direction/strength/confidence
        """
        source = self.config.resolve("signal")
        try:
            if source == "factor":
                result = self._signal_factor(symbol, market_data, **kwargs)
            else:
                result = self._signal_legacy(symbol, market_data, **kwargs)

            result.source = source
            self._log_route("signal", symbol, result)
            return result

        except Exception as e:
            logger.error(f"[Router] signal/{source} 异常: {e}, fallback to legacy")
            result = self._signal_legacy(symbol, market_data, **kwargs)
            result.source = "legacy_fallback"
            result.metadata["fallback_reason"] = str(e)
            self._log_route("signal", symbol, result)
            return result

    def _signal_factor(
        self, symbol: str, market_data: Dict, **kwargs
    ) -> RouteResult:
        """新链路: 因子评估"""
        assembly = self._get_factor_assembly()

        # 市场上下文门禁 (VIX + 资本水位)
        ctx = self._get_market_context().get_context()
        if not ctx.can_trade:
            return RouteResult(
                allowed=False,
                direction=None,
                reason=f"MarketContext 拒绝: {ctx.risk_state.value}",
                metadata={"context": ctx.to_dict()},
            )

        # 因子评估
        try:
            eval_result = assembly.evaluate(symbol, market_data)
        except Exception as e:
            return RouteResult(
                allowed=False,
                direction=None,
                reason=f"因子评估异常: {e}",
            )

        if eval_result is None or not eval_result.get("triggered", False):
            return RouteResult(
                allowed=False,
                direction=None,
                reason="因子条件未满足",
                metadata={"factors": eval_result},
            )

        return RouteResult(
            allowed=True,
            direction=eval_result.get("direction", "LONG"),
            strength=eval_result.get("strength", 50),
            confidence=eval_result.get("confidence", 0.6),
            reason=f"因子触发: {eval_result.get('variant', 'unknown')}",
            metadata={
                "factors": eval_result,
                "context": ctx.to_dict(),
                "position_scale": ctx.position_scale,
            },
        )

    def _signal_legacy(
        self, symbol: str, market_data: Dict, **kwargs
    ) -> RouteResult:
        """旧链路: signal_pipeline 的信号生成 (adapter)"""
        # 旧链路通过 scheduler 调用, 这里仅做格式转换
        # 实际旧链路在 scan_loop → _process_symbol_impl 中运行
        # StrategyRouter 不直接调用旧链路, 而是被旧链路调用

        # 如果从旧链路调用, 传入的 kwargs 中会有 SignalScore
        signal_score = kwargs.get("signal_score")
        if signal_score:
            return RouteResult(
                allowed=True,
                direction=str(signal_score.direction) if hasattr(signal_score, 'direction') else "LONG",
                strength=getattr(signal_score, 'strength', 50),
                confidence=getattr(signal_score, 'confidence', 0.5),
                reason="旧链路信号传递",
                metadata={"signal_score": str(signal_score)},
            )

        # 无信号 (被独立调用时)
        return RouteResult(
            allowed=False,
            direction=None,
            reason="旧链路未产生信号 (需通过 scan_loop 调用)",
        )

    # ──────────────────────────────────────
    #  风控路由
    # ──────────────────────────────────────
    def route_risk(
        self,
        symbol: str,
        signal: RouteResult,
        **kwargs
    ) -> RouteResult:
        """
        风控检查路由

        Args:
            symbol: 股票代码
            signal: 上游信号路由结果

        Returns:
            RouteResult with allowed/reason updated
        """
        source = self.config.resolve("risk")

        # 市场上下文是通用门禁 (无论哪条链路都检查)
        ctx = self._get_market_context().get_context()
        gate_allowed, gate_reason = self._get_market_context().check_entry_allowed(symbol)

        if not gate_allowed:
            signal.allowed = False
            signal.reason = f"MarketContext 门禁: {gate_reason}"
            signal.metadata["market_gate"] = gate_reason
            return signal

        try:
            if source == "factor":
                result = self._risk_factor(symbol, signal, ctx, **kwargs)
            else:
                result = self._risk_legacy(symbol, signal, **kwargs)

            result.source = f"risk:{source}"
            self._log_route("risk", symbol, result)
            return result

        except Exception as e:
            logger.error(f"[Router] risk/{source} 异常: {e}, fallback to legacy")
            return self._risk_legacy(symbol, signal, **kwargs)

    def _risk_factor(
        self, symbol: str, signal: RouteResult, ctx, **kwargs
    ) -> RouteResult:
        """新链路: 动态风控"""
        risk = self._get_dynamic_risk()
        allowed, reason = risk.check_entry_allowed(symbol)

        if not allowed:
            signal.allowed = False
            signal.reason = f"动态风控拒绝: {reason}"
            return signal

        # 应用资本水位缩放
        if ctx.position_scale < 1.0:
            signal.metadata["position_scale"] = ctx.position_scale
            signal.metadata["capital_level"] = ctx.capital_level.value

        return signal

    def _risk_legacy(
        self, symbol: str, signal: RouteResult, **kwargs
    ) -> RouteResult:
        """旧链路: RiskEngine (adapter)"""
        # 旧链路风控在 order_executor._check_gates() 中执行
        # 这里直接透传, 由旧链路自行检查
        return signal

    # ──────────────────────────────────────
    #  仓位路由
    # ──────────────────────────────────────
    def route_sizing(
        self,
        symbol: str,
        signal: RouteResult,
        market_data: Dict[str, Any],
        **kwargs
    ) -> RouteResult:
        """
        仓位计算路由

        Returns:
            RouteResult with metadata["shares"] and metadata["position_value"]
        """
        source = self.config.resolve("sizing")

        try:
            if source == "factor":
                return self._sizing_factor(symbol, signal, market_data, **kwargs)
            else:
                return self._sizing_legacy(symbol, signal, **kwargs)
        except Exception as e:
            logger.error(f"[Router] sizing/{source} 异常: {e}")
            return self._sizing_legacy(symbol, signal, **kwargs)

    def _sizing_factor(
        self, symbol: str, signal: RouteResult, market_data: Dict, **kwargs
    ) -> RouteResult:
        """新链路: ATR 仓位控制 + Edge 过滤"""
        from app.exit.position_sizing import PositionSizing
        from app.exit.cost_model import CostModel

        ctx = self._get_market_context().get_context()
        capital = ctx.current_equity if ctx.current_equity > 0 else kwargs.get("capital", 100000)

        sizer = PositionSizing(capital=capital)
        cost_model = CostModel()

        atr = market_data.get("atr", 0)
        price = market_data.get("price", 0)

        if atr <= 0 or price <= 0:
            signal.allowed = False
            signal.reason = "ATR/price 数据缺失, 无法计算仓位"
            return signal

        sizing = sizer.calculate(
            price=price,
            atr=atr,
            sl_multiplier=kwargs.get("sl_multiplier", 1.0),
        )

        # Edge/Cost 过滤
        edge = signal.metadata.get("edge", signal.strength / 100 * 0.02)
        cost = cost_model.estimate(price, sizing["shares"], signal.direction or "LONG")
        edge_cost_ratio = edge / cost["total_cost_pct"] if cost["total_cost_pct"] > 0 else 0

        signal.metadata.update({
            "shares": sizing["shares"],
            "position_value": sizing["position_value"],
            "risk_dollars": sizing["risk_dollars"],
            "cost_estimate": cost,
            "edge_cost_ratio": edge_cost_ratio,
        })

        # 应用资本水位缩放
        if ctx.position_scale < 1.0:
            original_shares = sizing["shares"]
            sizing["shares"] = max(1, int(sizing["shares"] * ctx.position_scale))
            signal.metadata["shares_scaled"] = sizing["shares"]
            signal.metadata["shares_original"] = original_shares

        return signal

    def _sizing_legacy(
        self, symbol: str, signal: RouteResult, **kwargs
    ) -> RouteResult:
        """旧链路: 透传 (旧系统由 order_executor._get_order_qty 计算)"""
        signal.metadata["sizing_source"] = "legacy_order_executor"
        return signal

    # ──────────────────────────────────────
    #  审计日志
    # ──────────────────────────────────────
    def _log_route(self, module: str, symbol: str, result: RouteResult):
        """记录路由决策审计日志"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "module": module,
            "symbol": symbol,
            "source": result.source,
            "allowed": result.allowed,
            "direction": result.direction,
            "reason": result.reason,
        }
        self._audit_log.append(entry)

        # 保留最近 1000 条
        if len(self._audit_log) > 1000:
            self._audit_log = self._audit_log[-500:]

        level = logging.INFO if result.allowed else logging.DEBUG
        logger.log(level, f"[Router] {module}/{result.source} {symbol}: "
                          f"{'✅' if result.allowed else '❌'} {result.reason}")

    def get_audit_log(self, limit: int = 50) -> List[Dict]:
        """获取最近的路由审计日志"""
        return self._audit_log[-limit:]

    def get_status(self) -> Dict[str, Any]:
        """获取路由器状态"""
        return {
            "master_switch": self.config.master_switch.value,
            "signal": self.config.resolve("signal"),
            "risk": self.config.resolve("risk"),
            "exit": self.config.resolve("exit"),
            "sizing": self.config.resolve("sizing"),
            "audit_log_size": len(self._audit_log),
        }
