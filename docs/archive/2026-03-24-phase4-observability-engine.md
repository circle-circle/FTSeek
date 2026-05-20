# Phase 4: 观测引擎实现计划

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal**: 实现观测引擎，包括实时监控、钉钉/飞书告警系统、失败追踪、审计日志

**Architecture**: 基于 engine.py 基类，实现 ObservabilityEngine 接口，包含 real_time_monitor/alert_manager(钉钉/飞书)/failure_tracker/audit_logger 模块

**Tech Stack**: Python 3.8+, pytest, requests, 钉钉 Webhook/飞书开放平台

**预计时间**: 1-2 天  
**任务数**: 8 个

---

## Task 1: 实时监控指标收集器

**Files:**
- Create: `app/observability/metrics_collector.py`
- Test: `tests/observability/test_metrics_collector.py`

**Step 1: Write the failing test**
```python
# tests/observability/test_metrics_collector.py
import pytest
from app.observability.metrics_collector import MetricsCollector

def test_collect_system_metrics():
    """测试收集系统指标"""
    collector = MetricsCollector()
    
    metrics = collector.collect_system_metrics()
    
    assert "cpu_percent" in metrics
    assert "memory_percent" in metrics
    assert "disk_percent" in metrics

def test_collect_trading_metrics():
    """测试收集交易指标"""
    collector = MetricsCollector()
    
    # 模拟添加交易数据
    collector.record_signal("signal_1")
    collector.record_order("order_1")
    collector.record_fill("order_1", 100)
    
    metrics = collector.collect_trading_metrics()
    
    assert metrics["signals_count"] == 1
    assert metrics["orders_count"] == 1
    assert metrics["fills_count"] == 1
```

**Step 2: Run test — confirm it fails**
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
pytest tests/observability/test_metrics_collector.py::test_collect_system_metrics -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/observability/metrics_collector.py
import psutil
from datetime import datetime
from typing import Dict, List

