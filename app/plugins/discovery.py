#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 插件系统

自动发现并注册 app/plugins/ 下所有插件。

版本历史:
  2026-05-19 v1: 创建 (Phase 20)
"""

import importlib
import logging
import os
import pkgutil
from typing import List

logger = logging.getLogger(__name__)


def discover_plugins() -> List[str]:
    """
    自动发现并注册 app/plugins/ 下所有插件

    Returns:
        已注册插件名称列表
    """
    from app.core.domain_events import get_event_bus

    registered_plugins = []

    # 获取插件目录路径
    plugins_dir = os.path.dirname(__file__)

    for importer, modname, ispkg in pkgutil.iter_modules([plugins_dir]):
        if modname.startswith('_'):
            continue

        try:
            mod = importlib.import_module(f"app.plugins.{modname}")

            # 查找具有 register 方法的类
            for attr_name in dir(mod):
                cls = getattr(mod, attr_name)
                if isinstance(cls, type) and hasattr(cls, 'register'):
                    try:
                        instance = cls()
                        instance.register(get_event_bus())
                        registered_plugins.append(modname)
                        logger.info(f"[插件系统] 已注册插件：{modname}")
                    except Exception as e:
                        logger.warning(f"[插件系统] 注册插件 {modname} 失败：{e}")
        except Exception as e:
            logger.warning(f"[插件系统] 加载模块 {modname} 失败：{e}")

    return registered_plugins


# 导出公共接口
__all__ = ['discover_plugins']
