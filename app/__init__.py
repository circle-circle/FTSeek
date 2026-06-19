"""
FTSeek-US - 多 Agent 美股交易平台

三大引擎:
- Risk Engine: 风险引擎 (熔断 + 仓位限制 + 动态止损止盈)
- Execution Engine: 执行引擎 (智能路由 + 订单优化 + 幂等机制)
- Observability Engine: 观测引擎 (监控 + 告警 + 审计 + 回放)
"""

__version__ = "1.0.0"
__author__ = "FTSeek Team"

from .core.engine import RiskEngine, ExecutionEngine, ObservabilityEngine

__all__ = [
    "RiskEngine",
    "ExecutionEngine",
    "ObservabilityEngine",
]
