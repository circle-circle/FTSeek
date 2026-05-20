# tests/execution/test_idempotency.py
import pytest
from datetime import datetime, timedelta
from app.execution.idempotency import IdempotencyManager

def test_idempotency_prevents_duplicate():
    """测试幂等机制防止重复下单"""
    manager = IdempotencyManager(window_seconds=3600)
    
    client_order_id = "FTUS_AAPL_1234567890"
    
    # 第一次记录
    assert manager.is_duplicate(client_order_id) == False
    manager.record_order(client_order_id, "order_1")
    
    # 第二次检查应该是重复
    assert manager.is_duplicate(client_order_id) == True

def test_idempotency_window_expires():
    """测试幂等窗口过期"""
    manager = IdempotencyManager(window_seconds=1)  # 1 秒窗口 (测试用)
    
    client_order_id = "FTUS_TEST_1"
    manager.record_order(client_order_id, "order_1")
    
    import time
    time.sleep(1.1)
    
    # 过期后应该不再是重复
    assert manager.is_duplicate(client_order_id) == False
