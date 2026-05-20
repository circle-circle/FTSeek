# tests/observability/test_alert_manager.py
import pytest
from app.observability.alert_manager import AlertManager

def test_alert_manager_routes_to_correct_provider():
    """测试告警管理器路由到正确的提供商"""
    manager = AlertManager(
        provider="dingtalk",
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=test"
    )
    
    assert manager.alerter is not None
    assert manager.provider == "dingtalk"

def test_alert_manager_sends_alert():
    """测试告警管理器发送告警"""
    manager = AlertManager(
        provider="dingtalk",
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=test"
    )
    
    # 测试接口 (不实际发送)
    message = manager.format_alert("CRITICAL", "测试", "内容")
    assert message is not None
