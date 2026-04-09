#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 配置中心 (ConfigCenter) v1.0

功能:
- 统一加载所有配置文件 (YAML/JSON/ENV)
- 支持配置合并、覆盖、校验
- 支持热重载 (检测文件变更)
- 提供配置访问接口 (点式访问)
- 配置变更通知回调

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
#  配置访问代理 (支持点式访问)
# ============================================================
class ConfigProxy:
    """配置代理，支持 dict 和点式访问"""

    def __init__(self, data: Dict, prefix: str = ""):
        self._data = data
        self._prefix = prefix

    def __getattr__(self, name: str) -> Any:
        if name.startswith('_'):
            raise AttributeError(name)

        key = f"{self._prefix}.{name}" if self._prefix else name

        if name not in self._data:
            raise AttributeError(f"配置不存在：{key}")

        value = self._data[name]

        # 嵌套 dict 自动代理
        if isinstance(value, dict):
            return ConfigProxy(value, key)

        return value

    def __getitem__(self, key: str) -> Any:
        value = self._data.get(key)
        if isinstance(value, dict):
            return ConfigProxy(value, f"{self._prefix}.{key}" if self._prefix else key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持嵌套路径"""
        parts = key.split('.')
        current = self._data

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default

        if isinstance(current, dict):
            return ConfigProxy(current, key)

        return current

    def to_dict(self) -> Dict:
        """转换为普通字典"""
        return self._data.copy()

    def keys(self):
        return self._data.keys()

    def __repr__(self) -> str:
        return f"ConfigProxy({list(self._data.keys())})"


# ============================================================
#  配置中心主类
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
    CONFIG_FILES = [
        "unified_config.yaml",      # 旧版主配置 (基础)
        "config.yaml",              # 统一配置文件 (v2.0 - 最高优先级)
        "alert_rules.yaml",         # 告警规则
        "trading_rules_20260402.yaml",  # 交易规则
    ]

    # 已废弃的配置文件 (仅兼容，不再使用)
    DEPRECATED_FILES = [
        "risk_config.yaml",
        "strategy_params.json",
        "dynamic_exit_params.yaml",
        "position_strategy.yaml",
        "app_config.yaml",
        "system_config.yaml",
    ]

    def __init__(self, config_dir: str = None):
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

            # 2. 应用环境变量覆盖
            self._apply_env_overrides()

            # 3. 创建代理
            self._proxy = ConfigProxy(self._data)

            # 4. 执行配置校验
            self._validate_configs()

            # 5. 输出加载结果
            if self._validation_errors:
                logger.error(f"配置加载完成 | 错误：{len(self._validation_errors)} | 警告：{len(self._validation_warnings)}")
                for error in self._validation_errors[:5]:
                    logger.error(f"  - {error}")
                return False
            else:
                logger.info(f"配置加载完成 | 警告：{len(self._validation_warnings)}")
                for warning in self._validation_warnings[:5]:
                    logger.warning(f"  - {warning}")
                return True

    def _load_config_file(self, filename: str) -> bool:
        """加载单个配置文件"""
        filepath = self.config_dir / filename

        if not filepath.exists():
            logger.debug(f"配置文件不存在 (可选): {filename}")
            return False

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            # 计算哈希
            file_hash = hashlib.md5(content.encode()).hexdigest()
            self._file_hashes[filename] = file_hash

            # 解析配置
            if filename.endswith('.yaml') or filename.endswith('.yml'):
                config = yaml.safe_load(content) or {}
            elif filename.endswith('.json'):
                config = json.loads(content)
            else:
                logger.warning(f"不支持的配置文件格式：{filename}")
                return False

            # 合并配置 (深度合并)
            self._data = self._deep_merge(self._data, config)

            logger.debug(f"配置已加载：{filename}")
            return True

        except Exception as e:
            logger.error(f"加载配置文件失败 {filename}: {e}")
            self._validation_errors.append(f"加载失败：{filename} - {e}")
            return False

    def _deep_merge(self, base: Dict, override: Dict) -> Dict:
        """深度合并两个字典"""
        result = base.copy()

        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def _apply_env_overrides(self):
        """应用环境变量覆盖配置

        支持的环境变量分类:
        1. 基础交易配置
        2. 风控配置
        3. 策略配置 (支持动态策略参数)
        4. 通知配置
        5. AI 服务配置
        6. 日志配置
        """
        # 环境变量前缀映射 (基础配置)
        env_mapping = {
            # === 基础交易配置 ===
            'TRADING_MODE': 'trading.mode',
            'MAX_DAILY_TRADES': 'trading.max_daily_trades',
            'MAX_TOTAL_POSITION': 'risk_limits.max_total_position',
            'MAX_OPEN_POSITIONS': 'trading.max_open_positions',
            'MIN_SIGNAL_CONFIDENCE': 'trading.min_signal_confidence',

            # === 风控配置 ===
            'MAX_DRAWDOWN': 'risk_limits.max_drawdown',
            'MIN_CONFIDENCE': 'risk_limits.min_confidence',
            'MAX_SINGLE_POSITION': 'risk_limits.max_single_position',
            'MAX_SINGLE_LOSS': 'risk_limits.max_single_loss',
            'STOP_LOSS_PERCENT': 'stop_loss.default_percent',
            'TAKE_PROFIT_PERCENT': 'take_profit.default_percent',

            # === 通知配置 ===
            'DINGTALK_WEBHOOK_URL': 'notification.dingtalk.webhook_url',
            'DINGTALK_SECRET': 'notification.dingtalk.secret',
            'DINGTALK_APP_KEY': 'notification.dingtalk.app_key',
            'DINGTALK_APP_SECRET': 'notification.dingtalk.app_secret',
            'DINGTALK_AGENT_ID': 'notification.dingtalk.agent_id',

            # === 富途配置 ===
            'FUTU_LOGIN_ACCOUNT': 'futu.login_account',
            'FUTU_TRD_PWD_MD5': 'futu.trade_password_md5',

            # === AI 服务配置 ===
            'BAILIAN_API_KEY': 'ai.api_key',
            'BAILIAN_ENDPOINT': 'ai.endpoint',
            'BAILIAN_MODEL': 'ai.model',

            # === 日志配置 ===
            'LOG_LEVEL': 'logging.level',

            # === 共识引擎配置 ===
            'CONSENSUS_THRESHOLD': 'strategy.consensus.threshold',
            'CONSENSUS_WINDOW': 'strategy.consensus.window_seconds',
            'SIGNAL_COOLDOWN': 'strategy.cooldown.signal_cooldown',

            # === 融合权重配置 ===
            'TECHNICAL_WEIGHT': 'strategy.fusion.technical_weight',
            'SENTIMENT_WEIGHT': 'strategy.fusion.sentiment_weight',
            'AI_WEIGHT': 'strategy.fusion.ai_weight',

            # === 策略开关 ===
            'STRATEGY_CONFLUENCE_V2': 'strategy.confluence_v2.enabled',
            'STRATEGY_SUPERTRAND': 'strategy.supertrend.enabled',
            'STRATEGY_SENTIMENT_AI': 'strategy.sentiment_ai.enabled',
            'STRATEGY_OPEN_CLOSE_EFFECT': 'strategy.open_close_effect.enabled',
        }

        # 策略参数动态映射 (支持 STRATEGY_<NAME>_<PARAM> 格式)
        # 例如：STRATEGY_CONFLUENCE_V2_MIN_FACTORS=4
        strategy_params = [
            ('confluence_v2', ['min_factors', 'rsi_period', 'st_multiplier', 'adx_threshold', 'stop_loss_pct', 'take_profit_pct']),
            ('supertrend', ['atr_period', 'multiplier']),
            ('sentiment_ai', ['positive_threshold', 'negative_threshold', 'cooldown_seconds', 'lookback_hours']),
            ('fusion_v2', ['technical_weight', 'sentiment_weight', 'min_confidence', 'sentiment_threshold']),
            ('rsi', ['period', 'overbought', 'oversold']),
            ('macd', ['fast_period', 'slow_period', 'signal_period']),
            ('bollinger', ['period', 'std_dev']),
        ]

        # 应用基础映射
        for env_var, config_key in env_mapping.items():
            value = os.environ.get(env_var)
            if value:
                parsed_value = self._parse_value(value)
                self._set_nested_value(self._data, config_key, parsed_value)
                logger.info(f"环境变量覆盖配置：{config_key} = {parsed_value}")

        # 应用策略参数动态映射
        for strategy_name, params in strategy_params:
            for param in params:
                env_var = f'STRATEGY_{strategy_name.upper()}_{param.upper()}'
                value = os.environ.get(env_var)
                if value:
                    config_key = f'strategy.{strategy_name}.{param}'
                    parsed_value = self._parse_value(value)
                    self._set_nested_value(self._data, config_key, parsed_value)
                    logger.info(f"环境变量覆盖策略参数：{config_key} = {parsed_value}")

    def _parse_value(self, value: str) -> Any:
        """
        解析字符串值为合适的类型

        支持的类型:
        - 布尔值：'true', 'false', 'yes', 'no', '1', '0'
        - 整数：纯数字字符串
        - 浮点数：包含小数点的数字
        - 列表：JSON 数组格式
        - 字符串：其他情况

        Args:
            value: 待解析的字符串

        Returns:
            解析后的值
        """
        # 布尔值
        if value.lower() in ('true', 'yes', '1'):
            return True
        if value.lower() in ('false', 'no', '0'):
            return False

        # 整数
        if value.isdigit():
            return int(value)

        # 负数整数
        if value.startswith('-') and value[1:].isdigit():
            return -int(value[1:])

        # 浮点数
        try:
            return float(value)
        except ValueError:
            pass

        # JSON 列表
        if value.startswith('[') and value.endswith(']'):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass

        # 默认为字符串
        return value

    def _is_float(self, value: str) -> bool:
        """判断是否为浮点数 (已废弃，使用 _parse_value)"""
        try:
            float(value)
            return '.' in value
        except ValueError:
            return False

    def _set_nested_value(self, data: Dict, key: str, value: Any):
        """设置嵌套字典的值"""
        parts = key.split('.')
        current = data

        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        current[parts[-1]] = value

    # ==================== 配置访问 ====================

    def __getattr__(self, name: str) -> Any:
        """支持点式访问配置"""
        if name.startswith('_'):
            raise AttributeError(name)

        if self._proxy is None:
            self.load()

        try:
            return getattr(self._proxy, name)
        except AttributeError:
            raise AttributeError(f"配置不存在：{name}")

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值，支持嵌套路径

        Args:
            key: 配置路径，如 'trading.mode'
            default: 默认值

        Returns:
            配置值
        """
        if self._proxy is None:
            self.load()

        return self._proxy.get(key, default)

    def get_dict(self) -> Dict:
        """获取完整配置字典"""
        if self._proxy is None:
            self.load()

        return self._proxy.to_dict()

    # ==================== 配置校验 ====================

    def _validate_configs(self):
        """执行配置校验"""
        # 1. 检查必要配置
        required_fields = [
            ('trading.mode', '交易模式未配置'),
            ('risk_limits.max_drawdown', '最大回撤未配置'),
            ('risk_limits.max_daily_trades', '日交易次数限制未配置'),
        ]

        for field, error_msg in required_fields:
            value = self.get(field)
            if value is None:
                # 仅当配置文件存在时才报错
                if self._file_hashes:
                    self._validation_errors.append(error_msg)
            elif field == 'risk_limits.max_daily_trades' and value <= 0:
                self._validation_errors.append("日交易次数必须大于 0")

        # 2. 检查配置合理性
        max_drawdown = self.get('risk_limits.max_drawdown', 0)
        if max_drawdown <= 0 or max_drawdown > 0.5:
            self._validation_warnings.append(
                f"最大回撤 {max_drawdown} 超出合理范围 (0-50%)"
            )

        max_daily_trades = self.get('trading.max_daily_trades', 0)
        if max_daily_trades <= 0:
            self._validation_errors.append("日交易次数必须大于 0")
        elif max_daily_trades > 50:
            self._validation_warnings.append(
                f"日交易次数 {max_daily_trades} 过高，建议≤20"
            )

        # 3. 检查止盈止损配置合理性
        stop_loss = abs(self.get('risk_limits.max_single_loss', 0))
        take_profit_default = self.get('take_profit.default_percent', 0)
        take_profit_trailing = self.get('take_profit.trailing_percent', 0)

        # 检查默认止盈与止损的盈亏比
        if stop_loss > 0 and take_profit_default > 0:
            if take_profit_default < stop_loss * 0.8:
                self._validation_warnings.append(
                    f"止盈阈值 ({take_profit_default:.1%}) 小于止损 ({stop_loss:.1%})，盈亏比可能不合理 (建议≥1.5:1)"
                )

        # trailing stop 通常设置较小，不强制要求大于止损

        # 4. 检查环境变量占位符
        self._check_env_placeholders()

    def _check_env_placeholders(self):
        """检查未解析的环境变量占位符"""
        flat_config = self._flatten_dict(self._data)

        # 可选的环境变量（未设置时不产生警告）
        optional_env_vars = {
            'DINGTALK_APP_KEY',      # 钉钉企业应用模式（可选，Webhook 模式不需要）
            'DINGTALK_APP_SECRET',   # 钉钉企业应用模式（可选）
            'DINGTALK_AGENT_ID',     # 钉钉企业应用模式（可选）
            'ADMIN_PHONE',           # 告警联系人（可选）
            'ADMIN_EMAIL',           # 告警联系人（可选）
            'FEISHU_SECRET',         # 飞书机器人签名（可选，Webhook 模式不需要）
        }

        for key, value in flat_config.items():
            if isinstance(value, str) and '${' in value and '}' in value:
                # 检查是否有对应的环境变量
                env_var = value.replace('${', '').replace('}', '')

                # 跳过可选环境变量
                if env_var in optional_env_vars:
                    continue

                if not os.environ.get(env_var):
                    self._validation_warnings.append(
                        f"环境变量未设置：{env_var} (配置：{key})"
                    )

    def _flatten_dict(self, d: Dict, parent_key: str = '', sep: str = '.') -> Dict:
        """展平嵌套字典"""
        items = []
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(self._flatten_dict(v, new_key, sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    # ==================== 热重载 ====================

    def reload_if_changed(self) -> bool:
        """
        检查配置文件变更并重载

        Returns:
            bool: 是否执行了重载
        """
        with self._lock:
            changed_files = []

            for filename, old_hash in list(self._file_hashes.items()):
                filepath = self.config_dir / filename
                if not filepath.exists():
                    continue

                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()

                    new_hash = hashlib.md5(content.encode()).hexdigest()

                    if new_hash != old_hash:
                        changed_files.append(filename)
                        logger.info(f"配置文件变更：{filename}")

                        # 触发变更回调
                        self._trigger_change_callbacks(filename, old_hash, new_hash)

                except Exception as e:
                    logger.error(f"检查配置变更失败 {filename}: {e}")

            if changed_files:
                return self.load(force=True)

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
                    import time
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

    # ==================== 配置导出 ====================

    def export(self, output_path: str, format: str = 'yaml') -> bool:
        """
        导出当前配置到文件

        Args:
            output_path: 输出文件路径
            format: 导出格式 (yaml/json)

        Returns:
            bool: 是否导出成功
        """
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                if format == 'yaml':
                    yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True)
                elif format == 'json':
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                else:
                    logger.error(f"不支持的导出格式：{format}")
                    return False

            logger.info(f"配置已导出：{output_path}")
            return True

        except Exception as e:
            logger.error(f"配置导出失败：{e}")
            return False

    # ==================== 配置校验公开接口 ====================

    def is_valid(self) -> bool:
        """配置是否有效"""
        return len(self._validation_errors) == 0

    def get_validation_errors(self) -> List[str]:
        """获取校验错误列表"""
        return self._validation_errors.copy()

    def get_validation_warnings(self) -> List[str]:
        """获取校验警告列表"""
        return self._validation_warnings.copy()

    def get_validation_summary(self) -> str:
        """获取校验摘要"""
        errors = len(self._validation_errors)
        warnings = len(self._validation_warnings)

        if errors > 0:
            return f"❌ 校验失败：{errors}个错误，{warnings}个警告"
        elif warnings > 0:
            return f"⚠️ 校验通过：{warnings}个警告"
        else:
            return "✅ 校验通过"


# ============================================================
#  全局单例
# ============================================================
_config_center: Optional[ConfigCenter] = None
_config_lock = threading.Lock()


def get_config_center(config_dir: str = None) -> ConfigCenter:
    """获取配置中心单例"""
    global _config_center

    with _config_lock:
        if _config_center is None:
            _config_center = ConfigCenter(config_dir)

        return _config_center


def load_config(config_dir: str = None, force: bool = False) -> ConfigCenter:
    """加载配置并返回配置中心"""
    center = get_config_center(config_dir)
    center.load(force=force)
    return center


# ============================================================
#  便捷函数 (用于替换旧 config 模块)
# ============================================================

def get_value(key: str, default: Any = None) -> Any:
    """获取配置值的便捷函数"""
    return get_config_center().get(key, default)


def get_dict(key: str) -> Optional[Dict]:
    """获取配置字典的便捷函数"""
    value = get_config_center().get(key)
    if isinstance(value, dict):
        return value
    elif hasattr(value, 'to_dict'):
        return value.to_dict()
    return default
