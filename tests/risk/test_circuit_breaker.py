import pytest
from datetime import datetime, timedelta
from app.risk.circuit_breaker import StrategyCircuitBreaker
from app.core.models import SignalScore, Direction

def test_strategy_circuit_breaker_triggers_after_consecutive_losses():
    """测试策略级熔断：连续 5 笔亏损后触发"""
    breaker = StrategyCircuitBreaker(
        consecutive_losses_threshold=5,
        pause_duration_seconds=1800,  # 30 分钟
    )
    
    # 模拟连续 5 笔亏损
    for i in range(5):
        breaker.record_loss("strategy_1")
    
    # 应该触发熔断
    assert breaker.is_triggered("strategy_1") == True
    assert breaker.get_remaining_pause("strategy_1") > 0

def test_strategy_circuit_breaker_resets_after_pause():
    """测试熔断在暂停期后自动重置"""
    breaker = StrategyCircuitBreaker(
        consecutive_losses_threshold=3,
        pause_duration_seconds=1,  # 2026-07-26: 原 60s+sleep(61) 纯睡两分钟, 缩短语义不变
    )
    
    # 触发熔断
    for i in range(3):
        breaker.record_loss("strategy_1")
    
    assert breaker.is_triggered("strategy_1") == True
    
    # 等待暂停期后
    import time
    time.sleep(1.2)
    
    # 应该自动重置
    assert breaker.is_triggered("strategy_1") == False

def test_symbol_circuit_breaker_on_high_volatility():
    """测试品种级熔断：高波动率触发"""
    from app.risk.circuit_breaker import SymbolCircuitBreaker
    
    breaker = SymbolCircuitBreaker(
        volatility_threshold=0.05,  # 5% 波动率
    )
    
    # 正常波动率
    assert breaker.check_volatility("AAPL", 0.03) == True  # 可交易
    
    # 高波动率
    assert breaker.check_volatility("TSLA", 0.06) == False  # 触发熔断

def test_system_circuit_breaker_on_market_data_down():
    """测试系统级熔断：行情断流 (2026-07-26 改写: 04-17 起为健康评分制, 非时间超时制)"""
    from app.risk.circuit_breaker import SystemCircuitBreaker
    
    breaker = SystemCircuitBreaker()
    
    # 正常
    breaker.update_market_data_status(True)
    assert breaker.is_triggered() == False
    
    # 连续失败上报 → 健康度跌破阈值 → 熔断
    for _ in range(10):
        breaker.update_market_data_status(False)
    assert breaker.is_triggered() or breaker.is_degraded()
