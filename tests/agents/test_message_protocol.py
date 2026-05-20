import pytest
from app.agents.message_protocol import AgentMessage, MessageType

def test_agent_message_creation():
    """测试 Agent 消息创建"""
    msg = AgentMessage(
        msg_type=MessageType.SIGNAL,
        from_agent="decision",
        to_agent="risk",
        payload={"signal_id": "123", "strength": 75}
    )
    
    assert msg.msg_type == MessageType.SIGNAL
    assert msg.from_agent == "decision"
    assert msg.payload["signal_id"] == "123"

def test_message_serialization():
    """测试消息序列化"""
    msg = AgentMessage(
        msg_type=MessageType.ORDER,
        from_agent="risk",
        to_agent="execution",
        payload={"order_id": "456"}
    )
    
    serialized = msg.to_dict()
    assert "msg_id" in serialized
    assert "payload" in serialized
