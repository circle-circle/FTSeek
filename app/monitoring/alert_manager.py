#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 告警管理器

功能：
1. 告警规则配置和管理
2. 分级告警 (INFO/WARNING/CRITICAL)
3. 告警冷却机制 (防止告警风暴)
4. 飞书告警推送集成
5. 告警历史记录

预期收益：
- 故障发现时间 30 分钟 → 2 分钟
- 告警准确率提升
"""

import time
import json
import hashlib
import threading
import requests
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum
import logging
import yaml

# 导入统一日志配置
from ..core.logging_config import setup_logging, trace_context

logger = setup_logging("monitoring.alerts")


# ============================================================
# 告警级别定义
# ============================================================
class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"           # 信息
    WARNING = "warning"     # 警告
    CRITICAL = "critical"   # 严重


@dataclass
class AlertRule:
    """告警规则"""
    name: str
    metric: str
    condition: str  # ">", "<", ">=", "<=", "==", "!="
    threshold: float
    level: AlertLevel
    message: str
    cooldown_seconds: int = 3600  # 冷却时间 (默认 1 小时)
    enabled: bool = True
    contacts: List[str] = field(default_factory=list)
    
    def check(self, value: float) -> bool:
        """检查是否触发告警"""
        if self.condition == ">":
            return value > self.threshold
        elif self.condition == "<":
            return value < self.threshold
        elif self.condition == ">=":
            return value >= self.threshold
        elif self.condition == "<=":
            return value <= self.threshold
        elif self.condition == "==":
            return value == self.threshold
        elif self.condition == "!=":
            return value != self.threshold
        return False


@dataclass
class AlertEvent:
    """告警事件"""
    id: str
    rule_name: str
    metric: str
    value: Any
    threshold: float
    level: AlertLevel
    message: str
    timestamp: str
    acknowledged: bool = False
    resolved: bool = False
    resolved_at: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        # Convert AlertLevel enum to string for JSON serialization
        if isinstance(result.get('level'), AlertLevel):
            result['level'] = result['level'].value
        return result


# ============================================================
# 默认告警规则
# ============================================================
DEFAULT_ALERT_RULES = [
    AlertRule(
        name="opend_disconnected",
        metric="opend_connection_status",
        condition="<",
        threshold=0.5,
        level=AlertLevel.CRITICAL,
        message="OpenD 连接已断开！请立即检查！",
        cooldown_seconds=300,  # 5 分钟冷却
        contacts=["admin"]
    ),
    AlertRule(
        name="high_cpu_usage",
        metric="system_cpu_percent",
        condition=">",
        threshold=90.0,
        level=AlertLevel.WARNING,
        message="CPU 使用率过高 ({value}%)",
        cooldown_seconds=1800  # 30 分钟冷却
    ),
    AlertRule(
        name="high_memory_usage",
        metric="system_memory_percent",
        condition=">",
        threshold=85.0,
        level=AlertLevel.WARNING,
        message="内存使用率过高 ({value}%)",
        cooldown_seconds=1800
    ),
    AlertRule(
        name="low_trade_success_rate",
        metric="trade_success_rate",
        condition="<",
        threshold=90.0,
        level=AlertLevel.WARNING,
        message="交易成功率过低 ({value}%)",
        cooldown_seconds=3600
    ),
    AlertRule(
        name="high_disk_usage",
        metric="system_disk_percent",
        condition=">",
        threshold=90.0,
        level=AlertLevel.WARNING,
        message="磁盘使用率过高 ({value}%)",
        cooldown_seconds=3600
    ),
]


# ============================================================
# 飞书告警推送
# ============================================================
class FeishuNotifier:
    """飞书告警推送"""
    
    def __init__(self, webhook_url: str = None, secret: str = None):
        """
        初始化飞书推送
        
        Args:
            webhook_url: 飞书机器人 Webhook URL
            secret: 飞书签名密钥 (可选)
        """
        self.webhook_url = webhook_url
        self.secret = secret
        self.timeout = 10
        
        logger.info(f"飞书推送初始化 [webhook={bool(webhook_url)}]")
    
    def _generate_sign(self, timestamp: str) -> str:
        """生成飞书签名"""
        if not self.secret:
            return ""
        
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hashlib.sha256(string_to_sign.encode('utf-8')).digest()
        sign = base64.b64encode(hmac_code).decode('utf-8')
        return sign
    
    def send_alert(self, alert: AlertEvent, contacts: List[str] = None) -> bool:
        """
        发送告警到飞书
        
        Args:
            alert: 告警事件
            contacts: 联系人列表 (可选)
        
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            logger.warning("飞书 Webhook URL 未配置，跳过告警推送")
            return False
        
        # 构建告警消息
        level_colors = {
            AlertLevel.INFO: "blue",
            AlertLevel.WARNING: "orange",
            AlertLevel.CRITICAL: "red"
        }
        
        level_emojis = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.CRITICAL: "🚨"
        }
        
        color = level_colors.get(alert.level, "blue")
        emoji = level_emojis.get(alert.level, "📢")
        
        # 构建消息卡片
        card_content = {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "template": color,
                "title": {
                    "tag": "plain_text",
                    "content": f"{emoji} FTSeek 告警通知"
                }
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**告警规则**: {alert.rule_name}\n**告警级别**: {alert.level.value.upper()}\n**指标**: {alert.metric}\n**当前值**: {alert.value}\n**阈值**: {alert.threshold}\n**时间**: {alert.timestamp}"
                    }
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**详情**: {alert.message}"
                    }
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "查看仪表盘"
                            },
                            "url": "http://localhost:8899/dashboard",
                            "type": "default"
                        }
                    ]
                }
            ]
        }
        
        # 发送请求
        try:
            payload = {
                "msg_type": "interactive",
                "card": card_content
            }
            
            headers = {"Content-Type": "application/json"}
            
            response = requests.post(
                self.webhook_url,
                json=payload,
                headers=headers,
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 0 or result.get("StatusCode") == 0:
                    logger.info(f"飞书告警发送成功：{alert.rule_name}")
                    return True
                else:
                    logger.error(f"飞书告警发送失败：{result}")
            else:
                logger.error(f"飞书告警 HTTP 错误：{response.status_code}")
        
        except Exception as e:
            logger.error(f"飞书告警发送异常：{e}")
        
        return False
    
    def send_markdown(self, title: str, content: str) -> bool:
        """发送 Markdown 消息"""
        if not self.webhook_url:
            return False
        
        try:
            payload = {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "template": "blue",
                        "title": {"tag": "plain_text", "content": title}
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {"tag": "lark_md", "content": content}
                        }
                    ]
                }
            }
            
            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout
            )
            
            return response.status_code == 200
        
        except Exception as e:
            logger.error(f"飞书 Markdown 发送异常：{e}")
            return False


