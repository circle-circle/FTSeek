"""监控指标收集器测试"""
import pytest
from unittest.mock import patch, MagicMock
from app.observability.metrics_collector import MetricsCollector


@patch('app.notifier.dingtalk.DingTalkNotifier')
def test_collect_system_metrics(mock_notifier):
    """测试收集系统指标"""
    # Mock 钉钉推送
    mock_notifier.return_value.enabled = False
    
    collector = MetricsCollector()
    
    metrics = collector.collect_system_metrics()
    
    assert "cpu_percent" in metrics
    assert "memory_percent" in metrics
    assert "disk_percent" in metrics


@patch('app.notifier.dingtalk.DingTalkNotifier')
def test_collect_trading_metrics(mock_notifier):
    """测试收集交易指标"""
    # Mock 钉钉推送
    mock_notifier.return_value.enabled = False
    
    collector = MetricsCollector()
    
    # 模拟添加交易数据
    collector.record_signal("signal_1")
    collector.record_order("order_1")
    collector.record_fill("order_1", 100)
    
    metrics = collector.collect_trading_metrics()
    
    assert metrics["signals_count"] == 1
    assert metrics["orders_count"] == 1
    assert metrics["fills_count"] == 1


@patch('app.notifier.dingtalk.DingTalkNotifier')
def test_collect_data_source_metrics(mock_notifier):
    """测试收集数据源指标"""
    mock_notifier.return_value.enabled = False
    
    collector = MetricsCollector()
    
    # 模拟数据源请求
    collector.record_data_source_request("twelvedata", success=True, latency_ms=100)
    collector.record_data_source_request("finnhub", success=True, latency_ms=50)
    collector.record_data_source_request("alphavantage", success=False, latency_ms=500)
    
    metrics = collector.collect_data_source_metrics()
    
    assert "twelvedata" in metrics
    assert "finnhub" in metrics
    assert "alphavantage" in metrics
    assert metrics["twelvedata"]["success_rate"] == 1.0
    assert metrics["finnhub"]["success_rate"] == 1.0
    assert metrics["alphavantage"]["success_rate"] == 0.0


@patch('app.notifier.dingtalk.DingTalkNotifier')
def test_collect_risk_metrics(mock_notifier):
    """测试收集风险指标"""
    mock_notifier.return_value.enabled = False
    
    collector = MetricsCollector()
    
    # 模拟风控事件
    collector.record_risk_check("signal_1", allowed=True)
    collector.record_risk_check("signal_2", allowed=False, reason="low_confidence")
    collector.record_circuit_breaker_trigger("strategy_1")
    
    metrics = collector.collect_risk_metrics()
    
    assert metrics["risk_checks"] == 2
    assert metrics["risk_allowed"] == 1
    assert metrics["risk_rejected"] == 1
    assert metrics["circuit_breaker_triggers"] == 1


@patch('app.notifier.dingtalk.DingTalkNotifier')
def test_collect_voting_metrics(mock_notifier):
    """测试收集投票指标"""
    mock_notifier.return_value.enabled = False
    
    collector = MetricsCollector()
    
    # 模拟投票事件
    collector.record_voting_round(
        symbol="US.AAPL",
        scores=[70, 65, 75, 60],
        consensus=0.65,
        decision="BUY"
    )
    
    metrics = collector.collect_voting_metrics()
    
    assert metrics["voting_rounds"] == 1
    assert "avg_consensus" in metrics
    assert "decisions" in metrics
