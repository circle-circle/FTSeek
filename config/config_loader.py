#!/usr/bin/env python3
"""
FTSeek-US 配置加载器 - 从 unified_config.yaml 统一加载

设计原则:
1. 单一数据源 (SSOT) - 所有配置来自 unified_config.yaml
2. 环境变量优先 - ${VAR} 占位符被环境变量替换
3. 类型安全 - 加载时验证类型和范围
4. 向后兼容 - 提供旧接口 (RISK_CONFIG, DECISION_CONFIG 等)

使用方式:
    from config.config_loader import get_config
    config = get_config()
    risk_config = config.risk
    min_confidence = config.risk.signal_quality.min_confidence
"""

import os
import re
import yaml
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ============================================================================
# 配置数据类定义
# ============================================================================

@dataclass
class WatchlistStock:
    code: str
    name: str
    sector: str
    enabled: bool = True
    tier: str = "TIER1"
    order_qty: int = 50
    max_position_ratio: float = 0.20
    stop_loss: float = -0.10
    take_profit: float = 0.20
    notes: str = ""


@dataclass
class WatchlistConfig:
    us_stocks: list = field(default_factory=list)
    etfs: list = field(default_factory=list)


@dataclass
class SignalThresholds:
    min_score: int = 50
    medium_score: int = 70
    high_score: int = 85


@dataclass
class PositionRatios:
    low: float = 0.25
    medium: float = 0.50
    high: float = 1.00


@dataclass
class SignalTiming:
    cooldown_seconds: int = 60
    expire_seconds: int = 300


@dataclass
class DecisionConfig:
    signal_thresholds: SignalThresholds = field(default_factory=SignalThresholds)
    position_ratios: PositionRatios = field(default_factory=PositionRatios)
    signal_timing: SignalTiming = field(default_factory=SignalTiming)
    strategy_weights: dict = field(default_factory=dict)


@dataclass
class AccountRisk:
    max_daily_drawdown: float = 0.03
    max_total_drawdown: float = 0.08
    # max_leverage / min_margin_ratio 已删除 (2026-06-20): 零代码消费


@dataclass
class PositionRisk:
    max_per_symbol: float = 0.25
    max_per_direction: float = 0.65
    max_total: float = 0.80


@dataclass
class TradingRisk:
    max_daily_trades: int = 500
    max_order_amount: int = 120000
    min_liquidity_ratio: float = 0.5


@dataclass
class SignalQuality:
    min_confidence: float = 0.70
    min_consensus: float = 0.3
    min_score: int = 60


@dataclass
class StopLoss:
    default: float = -0.06
    trailing_atr_mult: float = 2.0
    time_stop_hours: int = 72


@dataclass
class TakeProfit:
    tier1: float = 0.08
    tier2: float = 0.15
    tier3: float = 0.25


@dataclass
class RiskConfig:
    account: AccountRisk = field(default_factory=AccountRisk)
    position: PositionRisk = field(default_factory=PositionRisk)
    trading: TradingRisk = field(default_factory=TradingRisk)
    signal_quality: SignalQuality = field(default_factory=SignalQuality)
    stop_loss: StopLoss = field(default_factory=StopLoss)
    take_profit: TakeProfit = field(default_factory=TakeProfit)
    circuit_breaker: dict = field(default_factory=dict)


@dataclass
class OptionsConfig:
    enabled: bool = False
    max_holding_hours: int = 72
    profit_target_pct: float = 0.5
    stop_loss_pct: float = -0.3
    min_dte: int = 7
    max_dte: int = 45
    preferred_dte: int = 30
    max_cost_per_contract: int = 2000
    take_profit_ratio: float = 2.0
    stop_loss_ratio: float = 0.5
    force_close_dte: int = 2
    quick_exit: dict = field(default_factory=dict)


@dataclass
class ServiceConfig:
    api_host: str = "0.0.0.0"
    api_port: int = 8901
    api_external_url: str = ""
    api_token: str = ""
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""


@dataclass
class DatabaseConfig:
    type: str = "sqlite"
    path: str = "data/ftseek_us.db"
    backup_enabled: bool = True
    backup_interval_hours: int = 24


# ============================================================================
# 配置加载器
# ============================================================================

