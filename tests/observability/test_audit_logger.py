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
