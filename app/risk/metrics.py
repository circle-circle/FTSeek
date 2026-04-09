"""
风控指标模块
负责风险指标计算、限制配置、度量标准
"""
import logging
import os
import yaml
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger("ftseek.risk.metrics")
logger.propagate = False


@dataclass
class CheckResult:
    """风控检查结果"""
    passed: bool
    reason: str
    level: str = ""
    details: Dict = field(default_factory=dict)


@dataclass
class RiskCheckResult:
    """交易前风控检查结果"""
    passed: bool
    details: Dict[str, bool] = field(default_factory=dict)
    timestamp: Optional[datetime] = None


@dataclass
class RiskConfig:
    """风控配置"""
    # 交易模式
    trading_mode: str = "paper"
    initial_cash: float = 1000000.0
    
    # 信号级
    min_confidence: float = 0.60
    
    # 订单级
    max_single_position: float = 0.20
    max_single_loss: float = 0.10
    
    # 仓位级
    max_total_position: float = 0.80
    max_daily_trades: int = 10
    max_daily_loss: float = 0.05
    max_weekly_loss: float = 0.10
    
    # 账户级
    max_drawdown: float = 0.15
    consecutive_loss_limit: int = 5
    
    # 策略级
    strategy_min_trades: int = 20
    strategy_min_winrate: float = 0.40
    
    # 预警配置
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    
    @classmethod
    def from_yaml(cls, config_path: str) -> "RiskConfig":
        """从 YAML 配置文件加载"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            # 处理环境变量占位符
            def resolve_env(value):
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    env_var = value[2:-1]
                    return os.environ.get(env_var, "")
                return value
            
            def resolve_dict(d):
                if isinstance(d, dict):
                    return {k: resolve_dict(v) for k, v in d.items()}
                elif isinstance(d, list):
                    return [resolve_dict(item) for item in d]
                else:
                    return resolve_env(d)
            
            config = resolve_dict(config)
            
            risk_limits = config.get("risk_limits", {})
            alerts = config.get("alerts", {})
            trading = config.get("trading", {})
            
            return cls(
                trading_mode=trading.get("mode", "paper"),
                initial_cash=trading.get("paper_initial_cash", 1000000.0),
                min_confidence=risk_limits.get("min_confidence", 0.60),
                max_single_position=risk_limits.get("max_single_position", 0.20),
                max_single_loss=risk_limits.get("max_single_loss", 0.10),
                max_total_position=risk_limits.get("max_total_position", 0.80),
                max_daily_trades=risk_limits.get("max_daily_trades", 10),
                max_daily_loss=risk_limits.get("max_daily_loss", 0.05),
                max_weekly_loss=risk_limits.get("max_weekly_loss", 0.10),
                max_drawdown=risk_limits.get("max_drawdown", 0.15),
                consecutive_loss_limit=risk_limits.get("consecutive_loss_limit", 5),
                strategy_min_trades=risk_limits.get("strategy_min_trades", 20),
                strategy_min_winrate=risk_limits.get("strategy_min_winrate", 0.40),
                dingtalk_webhook=alerts.get("dingtalk_webhook", ""),
                dingtalk_secret=alerts.get("dingtalk_secret", ""),
            )
        except Exception as e:
            logger.warning(f"加载风控配置失败：{e}, 使用默认配置")
            return cls()


@dataclass
class RiskLimit:
    """风险限制"""
    # 单笔交易限制
    max_single_position_pct: float = 0.20  # 最大单笔仓位 20%
    max_single_loss_pct: float = 0.10      # 最大单笔亏损 10%
    
    # 总仓位限制
    max_total_position_pct: float = 0.80   # 最大总仓位 80%
    
    # 日/周限制
    max_daily_trades: int = 10
    max_daily_loss_pct: float = 0.05       # 最大日亏损 5%
    max_weekly_loss_pct: float = 0.10      # 最大周亏损 10%
    
    # 回撤限制
    max_drawdown_pct: float = 0.15         # 最大回撤 15%
    
    # 连亏限制
    consecutive_loss_limit: int = 5
    
    # 置信度限制
    min_confidence: float = 0.60
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'max_single_position_pct': self.max_single_position_pct,
            'max_single_loss_pct': self.max_single_loss_pct,
            'max_total_position_pct': self.max_total_position_pct,
            'max_daily_trades': self.max_daily_trades,
            'max_daily_loss_pct': self.max_daily_loss_pct,
            'max_weekly_loss_pct': self.max_weekly_loss_pct,
            'max_drawdown_pct': self.max_drawdown_pct,
            'consecutive_loss_limit': self.consecutive_loss_limit,
            'min_confidence': self.min_confidence,
        }


@dataclass
class RiskMetrics:
    """风险指标"""
    # 账户指标
    total_capital: float = 0.0
    available_cash: float = 0.0
    total_position_value: float = 0.0
    position_ratio: float = 0.0
    
    # 盈亏指标
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    weekly_pnl: float = 0.0
    weekly_pnl_pct: float = 0.0
    
    # 交易统计
    total_trades: int = 0
    daily_trades: int = 0
    weekly_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    
    # 风险指标
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    consecutive_losses: int = 0
    sharpe_ratio: float = 0.0
    
    # 时间戳
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'total_capital': self.total_capital,
            'available_cash': self.available_cash,
            'total_position_value': self.total_position_value,
            'position_ratio': self.position_ratio,
            'total_pnl': self.total_pnl,
            'total_pnl_pct': self.total_pnl_pct,
            'daily_pnl': self.daily_pnl,
            'daily_pnl_pct': self.daily_pnl_pct,
            'weekly_pnl': self.weekly_pnl,
            'weekly_pnl_pct': self.weekly_pnl_pct,
            'total_trades': self.total_trades,
            'daily_trades': self.daily_trades,
            'weekly_trades': self.weekly_trades,
            'win_count': self.win_count,
            'loss_count': self.loss_count,
            'win_rate': self.win_rate,
            'max_drawdown': self.max_drawdown,
            'current_drawdown': self.current_drawdown,
            'consecutive_losses': self.consecutive_losses,
            'sharpe_ratio': self.sharpe_ratio,
            'timestamp': self.timestamp.isoformat(),
        }


class StopLossType(Enum):
    """止损类型"""
    FIXED = "fixed"              # 固定百分比止损
    TRAILING = "trailing"        # 追踪止损
    TIME_BASED = "time_based"    # 时间止损
    TECHNICAL = "technical"      # 技术位止损
    VOLATILITY = "volatility"    # 波动率止损


# 工厂函数
def create_risk_config(config_path: Optional[str] = None) -> RiskConfig:
    """
    创建风控配置
    
    Args:
        config_path: 配置文件路径
    
    Returns:
        RiskConfig 实例
    """
    if config_path and os.path.exists(config_path):
        return RiskConfig.from_yaml(config_path)
    return RiskConfig()


def create_risk_metrics() -> RiskMetrics:
    """
    创建风控指标
    
    Returns:
        RiskMetrics 实例
    """
    return RiskMetrics()


def create_risk_limits(**kwargs) -> RiskLimit:
    """
    创建风险限制
    
    Args:
        **kwargs: 覆盖默认值
    
    Returns:
        RiskLimit 实例
    """
    limits = RiskLimit()
    for key, value in kwargs.items():
        if hasattr(limits, key):
            setattr(limits, key, value)
    return limits
