"""
ConfigLoader — 统一配置加载器

SSOT (Single Source of Truth): config/unified_config.yaml
原则: 环境变量 > unified_config.yaml > 代码默认值

所有模块通过此加载器获取配置，禁止直接读 YAML 或硬编码。

使用方式:
    from app.core.config_loader import cfg
    
    # 点路径访问
    cfg.get("futu.host")              → "127.0.0.1"
    cfg.get("stop_loss.default_percent") → 0.03
    cfg.get("trading.watch_list_hk")  → ["HK.00700", ...]
    
    # 带默认值
    cfg.get("unknown.key", default=42) → 42
    
    # 整段获取
    cfg.get_section("stop_loss") → {"default_percent": 0.03, ...}
"""

import os
import re
import yaml
import logging
import threading
from typing import Any, Optional, Dict
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ftseek.config_loader")

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_UNIFIED_CONFIG_PATH = _PROJECT_ROOT / "config" / "unified_config.yaml"


class ConfigLoader:
    """
    统一配置加载器 — 单例
    
    特性:
    1. 启动时加载 unified_config.yaml
    2. 环境变量自动解析 ${ENV_VAR} 和 ${ENV_VAR:default} 语法
    3. 点路径访问 (如 "trading.watch_list_hk")
    4. 线程安全
    5. 支持运行时重新加载
    """
    
    _instance: Optional['ConfigLoader'] = None
    _lock = threading.Lock()
    
    def __init__(self, config_path: Optional[str] = None):
        self._config_path = Path(config_path) if config_path else _UNIFIED_CONFIG_PATH
        self._data: Dict[str, Any] = {}
        self._load_time: Optional[str] = None
        self._reload_count = 0
        self._load()
    
    def _load(self):
        """加载并解析配置文件"""
        if not self._config_path.exists():
            raise FileNotFoundError(
                f"统一配置文件不存在: {self._config_path}\n"
                f"请确保 config/unified_config.yaml 存在"
            )
        
        with open(self._config_path, 'r', encoding='utf-8') as f:
            raw = yaml.safe_load(f)
        
        # 解析环境变量
        self._data = self._resolve_env_vars(raw)
        self._load_time = datetime.now().isoformat()
        self._reload_count += 1
        
        logger.info(
            f"配置已加载: {self._config_path.name} "
            f"({len(self._flatten(self._data))} 个配置项, "
            f"reload #{self._reload_count})"
        )
    
    def _resolve_env_vars(self, obj: Any) -> Any:
        """递归解析 ${ENV_VAR} 和 ${ENV_VAR:default} 语法"""
        if isinstance(obj, str):
            return self._resolve_single(obj)
        elif isinstance(obj, dict):
            return {k: self._resolve_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._resolve_env_vars(item) for item in obj]
        return obj
    
    def _resolve_single(self, text: str) -> str:
        """解析单个字符串中的环境变量"""
        # 匹配 ${VAR} 或 ${VAR:default}
        pattern = r'\$\{([^}]+)\}'
        
        def _replace(match):
            expr = match.group(1)
            if ':' in expr:
                var_name, default = expr.split(':', 1)
                return os.getenv(var_name, default)
            else:
                return os.getenv(expr, match.group(0))  # 找不到保留原文
        
        return re.sub(pattern, _replace, text)
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        通过点路径获取配置值
        
        Args:
            key: 点路径 (如 "stop_loss.default_percent")
            default: 默认值（如果 key 不存在）
        
        Returns:
            配置值或默认值
        """
        parts = key.split('.')
        current = self._data
        
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        
        return current
    
    def get_section(self, section: str) -> Dict[str, Any]:
        """获取整段配置"""
        result = self.get(section, {})
        if isinstance(result, dict):
            return result
        return {}
    
    def reload(self):
        """重新加载配置（热重载）"""
        with self._lock:
            old_count = self._reload_count
            self._load()
            logger.info(f"配置热重载完成 (#{self._reload_count})")
    
    @property
    def load_time(self) -> Optional[str]:
        return self._load_time
    
    @property
    def config_path(self) -> Path:
        return self._config_path
    
    # ===== 便捷访问属性 =====
    
    @property
    def all(self) -> Dict[str, Any]:
        """获取全部配置（只读）"""
        return dict(self._data)
    
    def _flatten(self, d: dict, parent_key: str = '', sep: str = '.') -> dict:
        """扁平化配置字典（用于统计）"""
        items = {}
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(self._flatten(v, new_key, sep))
            else:
                items[new_key] = v
        return items
    
    @classmethod
    def get_instance(cls, config_path: Optional[str] = None) -> 'ConfigLoader':
        """获取单例实例"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(config_path)
            return cls._instance


# ===== 全局便捷访问 =====

def get_config_loader() -> ConfigLoader:
    """获取配置加载器实例"""
    return ConfigLoader.get_instance()


# 模块级单例
cfg: ConfigLoader = ConfigLoader.get_instance()
