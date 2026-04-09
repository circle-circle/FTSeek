"""
ConfigManager - 配置管理器

功能:
- 统一配置管理
- 配置热重载
- 配置验证
- 配置变更通知
"""

import os
import json
import yaml
import threading
from typing import Any, Dict, Optional, Callable, List
from datetime import datetime
import logging

from app.core.state_manager import StateManager
from app.core.decision_log import DecisionLog

logger = logging.getLogger("ftseek.core.config_manager")
logger.propagate = False  # 防止日志重复


class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_file: str = 'config/app_config.yaml'):
        """
        初始化配置管理器
        
        Args:
            config_file: 配置文件路径
        """
        self.config_file = config_file
        self._config: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._state_mgr = StateManager()
        self._decision_log = DecisionLog()
        self._callbacks: List[Callable] = []
        
        # 加载配置
        self.load()
    
    def load(self) -> bool:
        """加载配置文件"""
        with self._lock:
            try:
                if os.path.exists(self.config_file):
                    with open(self.config_file, 'r', encoding='utf-8') as f:
                        if self.config_file.endswith('.yaml') or self.config_file.endswith('.yml'):
                            self._config = yaml.safe_load(f) or {}
                        else:
                            self._config = json.load(f)
                    
                    logger.info(f"Config loaded from {self.config_file}")
                    return True
                else:
                    logger.warning(f"Config file not found: {self.config_file}")
                    self._config = self._get_default_config()
                    return False
            except Exception as e:
                logger.error(f"Failed to load config: {e}")
                self._config = self._get_default_config()
                return False
    
    def _get_default_config(self) -> Dict:
        """获取默认配置"""
        return {
            'version': '4.3.0',
            'environment': 'production',
            'trading': {
                'mode': 'paper',
                'max_daily_trades': 10,
                'max_single_position': 0.20,
                'max_total_position': 0.60,
            },
            'notification': {
                'dingtalk': {
                    'enabled': True,
                    'mode': 'enterprise',
                    'timeout': 600,
                }
            },
            'features': {
                'four_step_confirmation': True,
                'auto_confirm': True,
                'smart_push': True,
            }
        }
    
    def save(self) -> bool:
        """保存配置"""
        with self._lock:
            try:
                config_dir = os.path.dirname(self.config_file)
                if config_dir and not os.path.exists(config_dir):
                    os.makedirs(config_dir, exist_ok=True)
                
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    if self.config_file.endswith('.yaml') or self.config_file.endswith('.yml'):
                        yaml.dump(self._config, f, default_flow_style=False, allow_unicode=True)
                    else:
                        json.dump(self._config, f, indent=2, ensure_ascii=False)
                
                logger.info(f"Config saved to {self.config_file}")
                return True
            except Exception as e:
                logger.error(f"Failed to save config: {e}")
                return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        with self._lock:
            keys = key.split('.')
            value = self._config
            for k in keys:
                if isinstance(value, dict) and k in value:
                    value = value[k]
                else:
                    return default
            return value
    
    def set(self, key: str, value: Any, reason: str = "") -> bool:
        """设置配置值"""
        with self._lock:
            old_value = self.get(key)
            
            keys = key.split('.')
            config = self._config
            for k in keys[:-1]:
                if k not in config:
                    config[k] = {}
                config = config[k]
            
            config[keys[-1]] = value
            
            # 记录配置变更
            self._decision_log.log_config_change(
                key=key,
                old_value=old_value,
                new_value=value,
                reason=reason
            )
            
            # 通知回调
            self._notify_callbacks(key, old_value, value)
            
            logger.info(f"Config set: {key} = {value}")
            return True
    
    def reload(self) -> bool:
        """重新加载配置（热重载）"""
        result = self.load()
        if result:
            logger.info("Config reloaded (hot reload)")
        return result
    
    def register_callback(self, callback: Callable) -> None:
        """注册配置变更回调"""
        self._callbacks.append(callback)
    
    def _notify_callbacks(self, key: str, old_value: Any, new_value: Any) -> None:
        """通知配置变更"""
        for callback in self._callbacks:
            try:
                callback(key, old_value, new_value)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        with self._lock:
            return self._config.copy()
    
    def validate(self) -> bool:
        """验证配置"""
        required_keys = ['version', 'environment', 'trading']
        for key in required_keys:
            if key not in self._config:
                logger.error(f"Missing required config: {key}")
                return False
        return True
    
    def __getitem__(self, key: str) -> Any:
        """支持字典式访问"""
        return self.get(key)
    
    def __setitem__(self, key: str, value: Any) -> None:
        """支持字典式设置"""
        self.set(key, value)
