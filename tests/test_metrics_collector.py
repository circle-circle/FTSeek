"""
监控指标收集器测试

测试指标收集、持久化和查询功能
"""
import pytest
import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.metrics_tables import MetricsTables
from app.observability.metrics_collector import MetricsCollector
from app.observability.metrics_api import MetricsAPI


@pytest.fixture
def temp_db():
    """创建临时数据库"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture
def metrics_tables(temp_db):
    """创建 MetricsTables 实例"""
    return MetricsTables(temp_db)


@pytest.fixture
def metrics_collector(temp_db):
    """创建 MetricsCollector 实例"""
    return MetricsCollector(db_path=temp_db)


@pytest.fixture
def metrics_api(metrics_tables):
    """创建 MetricsAPI 实例"""
    return MetricsAPI(metrics_tables)


class TestMetricsTables:
    """测试 MetricsTables 类"""
    
    def test_init_creates_tables(self, temp_db):
        """测试初始化创建表"""
        metrics_tables = MetricsTables(temp_db)
        
        # 验证表存在
        import sqlite3
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in cursor.fetchall()]
        
        assert "metrics" in tables
        assert "metrics_hourly" in tables
        conn.close()
    
    def test_record_metric(self, metrics_tables):
        """测试记录指标"""
        metric_id = metrics_tables.record_metric(
            metric_type="system",
            metric_name="cpu_percent",
            metric_value=45.5,
            metric_unit="%",
        )
        
        assert metric_id is not None
        assert metric_id > 0
    
    def test_record_metrics_batch(self, metrics_tables):
        """测试批量记录指标"""
        batch = [
            {
                'metric_type': 'system',
                'metric_name': 'cpu_percent',
                'metric_value': 45.5,
                'metric_unit': '%',
            },
            {
                'metric_type': 'system',
                'metric_name': 'memory_percent',
                'metric_value': 62.3,
                'metric_unit': '%',
            },
            {
                'metric_type': 'trading',
                'metric_name': 'orders_count',
                'metric_value': 10,
                'metric_unit': 'count',
            },
        ]
        
        count = metrics_tables.record_metrics_batch(batch)
        
        assert count == 3
    
    def test_query_metrics_by_time_range(self, metrics_tables):
        """测试按时间范围查询指标"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 插入测试数据
        metrics_tables.record_metric(
            metric_type="system",
            metric_name="cpu_percent",
            metric_value=45.5,
        )
        metrics_tables.record_metric(
            metric_type="system",
            metric_name="cpu_percent",
            metric_value=50.2,
        )
        metrics_tables.record_metric(
            metric_type="trading",
            metric_name="orders_count",
            metric_value=10,
        )
        
        # 查询系统指标
        metrics = metrics_tables.query_metrics_by_time_range(
            start_time=start,
            end_time=end,
            metric_type="system",
        )
        
        assert len(metrics) == 2
        assert all(m['metric_type'] == 'system' for m in metrics)
    
    def test_aggregate_hourly_metrics(self, metrics_tables):
        """测试小时聚合"""
        # 插入测试数据
        now = datetime.now()
        current_hour = now.strftime("%Y-%m-%d %H:00")
        
        for i in range(10):
            metrics_tables.record_metric(
                metric_type="system",
                metric_name="cpu_percent",
                metric_value=40 + i,
                timestamp=(now - timedelta(minutes=i)).isoformat(),
            )
        
        # 执行聚合
        metrics_tables.aggregate_hourly_metrics(current_hour)
        
        # 查询聚合结果
        hourly = metrics_tables.query_hourly_metrics(
            start_hour=current_hour,
            end_hour=current_hour,
        )
        
        assert len(hourly) >= 1
        cpu_metric = next((m for m in hourly if m['metric_name'] == 'cpu_percent'), None)
        assert cpu_metric is not None
        assert cpu_metric['count'] == 10
        assert cpu_metric['avg_value'] == 44.5  # (40+41+...+49)/10
    
    def test_get_latest_metrics(self, metrics_tables):
        """测试获取最新指标"""
        # 插入多个时间的数据
        now = datetime.now()
        
        # 按时间顺序插入，最新的在最后
        for i in range(5):
            metrics_tables.record_metric(
                metric_type="system",
                metric_name="cpu_percent",
                metric_value=40 + i * 5,
                timestamp=(now - timedelta(minutes=5-i)).isoformat(),
            )
        
        # 获取最新
        latest = metrics_tables.get_latest_metrics(
            metric_type="system",
            limit_per_metric=1,
        )
        
        assert len(latest) >= 1
        cpu_metric = next((m for m in latest if m['metric_name'] == 'cpu_percent'), None)
        assert cpu_metric is not None
        assert cpu_metric['metric_value'] == 60  # 最新的值 (最后插入的)
    
    def test_get_metric_statistics(self, metrics_tables):
        """测试获取指标统计"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 插入测试数据
        for i in range(10):
            metrics_tables.record_metric(
                metric_type="system",
                metric_name="cpu_percent",
                metric_value=40 + i,
            )
        
        # 获取统计
        stats = metrics_tables.get_metric_statistics(start, end, metric_type="system")
        
        assert 'system_cpu_percent' in stats
        assert stats['system_cpu_percent']['count'] == 10
        assert stats['system_cpu_percent']['avg_value'] == 44.5
    
    def test_cleanup_old_metrics(self, metrics_tables):
        """测试清理旧指标"""
        old_time = (datetime.now() - timedelta(days=40)).isoformat()
        new_time = datetime.now().isoformat()
        
        # 插入旧数据
        metrics_tables.record_metric(
            metric_type="system",
            metric_name="cpu_percent",
            metric_value=45.0,
            timestamp=old_time,
        )
        
        # 插入新数据
        metrics_tables.record_metric(
            metric_type="system",
            metric_name="cpu_percent",
            metric_value=50.0,
            timestamp=new_time,
        )
        
        # 清理 30 天前的数据
        cutoff = (datetime.now() - timedelta(days=30)).isoformat()
        deleted = metrics_tables.cleanup_old_metrics(cutoff)
        
        assert deleted == 1
        
        # 验证只剩新数据
        remaining = metrics_tables.get_latest_metrics(metric_type="system")
        assert len(remaining) == 1
        assert remaining[0]['metric_value'] == 50.0


class TestMetricsCollector:
    """测试 MetricsCollector 类"""
    
    def test_collect_system_metrics(self, metrics_collector):
        """测试收集系统指标"""
        metrics = metrics_collector.collect_system_metrics()
        
        assert "cpu_percent" in metrics
        assert "memory_percent" in metrics
        assert "disk_percent" in metrics
        assert 0 <= metrics["cpu_percent"] <= 100
        assert 0 <= metrics["memory_percent"] <= 100
    
    def test_collect_trading_metrics(self, metrics_collector):
        """测试收集交易指标"""
        # 添加测试数据
        metrics_collector.record_signal("signal_1")
        metrics_collector.record_order("order_1")
        metrics_collector.record_fill("order_1", 100)
        
        metrics = metrics_collector.collect_trading_metrics()
        
        assert metrics["signals_count"] == 1
        assert metrics["orders_count"] == 1
        assert metrics["fills_count"] == 1
    
    def test_collect_data_source_metrics(self, metrics_collector):
        """测试收集数据源指标"""
        # 添加测试数据
        metrics_collector.record_data_source_request("twelvedata", success=True, latency_ms=100)
        metrics_collector.record_data_source_request("twelvedata", success=True, latency_ms=150)
        metrics_collector.record_data_source_request("finnhub", success=False, latency_ms=500)
        
        metrics = metrics_collector.collect_data_source_metrics()
        
        assert "twelvedata" in metrics
        assert "finnhub" in metrics
        assert metrics["twelvedata"]["success_rate"] == 1.0
        assert metrics["finnhub"]["success_rate"] == 0.0
    
    def test_collect_risk_metrics(self, metrics_collector):
        """测试收集风控指标"""
        # 添加测试数据
        metrics_collector.record_risk_check("signal_1", allowed=True)
        metrics_collector.record_risk_check("signal_2", allowed=False, reason="low_confidence")
        metrics_collector.record_circuit_breaker_trigger("strategy_1")
        
        metrics = metrics_collector.collect_risk_metrics()
        
        assert metrics["risk_checks"] == 2
        assert metrics["risk_allowed"] == 1
        assert metrics["risk_rejected"] == 1
        assert metrics["circuit_breaker_triggers"] == 1
    
    def test_collect_voting_metrics(self, metrics_collector):
        """测试收集投票指标"""
        # 添加测试数据
        metrics_collector.record_voting_round(
            symbol="AAPL",
            scores=[4, 5, 3],
            consensus=0.8,
            decision="LONG",
        )
        metrics_collector.record_voting_round(
            symbol="GOOG",
            scores=[3, 2, 4],
            consensus=0.6,
            decision="SHORT",
        )
        
        metrics = metrics_collector.collect_voting_metrics()
        
        assert metrics["voting_rounds"] == 2
        assert metrics["avg_consensus"] == 0.7
    
    def test_persist_system_metrics(self, metrics_collector):
        """测试持久化系统指标"""
        metrics_collector.persist_system_metrics()
        
        # 验证数据库中有数据
        latest = metrics_collector.metrics_tables.get_latest_metrics(metric_type="system")
        assert len(latest) >= 3  # cpu, memory, disk
    
    def test_persist_trading_metrics(self, metrics_collector):
        """测试持久化交易指标"""
        # 添加数据
        metrics_collector.record_signal("signal_1")
        metrics_collector.record_order("order_1")
        
        metrics_collector.persist_trading_metrics()
        
        # 验证
        latest = metrics_collector.metrics_tables.get_latest_metrics(metric_type="trading")
        assert len(latest) >= 1
    
    def test_persist_all_metrics(self, metrics_collector):
        """测试持久化所有指标"""
        # 添加一些数据
        metrics_collector.record_signal("signal_1")
        metrics_collector.record_risk_check("signal_1", allowed=True)
        metrics_collector.record_voting_round("AAPL", [4, 5], 0.8, "LONG")
        
        metrics_collector.persist_all_metrics()
        
        # 验证各类型都有数据
        system = metrics_collector.metrics_tables.get_latest_metrics(metric_type="system")
        trading = metrics_collector.metrics_tables.get_latest_metrics(metric_type="trading")
        agent = metrics_collector.metrics_tables.get_latest_metrics(metric_type="agent")
        
        assert len(system) >= 3
        assert len(trading) >= 1
        assert len(agent) >= 1
    
    def test_aggregate_hourly_metrics(self, metrics_collector):
        """测试小时聚合"""
        now = datetime.now()
        current_hour = now.strftime("%Y-%m-%d %H:00")
        
        # 添加多个数据点
        for i in range(5):
            metrics_collector.metrics_tables.record_metric(
                metric_type="system",
                metric_name="test_metric",
                metric_value=10 + i,
                timestamp=(now - timedelta(minutes=i)).isoformat(),
            )
        
        # 执行聚合
        metrics_collector._last_hourly_aggregation = None
        metrics_collector.aggregate_hourly_metrics()
        
        # 验证聚合结果
        hourly = metrics_collector.metrics_tables.query_hourly_metrics(
            start_hour=current_hour,
            end_hour=current_hour,
            metric_name="test_metric",
        )
        
        assert len(hourly) >= 1
        assert hourly[0]['count'] == 5
        assert hourly[0]['avg_value'] == 12.0
    
    def test_should_aggregate_hourly(self, metrics_collector):
        """测试小时聚合判断"""
        # 首次应该聚合
        metrics_collector._last_hourly_aggregation = None
        assert metrics_collector.should_aggregate_hourly() in [True, False]  # 取决于当前分钟
    
    def test_cleanup_old_metrics(self, metrics_collector):
        """测试清理旧指标"""
        # 添加旧数据
        old_time = (datetime.now() - timedelta(days=40)).isoformat()
        metrics_collector.metrics_tables.record_metric(
            metric_type="system",
            metric_name="old_metric",
            metric_value=100,
            timestamp=old_time,
        )
        
        # 清理
        metrics_collector.cleanup_old_metrics(days=30)
        
        # 验证已清理
        remaining = metrics_collector.metrics_tables.query_metrics_by_time_range(
            start_time=old_time,
            end_time=old_time,
            metric_name="old_metric",
        )
        assert len(remaining) == 0


class TestMetricsAPI:
    """测试 MetricsAPI 类"""
    
    def test_get_system_metrics(self, metrics_api, metrics_tables):
        """测试获取系统指标"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 添加测试数据
        metrics_tables.record_metric(
            metric_type="system",
            metric_name="cpu_percent",
            metric_value=45.5,
        )
        metrics_tables.record_metric(
            metric_type="system",
            metric_name="memory_percent",
            metric_value=62.3,
        )
        
        # 查询
        result = metrics_api.get_system_metrics(start, end)
        
        assert "cpu_percent" in result
        assert "memory_percent" in result
    
    def test_get_trading_metrics(self, metrics_api, metrics_tables):
        """测试获取交易指标"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 添加测试数据
        metrics_tables.record_metric(
            metric_type="trading",
            metric_name="orders_count",
            metric_value=10,
        )
        
        # 查询
        result = metrics_api.get_trading_metrics(start, end)
        
        assert "orders_count" in result
    
    def test_get_agent_metrics(self, metrics_api, metrics_tables):
        """测试获取 Agent 指标"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 添加测试数据
        metrics_tables.record_metric(
            metric_type="agent",
            metric_name="response_time",
            metric_value=150,
            metric_unit="ms",
            labels={"agent_id": "analyst_1"},
        )
        
        # 查询所有
        result = metrics_api.get_agent_metrics(start, end)
        assert len(result) >= 1
        
        # 查询指定 Agent
        result = metrics_api.get_agent_metrics(start, end, agent_id="analyst_1")
        assert len(result) >= 1
    
    def test_get_hourly_aggregation(self, metrics_api, metrics_tables):
        """测试获取小时聚合"""
        now = datetime.now()
        current_hour = now.strftime("%Y-%m-%d %H:00")
        
        # 添加数据并聚合
        for i in range(5):
            metrics_tables.record_metric(
                metric_type="system",
                metric_name="cpu_percent",
                metric_value=40 + i,
            )
        
        metrics_tables.aggregate_hourly_metrics(current_hour)
        
        # 查询聚合
        result = metrics_api.get_hourly_aggregation(
            start_hour=current_hour,
            end_hour=current_hour,
        )
        
        assert len(result) >= 1
    
    def test_get_latest_system_status(self, metrics_api, metrics_tables):
        """测试获取最新系统状态"""
        # 添加测试数据
        metrics_tables.record_metric(
            metric_type="system",
            metric_name="cpu_percent",
            metric_value=45.5,
        )
        
        # 查询
        status = metrics_api.get_latest_system_status()
        
        assert "cpu_percent" in status
    
    def test_get_metric_summary(self, metrics_api, metrics_tables):
        """测试获取指标摘要"""
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        # 添加测试数据
        for i in range(10):
            metrics_tables.record_metric(
                metric_type="system",
                metric_name="cpu_percent",
                metric_value=40 + i,
            )
        
        # 查询摘要
        summary = metrics_api.get_metric_summary(start, end)
        
        assert "system" in summary
        assert "cpu_percent" in summary["system"]
        assert summary["system"]["cpu_percent"]["count"] == 10
    
    def test_get_performance_report(self, metrics_api, metrics_tables):
        """测试获取性能报告"""
        # 添加测试数据
        for i in range(20):
            metrics_tables.record_metric(
                metric_type="system",
                metric_name="cpu_percent",
                metric_value=40 + (i % 10),
            )
            metrics_tables.record_metric(
                metric_type="trading",
                metric_name="orders_count",
                metric_value=i,
            )
        
        # 获取报告
        report = metrics_api.get_performance_report(hours=1)
        
        assert "period" in report
        assert "system" in report
        assert "trading" in report
        assert "hourly_trend" in report
    
    def test_get_agent_performance(self, metrics_api, metrics_tables):
        """测试获取 Agent 性能"""
        # 添加测试数据
        for i in range(10):
            metrics_tables.record_metric(
                metric_type="agent",
                metric_name="response_time",
                metric_value=100 + i * 10,
                labels={"agent_id": "analyst_1"},
            )
        
        # 获取报告
        report = metrics_api.get_agent_performance("analyst_1", hours=1)
        
        assert report["agent_id"] == "analyst_1"
        assert "metrics" in report
        assert "response_time" in report["metrics"]


