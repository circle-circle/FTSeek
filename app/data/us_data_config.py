"""
美股数据配置管理器 (US Data Config Manager)

功能:
1. 集中管理美股数据源配置
2. 自动加载 API Key (环境变量 → .env.us 共享 → 默认空)
3. 提供配置验证和自检
4. 降低模块耦合

更新时间: 2026-04-13
"""

import os
import logging
import yaml
from typing import Dict, Optional
from pathlib import Path

logger = logging.getLogger("ftseek.us_config")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "us_data_config.yaml"
)


class USDataConfig:
    """美股数据配置管理器"""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self._config: Dict = {}
        self._loaded = False
        self._load()

    def _load(self):
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
            self._loaded = True
            logger.info(f"[USDataConfig] 配置已加载: {self.config_path}")
        except Exception as e:
            logger.warning(f"[USDataConfig] 配置加载失败，使用默认值: {e}")
            self._config = {}

    def get(self, key: str, default=None):
        """获取配置值 (支持点号分隔的路径)"""
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default

    # ============================================================
    #  API Key 管理
    # ============================================================

    def get_api_key(self, provider: str) -> str:
        """
        获取 API Key (多级降级)

        优先级:
        1. 环境变量
        2. .env.us 共享文件
        3. 默认空字符串
        """
        # 1. 环境变量
        env_key_map = {
            'twelvedata': 'TWELVEDATA_API_KEY',
            'finnhub': 'FINNHUB_API_KEY',
            'alphavantage': 'ALPHAVANTAGE_API_KEY',
        }

        env_var = env_key_map.get(provider.lower())
        if env_var:
            val = os.getenv(env_var, '')
            if val:
                return val

        # 2. .env.us 共享
        fallback_path = self.get('api_keys.fallback_env_path', '/opt/ftseek/FTSeek-US/.env.us')
        if os.path.exists(fallback_path):
            try:
                with open(fallback_path) as f:
                    for line in f:
                        if line.startswith(env_var + '='):
                            return line.split('=', 1)[1].strip()
            except Exception:
                pass

        # 3. 默认空
        return ''

    # ============================================================
    #  数据源配置
    # ============================================================

    def get_provider_config(self, provider: str) -> Dict:
        """获取数据提供商配置"""
        return self.get(f'us_data_sources.{provider}', {})

    def get_rate_limit(self, provider: str) -> Dict:
        """获取限速配置"""
        return self.get_provider_config(provider).get('rate_limit', {})

    # ============================================================
    #  盘前配置
    # ============================================================

    def get_premarket_config(self) -> Dict:
        """获取盘前数据获取配置"""
        return self.get('premarket', {})

    # ============================================================
    #  健康检查
    # ============================================================

    def get_health_check_config(self) -> Dict:
        """获取健康检查配置"""
        return self.get('health_check', {})

    # ============================================================
    #  自检
    # ============================================================

    def self_check(self) -> Dict:
        """
        配置自检

        Returns:
            检查结果字典
        """
        result = {
            'healthy': True,
            'config_loaded': self._loaded,
            'sources': {},
            'warnings': [],
        }

        if not self._loaded:
            result['healthy'] = False
            result['warnings'].append('配置文件未加载')

        # 检查各数据源
        for provider in ['twelvedata', 'finnhub', 'alphavantage']:
            key = self.get_api_key(provider)
            enabled = self.get(f'us_data_sources.{provider}.enabled', False)
            has_key = bool(key)

            source_status = {
                'enabled': enabled,
                'key_configured': has_key,
                'key_preview': f"{key[:8]}...{key[-4:]}" if len(key) > 12 else ('已配置' if key else '未配置'),
            }
            result['sources'][provider] = source_status

            if enabled and not has_key:
                result['warnings'].append(f'{provider}: 已启用但 API Key 未配置')
                result['healthy'] = False

        return result


# ============================================================
#  全局单例
# ============================================================
_config: Optional[USDataConfig] = None


def get_us_data_config(config_path: str = None) -> USDataConfig:
    """获取美股数据配置单例"""
    global _config
    if _config is None:
        if config_path:
            _config = USDataConfig(config_path)
        else:
            _config = USDataConfig()
    return _config


def reset_config():
    """重置单例 (用于测试)"""
    global _config
    _config = None