class ConfigLoader:
    """统一配置加载器"""
    
    _instance = None
    _config_path = None
    _raw_config = {}
    
    def __init__(self, config_path: str = None):
        if config_path is None:
            self._config_path = Path(__file__).resolve().parent / "unified_config.yaml"
        else:
            self._config_path = Path(config_path)
        
        if not self._config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self._config_path}")
        
        self._load()
    
    @classmethod
    def get_instance(cls, config_path: str = None) -> 'ConfigLoader':
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance
    
    def _resolve_env_vars(self, value: Any) -> Any:
        """解析环境变量占位符 ${VAR}"""
        if isinstance(value, str):
            pattern = r'\$\{([^}]+)\}'
            def replacer(match):
                env_var = match.group(1)
                return os.getenv(env_var, match.group(0))
            return re.sub(pattern, replacer, value)
        return value
    
    def _resolve_dict_env(self, d: dict) -> dict:
        """递归解析字典中的环境变量"""
        result = {}
        for k, v in d.items():
            if isinstance(v, dict):
                result[k] = self._resolve_dict_env(v)
            else:
                result[k] = self._resolve_env_vars(v)
        return result
    
    def _load(self):
        """加载配置文件"""
        with open(self._config_path, 'r', encoding='utf-8') as f:
            raw = yaml.safe_load(f)
        
        # 解析环境变量
        self._raw_config = self._resolve_dict_env(raw)
        logger.info(f"✅ 统一配置已加载: {self._config_path}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持点式路径 (如 'risk.signal_quality.min_confidence')"""
        keys = key.split('.')
        value = self._raw_config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    # ------------------------------------------------------------------
    # 兼容旧接口 (供 config_us.py 风格调用)
    # ------------------------------------------------------------------
    
    @property
    def WATCH_LIST_US(self) -> list:
        stocks = self.get('watchlist.us_stocks', [])
        return [s['code'] for s in stocks if s.get('enabled', False)]
    
    @property
    def DECISION_CONFIG(self) -> dict:
        return {
            'SIGNAL_MIN_SCORE': self.get('decision.signal_thresholds.min_score', 50),
            'SIGNAL_MEDIUM_SCORE': self.get('decision.signal_thresholds.medium_score', 70),
            'SIGNAL_HIGH_SCORE': self.get('decision.signal_thresholds.high_score', 85),
            'POSITION_RATIO_LOW': self.get('decision.position_ratios.low', 0.25),
            'POSITION_RATIO_MEDIUM': self.get('decision.position_ratios.medium', 0.50),
            'POSITION_RATIO_HIGH': self.get('decision.position_ratios.high', 1.00),
            'SIGNAL_COOLDOWN': self.get('strategy.signal_cooldown', 60),  # 2026-06-13: 统一读 SSOT
            'SIGNAL_EXPIRE_SECONDS': self.get('decision.signal_timing.expire_seconds', 300),
            'STRATEGY_WEIGHTS': self.get('decision.strategy_weights', {}),
        }
    
    @property
    def RISK_CONFIG(self) -> dict:
        return {
            'MAX_DAILY_DRAWDOWN': self.get('risk.account.max_daily_drawdown', 0.03),
            'MAX_TOTAL_DRAWDOWN': self.get('risk.account.max_total_drawdown', 0.08),
            'MAX_POSITION_PER_SYMBOL': self.get('risk.position.max_per_symbol', 0.25),
            'MAX_POSITION_PER_DIRECTION': self.get('risk.position.max_per_direction', 0.65),
            'MAX_TOTAL_POSITION': self.get('risk.position.max_total', 0.80),
            'MAX_DAILY_TRADES': self.get('risk.trading.max_daily_trades', 500),
            'MAX_ORDER_AMOUNT': self.get('risk.trading.max_order_amount', 120000),
            'MIN_CONFIDENCE': self.get('risk.signal_quality.min_confidence', 0.70),
            'MIN_CONSENSUS': self.get('risk.signal_quality.min_consensus', 0.3),
            'SIGNAL_MIN_SCORE': self.get('risk.signal_quality.min_score', 60),
            'DEFAULT_STOP_LOSS': self.get('risk.stop_loss.default', -0.06),
            'DEFAULT_TAKE_PROFIT_1': self.get('risk.take_profit.tier1', 0.08),
            'DEFAULT_TAKE_PROFIT_2': self.get('risk.take_profit.tier2', 0.15),
            'DEFAULT_TAKE_PROFIT_3': self.get('risk.take_profit.tier3', 0.25),
            'TIME_STOP_HOURS': self.get('risk.stop_loss.time_stop_hours', 72),
            'CIRCUIT_BREAKER': self.get('risk.circuit_breaker', {}),
            # OPTIONS_CONFIG 已删除 (2026-06-20): 零消费者。期权已永久禁用,
            # 活跃的 options.short_strategy/quick_exit 由各自模块直接读 yaml。
        }
    
    @property
    def EXECUTION_CONFIG(self) -> dict:
        return {
            # 订单类型选择
            "MARKET_ORDER_MIN_SCORE": self.get('execution.order_type_selection.market_order_min_score', 80),
            "LIMIT_ORDER_MAX_SCORE": self.get('execution.order_type_selection.limit_order_max_score', 60),
            "TWAP_ORDER_MIN_VALUE": self.get('execution.order_type_selection.twap_order_min_value', 100000),
            # 重试机制
            "MAX_RETRY_ATTEMPTS": self.get('execution.retry.max_attempts', 3),
            "RETRY_DELAY_SECONDS": self.get('execution.retry.delay_seconds', 5),
            # 滑点控制
            "MAX_SLIPPAGE_RATIO": self.get('execution.slippage.max_ratio', 0.005),
            "SLIPPAGE_CHECK_ENABLED": self.get('execution.slippage.check_enabled', True),
            # 订单状态追踪
            "ORDER_STATE_CHECK_INTERVAL": self.get('execution.order_tracking.check_interval', 2),
            "ORDER_TIMEOUT_SECONDS": self.get('execution.order_tracking.timeout_seconds', 300),
            # 幂等机制
            "IDEMPOTENCY_WINDOW_SECONDS": self.get('execution.idempotency.window_seconds', 3600),
        }

    @property
    def OBSERVABILITY_CONFIG(self) -> dict:
        return {
            # 监控指标收集间隔
            "METRICS_COLLECT_INTERVAL": self.get('observability.metrics_collect_interval', 10),
            # 告警阈值
            "ALERT_THRESHOLDS": self.get('observability.alert_thresholds', {
                "api_error_rate": 0.05,
                "market_data_latency": 5.0,
                "db_write_latency": 1.0,
                "task_backlog": 100,
            }),
            # 告警通知 (从 service.dingtalk 读取，已自动解析环境变量)
            "ALERT_PROVIDER": "dingtalk",
            "DINGTALK_WEBHOOK_URL_US": self.get('service.dingtalk.webhook_url', ''),
            "DINGTALK_SECRET_US": self.get('service.dingtalk.secret', ''),
            "FEISHU_WEBHOOK_URL_US": self.get('service.feishu.webhook_url', ''),
            "FEISHU_APP_ID": self.get('service.feishu.app_id', ''),
            "FEISHU_APP_SECRET": self.get('service.feishu.app_secret', ''),
            # 失败追踪
            "FAILURE_TRACKING": self.get('observability.failure_tracking', {
                "enabled": True,
                "retention_days": 30,
            }),
            # 审计日志
            "AUDIT_LOG": self.get('observability.audit_log', {
                "enabled": True,
                "retention_days": 90,
            }),
        }

    @property
    def DATABASE_CONFIG(self) -> dict:
        return {
            "type": self.get('database.type', 'sqlite'),
            "path": self.get('database.path', 'data/ftseek_us.db'),
            "backup_enabled": self.get('database.backup_enabled', True),
            "backup_interval_hours": self.get('database.backup_interval_hours', 24),
        }

    @property
    def LOGGING_CONFIG(self) -> dict:
        return {
            "level": self.get('logging.level', 'INFO'),
            "format": self.get('logging.format', '%(asctime)s [%(levelname)s] %(name)s: %(message)s'),
            "file": self.get('logging.file', 'logs/ftseek_us.log'),
            "max_size_mb": self.get('logging.max_size_mb', 10),
            "backup_count": self.get('logging.backup_count', 5),
            "rotation": self.get('logging.rotation', 'daily'),
        }

    @property
    def AGENT_CONFIG(self) -> dict:
        return {
            "main_agent": self.get('agents.main_agent', {}),
            "decision_agent": self.get('agents.decision_agent', {}),
            "risk_agent": self.get('agents.risk_agent', {}),
            "execution_agent": self.get('agents.execution_agent', {}),
            "observability_agent": self.get('agents.observability_agent', {}),
        }

    @property
    def FEATURE_FLAGS(self) -> dict:
        return {
            "ENABLE_DECISION_ENGINE": self.get('features.decision_engine', True),
            "ENABLE_RISK_ENGINE": self.get('features.risk_engine', True),
            "ENABLE_EXECUTION_ENGINE": self.get('features.execution_engine', True),
            "ENABLE_OBSERVABILITY_ENGINE": self.get('features.observability_engine', True),
            "ENABLE_MULTI_AGENT": self.get('features.multi_agent', True),
            "ENABLE_SIMULATION_MODE": self.get('features.simulation_mode', True),
            "ENABLE_BACKTEST": self.get('features.backtest', True),
            "ENABLE_PAPER_TRADING": self.get('features.paper_trading', True),
        }

    def validate(self) -> list:
        """验证配置一致性"""
        errors = []
        warnings = []
        
        # 检查关键配置
        if not self.get('service.api.token'):
            errors.append("FTSEEK_US_API_TOKEN 未设置")
        
        # 检查阈值合理性
        min_conf = self.get('risk.signal_quality.min_confidence', 0.70)
        if min_conf < 0.3 or min_conf > 0.9:
            warnings.append(f"MIN_CONFIDENCE={min_conf} 超出合理范围 (0.3-0.9)")

        # 检查期权配置一致性
        opt_max_hours = self.get('options.max_holding_hours', 72)
        opt_profit = self.get('options.profit_target_pct', 0.5)
        opt_stop = self.get('options.stop_loss_pct', -0.3)
        
        if opt_profit <= abs(opt_stop):
            warnings.append(f"期权止盈 ({opt_profit}) <= 止损 ({opt_stop})，盈亏比不合理")
        
        for w in warnings:
            logger.warning(f"[配置警告] {w}")
        
        if errors:
            raise ValueError("配置验证失败:\n" + "\n".join(errors))
        
        return warnings


# ============================================================================
# 全局单例
# ============================================================================

def get_config(config_path: str = None) -> ConfigLoader:
    """获取全局配置实例"""
    return ConfigLoader.get_instance(config_path)
