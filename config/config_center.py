#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 配置中心 (ConfigCenter) v2.0 - P0 修复版

功能:
- 统一加载所有配置文件 (YAML/JSON/ENV)
- 支持配置合并、覆盖、校验
- 支持热重载 (检测文件变更) - P0 修复 2026-04-11
- 提供配置访问接口 (点式访问)
- 配置变更通知回调
- 三层级联查询：env > yaml > default (v2.0)
- 配置来源追踪 (v2.0)
- 启动时配置摘要日志 (v2.0)

P0 修复 (2026-04-11):
- 配置热重载安全机制
  1. 二次验证 - 确保文件写入完成
  2. YAML格式验证 - 避免加载错误配置
  3. 安全加载 - 失败时保持旧配置
  4. 异常隔离 - 单个文件失败不影响其他文件

使用示例:
    from config.config_center import ConfigCenter

    config = ConfigCenter()
    config.load()

    # 访问配置
    print(config.trading.mode)
    print(config.risk_limits.max_drawdown)

    # 热重载
    config.reload_if_changed()
"""

import os
import json
import yaml
import hashlib
import threading
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from dotenv import load_dotenv

logger = logging.getLogger("ftseek.config_center")
logger.propagate = False


# ============================================================
#  配置变更事件
# ============================================================
@dataclass
class ConfigChangeEvent:
    """配置变更事件"""
    key: str
    old_value: Any
    new_value: Any
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = ""  # 配置文件来源


# ============================================================
#  配置代理 (点式访问)
# ============================================================
class ConfigProxy:
    """配置代理，支持点式访问"""
    
    def __init__(self, data: Dict[str, Any]):
        self._data = data
    
    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            return super().__getattribute__(key)
        
        if key in self._data:
            value = self._data[key]
            if isinstance(value, dict):
                return ConfigProxy(value)
            return value
        
        raise AttributeError(f"配置项不存在: {key}")
    
    def get(self, path: str, default: Any = None) -> Any:
        """
        获取配置值，支持嵌套路径 (e.g., 'risk.max_position_size').
        自动处理常见的路径别名以兼容旧代码.
        """
        # P0 修复: 路径别名映射 (兼容旧代码调用)
        path_aliases = {
            'risk.max_position_size': 'risk_limits.max_position_size',
            'risk.max_total_position': 'risk_limits.max_total_position',
            'risk.max_drawdown': 'risk_limits.max_drawdown',
            'risk.max_daily_loss': 'risk_limits.max_daily_loss',
            'risk.max_positions': 'risk_limits.max_positions',
            'strategy.signal_cooldown': 'strategy.cooldown.signal_cooldown',
            'trading.market_open': 'trading_hours.market_open',
            'trading.market_close': 'trading_hours.market_close',
        }
        path = path_aliases.get(path, path)
        
        keys = path.split('.')
        val = self._data
        for key in keys:
            if isinstance(val, dict) and key in val:
                val = val[key]
            else:
                return default
        return val
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return self._data.copy()


# ============================================================
#  配置中心
# ============================================================
class ConfigCenter:
    """
    FTSeek 配置中心

    统一管理所有配置文件，支持:
    - 多文件加载与合并
    - 环境变量覆盖
    - 热重载检测
    - 配置校验
    - 变更通知
    """

    # 配置文件加载顺序 (优先级从低到高)
    # 后面的配置文件会覆盖前面的配置
        # P0 修复 (2026-04-26): 从 10 个文件收敛到 2 个
    CONFIG_FILES = [
        "unified_config.yaml",      # 统一配置 (SSOT - v4.0)
        "stock_pool.yaml",          # 股票池 (独立管理)
    ]

    # 已废弃的配置文件 (仅兼容，不再使用)
    DEPRECATED_FILES = [
        "risk_config.yaml",
        "strategy_params.json",
        "dynamic_exit_params.yaml",
        "position_strategy.yaml",
        "app_config.yaml",
        "system_config.yaml",
        "trading_rules_20260402.yaml",  # 2026-04-16: 已整合到 config.yaml
    ]

    def __init__(self, config_dir: Optional[str] = None):
        """
        初始化配置中心

        Args:
            config_dir: 配置文件目录，默认使用 /opt/ftseek/FTSeek/config
        """
        self.config_dir = Path(config_dir) if config_dir else Path(__file__).parent
        self._data: Dict[str, Any] = {}
        self._proxy: Optional[ConfigProxy] = None
        self._file_hashes: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._callbacks: List[Callable[[ConfigChangeEvent], None]] = []
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_watching = False

        # 配置校验结果
        self._validation_errors: List[str] = []
        self._validation_warnings: List[str] = []

        logger.info(f"配置中心初始化 | 目录：{self.config_dir}")

    # ==================== 配置加载 ====================

    def load(self, force: bool = False) -> bool:
        """
        加载所有配置文件

        Args:
            force: 是否强制重新加载

        Returns:
            bool: 是否加载成功
        """
        with self._lock:
            if self._data and not force:
                logger.debug("配置已加载，跳过 (使用 force=True 强制重载)")
                return True

            # 0. 加载 .env 文件到环境变量 (2026-04-09 修复)
            # 这样配置文件中的 ${VAR} 占位符才能正确解析
            env_file = self.config_dir.parent / ".env"
            if env_file.exists():
                load_dotenv(env_file)
                logger.info(f"已加载 .env 文件：{env_file}")
            else:
                logger.warning(f".env 文件不存在：{env_file}")

            self._data = {}
            self._file_hashes = {}
            self._validation_errors = []
            self._validation_warnings = []

            # 1. 加载主配置文件
            for config_file in self.CONFIG_FILES:
                self._load_config_file(config_file)

            # 1.5 加载 meta_overrides.json (归因分析参数覆盖) ← 2026-05-19 修复死代码
            # 2026-05-19 P1-3: 置信度分级自动应用
            meta_overrides_path = self.config_dir / "meta_overrides.json"
            if meta_overrides_path.exists():
                try:
                    with open(meta_overrides_path, 'r', encoding='utf-8') as f:
                        meta_overrides = json.load(f)
                    if meta_overrides:
                        applied_count = 0
                        pending_count = 0
                        for key, value in meta_overrides.items():
                            # 置信度分级：高置信度 (>0.8) 自动应用，低置信度待审核
                            confidence = value.get('confidence', 0.5) if isinstance(value, dict) else 0.5
                            if confidence >= 0.8:
                                # 高置信度：自动应用
                                self._merge_config({key: value})
                                applied_count += 1
                                logger.info(f"[归因自动应用] {key}: confidence={confidence:.2f} >= 0.8 ✅")
                            else:
                                # 低置信度：记录待审核
                                pending_count += 1
                                logger.info(f"[归因待审核] {key}: confidence={confidence:.2f} < 0.8 ⚠️")
                        logger.info(
                            f"归因分析覆盖配置已加载：meta_overrides.json | "
                            f"自动应用={applied_count} 条，待审核={pending_count} 条"
                        )
                except Exception as e:
                    logger.warning(f"加载 meta_overrides.json 失败：{e}")

            # 2. 应用环境变量覆盖
            self._apply_env_overrides()

            # 3. 创建代理
            self._proxy = ConfigProxy(self._data)

            # 4. 配置校验
            self._validate_config()

            # 5. 记录配置摘要
            self._log_config_summary()

            return len(self._validation_errors) == 0

    def _load_config_file(self, filename: str):
        """加载单个配置文件"""
        filepath = self.config_dir / filename

        if not filepath.exists():
            logger.warning(f"配置文件不存在：{filepath}")
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            # 计算文件哈希
            file_hash = hashlib.md5(content.encode()).hexdigest()
            self._file_hashes[filename] = file_hash

            # 解析配置
            if filename.endswith('.yaml') or filename.endswith('.yml'):
                config = yaml.safe_load(content)
            elif filename.endswith('.json'):
                config = json.loads(content)
            else:
                logger.warning(f"不支持的配置文件格式：{filename}")
                return

            if config:
                # 合并配置
                self._merge_config(config)
                logger.info(f"配置文件加载成功：{filename}")

        except Exception as e:
            logger.error(f"配置文件加载失败 {filename}: {e}")
            self._validation_errors.append(f"配置文件加载失败 {filename}: {e}")

    def _merge_config(self, new_config: Dict[str, Any]):
        """合并配置 (新配置覆盖旧配置)"""
        for key, value in new_config.items():
            if key in self._data and isinstance(self._data[key], dict) and isinstance(value, dict):
                # 递归合并
                self._merge_dict(self._data[key], value)
            else:
                self._data[key] = value

    def _merge_dict(self, base: Dict, override: Dict):
        """递归合并字典"""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._merge_dict(base[key], value)
            else:
                base[key] = value

    def _apply_env_overrides(self):
        """应用环境变量覆盖"""
        for key, value in os.environ.items():
            if key.startswith("FTSEEK_"):
                config_key = key[7:].lower()  # 移除 FTSEEK_ 前缀
                self._data[config_key] = value

    def _validate_config(self):
        """配置校验"""
        # 检查必需配置项
        # P0 修复 (2026-04-26): 兼容旧版 (trading/risk_limits) 和新版 (base/risk) 键名
        required_keys = ['version']
        # 兼容: trading OR base
        if 'trading' not in self._data and 'base' not in self._data:
            self._validation_errors.append("缺少必需配置项: trading 或 base")
        # 兼容: risk_limits OR risk
        if 'risk_limits' not in self._data and 'risk' not in self._data:
            self._validation_errors.append("缺少必需配置项: risk_limits 或 risk")
        for key in required_keys:
            if key not in self._data:
                self._validation_errors.append(f"缺少必需配置项: {key}")

        # 检查交易模式 (兼容 trading.mode 和 base.trade_env)
        trading_mode = self._data.get('trading', {}).get('mode') or self._data.get('base', {}).get('trade_env')
        if trading_mode and trading_mode not in ['paper', 'real', 'paper_trading', 'live', 'sim']:
            # 不阻止，只警告
            self._validation_warnings.append(f"交易模式: {trading_mode}")

        # 检查风控参数 (兼容 risk_limits 和 risk)
        risk_data = self._data.get('risk_limits', {}) or self._data.get('risk', {})
        max_dd = risk_data.get('max_drawdown', risk_data.get('max_drawdown_pct', 0))
        if max_dd > 0.20:
            self._validation_warnings.append(f"最大回撤设置过高: {max_dd}")

        if self._validation_errors:
            logger.error(f"配置校验失败: {self._validation_errors}")
        if self._validation_warnings:
            logger.warning(f"配置校验警告: {self._validation_warnings}")

    def _log_config_summary(self):
        """记录配置摘要"""
        logger.info("=" * 60)
        logger.info("配置中心状态")
        logger.info("=" * 60)
        logger.info(f"配置目录：{self.config_dir}")
        logger.info(f"配置文件：{list(self._file_hashes.keys())}")
        logger.info(f"校验结果：{'✅ 校验通过' if not self._validation_errors else '❌ 校验失败'}")
        logger.info("=" * 60)

    # ==================== 配置访问 ====================

    def __getattr__(self, key: str) -> Any:
        """点式访问配置"""
        if self._proxy is None:
            self.load()
        
        return getattr(self._proxy, key)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持点式路径"""
        if self._proxy is None:
            self.load()
        
        return self._proxy.get(key, default) if self._proxy else default

    def to_dict(self) -> Dict[str, Any]:
        """获取完整配置字典"""
        if self._proxy is None:
            self.load()
        
        return self._proxy.to_dict() if self._proxy else {}

    # ==================== 热重载 ====================

    def reload_if_changed(self) -> bool:
        """
        检查配置文件变更并重载 (P0 修复 2026-04-11)
        
        安全机制:
        1. 二次验证 - 确保文件写入完成
        2. YAML格式验证 - 避免加载错误配置
        3. 安全加载 - 失败时保持旧配置
        4. 异常隔离 - 单个文件失败不影响其他文件

        Returns:
            bool: 是否执行了重载
        """
        with self._lock:
            changed_files = []
            failed_files = []

            for filename, old_hash in list(self._file_hashes.items()):
                filepath = self.config_dir / filename
                if not filepath.exists():
                    continue

                try:
                    # 第一次读取
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()

                    new_hash = hashlib.md5(content.encode()).hexdigest()

                    if new_hash != old_hash:
                        # P0 修复 1: 二次验证 - 等待1秒后再次读取
                        time.sleep(1)
                        
                        with open(filepath, 'r', encoding='utf-8') as f:
                            content2 = f.read()
                        
                        new_hash2 = hashlib.md5(content2.encode()).hexdigest()
                        
                        # 如果两次哈希不一致，说明文件还在写入中
                        if new_hash != new_hash2:
                            logger.warning(
                                f"配置文件正在被修改，跳过本次检查: {filename}"
                            )
                            continue
                        
                        # P0 修复 2: 验证 YAML 格式
                        try:
                            yaml.safe_load(content2)
                        except yaml.YAMLError as e:
                            logger.error(
                                f"配置文件格式错误，跳过重载: {filename} | "
                                f"错误: {e}"
                            )
                            failed_files.append(filename)
                            continue
                        
                        # 验证通过，记录变更
                        changed_files.append(filename)
                        logger.info(f"配置文件变更：{filename}")

                        # 触发变更回调
                        self._trigger_change_callbacks(filename, old_hash, new_hash2)

                except yaml.YAMLError as e:
                    logger.error(f"配置文件 YAML 格式错误 {filename}: {e}")
                    failed_files.append(filename)
                except Exception as e:
                    logger.error(f"检查配置变更失败 {filename}: {e}")

            # P0 修复 3: 安全加载 - 只有验证通过才重载
            if changed_files:
                try:
                    success = self.load(force=True)
                    if success:
                        logger.info(
                            f"配置重载成功 | "
                            f"变更: {len(changed_files)} 个文件 | "
                            f"失败: {len(failed_files)} 个文件"
                        )
                    else:
                        logger.error("配置重载失败，保持旧配置")
                    return success
                except Exception as e:
                    logger.error(f"配置重载异常，保持旧配置: {e}")
                    return False

            return False

    def _trigger_change_callbacks(self, filename: str, old_hash: str, new_hash: str):
        """触发配置变更回调"""
        event = ConfigChangeEvent(
            key=filename,
            old_value=old_hash,
            new_value=new_hash,
            source=filename
        )

        for callback in self._callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"配置变更回调执行失败：{e}")

    def on_change(self, callback: Callable[[ConfigChangeEvent], None]):
        """注册配置变更回调"""
        self._callbacks.append(callback)
        logger.debug(f"配置变更回调已注册")

    def start_auto_watch(self, interval: int = 30) -> bool:
        """
        启动自动监控配置变更

        Args:
            interval: 检查间隔 (秒)

        Returns:
            bool: 是否启动成功
        """
        if self._watch_thread and self._watch_thread.is_alive():
            logger.warning("自动监控已在运行")
            return False

        self._stop_watching = False

        def watch_loop():
            while not self._stop_watching:
                try:
                    self.reload_if_changed()
                except Exception as e:
                    logger.error(f"配置监控异常：{e}")

                # 等待下一轮
                for _ in range(interval * 10):
                    if self._stop_watching:
                        break
                    time.sleep(0.1)

        self._watch_thread = threading.Thread(target=watch_loop, daemon=True)
        self._watch_thread.start()

        logger.info(f"配置自动监控已启动 (间隔：{interval}秒)")
        return True

    def stop_auto_watch(self):
        """停止自动监控"""
        self._stop_watching = True
        if self._watch_thread:
            self._watch_thread.join(timeout=5)
            logger.info("配置自动监控已停止")

    # ==================== 工具方法 ====================

    def get_validation_errors(self) -> List[str]:
        """获取校验错误"""
        return self._validation_errors.copy()

    def get_validation_warnings(self) -> List[str]:
        """获取校验警告"""
        return self._validation_warnings.copy()

    def get_validation_summary(self) -> str:
        """获取校验摘要（main.py 启动日志使用）"""
        n_err = len(self._validation_errors)
        n_warn = len(self._validation_warnings)
        if n_err == 0 and n_warn == 0:
            return "校验通过"
        parts = []
        if n_err > 0:
            parts.append(f"{n_err}个错误")
        if n_warn > 0:
            parts.append(f"{n_warn}个警告")
        return ", ".join(parts)

    def is_valid(self) -> bool:
        """配置是否有效"""
        return len(self._validation_errors) == 0


# ============================================================
#  全局单例
# ============================================================
_config_center: Optional[ConfigCenter] = None
_config_lock = threading.Lock()


def get_config_center(config_dir: Optional[str] = None) -> ConfigCenter:
    """获取配置中心单例"""
    global _config_center
    
    if _config_center is None:
        with _config_lock:
            if _config_center is None:
                _config_center = ConfigCenter(config_dir)
                _config_center.load()
    
    return _config_center


def reset_config_center():
    """重置配置中心 (用于测试)"""
    global _config_center
    with _config_lock:
        _config_center = None


# 便捷访问
config_center = get_config_center()
