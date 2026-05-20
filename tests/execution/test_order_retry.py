# tests/execution/test_order_retry.py
import pytest
from app.execution.order_retry import OrderRetryManager, ErrorCategory, classify_error


def test_retry_manager_allows_retry():
    """测试重试管理器允许重试"""
    manager = OrderRetryManager(max_attempts=3, delay_seconds=0)  # 0秒退避，测试不等待

    order_id = "order_1"

    # 前 3 次应该允许重试
    can_retry, _ = manager.should_retry(order_id)
    assert can_retry == True
    manager.record_attempt(order_id)

    import time
    time.sleep(0.01)  # 短暂等待退避

    can_retry, _ = manager.should_retry(order_id)
    assert can_retry == True
    manager.record_attempt(order_id)

    time.sleep(0.01)

    can_retry, _ = manager.should_retry(order_id)
    assert can_retry == True
    manager.record_attempt(order_id)

    time.sleep(0.01)

    # 第 4 次应该拒绝
    can_retry, _ = manager.should_retry(order_id)
    assert can_retry == False


def test_retry_manager_get_delay():
    """测试获取重试延迟"""
    manager = OrderRetryManager(max_attempts=3, delay_seconds=5)

    order_id = "order_1"
    manager.record_attempt(order_id)

    # 延迟应该从 base_delay 开始，失败后按指数退避增长
    delay = manager.get_delay(order_id)
    assert delay > 0


def test_error_classification_retryable():
    """测试可重试错误分类"""
    assert classify_error("Connection timed out") == ErrorCategory.RETRYABLE
    assert classify_error("Rate limit exceeded") == ErrorCategory.RETRYABLE
    assert classify_error("503 Service Unavailable") == ErrorCategory.RETRYABLE


def test_error_classification_non_retryable():
    """测试不可重试错误分类"""
    assert classify_error("Insufficient funds") == ErrorCategory.NON_RETRYABLE
    assert classify_error("Market is closed") == ErrorCategory.NON_RETRYABLE
    assert classify_error("Stock suspended") == ErrorCategory.NON_RETRYABLE


def test_error_classification_manual():
    """测试需人工介入错误分类"""
    assert classify_error("Account locked") == ErrorCategory.MANUAL_INTERVENE
    assert classify_error("Permission denied") == ErrorCategory.MANUAL_INTERVENE


def test_non_retryable_blocks_immediately():
    """测试不可重试错误立即阻塞"""
    manager = OrderRetryManager(max_attempts=3)
    order_id = "order_blocked"

    # 记录一次不可重试错误
    manager.record_attempt(order_id, error_msg="Insufficient funds", success=False)

    can_retry, reason = manager.should_retry(order_id)
    assert can_retry == False
    assert "不可重试" in reason


def test_manual_intervene_blocks():
    """测试人工介入错误立即阻塞"""
    manager = OrderRetryManager(max_attempts=3)
    order_id = "order_manual"

    manager.record_attempt(order_id, error_msg="Account locked", success=False)

    can_retry, reason = manager.should_retry(order_id)
    assert can_retry == False
    assert "人工介入" in reason


def test_retry_status():
    """测试获取重试状态"""
    manager = OrderRetryManager(max_attempts=3)
    order_id = "order_status"

    status = manager.get_status(order_id)
    assert status["status"] == "not_started"

    manager.record_attempt(order_id, error_msg="Timeout", success=False)
    status = manager.get_status(order_id)
    assert status["attempts"] == 1
    assert status["error_category"] == "retryable"
