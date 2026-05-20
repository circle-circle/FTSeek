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
    
    assert "测试告警" in message["markdown"]["text"]
    assert "CRITICAL" in message["markdown"]["text"]
    assert "这是一条测试告警消息" in message["markdown"]["text"]