# ============================================================
# 告警管理器
# ============================================================
class AlertManager:
    """
    告警管理器
    
    功能：
    1. 告警规则管理
    2. 告警触发和冷却
    3. 多渠道告警推送
    4. 告警历史记录
    """
    
    def __init__(
        self,
        rules_config_path: Path = None,
        feishu_webhook: str = None,
        feishu_secret: str = None,
        storage_dir: Path = None
    ):
        """
        初始化告警管理器
        
        Args:
            rules_config_path: 告警规则配置文件路径
            feishu_webhook: 飞书 Webhook URL
            feishu_secret: 飞书签名密钥
            storage_dir: 告警历史存储目录
        """
        self.rules_config_path = rules_config_path or Path('/opt/ftseek/FTSeek/config/alert_rules.yaml')
        self.storage_dir = storage_dir or Path('/opt/ftseek/FTSeek/data/alerts')
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        # 告警规则
        self._rules: Dict[str, AlertRule] = {r.name: r for r in DEFAULT_ALERT_RULES}
        
        # 告警冷却记录
        self._cooldowns: Dict[str, float] = {}
        
        # 告警历史
        self._alerts: List[AlertEvent] = []
        self._max_history = 1000
        
        # 飞书推送
        self.feishu_notifier = FeishuNotifier(feishu_webhook, feishu_secret)
        
        # 告警回调
        self._callbacks: List[Callable] = []
        
        # 锁
        self._lock = threading.RLock()
        
        # 加载规则
        self._load_rules()
        
        logger.info(f"告警管理器初始化完成 [rules={len(self._rules)}]")
    
    # ============================================================
    # 规则管理
    # ============================================================
    
    def _load_rules(self):
        """加载告警规则配置"""
        if not self.rules_config_path.exists():
            logger.info(f"告警规则配置文件不存在，使用默认规则：{self.rules_config_path}")
            self._save_rules()
            return
        
        try:
            with open(self.rules_config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            if config and 'rules' in config:
                loaded_rules = {}
                for rule_config in config['rules']:
                    rule = AlertRule(
                        name=rule_config['name'],
                        metric=rule_config['metric'],
                        condition=rule_config['condition'],
                        threshold=rule_config['threshold'],
                        level=AlertLevel(rule_config.get('level', 'warning')),
                        message=rule_config['message'],
                        cooldown_seconds=rule_config.get('cooldown_seconds', 3600),
                        enabled=rule_config.get('enabled', True),
                        contacts=rule_config.get('contacts', [])
                    )
                    loaded_rules[rule.name] = rule
                
                self._rules = loaded_rules
                logger.info(f"✅ 加载告警规则：{len(self._rules)} 条")
        
        except Exception as e:
            logger.error(f"❌ 加载告警规则失败：{e}")
    
    def _save_rules(self):
        """保存告警规则配置"""
        try:
            config = {
                'version': '1.0',
                'updated': datetime.now().isoformat(),
                'rules': [asdict(rule) for rule in self._rules.values()]
            }
            
            with open(self.rules_config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            
            logger.info(f"告警规则已保存：{self.rules_config_path}")
        
        except Exception as e:
            logger.error(f"保存告警规则失败：{e}")
    
    def add_rule(self, rule: AlertRule):
        """添加告警规则"""
        with self._lock:
            self._rules[rule.name] = rule
            self._save_rules()
            logger.info(f"添加告警规则：{rule.name}")
    
    def remove_rule(self, name: str):
        """删除告警规则"""
        with self._lock:
            if name in self._rules:
                del self._rules[name]
                self._save_rules()
                logger.info(f"删除告警规则：{name}")
    
    def enable_rule(self, name: str):
        """启用告警规则"""
        with self._lock:
            if name in self._rules:
                self._rules[name].enabled = True
                self._save_rules()
    
    def disable_rule(self, name: str):
        """禁用告警规则"""
        with self._lock:
            if name in self._rules:
                self._rules[name].enabled = False
                self._save_rules()
    
    # ============================================================
    # 告警检查
    # ============================================================
    
    def check_metric(self, metric_name: str, value: float) -> List[AlertEvent]:
        """
        检查指标是否触发告警
        
        Args:
            metric_name: 指标名称
            value: 指标值
        
        Returns:
            触发的告警事件列表
        """
        triggered_alerts = []
        
        with self._lock:
            for rule in self._rules.values():
                if not rule.enabled:
                    continue
                
                if rule.metric != metric_name:
                    continue
                
                # 检查是否触发
                if rule.check(value):
                    # 检查冷却
                    if self._is_in_cooldown(rule.name):
                        logger.debug(f"告警冷却中，跳过：{rule.name}")
                        continue
                    
                    # 创建告警事件
                    alert = self._create_alert(rule, value)
                    triggered_alerts.append(alert)
                    
                    # 设置冷却
                    self._set_cooldown(rule.name, rule.cooldown_seconds)
                    
                    # 发送告警
                    self._send_alert(alert, rule.contacts)
        
        return triggered_alerts
    
    def _is_in_cooldown(self, rule_name: str) -> bool:
        """检查是否在冷却期内"""
        if rule_name not in self._cooldowns:
            return False
        
        cooldown_end = self._cooldowns[rule_name]
        return time.time() < cooldown_end
    
    def _set_cooldown(self, rule_name: str, seconds: int):
        """设置冷却时间"""
        self._cooldowns[rule_name] = time.time() + seconds
        logger.debug(f"告警冷却设置：{rule_name} ({seconds}s)")
    
    def _create_alert(self, rule: AlertRule, value: float) -> AlertEvent:
        """创建告警事件"""
        alert_id = hashlib.md5(
            f"{rule.name}:{value}:{time.time()}".encode()
        ).hexdigest()[:12]
        
        message = rule.message.format(value=value, threshold=rule.threshold)
        
        alert = AlertEvent(
            id=alert_id,
            rule_name=rule.name,
            metric=rule.metric,
            value=value,
            threshold=rule.threshold,
            level=rule.level,
            message=message,
            timestamp=datetime.now().isoformat()
        )
        
        # 保存到历史
        self._alerts.append(alert)
        if len(self._alerts) > self._max_history:
            self._alerts = self._alerts[-self._max_history:]
        
        return alert
    
    def _send_alert(self, alert: AlertEvent, contacts: List[str] = None):
        """发送告警"""
        logger.warning(f"🚨 告警触发：{alert.rule_name} [{alert.level.value}] - {alert.message}")
        
        # 飞书推送
        if alert.level in [AlertLevel.WARNING, AlertLevel.CRITICAL]:
            self.feishu_notifier.send_alert(alert, contacts)
        
        # 回调通知
        for callback in self._callbacks:
            try:
                callback(alert)
            except Exception as e:
                logger.error(f"告警回调失败：{e}")
        
        # 保存告警历史
        self._save_alert_history()
    
    def _save_alert_history(self):
        """保存告警历史"""
        try:
            history_file = self.storage_dir / "alert_history.json"
            
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump([a.to_dict() for a in self._alerts[-100:]], f, ensure_ascii=False, indent=2)
        
        except Exception as e:
            logger.error(f"保存告警历史失败：{e}")
    
    # ============================================================
    # 告警管理
    # ============================================================
    
    def acknowledge_alert(self, alert_id: str):
        """确认告警"""
        with self._lock:
            for alert in self._alerts:
                if alert.id == alert_id:
                    alert.acknowledged = True
                    logger.info(f"告警已确认：{alert_id}")
                    break
    
    def resolve_alert(self, alert_id: str):
        """解决告警"""
        with self._lock:
            for alert in self._alerts:
                if alert.id == alert_id:
                    alert.resolved = True
                    alert.resolved_at = datetime.now().isoformat()
                    logger.info(f"告警已解决：{alert_id}")
                    break
    
    def get_active_alerts(self) -> List[AlertEvent]:
        """获取未解决的告警"""
        return [a for a in self._alerts if not a.resolved]
    
    def get_alert_history(
        self,
        start_time: datetime = None,
        level: AlertLevel = None,
        limit: int = 100
    ) -> List[AlertEvent]:
        """获取告警历史"""
        alerts = self._alerts.copy()
        
        if start_time:
            alerts = [
                a for a in alerts 
                if datetime.fromisoformat(a.timestamp) >= start_time
            ]
        
        if level:
            alerts = [a for a in alerts if a.level == level]
        
        return alerts[-limit:]
    
    def register_callback(self, callback: Callable):
        """注册告警回调"""
        self._callbacks.append(callback)
    
    # ============================================================
    # 状态和统计
    # ============================================================
    
    def get_status(self) -> Dict[str, Any]:
        """获取告警管理器状态"""
        active_alerts = self.get_active_alerts()
        
        return {
            "rules_count": len(self._rules),
            "enabled_rules": sum(1 for r in self._rules.values() if r.enabled),
            "active_alerts": len(active_alerts),
            "total_alerts": len(self._alerts),
            "storage_dir": str(self.storage_dir)
        }
    
    def get_stats(self, hours: int = 24) -> Dict[str, Any]:
        """获取告警统计"""
        cutoff = datetime.now() - timedelta(hours=hours)
        recent_alerts = [
            a for a in self._alerts 
            if datetime.fromisoformat(a.timestamp) >= cutoff
        ]
        
        by_level = {}
        by_rule = {}
        
        for alert in recent_alerts:
            level = alert.level.value
            by_level[level] = by_level.get(level, 0) + 1
            
            rule = alert.rule_name
            by_rule[rule] = by_rule.get(rule, 0) + 1
        
        return {
            "period_hours": hours,
            "total": len(recent_alerts),
            "by_level": by_level,
            "by_rule": by_rule
        }


# ============================================================
# 全局单例
# ============================================================
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """获取全局告警管理器实例"""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


def init_alert_manager(
    rules_config_path: Path = None,
    feishu_webhook: str = None,
    feishu_secret: str = None
) -> AlertManager:
    """初始化全局告警管理器"""
    global _alert_manager
    _alert_manager = AlertManager(
        rules_config_path=rules_config_path,
        feishu_webhook=feishu_webhook,
        feishu_secret=feishu_secret
    )
    return _alert_manager


# ============================================================
# 便捷函数
# ============================================================
def check_alert(metric_name: str, value: float) -> List[AlertEvent]:
    """检查指标告警"""
    return get_alert_manager().check_metric(metric_name, value)


def send_manual_alert(
    level: AlertLevel,
    title: str,
    message: str
) -> bool:
    """发送手动告警"""
    manager = get_alert_manager()
    
    # 创建临时告警事件
    alert = AlertEvent(
        id=hashlib.md5(f"{title}:{time.time()}".encode()).hexdigest()[:12],
        rule_name="manual",
        metric="manual",
        value=0,
        threshold=0,
        level=level,
        message=message,
        timestamp=datetime.now().isoformat()
    )
    
    return manager.feishu_notifier.send_alert(alert)


# ============================================================
# 主函数 (测试用)
# ============================================================
if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='FTSeek 告警管理器')
    parser.add_argument('--test', action='store_true', help='测试告警功能')
    parser.add_argument('--status', action='store_true', help='显示状态')
    parser.add_argument('--init-rules', action='store_true', help='初始化告警规则')
    
    args = parser.parse_args()
    
    manager = get_alert_manager()
    
    if args.test:
        print("\n🧪 测试告警功能:")
        
        # 测试告警触发
        print("\n1. 测试 OpenD 断线告警 (CRITICAL):")
        alerts = manager.check_metric("opend_connection_status", 0)
        print(f"   触发告警：{len(alerts)} 条")
        
        print("\n2. 测试 CPU 使用率告警 (WARNING):")
        alerts = manager.check_metric("system_cpu_percent", 95.0)
        print(f"   触发告警：{len(alerts)} 条")
        
        print("\n3. 测试冷却机制 (重复告警应被跳过):")
        alerts = manager.check_metric("system_cpu_percent", 95.0)
        print(f"   触发告警：{len(alerts)} 条 (应为 0)")
        
        # 显示活跃告警
        print("\n📋 活跃告警:")
        for alert in manager.get_active_alerts():
            print(f"   [{alert.level.value}] {alert.rule_name}: {alert.message}")
        
        # 显示统计
        print("\n📊 告警统计:")
        stats = manager.get_stats(hours=24)
        print(f"   24 小时内告警总数：{stats['total']}")
        print(f"   按级别：{stats['by_level']}")
    
    elif args.init_rules:
        print("初始化告警规则配置...")
        manager._save_rules()
        print(f"✅ 告警规则已保存：{manager.rules_config_path}")
    
    elif args.status:
        status = manager.get_status()
        print("📊 告警管理器状态:")
        print(f"  规则数量：{status['rules_count']}")
        print(f"  启用规则：{status['enabled_rules']}")
        print(f"  活跃告警：{status['active_alerts']}")
        print(f"  总告警数：{status['total_alerts']}")
    
    else:
        parser.print_help()
