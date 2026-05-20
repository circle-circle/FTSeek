#!/usr/bin/env python3
"""
FTSeek 异常分类器 (Anomaly Classifier)

职责:
1. 接收系统异常事件
2. 按交易价值分为 P0/P1/P2 三级
3. 判断是否影响今日交易
4. 写入异常队列 (SQLite)
5. 触发通知 + Agent 处理

设计原则:
- 渐进式叠加，不破坏现有告警系统
- 基于交易影响分类，不是技术严重性
- 即发即忘，不阻塞主流程

使用方式:
    from app.anomaly_classifier import report_anomaly
    
    try:
        ...
    except Exception as e:
        report_anomaly(
            source="opend",
            anomaly_type="connection_lost",
            message=str(e),
            impact_scope="all_trading",
        )
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger("ftseek.anomaly_classifier")


# ============================================================
#  异常级别定义
# ============================================================

class AnomalySeverity(Enum):
    """异常严重级别 (按交易价值分类)"""
    P0 = "P0"  # 🚨 阻断交易 - 必须立即处理
    P1 = "P1"  # ⚠️  影响质量 - 需要确认/修复
    P2 = "P2"  # ℹ️  信息记录 - 仅观察


@dataclass
class AnomalyEvent:
    """异常事件"""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    source: str = ""           # 来源模块 (e.g., "opend", "risk", "signal")
    anomaly_type: str = ""     # 异常类型 (e.g., "connection_lost", "lot_size_error")
    severity: AnomalySeverity = AnomalySeverity.P2
    message: str = ""          # 异常描述
    impact_scope: str = ""     # 影响范围 (e.g., "all_trading", "single_symbol", "data_only")
    affected_symbols: List[str] = field(default_factory=list)
    
    # 分类结果
    trading_impact: str = ""   # 对交易的影响: "block", "degrade", "observe"
    auto_fixable: bool = False # 是否可自动修复
    suggested_action: str = "" # 建议行动
    
    # 状态
    status: str = "pending"    # pending / confirmed / fixed / ignored / escalated
    confirmed_by: str = ""     # "agent" / "human" / "auto"
    resolution: str = ""       # 处理结果
    
    # 关联
    related_alert_id: str = "" # 关联的告警 ID
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d['severity'] = self.severity.value
        return d
    
    @classmethod
    def from_dict(cls, d: dict) -> 'AnomalyEvent':
        d = d.copy()
        d['severity'] = AnomalySeverity(d.get('severity', 'P2'))
        return cls(**d)


# ============================================================
#  分类规则 (核心：按交易价值分类)
# ============================================================

CLASSIFICATION_RULES = {
    # ═══════════════════════════════════════════════════════════
    # P0: 阻断交易 (必须立即处理，否则不能交易)
    # ═══════════════════════════════════════════════════════════
    "opend_connection_lost": {
        "severity": AnomalySeverity.P0,
        "trading_impact": "block",
        "auto_fixable": True,
        "suggested_action": "尝试重启 OpenD 连接，若失败则停止交易",
    },
    "account_margin_call": {
        "severity": AnomalySeverity.P0,
        "trading_impact": "block",
        "auto_fixable": False,
        "suggested_action": "立即通知人工干预，停止所有新开仓",
    },
    "risk_limit_breached": {
        "severity": AnomalySeverity.P0,
        "trading_impact": "block",
        "auto_fixable": False,
        "suggested_action": "触发风控熔断，停止交易并通知",
    },
    "database_corruption": {
        "severity": AnomalySeverity.P0,
        "trading_impact": "block",
        "auto_fixable": False,
        "suggested_action": "停止交易，恢复数据库备份",
    },
    "position_sync_failed": {
        "severity": AnomalySeverity.P0,
        "trading_impact": "block",
        "auto_fixable": False,
        "suggested_action": "停止交易，手动对账持仓",
    },
    
    # ═══════════════════════════════════════════════════════════
    # P1: 影响质量 (可交易，但质量下降，需要确认)
    # ═══════════════════════════════════════════════════════════
    "lot_size_mismatch": {
        "severity": AnomalySeverity.P1,
        "trading_impact": "degrade",
        "auto_fixable": True,
        "suggested_action": "重新查询 lot_size，更新缓存",
    },
    "subscription_limit_reached": {
        "severity": AnomalySeverity.P1,
        "trading_impact": "degrade",
        "auto_fixable": False,
        "suggested_action": "清理不必要订阅，释放槽位",
    },
    "signal_delay": {
        "severity": AnomalySeverity.P1,
        "trading_impact": "degrade",
        "auto_fixable": False,
        "suggested_action": "检查数据源延迟，降级到缓存数据",
    },
    "partial_data_loss": {
        "severity": AnomalySeverity.P1,
        "trading_impact": "degrade",
        "auto_fixable": True,
        "suggested_action": "尝试重新获取数据，使用备用数据源",
    },
    "strategy_error": {
        "severity": AnomalySeverity.P1,
        "trading_impact": "degrade",
        "auto_fixable": False,
        "suggested_action": "检查策略逻辑，可能影响信号质量",
    },
    "order_rejected": {
        "severity": AnomalySeverity.P1,
        "trading_impact": "degrade",
        "auto_fixable": False,
        "suggested_action": "检查订单参数，可能需要手动下单",
    },
    
    # ═══════════════════════════════════════════════════════════
    # P2: 信息记录 (不影响交易，仅观察)
    # ═══════════════════════════════════════════════════════════
    "config_fallback": {
        "severity": AnomalySeverity.P2,
        "trading_impact": "observe",
        "auto_fixable": False,
        "suggested_action": "记录并观察，不影响交易",
    },
    "non_critical_timeout": {
        "severity": AnomalySeverity.P2,
        "trading_impact": "observe",
        "auto_fixable": True,
        "suggested_action": "重试一次，失败则忽略",
    },
    "degradation_activated": {
        "severity": AnomalySeverity.P2,
        "trading_impact": "observe",
        "auto_fixable": False,
        "suggested_action": "降级策略已激活，观察效果",
    },
    "cache_miss": {
        "severity": AnomalySeverity.P2,
        "trading_impact": "observe",
        "auto_fixable": True,
        "suggested_action": "重新加载缓存",
    },
    "log_rotation": {
        "severity": AnomalySeverity.P2,
        "trading_impact": "observe",
        "auto_fixable": False,
        "suggested_action": "正常日志轮转，无需处理",
    },
}


# ============================================================
#  异常分类器 (单例)
# ============================================================

class AnomalyClassifier:
    """异常分类器"""
    
    _instance = None
    _db_path: str = ""
    
    def __new__(cls, db_path: str = None):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, db_path: str = None):
        if self._initialized:
            return
        
        self._db_path = db_path or "/opt/ftseek/FTSeek/data/anomaly_queue.db"
        self._init_db()
        self._initialized = True
        logger.info(f"✅ AnomalyClassifier 已初始化: {self._db_path}")
    
    def _init_db(self):
        """初始化异常队列数据库"""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS anomalies (
                    event_id TEXT PRIMARY KEY,
                    timestamp REAL,
                    source TEXT,
                    anomaly_type TEXT,
                    severity TEXT,
                    message TEXT,
                    impact_scope TEXT,
                    affected_symbols TEXT,
                    trading_impact TEXT,
                    auto_fixable INTEGER,
                    suggested_action TEXT,
                    status TEXT DEFAULT 'pending',
                    confirmed_by TEXT DEFAULT '',
                    resolution TEXT DEFAULT '',
                    related_alert_id TEXT,
                    metadata TEXT DEFAULT '{}'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON anomalies(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_severity ON anomalies(severity)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON anomalies(timestamp)")
    
    def classify(self, event: AnomalyEvent) -> AnomalyEvent:
        """
        分类异常事件
        
        流程:
        1. 匹配预定义规则
        2. 无规则时使用默认分类
        3. 写入异常队列
        4. 触发通知 (P0/P1)
        """
        # 1. 匹配规则
        rule = CLASSIFICATION_RULES.get(event.anomaly_type)
        
        if rule:
            event.severity = rule["severity"]
            event.trading_impact = rule["trading_impact"]
            event.auto_fixable = rule["auto_fixable"]
            event.suggested_action = rule["suggested_action"]
        else:
            # 默认分类
            event.severity = AnomalySeverity.P2
            event.trading_impact = "observe"
            event.auto_fixable = False
            event.suggested_action = "观察，暂不处理"
            logger.debug(f"[AnomalyClassifier] 未匹配规则，使用默认分类: {event.anomaly_type}")
        
        # 2. 写入队列
        self._save(event)
        
        # 3. 记录日志
        severity_emoji = {"P0": "🚨", "P1": "⚠️", "P2": "ℹ️"}.get(event.severity.value, "ℹ️")
        logger.info(
            f"{severity_emoji} [{event.severity.value}] {event.source}/{event.anomaly_type}: "
            f"{event.message[:100]}"
        )
        
        return event
    
    def _save(self, event: AnomalyEvent):
        """保存异常事件到数据库"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO anomalies (
                    event_id, timestamp, source, anomaly_type, severity,
                    message, impact_scope, affected_symbols, trading_impact,
                    auto_fixable, suggested_action, status, confirmed_by,
                    resolution, related_alert_id, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id, event.timestamp, event.source, event.anomaly_type,
                event.severity.value, event.message, event.impact_scope,
                json.dumps(event.affected_symbols), event.trading_impact,
                int(event.auto_fixable), event.suggested_action, event.status,
                event.confirmed_by, event.resolution, event.related_alert_id,
                json.dumps(event.metadata)
            ))
    
    def get_pending(self, severity: AnomalySeverity = None, limit: int = 50) -> List[AnomalyEvent]:
        """获取待处理的异常"""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            if severity:
                rows = conn.execute(
                    "SELECT * FROM anomalies WHERE status='pending' AND severity=? ORDER BY timestamp DESC LIMIT ?",
                    (severity.value, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM anomalies WHERE status='pending' ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            
            return [self._row_to_event(r) for r in rows]
    
    def get_recent(self, hours: int = 24, limit: int = 20) -> List[AnomalyEvent]:
        """获取最近的异常"""
        cutoff = time.time() - (hours * 3600)
        
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM anomalies WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?",
                (cutoff, limit)
            ).fetchall()
            
            return [self._row_to_event(r) for r in rows]
    
    def update_status(self, event_id: str, status: str, confirmed_by: str = "", resolution: str = ""):
        """更新异常状态"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE anomalies SET status=?, confirmed_by=?, resolution=? WHERE event_id=?",
                (status, confirmed_by, resolution, event_id)
            )
    
    def get_stats(self, hours: int = 24) -> Dict:
        """获取异常统计"""
        cutoff = time.time() - (hours * 3600)
        
        with sqlite3.connect(self._db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM anomalies WHERE timestamp > ?", (cutoff,)
            ).fetchone()[0]
            
            by_severity = {}
            for sev in ["P0", "P1", "P2"]:
                count = conn.execute(
                    "SELECT COUNT(*) FROM anomalies WHERE timestamp > ? AND severity=?",
                    (cutoff, sev)
                ).fetchone()[0]
                by_severity[sev] = count
            
            by_status = {}
            for status in ["pending", "confirmed", "fixed", "ignored", "escalated"]:
                count = conn.execute(
                    "SELECT COUNT(*) FROM anomalies WHERE timestamp > ? AND status=?",
                    (cutoff, status)
                ).fetchone()[0]
                by_status[status] = count
            
            return {
                "total": total,
                "by_severity": by_severity,
                "by_status": by_status,
                "period_hours": hours
            }
    
    def _row_to_event(self, row: sqlite3.Row) -> AnomalyEvent:
        """将数据库行转换为异常事件"""
        d = dict(row)
        d['severity'] = AnomalySeverity(d['severity'])
        d['affected_symbols'] = json.loads(d['affected_symbols'] or '[]')
        d['auto_fixable'] = bool(d['auto_fixable'])
        d['metadata'] = json.loads(d['metadata'] or '{}')
        return AnomalyEvent(**d)
    
    def clear_old(self, days: int = 7):
        """清理旧异常记录"""
        cutoff = time.time() - (days * 86400)
        
        with sqlite3.connect(self._db_path) as conn:
            deleted = conn.execute(
                "DELETE FROM anomalies WHERE timestamp < ? AND status IN ('fixed', 'ignored')",
                (cutoff,)
            ).rowcount
            
            if deleted:
                logger.info(f"[AnomalyClassifier] 清理了 {deleted} 条旧异常记录")


# ============================================================
#  便捷函数 (供各模块调用)
# ============================================================

_classifier: Optional[AnomalyClassifier] = None


def get_classifier(db_path: str = None) -> AnomalyClassifier:
    """获取分类器单例"""
    global _classifier
    if _classifier is None:
        _classifier = AnomalyClassifier(db_path)
    return _classifier


def report_anomaly(
    source: str,
    anomaly_type: str,
    message: str,
    impact_scope: str = "",
    affected_symbols: List[str] = None,
    metadata: Dict = None,
    related_alert_id: str = "",
) -> AnomalyEvent:
    """
    报告异常 (供各模块调用)
    
    用法:
        from app.anomaly_classifier import report_anomaly
        
        try:
            ...
        except Exception as e:
            report_anomaly(
                source="opend",
                anomaly_type="connection_lost",
                message=str(e),
                impact_scope="all_trading",
            )
    """
    classifier = get_classifier()
    
    event = AnomalyEvent(
        source=source,
        anomaly_type=anomaly_type,
        message=message,
        impact_scope=impact_scope,
        affected_symbols=affected_symbols or [],
        metadata=metadata or {},
        related_alert_id=related_alert_id,
    )
    
    return classifier.classify(event)
