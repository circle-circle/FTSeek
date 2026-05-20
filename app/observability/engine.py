"""观测引擎主类"""
from typing import Dict, Any, List, Optional
from datetime import datetime
from app.core.engine import ObservabilityEngine as BaseObservabilityEngine
from app.core.events import EventStore
from app.core.models import Alert, FailureRecord, TradeEvent, AlertLevel
from .metrics_collector import MetricsCollector
from .alert_manager import AlertManager
from .failure_tracker import FailureTracker
from .audit_logger import AuditLogger


class ObservabilityEngine(BaseObservabilityEngine):
    """观测引擎主类"""
    
    def initialize(self):
        """初始化观测引擎"""
        obs_config = self.config.get("observability", {})
        
        # 指标收集器
        self.metrics_collector = MetricsCollector()
        
        # 告警管理器 (可选配置)
        provider = obs_config.get("ALERT_PROVIDER", "dingtalk")
        webhook = obs_config.get("DINGTALK_WEBHOOK_URL_US")
        secret = obs_config.get("DINGTALK_SECRET_US")
        
        if webhook and secret:
            self.alert_manager = AlertManager(
                provider=provider,
                dingtalk_webhook=webhook,
                dingtalk_secret=secret,
                feishu_app_id=obs_config.get("FEISHU_APP_ID"),
                feishu_app_secret=obs_config.get("FEISHU_APP_SECRET"),
            )
        else:
            self.alert_manager = None  # 告警为可选功能
        
        # 失败追踪器
        retention_days = obs_config.get("FAILURE_RETENTION_DAYS", 30)
        self.failure_tracker = FailureTracker(retention_days=retention_days)
        
        # 审计日志
        self.audit_logger = AuditLogger(
            log_file=obs_config.get("AUDIT_LOG_FILE", "logs/audit.log"),
            retention_days=obs_config.get("AUDIT_RETENTION_DAYS", 90),
        )
        
        self.enabled = True
    
    def shutdown(self):
        """关闭观测引擎"""
        self.enabled = False
    
    def collect_metrics(self) -> Dict[str, Any]:
        """收集监控指标"""
        system_metrics = self.metrics_collector.collect_system_metrics()
        trading_metrics = self.metrics_collector.collect_trading_metrics()
        data_source_metrics = self.metrics_collector.collect_data_source_metrics()
        risk_metrics = self.metrics_collector.collect_risk_metrics()
        voting_metrics = self.metrics_collector.collect_voting_metrics()
        
        return {
            **system_metrics,
            **trading_metrics,
            "data_sources": data_source_metrics,
            "risk": risk_metrics,
            "voting": voting_metrics,
        }
    
    def check_alerts(self, metrics: Dict[str, Any]) -> List[Alert]:
        """检查告警条件"""
        alerts = []
        alert_thresholds = self.config.get("observability", {}).get("ALERT_THRESHOLDS", {})
        
        # CPU 告警
        if metrics.get("cpu_percent", 0) > alert_thresholds.get("cpu_percent", 90):
            alerts.append(Alert(
                level=AlertLevel.WARN,
                title="CPU 使用率过高",
                message=f"CPU 使用率：{metrics['cpu_percent']:.1f}%",
                category="system",
            ))
        
        # 内存告警
        if metrics.get("memory_percent", 0) > alert_thresholds.get("memory_percent", 90):
            alerts.append(Alert(
                level=AlertLevel.WARN,
                title="内存使用率过高",
                message=f"内存使用率：{metrics['memory_percent']:.1f}%",
                category="system",
            ))
        
        return alerts
    
    def track_failure(self, failure: FailureRecord):
        """追踪失败"""
        self.failure_tracker.record_failure(
            failure_type=failure.failure_type,
            symbol=failure.symbol,
            reason=failure.reason,
            signal_id=failure.signal_id,
            order_id=failure.order_id,
        )
        
        # 发送告警
        if failure.triggers_circuit_breaker:
            self.alert_manager.send_critical(
                title="熔断器触发",
                content=failure.reason,
                symbol=failure.symbol,
                failure_type=failure.failure_type.value,
            )
    
    def get_health_dashboard(self) -> Dict[str, Any]:
        """获取健康状态 (内部使用，无 API)"""
        return {
            "healthy": self.is_healthy(),
            "metrics": self.collect_metrics(),
            "failures_24h": self.failure_tracker.get_statistics()["recent_24h"],
        }
    
    def record_audit_event(
        self,
        event_type: str,
        user_id: Optional[str] = None,
        **kwargs
    ):
        """记录审计事件"""
        self.audit_logger.record_event(event_type, user_id, **kwargs)
    
    def replay_trades(self, start_time: datetime, end_time: datetime) -> List[TradeEvent]:
        """回放交易"""
        return self.event_store.query_events(
            start_time=start_time,
            end_time=end_time,
        )