class MetricsCollector:
    """监控指标收集器"""
    
    def __init__(self):
        self.signals: List[Dict] = []
        self.orders: List[Dict] = []
        self.fills: List[Dict] = []
    
    def collect_system_metrics(self) -> Dict[str, float]:
        """收集系统指标"""
        return {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage('/').percent,
            "timestamp": datetime.now().isoformat(),
        }
    
    def collect_trading_metrics(self) -> Dict[str, int]:
        """收集交易指标"""
        return {
            "signals_count": len(self.signals),
            "orders_count": len(self.orders),
            "fills_count": len(self.fills),
            "pending_orders": len([o for o in self.orders if o.get("status") == "pending"]),
        }
    
    def record_signal(self, signal_id: str, **kwargs):
        """记录信号"""
        self.signals.append({
            "signal_id": signal_id,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
    
    def record_order(self, order_id: str, **kwargs):
        """记录订单"""
        self.orders.append({
            "order_id": order_id,
            "timestamp": datetime.now().isoformat(),
            "status": "pending",
            **kwargs
        })
    
    def record_fill(self, order_id: str, quantity: float, **kwargs):
        """记录成交"""
        self.fills.append({
            "order_id": order_id,
            "quantity": quantity,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
        
        # 更新订单状态
        for order in self.orders:
            if order["order_id"] == order_id:
                order["status"] = "filled"
                break
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/observability/test_metrics_collector.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/observability/metrics_collector.py tests/observability/test_metrics_collector.py
git commit -m "feat(observability): 实现监控指标收集器 (Task 1/8)"
```

---

## Task 2: 钉钉告警推送器

**Files:**
- Create: `app/observability/dingtalk_alert.py`
- Test: `tests/observability/test_dingtalk_alert.py`

**Step 1: Write the failing test**
```python
# tests/observability/test_dingtalk_alert.py
import pytest
from app.observability.dingtalk_alert import DingTalkAlerter

def test_dingtalk_alerter_initialization():
    """测试钉钉告警器初始化"""
    alerter = DingTalkAlerter(
        webhook_url="https://oapi.dingtalk.com/robot/send?access_token=test",
        secret="test_secret"
    )
    
    assert alerter.webhook_url != ""
    assert alerter.secret != ""

def test_dingtalk_message_format():
    """测试钉钉消息格式"""
    alerter = DingTalkAlerter("test_url", "test_secret")
    
    message = alerter.format_alert(
        level="CRITICAL",
        title="测试告警",
        content="这是一条测试告警消息"
    )
    
    assert "测试告警" in message
    assert "CRITICAL" in message
    assert "这是一条测试告警消息" in message
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/observability/test_dingtalk_alert.py::test_dingtalk_alerter_initialization -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/observability/dingtalk_alert.py
import hmac
import hashlib
import base64
import requests
import time
from typing import Dict, Any, Optional
from urllib.parse import quote_plus

class DingTalkAlerter:
    """钉钉告警推送器"""
    
    def __init__(self, webhook_url: str, secret: str):
        self.webhook_url = webhook_url
        self.secret = secret
    
    def _generate_sign(self) -> str:
        """生成钉钉签名"""
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        
        hmac_code = hmac.new(
            secret_enc,
            string_to_sign_enc,
            digestmod=hashlib.sha256
        ).digest()
        
        sign = quote_plus(base64.b64encode(hmac_code))
        return f"&timestamp={timestamp}&sign={sign}"
    
    def format_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> Dict[str, Any]:
        """格式化告警消息"""
        # 根据级别设置颜色
        colors = {
            "INFO": "#00ff00",
            "WARN": "#ffa500",
            "CRITICAL": "#ff0000",
        }
        color = colors.get(level, "#000000")
        
        # Markdown 格式
        markdown = f"""## {title}

**级别**: <font color="{color}">{level}</font>

**内容**: {content}
"""
        
        # 添加额外字段
        for key, value in kwargs.items():
            markdown += f"\n**{key}**: {value}"
        
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": markdown
            }
        }
    
    def send_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> bool:
        """发送告警"""
        try:
            message = self.format_alert(level, title, content, **kwargs)
            
            url = self.webhook_url
            if self.secret:
                url += self._generate_sign()
            
            response = requests.post(url, json=message, timeout=10)
            
            result = response.json()
            return result.get("errcode") == 0
        except Exception as e:
            print(f"发送钉钉告警失败：{e}")
            return False
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/observability/test_dingtalk_alert.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/observability/dingtalk_alert.py tests/observability/test_dingtalk_alert.py
git commit -m "feat(observability): 实现钉钉告警推送器 (Task 2/8)"
```

---

## Task 3: 飞书告警推送器

**Files:**
- Create: `app/observability/feishu_alert.py`
- Test: `tests/observability/test_feishu_alert.py`

**Step 1: Write the failing test**
```python
# tests/observability/test_feishu_alert.py
import pytest
from app.observability.feishu_alert import FeishuAlerter

def test_feishu_message_format():
    """测试飞书消息格式"""
    alerter = FeishuAlerter(webhook_url="test_url")
    
    message = alerter.format_alert(
        level="CRITICAL",
        title="测试告警",
        content="这是一条测试告警消息"
    )
    
    assert "msg_type" in message
    assert "interactive" in message
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/observability/test_feishu_alert.py::test_feishu_message_format -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/observability/feishu_alert.py
import requests
from typing import Dict, Any

class FeishuAlerter:
    """飞书告警推送器"""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
    
    def format_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> Dict[str, Any]:
        """格式化飞书消息 (交互式卡片)"""
        # 根据级别设置颜色
        colors = {
            "INFO": "green",
            "WARN": "orange",
            "CRITICAL": "red",
        }
        color = colors.get(level, "gray")
        
        # 构建卡片元素
        elements = [
            {
                "tag": "div",
                "text": {
                    "content": f"**告警级别**: {level}",
                    "tag": "lark_md"
                }
            },
            {
                "tag": "div",
                "text": {
                    "content": f"**内容**: {content}",
                    "tag": "lark_md"
                }
            }
        ]
        
        # 添加额外字段
        for key, value in kwargs.items():
            elements.append({
                "tag": "div",
                "text": {
                    "content": f"**{key}**: {value}",
                    "tag": "lark_md"
                }
            })
        
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "content": title,
                        "tag": "plain_text"
                    },
                    "template": color
                },
                "elements": elements
            }
        }
    
    def send_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> bool:
        """发送告警"""
        try:
            message = self.format_alert(level, title, content, **kwargs)
            
            response = requests.post(
                self.webhook_url,
                json=message,
                timeout=10
            )
            
            result = response.json()
            return result.get("code") == 0 or result.get("StatusCode") == 0
        except Exception as e:
            print(f"发送飞书告警失败：{e}")
            return False
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/observability/test_feishu_alert.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/observability/feishu_alert.py tests/observability/test_feishu_alert.py
git commit -m "feat(observability): 实现飞书告警推送器 (Task 3/8)"
```

---

## Task 4: 告警管理器 (统一接口)

**Files:**
- Create: `app/observability/alert_manager.py`
- Test: `tests/observability/test_alert_manager.py`

**Step 1: Write the failing test**
```python
# tests/observability/test_alert_manager.py
import pytest
from app.observability.alert_manager import AlertManager

