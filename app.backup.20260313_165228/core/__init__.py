"""
FTSeek V4 Core Module

核心功能模块：
- StateManager: 状态管理器
- DecisionLog: 决策日志
- SessionContext: 会话上下文
- ConfigManager: 配置管理器
- VersionManager: 版本管理器
"""

from app.core.state_manager import StateManager
from app.core.decision_log import DecisionLog
from app.core.session_context import SessionContext
from app.core.config_manager import ConfigManager
from app.core.version_manager import VersionManager

__all__ = [
    'StateManager',
    'DecisionLog',
    'SessionContext',
    'ConfigManager',
    'VersionManager',
]

__version__ = '4.3.0'
