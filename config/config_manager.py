#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek V5 统一配置管理器

功能：
1. 集中管理所有配置（策略、风控、系统、通知等）
2. 配置热重载（监听文件变化）
3. 配置校验和默认值
4. 配置版本管理
5. 环境变量注入
6. 配置备份和回滚

设计原则：
- 单一数据源：unified_config.yaml 为主配置
- 向后兼容：保留旧配置文件30天
- 分层加载：基础配置 → 环境配置 → 运行时覆盖
"""

import os
import json
import yaml
import time
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

# watchdog 为可选依赖（用于热重载）
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = None

logger = logging.getLogger("ftseek.config_manager")
logger.propagate = False


# ============================================================
# 配置版本管理
# ============================================================
@dataclass
class ConfigVersion:
    """配置版本信息"""
    version: str
    created_at: str
    description: str
    config_file: str
    backup_path: Optional[str] = None


# ============================================================
# 配置管理器
# ============================================================
class ConfigManager:
    """
    统一配置管理器
    
    配置加载优先级：
    1. 环境变量（最高优先级）
    2. unified_config.yaml（主配置）
    3. 专项配置文件（向后兼容）
    4. 代码默认值（最低优先级）
    """
    
    def __init__(self, config_dir: str = None):
        """
        初始化配置管理器
        
        Args:
            config_dir: 配置文件目录
        """
        self.config_dir = Path(config_dir) if config_dir else Path(__file__).parent
        self._config_cache: Dict[str, Any] = {}
        self._last_load_time: float = 0
        self._watcher: Optional[Observer] = None
        self._listeners: List[callable] = []
        
        # 配置版本历史
        self.version_history: List[ConfigVersion] = []
        
        logger.info(f"配置管理器初始化: {self.config_dir}")
        
        # 加载配置
        self.load_all()
    
    # ============================================================
    # 配置加载
    # ============================================================
    
    def load_all(self) -> Dict[str, Any]:
        """
        加载所有配置（统一入口）
        
        Returns:
            完整配置字典
        """
        logger.info("开始加载统一配置...")
        
        # 1. 加载主配置
        main_config = self._load_main_config()
        
        # 2. 加载专项配置（向后兼容）
        legacy_configs = self._load_legacy_configs()
        
        # 3. 合并配置
        merged_config = self._merge_configs(main_config, legacy_configs)
        
        # 4. 注入环境变量
        merged_config = self._inject_env_vars(merged_config)
        
        # 5. 应用默认值
        merged_config = self._apply_defaults(merged_config)
        
        # 6. 校验配置
        self._validate_config(merged_config)
        
        # 缓存配置
        self._config_cache = merged_config
        self._last_load_time = time.time()
        
        logger.info(f"✅ 配置加载完成 (耗时: {time.time() - self._last_load_time:.3f}s)")
        
        return merged_config
    
    def _load_main_config(self) -> Dict[str, Any]:
        """加载主配置文件 unified_config.yaml"""
        main_config_path = self.config_dir / "unified_config.yaml"
        
        if not main_config_path.exists():
            logger.warning(f"主配置文件不存在: {main_config_path}")
            return {}
        
        try:
            with open(main_config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            logger.info(f"✅ 加载主配置: {main_config_path}")
            return config or {}
        except Exception as e:
            logger.error(f"❌ 加载主配置失败: {e}")
            return {}
    
    def _load_legacy_configs(self) -> Dict[str, Any]:
        """加载旧版专项配置（向后兼容）"""
        legacy_configs = {}
        
        # 策略配置
        strategy_config = self._load_json_config("strategy_params.json")
        if strategy_config:
            legacy_configs['strategy'] = strategy_config
        
        # 监控股票列表
        watchlist = self._load_json_config("watchlist.json")
        if watchlist:
            legacy_configs['watchlist'] = watchlist
        
        # 回测预设
        backtest_presets = self._load_json_config("backtest_presets.json")
        if backtest_presets:
            legacy_configs['backtest_presets'] = backtest_presets
        
        logger.info(f"✅ 加载 {len(legacy_configs)} 个专项配置")
        return legacy_configs
    
    def _load_json_config(self, filename: str) -> Optional[Dict]:
        """加载JSON配置文件"""
        filepath = self.config_dir / filename
        
        if not filepath.exists():
            return None
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载 {filename} 失败: {e}")
            return None
    
    def _merge_configs(self, main: Dict, legacy: Dict) -> Dict:
        """
        合并主配置和专项配置
        
        规则：
        - 主配置优先级高于专项配置
        - 专项配置作为补充
        """
        merged = {}
        
        # 深度合并
        def deep_merge(base: Dict, override: Dict) -> Dict:
            result = base.copy()
            for key, value in override.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = deep_merge(result[key], value)
                else:
                    result[key] = value
            return result
        
        # 主配置为基础
        merged = main.copy()
        
        # 合并策略配置
        if 'strategy' in legacy:
            if 'strategy' not in merged:
                merged['strategy'] = {}
            
            # 策略参数合并
            strategy_data = legacy['strategy']
            if 'strategy_weights' in strategy_data:
                if 'strategy' not in merged:
                    merged['strategy'] = {}
                merged['strategy']['weights'] = strategy_data['strategy_weights']
            
            if 'consensus' in strategy_data:
                merged['strategy']['consensus'] = strategy_data['consensus']
            
            if 'enabled_strategies' in strategy_data:
                merged['strategy']['enabled'] = strategy_data['enabled_strategies']
        
        # 合并监控股票
        if 'watchlist' in legacy:
            merged['watchlist'] = legacy['watchlist']
        
        # 合并回测预设
        if 'backtest_presets' in legacy:
            merged['backtest'] = legacy['backtest_presets']
        
        return merged
    
    def _inject_env_vars(self, config: Dict) -> Dict:
        """
        注入环境变量
        
        支持格式: "${ENV_VAR}" 或 "${ENV_VAR:default_value}"
        """
        def replace_env_vars(value):
            if isinstance(value, str):
                # 匹配 ${VAR} 或 ${VAR:default}
                import re
                pattern = r'\$\{([^}]+)\}'
                
                def replacer(match):
                    var_expr = match.group(1)
                    if ':' in var_expr:
                        var_name, default = var_expr.split(':', 1)
                        return os.getenv(var_name, default)
                    else:
                        return os.getenv(var_expr, match.group(0))
                
                return re.sub(pattern, replacer, value)
            elif isinstance(value, dict):
                return {k: replace_env_vars(v) for k, v in value.items()}
            elif isinstance(value, list):
                return [replace_env_vars(item) for item in value]
            else:
                return value
        
        return replace_env_vars(config)
    
    def _apply_defaults(self, config: Dict) -> Dict:
        """应用配置默认值"""
        defaults = {
            'version': '5.0.0',
            'environment': 'production',
            'trading': {
                'mode': 'paper',
                'max_daily_trades': 10,
                'max_single_position': 0.20,
                'max_total_position': 0.60,
            },
            'risk_limits': {
                'min_confidence': 0.45,
                'max_single_position': 0.18,
                'max_daily_loss': 0.03,
            },
            'strategy': {
                'consensus': {
                    'threshold': 1.5,
                    'window_seconds': 60,
                    'min_strategies': 1,
                },
                'weights': {
                    'confluence_v2': 1.5,
                    'supertrend': 1.0,
                    'sentiment_ai': 0.5,
                }
            },
            'logging': {
                'level': 'INFO',
                'format': '%(asctime)s [%(name)s] %(levelname)s: %(message)s',
            }
        }
        
        def deep_apply_defaults(base: Dict, default: Dict) -> Dict:
            result = base.copy()
            for key, value in default.items():
                if key not in result:
                    result[key] = value
                elif isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key] = deep_apply_defaults(result[key], value)
            return result
        
        return deep_apply_defaults(config, defaults)
    
    def _validate_config(self, config: Dict) -> bool:
        """
        校验配置完整性
        
        Returns:
            校验是否通过
        """
        errors = []
        warnings = []
        
        # 必填字段检查
        required_fields = [
            ('version', '版本号'),
            ('trading.mode', '交易模式'),
            ('futu.host', '富途主机'),
            ('futu.port', '富途端口'),
        ]
        
        for field_path, field_name in required_fields:
            value = self._get_nested_value(config, field_path)
            if value is None:
                errors.append(f"缺少必填配置: {field_name} ({field_path})")
        
        # 数值范围检查
        checks = [
            ('trading.max_daily_trades', 1, 100, '日交易次数'),
            ('trading.max_single_position', 0.01, 0.5, '单标的仓位'),
            ('risk_limits.min_confidence', 0.1, 0.9, '最小置信度'),
            ('strategy.consensus.threshold', 0.5, 3.0, '共识阈值'),
        ]
        
        for field_path, min_val, max_val, field_name in checks:
            value = self._get_nested_value(config, field_path)
            if value is not None:
                try:
                    value = float(value)
                    if not (min_val <= value <= max_val):
                        warnings.append(
                            f"配置值超出合理范围: {field_name}={value} "
                            f"(建议: {min_val}-{max_val})"
                        )
                except (ValueError, TypeError):
                    pass
        
        # 输出校验结果
        if errors:
            logger.error("❌ 配置校验失败:")
            for error in errors:
                logger.error(f"  - {error}")
            raise ValueError(f"配置校验失败: {len(errors)} 个错误")
        
        if warnings:
            logger.warning("⚠️ 配置警告:")
            for warning in warnings:
                logger.warning(f"  - {warning}")
        
        logger.info("✅ 配置校验通过")
        return True
    
    def _get_nested_value(self, d: Dict, path: str):
        """获取嵌套字典值"""
        keys = path.split('.')
        value = d
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value
    
    # ============================================================
    # 配置访问
    # ============================================================
    
    def get(self, key: str, default=None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键（支持点号分隔的路径，如 'trading.mode'）
            default: 默认值
        
        Returns:
            配置值
        """
        return self._get_nested_value(self._config_cache, key) or default
    
    def get_all(self) -> Dict[str, Any]:
        """获取完整配置"""
        return self._config_cache.copy()
    
    def get_strategy_config(self) -> Dict:
        """获取策略配置"""
        return self.get('strategy', {})
    
    def get_risk_config(self) -> Dict:
        """获取风控配置"""
        return self.get('risk_limits', {})
    
    def get_trading_config(self) -> Dict:
        """获取交易配置"""
        return self.get('trading', {})
    
    # ============================================================
    # 配置热重载
    # ============================================================
    
    def start_watcher(self):
        """启动配置文件监听（热重载）"""
        if self._watcher:
            logger.warning("配置监听器已在运行")
            return
        
        class ConfigFileHandler(FileSystemEventHandler):
            def __init__(self, manager: 'ConfigManager'):
                self.manager = manager
            
            def on_modified(self, event):
                if event.src_path.endswith(('.yaml', '.yml', '.json')):
                    logger.info(f"检测到配置文件变化: {event.src_path}")
                    self.manager.reload()
        
        self._watcher = Observer()
        handler = ConfigFileHandler(self)
        self._watcher.schedule(handler, str(self.config_dir), recursive=False)
        self._watcher.start()
        
        logger.info("✅ 配置监听器已启动")
    
    def stop_watcher(self):
        """停止配置监听器"""
        if self._watcher:
            self._watcher.stop()
            self._watcher.join()
            self._watcher = None
            logger.info("配置监听器已停止")
    
    def reload(self):
        """重新加载配置"""
        logger.info("🔄 重新加载配置...")
        old_config = self._config_cache.copy()
        
        try:
            self.load_all()
            
            # 通知监听器
            for listener in self._listeners:
                try:
                    listener(old_config, self._config_cache)
                except Exception as e:
                    logger.error(f"配置监听器回调失败: {e}")
            
            logger.info("✅ 配置重载成功")
        except Exception as e:
            logger.error(f"❌ 配置重载失败: {e}")
            # 恢复旧配置
            self._config_cache = old_config
    
    def add_listener(self, callback: callable):
        """
        添加配置变化监听器
        
        Args:
            callback: 回调函数 (old_config, new_config)
        """
        self._listeners.append(callback)
        logger.info(f"添加配置监听器 (总数: {len(self._listeners)})")
    
    # ============================================================
    # 配置备份和版本管理
    # ============================================================
    
    def backup_config(self, description: str = "") -> str:
        """
        备份当前配置
        
        Args:
            description: 备份说明
        
        Returns:
            备份文件路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self.config_dir / "backups" / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        # 备份所有配置文件
        for config_file in self.config_dir.glob("*.{yaml,yml,json}"):
            if config_file.is_file():
                shutil.copy2(config_file, backup_dir / config_file.name)
        
        # 记录版本历史
        version = ConfigVersion(
            version=self.get('version', 'unknown'),
            created_at=datetime.now().isoformat(),
            description=description or f"自动备份 {timestamp}",
            config_file="unified_config.yaml",
            backup_path=str(backup_dir)
        )
        self.version_history.append(version)
        
        logger.info(f"✅ 配置已备份: {backup_dir}")
        return str(backup_dir)
    
    def list_backups(self) -> List[Dict]:
        """列出所有配置备份"""
        backup_dir = self.config_dir / "backups"
        if not backup_dir.exists():
            return []
        
        backups = []
        for backup_path in sorted(backup_dir.iterdir()):
            if backup_path.is_dir():
                backups.append({
                    'path': str(backup_path),
                    'name': backup_path.name,
                    'created': datetime.fromtimestamp(backup_path.stat().st_mtime).isoformat(),
                })
        
        return backups
    
    def restore_backup(self, backup_path: str):
        """
        恢复配置备份
        
        Args:
            backup_path: 备份目录路径
        """
        backup_dir = Path(backup_path)
        if not backup_dir.exists():
            raise FileNotFoundError(f"备份不存在: {backup_path}")
        
        # 先备份当前配置
        self.backup_config("恢复前备份")
        
        # 恢复文件
        for config_file in backup_dir.glob("*.{yaml,yml,json}"):
            if config_file.is_file():
                target = self.config_dir / config_file.name
                shutil.copy2(config_file, target)
                logger.info(f"恢复配置: {config_file.name}")
        
        # 重新加载
        self.reload()
        logger.info(f"✅ 配置已恢复: {backup_path}")
    
    # ============================================================
    # 配置导出和诊断
    # ============================================================
    
    def export_config(self, output_path: str = None) -> str:
        """
        导出完整配置（包含默认值）
        
        Args:
            output_path: 输出文件路径
        
        Returns:
            输出文件路径
        """
        if output_path is None:
            output_path = str(self.config_dir / "exported_config.yaml")
        
        config = self.get_all()
        
        with open(output_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        logger.info(f"✅ 配置已导出: {output_path}")
        return output_path
    
    def get_config_status(self) -> Dict:
        """获取配置状态（用于诊断）"""
        return {
            'version': self.get('version'),
            'environment': self.get('environment'),
            'loaded_at': datetime.fromtimestamp(self._last_load_time).isoformat(),
            'config_dir': str(self.config_dir),
            'config_files': [
                str(f) for f in self.config_dir.glob("*.{yaml,yml,json}")
            ],
            'watcher_active': self._watcher is not None if self._watcher else False,
            'listeners_count': len(self._listeners),
            'backups_count': len(self.list_backups()),
        }


# ============================================================
# 全局单例
# ============================================================
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """获取全局配置管理器单例"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager


def get_config(key: str = None, default=None):
    """
    便捷函数：获取配置值
    
    Args:
        key: 配置键（如 'trading.mode'）
        default: 默认值
    
    Returns:
        配置值或完整配置字典
    """
    manager = get_config_manager()
    if key:
        return manager.get(key, default)
    return manager.get_all()


def reload_config():
    """便捷函数：重新加载配置"""
    get_config_manager().reload()