def test_alert_manager_routes_to_correct_provider():
    """测试告警管理器路由到正确的提供商"""
    manager = AlertManager(provider="dingtalk")
    
    assert manager.alerter is not None
    assert manager.provider == "dingtalk"

def test_alert_manager_sends_alert():
    """测试告警管理器发送告警"""
    manager = AlertManager(provider="dingtalk")
    
    # 测试接口 (不实际发送)
    message = manager.format_alert("CRITICAL", "测试", "内容")
    assert message is not None
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/observability/test_alert_manager.py::test_alert_manager_routes_to_correct_provider -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/observability/alert_manager.py
from typing import Dict, Any, Optional
from .dingtalk_alert import DingTalkAlerter
from .feishu_alert import FeishuAlerter

class AlertManager:
    """告警管理器 (统一接口)"""
    
    def __init__(
        self,
        provider: str = "dingtalk",
        dingtalk_webhook: Optional[str] = None,
        dingtalk_secret: Optional[str] = None,
        feishu_webhook: Optional[str] = None,
    ):
        self.provider = provider.lower()
        
        if self.provider == "dingtalk":
            if not dingtalk_webhook:
                raise ValueError("钉钉 webhook URL 必须提供")
            self.alerter = DingTalkAlerter(dingtalk_webhook, dingtalk_secret or "")
        elif self.provider == "feishu":
            if not feishu_webhook:
                raise ValueError("飞书 webhook URL 必须提供")
            self.alerter = FeishuAlerter(feishu_webhook)
        else:
            raise ValueError(f"不支持的告警提供商：{provider}")
    
    def format_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> Dict[str, Any]:
        """格式化告警"""
        return self.alerter.format_alert(level, title, content, **kwargs)
    
    def send_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> bool:
        """发送告警"""
        return self.alerter.send_alert(level, title, content, **kwargs)
    
    def send_info(self, title: str, content: str, **kwargs) -> bool:
        """发送 INFO 级别告警"""
        return self.send_alert("INFO", title, content, **kwargs)
    
    def send_warn(self, title: str, content: str, **kwargs) -> bool:
        """发送 WARN 级别告警"""
        return self.send_alert("WARN", title, content, **kwargs)
    
    def send_critical(self, title: str, content: str, **kwargs) -> bool:
        """发送 CRITICAL 级别告警"""
        return self.send_alert("CRITICAL", title, content, **kwargs)
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/observability/test_alert_manager.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/observability/alert_manager.py tests/observability/test_alert_manager.py
git commit -m "feat(observability): 实现告警管理器统一接口 (Task 4/8)"
```

---

## Task 5: 失败追踪器

**Files:**
- Create: `app/observability/failure_tracker.py`
- Test: `tests/observability/test_failure_tracker.py`

**Step 1: Write the failing test**
```python
# tests/observability/test_failure_tracker.py
import pytest
from app.observability.failure_tracker import FailureTracker
from app.core.models import FailureType

def test_failure_tracker_records_failure():
    """测试失败追踪器记录失败"""
    tracker = FailureTracker()
    
    tracker.record_failure(
        failure_type=FailureType.SIGNAL_EXPIRED,
        symbol="AAPL",
        reason="信号过期"
    )
    
    assert len(tracker.failures) == 1
    assert tracker.failures[0]["failure_type"] == "signal_expired"

def test_failure_tracker_get_statistics():
    """测试获取失败统计"""
    tracker = FailureTracker()
    
    tracker.record_failure(FailureType.SIGNAL_EXPIRED, "AAPL")
    tracker.record_failure(FailureType.SIGNAL_EXPIRED, "TSLA")
    tracker.record_failure(FailureType.EXEC_REJECTED, "AAPL")
    
    stats = tracker.get_statistics()
    
    assert stats["total"] == 3
    assert stats["by_type"]["signal_expired"] == 2
    assert stats["by_symbol"]["AAPL"] == 2
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/observability/test_failure_tracker.py::test_failure_tracker_records_failure -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/observability/failure_tracker.py
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from app.core.models import FailureType

