"""
统一配置入口 (SSOT)

此模块作为 FTSeek-US 运行时配置的唯一推荐入口：
- from app.core.config import get_config

底层委托给 app.core.config_container.ConfigContainer，
避免 ConfigLoader / UnifiedConfig / ConfigContainer 三重解析的混乱。

历史兼容:
- config.config_loader.get_config() -> ConfigLoader (legacy, config_container底层)
以上保留但不再推荐；新代码应统一使用 app.core.config.get_config().
(config/unified_config.py 已于 2026-08-12 删除, 零消费者)
"""
from __future__ import annotations

from app.core.config_container import ConfigContainer, get_config

__all__ = ["get_config", "ConfigContainer"]
