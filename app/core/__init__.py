"""
FTSeek Core Module (v5.0 - 配置收敛后)

核心功能：
- StateManager: 状态管理器
- DecisionLog: 决策日志
- SessionContext: 会话上下文
- ConfigContainer: 配置容器 (依赖注入)
- VersionManager: 版本管理器

配置架构 (2026-04-09 收敛后):
  - .env → 敏感环境变量
  - config/unified_config.yaml → 唯一配置源
  - ConfigCenter → 统一加载器
"""

from app.core.state_manager import StateManager
from app.core.decision_log import DecisionLog
from app.core.session_context import SessionContext
from app.core.config_container import ConfigContainer
from app.core.version_manager import VersionManager

__all__ = [
    'StateManager',
    'DecisionLog',
    'SessionContext',
    'ConfigContainer',
    'VersionManager',
]

__version__ = '5.0.0'