class FailureTracker:
    """失败追踪器"""
    
    def __init__(self, retention_days: int = 30):
        self.retention = timedelta(days=retention_days)
        self.failures: List[Dict[str, Any]] = []
    
    def record_failure(
        self,
        failure_type: FailureType,
        symbol: str,
        reason: str,
        **kwargs
    ):
        """记录失败"""
        self.failures.append({
            "failure_id": f"fail_{datetime.now().timestamp()}",
            "failure_type": failure_type.value,
            "symbol": symbol,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        })
        
        # 清理过期记录
        self._cleanup_expired()
    
    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            "total": len(self.failures),
            "by_type": {},
            "by_symbol": {},
            "recent_24h": 0,
        }
        
        now = datetime.now()
        cutoff = now - timedelta(hours=24)
        
        for failure in self.failures:
            # 按类型统计
            ftype = failure["failure_type"]
            stats["by_type"][ftype] = stats["by_type"].get(ftype, 0) + 1
            
            # 按股票统计
            symbol = failure["symbol"]
            stats["by_symbol"][symbol] = stats["by_symbol"].get(symbol, 0) + 1
            
            # 24 小时内
            ts = datetime.fromisoformat(failure["timestamp"])
            if ts > cutoff:
                stats["recent_24h"] += 1
        
        return stats
    
    def get_recent_failures(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取最近的失败记录"""
        return self.failures[-limit:]
    
    def _cleanup_expired(self):
        """清理过期记录"""
        cutoff = datetime.now() - self.retention
        self.failures = [
            f for f in self.failures
            if datetime.fromisoformat(f["timestamp"]) > cutoff
        ]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/observability/test_failure_tracker.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/observability/failure_tracker.py tests/observability/test_failure_tracker.py
git commit -m "feat(observability): 实现失败追踪器 (Task 5/8)"
```

---

## Task 6: 审计日志记录器

**Files:**
- Create: `app/observability/audit_logger.py`
- Test: `tests/observability/test_audit_logger.py`

**Step 1: Write the failing test**
```python
# tests/observability/test_audit_logger.py
import pytest
from app.observability.audit_logger import AuditLogger

def test_audit_logger_records_event():
    """测试审计日志记录事件"""
    logger = AuditLogger(log_file="/tmp/test_audit.log")
    
    logger.record_event(
        event_type="order_submitted",
        user_id="trader_1",
        details={"order_id": "123"}
    )
    
    # 验证记录存在
    assert len(logger.events) == 1
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/observability/test_audit_logger.py::test_audit_logger_records_event -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/observability/audit_logger.py
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path

class AuditLogger:
    """审计日志记录器"""
    
    def __init__(
        self,
        log_file: str = "logs/audit.log",
        retention_days: int = 90,
    ):
        self.log_file = Path(log_file)
        self.retention_days = retention_days
        self.events: List[Dict[str, Any]] = []
        
        # 确保目录存在
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 配置日志
        self.logger = logging.getLogger("audit")
        self.logger.setLevel(logging.INFO)
        
        handler = logging.FileHandler(self.log_file)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(message)s'
        ))
        self.logger.addHandler(handler)
    
    def record_event(
        self,
        event_type: str,
        user_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        """记录审计事件"""
        event = {
            "event_id": f"audit_{datetime.now().timestamp()}",
            "event_type": event_type,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            "details": details or {},
            **kwargs
        }
        
        self.events.append(event)
        
        # 写入日志文件
        self.logger.info(json.dumps(event, ensure_ascii=False))
        
        # 清理过期记录
        self._cleanup_expired()
    
    def query_events(
        self,
        event_type: Optional[str] = None,
        user_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询审计事件"""
        results = self.events
        
        if event_type:
            results = [e for e in results if e["event_type"] == event_type]
        
        if user_id:
            results = [e for e in results if e.get("user_id") == user_id]
        
        if start_time:
            results = [
                e for e in results
                if e["timestamp"] >= start_time
            ]
        
        if end_time:
            results = [
                e for e in results
                if e["timestamp"] <= end_time
            ]
        
        return results
    
    def _cleanup_expired(self):
        """清理过期记录"""
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        self.events = [
            e for e in self.events
            if datetime.fromisoformat(e["timestamp"]) > cutoff
        ]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/observability/test_audit_logger.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/observability/audit_logger.py tests/observability/test_audit_logger.py
git commit -m "feat(observability): 实现审计日志记录器 (Task 6/8)"
```

---

## Task 7: ObservabilityEngine 主类

**Files:**
- Create: `app/observability/engine.py`
- Test: `tests/observability/test_observability_engine.py`

**Step 1: Write the failing test**
```python
# tests/observability/test_observability_engine.py
import pytest
from app.observability.engine import ObservabilityEngine
from app.core.events import EventStore

def test_observability_engine_initialization():
    """测试观测引擎初始化"""
    event_store = EventStore(":memory:")
    engine = ObservabilityEngine(config={}, event_store=event_store)
    engine.initialize()
    
    assert engine.is_healthy() == True
    assert engine.metrics_collector is not None
    assert engine.alert_manager is not None
    assert engine.failure_tracker is not None
    assert engine.audit_logger is not None
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/observability/test_observability_engine.py::test_observability_engine_initialization -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/observability/engine.py
from typing import Dict, Any, List, Optional
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
        
        # 告警管理器
        provider = obs_config.get("ALERT_PROVIDER", "dingtalk")
        self.alert_manager = AlertManager(
            provider=provider,
            dingtalk_webhook=obs_config.get("DINGTALK_WEBHOOK_URL_US"),
            dingtalk_secret=obs_config.get("DINGTALK_SECRET_US"),
            feishu_webhook=obs_config.get("FEISHU_WEBHOOK_URL"),
        )
        
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
        
        return {
            **system_metrics,
            **trading_metrics,
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
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/observability/test_observability_engine.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/observability/engine.py tests/observability/test_observability_engine.py
git commit -m "feat(observability): 实现 ObservabilityEngine 主类 (Task 7/8)"
```

---

## Task 8: 端到端集成测试

**Files:**
- Create: `tests/observability/test_observability_integration.py`

**Step 1: Write integration test**
```python
# tests/observability/test_observability_integration.py
"""观测引擎端到端集成测试"""
import pytest
from app.observability.engine import ObservabilityEngine
from app.core.events import EventStore
from app.core.models import FailureRecord, FailureType

def test_observability_engine_full_workflow():
    """测试观测引擎完整工作流"""
    event_store = EventStore(":memory:")
    engine = ObservabilityEngine(config={}, event_store=event_store)
    engine.initialize()
    
    # 1. 收集指标
    metrics = engine.collect_metrics()
    assert "cpu_percent" in metrics
    assert "memory_percent" in metrics
    
    # 2. 记录失败
    failure = FailureRecord(
        failure_type=FailureType.SIGNAL_EXPIRED,
        symbol="AAPL",
        reason="测试失败",
    )
    engine.track_failure(failure)
    
    # 3. 获取统计
    stats = engine.failure_tracker.get_statistics()
    assert stats["total"] == 1
    
    # 4. 记录审计事件
    engine.record_audit_event(
        event_type="test_event",
        user_id="test_user",
        details={"test": "data"},
    )
    
    # 5. 查询审计日志
    events = engine.audit_logger.query_events(event_type="test_event")
    assert len(events) == 1
```

**Step 2: Run test — confirm it passes**
```bash
pytest tests/observability/test_observability_integration.py -v
```
Expected: PASS

**Step 3: Commit**
```bash
git add tests/observability/test_observability_integration.py
git commit -m "test(observability): 添加观测引擎端到端测试 (Task 8/8)"
```

---

## 完成验证

所有任务完成后运行：

```bash
# 运行所有观测引擎测试
pytest tests/observability/ -v --tb=short

# 检查测试覆盖率
pytest tests/observability/ --cov=app/observability --cov-report=term-missing

# 验证模块导入
python -c "from app.observability.engine import ObservabilityEngine; from app.core.events import EventStore; e = ObservabilityEngine({}, EventStore(':memory:')); e.initialize(); print('✅ ObservabilityEngine 导入成功')"
```

**预期结果**:
- ✅ 8/8 任务完成
- ✅ 所有测试通过
- ✅ 测试覆盖率 > 80%
- ✅ 模块导入无错误

---

*计划文档版本：1.0 | 创建日期：2026-03-24*
