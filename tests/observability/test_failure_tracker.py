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
    
    tracker.record_failure(FailureType.SIGNAL_EXPIRED, "AAPL", "信号过期")
    tracker.record_failure(FailureType.SIGNAL_EXPIRED, "TSLA", "信号过期")
    tracker.record_failure(FailureType.EXEC_REJECTED, "AAPL", "执行拒绝")
    
    stats = tracker.get_statistics()
    
    assert stats["total"] == 3
    assert stats["by_type"]["signal_expired"] == 2
    assert stats["by_symbol"]["AAPL"] == 2
