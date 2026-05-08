#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股数据配置管理器 (SSOT - 已迁移至 unified_config.yaml)"""

import logging

from config.config_center import get_config_center

logger = logging.getLogger("ftseek.us_config")

_config_center = get_config_center()

class USDataConfig:
    def __init__(self):
        self._config = _config_center.get('data_sources.us', {})
        logger.info(f"[USDataConfig] 配置已加载: data_sources.us")

    @property
    def sources(self):
        return self._config.get('sources', [])

    def is_enabled(self, source_name):
        for source in self.sources:
            if source['name'] == source_name:
                return source.get('enabled', False)
        return False


# Compatibility layer: for us_overnight_fetcher / us_data_fetcher
_us_data_config_singleton = None

def get_us_data_config():
    """Get US data config singleton"""
    global _us_data_config_singleton
    if _us_data_config_singleton is None:
        _us_data_config_singleton = USDataConfig()
    return _us_data_config_singleton
