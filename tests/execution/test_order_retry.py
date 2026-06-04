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


def test_chinese_retryable_classification():
    """中文瞬时错误必须归类为可重试，避免被错放弃"""
    assert classify_error("网络异常，请稍后重试") == ErrorCategory.RETRYABLE
    assert classify_error("请求超时") == ErrorCategory.RETRYABLE
    assert classify_error("连接已断开") == ErrorCategory.RETRYABLE
    assert classify_error("服务繁忙，请稍后再试") == ErrorCategory.RETRYABLE
    assert classify_error("请求过于频繁") == ErrorCategory.RETRYABLE
    assert classify_error("系统繁忙") == ErrorCategory.RETRYABLE


def test_chinese_non_retryable_classification():
    """中文永久性错误必须归类为不可重试"""
    assert classify_error("账户余额不足") == ErrorCategory.NON_RETRYABLE
    assert classify_error("可用资金不足") == ErrorCategory.NON_RETRYABLE
    assert classify_error("标的不存在") == ErrorCategory.NON_RETRYABLE
    assert classify_error("该股票已停牌") == ErrorCategory.NON_RETRYABLE
    assert classify_error("订单已撤销") == ErrorCategory.NON_RETRYABLE
    assert classify_error("参数无效") == ErrorCategory.NON_RETRYABLE


def test_chinese_manual_intervene_classification():
    """中文需人工介入错误必须正确识别"""
    assert classify_error("账户冻结，请联系客服") == ErrorCategory.MANUAL_INTERVENE
    assert classify_error("权限不足") == ErrorCategory.MANUAL_INTERVENE
    assert classify_error("保证金不足，触发强制平仓") == ErrorCategory.MANUAL_INTERVENE


def test_error_code_classification():
    """富途错误码分类"""
    # 网络层 -> 可重试
    assert classify_error("", error_code=-2) == ErrorCategory.RETRYABLE
    assert classify_error("unknown", error_code=-13) == ErrorCategory.RETRYABLE
    assert classify_error("", error_code=503) == ErrorCategory.RETRYABLE
    # 业务层 -> 不可重试
    assert classify_error("", error_code=3) == ErrorCategory.NON_RETRYABLE
    assert classify_error("", error_code=4) == ErrorCategory.NON_RETRYABLE
    # 权限层 -> 人工介入
    assert classify_error("", error_code=403) == ErrorCategory.MANUAL_INTERVENE


def test_unknown_error_defaults_retryable():
    """未分类错误默认可重试（保守策略）"""
    assert classify_error("some weird unknown error xyz") == ErrorCategory.RETRYABLE
    assert classify_error("未知错误码 9999", error_code=9999) == ErrorCategory.RETRYABLE


def test_unknown_error_still_capped_by_max_attempts():
    """未知错误默认可重试，但仍受 max_attempts 上限保护"""
    manager = OrderRetryManager(max_attempts=3, delay_seconds=0)
    order_id = "order_unknown"
    for _ in range(3):
        manager.record_attempt(order_id, error_msg="未知错误 xyz", success=False)
        import time as _t
        _t.sleep(0.01)
    can_retry, reason = manager.should_retry(order_id)
    assert can_retry is False
    assert "最大重试次数" in reason


def test_priority_manual_over_retryable():
    """分类优先级：人工介入应高于可重试，避免账户问题被反复重试"""
    # 同时含 "网络"(retryable) 与 "权限不足"(manual)，应判为人工介入
    assert classify_error("网络请求失败：权限不足") == ErrorCategory.MANUAL_INTERVENE


def test_hard_max_attempts_cap():
    """max_attempts 受 HARD_MAX_ATTEMPTS 限制"""
    manager = OrderRetryManager(max_attempts=999)
    assert manager.max_attempts == OrderRetryManager.HARD_MAX_ATTEMPTS
