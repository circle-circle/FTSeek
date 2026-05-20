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

    def get_api_key(self, provider: str) -> str:
        """获取美股数据源 API Key（从 unified_config 或环境变量读取）"""
        # 尝试从 data_sources.us.api_keys 读取
        api_keys = self._config.get('api_keys', {})
        if provider in api_keys:
            return api_keys[provider]
        # 降级：环境变量
        import os
        key_map = {
            'twelvedata': 'TWELVEDATA_API_KEY',
            'finnhub': 'FINNHUB_API_KEY',
            'alphavantage': 'ALPHAVANTAGE_API_KEY',
        }
        env_var = key_map.get(provider)
        if env_var:
            return os.environ.get(env_var, '')
        return ''


# Compatibility layer: for us_overnight_fetcher / us_data_fetcher
_us_data_config_singleton = None

def get_us_data_config():
    """Get US data config singleton"""
    global _us_data_config_singleton
    if _us_data_config_singleton is None:
        _us_data_config_singleton = USDataConfig()
    return _us_data_config_singleton
