"""
告警规则模块
负责定义和管理各种告警规则
"""
import logging
from typing import Dict, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("ftseek.notify.alert")


class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertType(Enum):
    """告警类型"""
    RISK_LIMIT = "risk_limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    CIRCUIT_BREAKER = "circuit_breaker"
    POSITION_LIMIT = "position_limit"
    DRAWDOWN_LIMIT = "drawdown_limit"
    MARKET_REGIME = "market_regime"
    SYSTEM_ERROR = "system_error"


@dataclass
class AlertRule:
    """告警规则"""
    alert_type: AlertType
    level: AlertLevel
    condition: str
    threshold: float
    message_template: str
    enabled: bool = True
    cooldown_seconds: int = 300  # 告警冷却时间 (秒)


class AlertManager:
    """告警管理器"""
    
    def __init__(self):
        """初始化告警管理器"""
        self._rules: Dict[AlertType, AlertRule] = {}
        self._last_alert_time: Dict[AlertType, float] = {}
        self._setup_default_rules()
    
    def _setup_default_rules(self):
        """设置默认告警规则"""
        self._rules = {
            AlertType.RISK_LIMIT: AlertRule(
                alert_type=AlertType.RISK_LIMIT,
                level=AlertLevel.WARNING,
                condition="risk_score > threshold",
                threshold=0.8,
                message_template="⚠️ 风控指标超限：风险分数={value:.2f}",
                enabled=True,
                cooldown_seconds=300,
            ),
            AlertType.STOP_LOSS: AlertRule(
                alert_type=AlertType.STOP_LOSS,
                level=AlertLevel.ERROR,
                condition="pnl_rate < -stop_loss_pct",
                threshold=-0.03,
                message_template="⚠️ 止损触发：{code} 盈亏={value:.2%}",
                enabled=True,
                cooldown_seconds=60,
            ),
            AlertType.TAKE_PROFIT: AlertRule(
                alert_type=AlertType.TAKE_PROFIT,
                level=AlertLevel.INFO,
                condition="pnl_rate > take_profit_pct",
                threshold=0.045,
                message_template="✅ 止盈触发：{code} 盈亏={value:.2%}",
                enabled=True,
                cooldown_seconds=60,
            ),
            AlertType.CIRCUIT_BREAKER: AlertRule(
                alert_type=AlertType.CIRCUIT_BREAKER,
                level=AlertLevel.CRITICAL,
                condition="circuit_breaker_triggered",
                threshold=0,
                message_template="🚨 熔断器触发：暂停交易 {duration}秒",
                enabled=True,
                cooldown_seconds=600,
            ),
            AlertType.POSITION_LIMIT: AlertRule(
                alert_type=AlertType.POSITION_LIMIT,
                level=AlertLevel.WARNING,
                condition="position_count > max_positions",
                threshold=5,
                message_template="⚠️ 持仓数量超限：当前={value} 上限={threshold}",
                enabled=True,
                cooldown_seconds=300,
            ),
            AlertType.DRAWDOWN_LIMIT: AlertRule(
                alert_type=AlertType.DRAWDOWN_LIMIT,
                level=AlertLevel.CRITICAL,
                condition="drawdown > max_drawdown",
                threshold=-0.15,
                message_template="🚨 回撤超限：当前回撤={value:.2%}",
                enabled=True,
                cooldown_seconds=600,
            ),
            AlertType.MARKET_REGIME: AlertRule(
                alert_type=AlertType.MARKET_REGIME,
                level=AlertLevel.WARNING,
                condition="regime == 'high_volatility' or regime == 'strong_down'",
                threshold=0,
                message_template="⚠️ 市场状态恶化：{regime}",
                enabled=True,
                cooldown_seconds=600,
            ),
            AlertType.SYSTEM_ERROR: AlertRule(
                alert_type=AlertType.SYSTEM_ERROR,
                level=AlertLevel.ERROR,
                condition="error_count > threshold",
                threshold=5,
                message_template="🚨 系统错误：{error_message}",
                enabled=True,
                cooldown_seconds=300,
            ),
        }
    
    def check_alert(self, alert_type: AlertType, value: float, context: Optional[Dict] = None) -> Optional[str]:
        """
        检查是否触发告警
        
        Args:
            alert_type: 告警类型
            value: 当前值
            context: 上下文信息 (可选)
        
        Returns:
            告警消息 (如果触发), 否则 None
        """
        rule = self._rules.get(alert_type)
        if not rule or not rule.enabled:
            return None
        
        # 检查冷却时间
        import time
        now = time.time()
        last_time = self._last_alert_time.get(alert_type, 0)
        if now - last_time < rule.cooldown_seconds:
            return None
        
        # 检查阈值
        triggered = False
        if alert_type in (AlertType.STOP_LOSS, AlertType.DRAWDOWN_LIMIT):
            triggered = value < rule.threshold
        elif alert_type in (AlertType.TAKE_PROFIT, AlertType.POSITION_LIMIT):
            triggered = value > rule.threshold
        elif alert_type == AlertType.RISK_LIMIT:
            triggered = value > rule.threshold
        else:
            triggered = value > rule.threshold
        
        if not triggered:
            return None
        
        # 更新告警时间
        self._last_alert_time[alert_type] = now
        
        # 构建告警消息
        context = context or {}
        message = rule.message_template.format(
            value=value,
            threshold=rule.threshold,
            **context
        )
        
        logger.warning(f"[告警] {alert_type.value}: {message}")
        return message
    
    def get_rule(self, alert_type: AlertType) -> Optional[AlertRule]:
        """获取告警规则"""
        return self._rules.get(alert_type)
    
    def update_rule(self, alert_type: AlertType, **kwargs):
        """更新告警规则"""
        rule = self._rules.get(alert_type)
        if rule:
            for key, value in kwargs.items():
                if hasattr(rule, key):
                    setattr(rule, key, value)
    
    def enable_alert(self, alert_type: AlertType):
        """启用告警"""
        rule = self._rules.get(alert_type)
        if rule:
            rule.enabled = True
    
    def disable_alert(self, alert_type: AlertType):
        """禁用告警"""
        rule = self._rules.get(alert_type)
        if rule:
            rule.enabled = False
    
    def get_all_rules(self) -> Dict[AlertType, AlertRule]:
        """获取所有告警规则"""
        return self._rules.copy()


# 单例实例
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """获取告警管理器单例"""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager
