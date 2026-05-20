# tests/observability/test_feishu_alert.py
import pytest
from app.observability.feishu_alert import FeishuAlerter

def test_feishu_message_format():
    """测试飞书消息格式"""
    alerter = FeishuAlerter(app_id="test_app_id", app_secret="test_secret")
    
    message = alerter.format_alert(
        level="CRITICAL",
        title="测试告警",
        content="这是一条测试告警消息"
    )
    
    assert "msg_type" in message
    assert message["msg_type"] == "interactive"
