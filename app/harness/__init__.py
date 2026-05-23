"""
FTSeek-US Harness — 策略进化与桥接层

模块:
- us_audit: 系统审计 (熔断器/健康检查)
- shadow_bridge: Shadow 交易桥接器 (新因子→shadow_trades)
- market_context: 市场上下文提供器 (VIX/SPY/资本水位/风险状态)
- strategy_router: 策略路由器 (新旧链路切换枢纽)
- pipeline_adapter: 管线适配器 (旧scheduler与新框架桥接)
- risk_gateway: 统一风控网关 (三层门禁合并)

设计原则:
- Overlay 模式: 不修改旧系统交易热路径
- 可切换: unified_config.yaml pipeline.master_switch 控制
- 降级安全: 新框架异常时自动 fallback 到旧链路
"""
from app.harness.us_audit import *
from app.harness.market_context import (
    MarketContextProvider,
    MarketContext,
    VixRegime,
    RiskState,
    CapitalLevel,
    MarketTrend,
)
from app.harness.strategy_router import (
    StrategyRouter,
    PipelineConfig,
    PipelineMode,
    RouteResult,
)
from app.harness.pipeline_adapter import PipelineAdapter, InterceptResult
from app.harness.risk_gateway import RiskGateway, RiskDecision
