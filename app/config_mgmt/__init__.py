"""
配置模块 (2026-04-11)

提供配置管理工具:
- auditor: 配置审计、梳理、文档生成
- splitter: 配置拆分/合并 (可选，当前未启用)

配置文件集中管理:
- config/config.yaml: 统一配置文件 (1361行, 37个顶级配置项)
- config/config_index.yaml: 自动生成的配置索引
- docs/CONFIG_GUIDE.md: 自动生成的配置文档

使用方法:
    # 审计配置并生成文档
    python3 -m app.config.auditor
    
    # 查看配置文档
    cat docs/CONFIG_GUIDE.md
"""

from app.config_mgmt.auditor import ConfigAuditor
from app.config_mgmt.splitter import ConfigSplitter

__all__ = ['ConfigAuditor', 'ConfigSplitter']