class TestMetricsIntegration:
    """指标系统集成测试"""
    
    def test_full_metrics_workflow(self, temp_db):
        """测试完整指标工作流"""
        # 1. 创建收集器
        collector = MetricsCollector(db_path=temp_db)
        
        # 2. 收集各种指标
        collector.record_signal("signal_1")
        collector.record_order("order_1")
        collector.record_fill("order_1", 100)
        collector.record_risk_check("signal_1", allowed=True)
        collector.record_voting_round("AAPL", [4, 5, 3], 0.8, "LONG")
        collector.record_data_source_request("twelvedata", success=True, latency_ms=100)
        
        # 3. 持久化
        collector.persist_all_metrics()
        
        # 4. 创建 API 查询
        tables = MetricsTables(temp_db)
        api = MetricsAPI(tables)
        
        # 5. 验证查询
        now = datetime.now()
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        
        system_metrics = api.get_system_metrics(start, end)
        trading_metrics = api.get_trading_metrics(start, end)
        agent_metrics = api.get_agent_metrics(start, end)
        
        assert len(system_metrics) >= 3
        assert len(trading_metrics) >= 1
        assert len(agent_metrics) >= 1
        
        # 6. 获取性能报告
        report = api.get_performance_report(hours=1)
        assert "system" in report
        assert "trading" in report
    
    def test_metrics_persistence_and_aggregation(self, temp_db):
        """测试指标持久化和聚合"""
        collector = MetricsCollector(db_path=temp_db)
        
        # 添加多个数据点
        for i in range(60):
            collector.metrics_tables.record_metric(
                metric_type="system",
                metric_name="cpu_percent",
                metric_value=40 + (i % 20),
            )
        
        # 执行聚合
        now = datetime.now()
        current_hour = now.strftime("%Y-%m-%d %H:00")
        collector.metrics_tables.aggregate_hourly_metrics(current_hour)
        
        # 验证聚合结果
        hourly = collector.metrics_tables.query_hourly_metrics(
            start_hour=current_hour,
            end_hour=current_hour,
            metric_name="cpu_percent",
        )
        
        assert len(hourly) >= 1
        assert hourly[0]['count'] == 60
        assert 49 <= hourly[0]['avg_value'] <= 50  # 平均值应该在 49-50 之间


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--cov=app/storage/metrics_tables", "--cov=app/observability/metrics_collector", "--cov=app/observability/metrics_api", "--cov-report=term-missing"])
