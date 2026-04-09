"""
Harness 多 Agent 协作框架

提供 Agent 基类、消息传递、超时控制和降级策略等核心功能。
"""

from .base import Agent, AgentState, AgentConfig, AgentResult
from .messaging import Message, MessageType, MessageChannel, MessageBus, get_message_bus
from .timeout import TimeoutController, TimeoutConfig, TimeoutState, TaskResult
from .fallback import FallbackStrategy, FallbackConfig, FallbackResult, ModelRouter
from .coordinator import Coordinator, TaskDefinition, WorkflowContext
from .strategist import Strategist
from .risk_officer import RiskOfficer, RiskCheckResult
from .context_engineer import ContextEngineer
from .trade_monitor import TradeMonitor

__version__ = "1.0.0"
__all__ = [
    # Base
    "Agent",
    "AgentState",
    "AgentConfig",
    "AgentResult",
    # Messaging
    "Message",
    "MessageType",
    "MessageChannel",
    "MessageBus",
    "get_message_bus",
    # Timeout
    "TimeoutController",
    "TimeoutConfig",
    "TimeoutState",
    "TaskResult",
    # Fallback
    "FallbackStrategy",
    "FallbackConfig",
    "FallbackResult",
    "ModelRouter",
    # Coordinator
    "Coordinator",
    "TaskDefinition",
    "WorkflowContext",
    # Professional Roles
    "Strategist",
    "RiskOfficer",
    "RiskCheckResult",
    "ContextEngineer",
    "TradeMonitor",
]
