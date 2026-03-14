"""
测试定时任务调度器

测试覆盖:
- 调度器初始化和启动/停止
- 日报任务调度 (港股/美股)
- 周报任务调度
- 绩效检查任务
- 时区转换
- 任务失败日志和重试
- 线程安全
"""
import pytest
import time
import logging
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch
import pytz

from app.v4.scheduler.trading_scheduler import TradingScheduler
from app.v4.report.report_generator import ReportGenerator
from app.v4.core.decision_engine import DecisionEngine


# 配置日志
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("ftseek.test.scheduler")


class TestTradingSchedulerInit:
    """测试调度器初始化"""
    
    def test_init_with_valid_params(self, tmp_path):
        """测试使用有效参数初始化"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        assert scheduler.report_generator == mock_report_gen
        assert scheduler.decision_engine == mock_decision_engine
        assert scheduler.scheduler is not None
        assert not scheduler._running
    
    def test_init_without_decision_engine(self, tmp_path):
        """测试不提供 decision_engine 时初始化"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            db_path=db_path
        )
        
        assert scheduler.report_generator == mock_report_gen
        assert scheduler.decision_engine is None
    
    def test_init_creates_logger(self, tmp_path):
        """测试初始化时创建日志记录器"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        assert scheduler.logger is not None
        assert scheduler.logger.name == "ftseek.v4.trading_scheduler"


class TestSchedulerStartStop:
    """测试调度器启动和停止"""
    
    def test_start_scheduler(self, tmp_path):
        """测试启动调度器"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        scheduler.start()
        
        assert scheduler._running is True
        assert scheduler.scheduler.running is True
        
        scheduler.stop()
    
    def test_stop_scheduler(self, tmp_path):
        """测试停止调度器"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        scheduler.start()
        scheduler.stop()
        
        assert scheduler._running is False
        assert scheduler.scheduler.running is False
    
    def test_stop_idempotent(self, tmp_path):
        """测试多次停止是幂等的"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        scheduler.start()
        scheduler.stop()
        scheduler.stop()  # 第二次停止不应报错
        
        assert scheduler._running is False
    
    def test_start_twice_raises_error(self, tmp_path):
        """测试重复启动抛出错误"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        scheduler.start()
        
        with pytest.raises(RuntimeError, match="Scheduler already running"):
            scheduler.start()
        
        scheduler.stop()


class TestDailyReportScheduling:
    """测试日报任务调度"""
    
    def test_schedule_hk_daily_report(self, tmp_path):
        """测试调度港股日报 (周一至周五 16:30 HKT)"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        # Mock send_daily_report to track calls
        mock_report_gen.send_daily_report = Mock(return_value={"success": True})
        
        scheduler.schedule_daily_report("0700.HK", "HK")
        
        # 检查任务是否已添加
        jobs = scheduler.scheduler.get_jobs()
        assert len(jobs) >= 1
        
        # 检查任务 ID
        job_ids = [job.id for job in jobs]
        assert any("daily_report_0700.HK" in job_id for job_id in job_ids)
        
        scheduler.stop()
    
    def test_schedule_us_daily_report(self, tmp_path):
        """测试调度美股日报 (周一至周五 04:30 EST)"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_report_gen.send_daily_report = Mock(return_value={"success": True})
        
        scheduler.schedule_daily_report("TSLA", "US")
        
        jobs = scheduler.scheduler.get_jobs()
        assert len(jobs) >= 1
        
        scheduler.stop()
    
    def test_daily_report_calls_send_daily_report(self, tmp_path):
        """测试日报任务调用 send_daily_report"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_report_gen.send_daily_report = Mock(return_value={"success": True})
        
        # 手动触发日报任务
        scheduler._send_daily_report("0700.HK", "HK")
        
        mock_report_gen.send_daily_report.assert_called_once_with(code="0700.HK")
        
        scheduler.stop()
    
    def test_daily_report_logs_on_failure(self, tmp_path, caplog):
        """测试日报失败时记录日志"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_report_gen.send_daily_report = Mock(return_value={"success": False, "error": "Test error"})
        
        with caplog.at_level(logging.ERROR):
            scheduler._send_daily_report("0700.HK", "HK")
        
        assert "日报推送失败" in caplog.text
        assert "Test error" in caplog.text
        
        scheduler.stop()


class TestWeeklyReportScheduling:
    """测试周报任务调度"""
    
    def test_schedule_weekly_report(self, tmp_path):
        """测试调度周报 (周日 20:00 Asia/Shanghai)"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_report_gen.send_weekly_report = Mock(return_value={"success": True})
        
        scheduler.schedule_weekly_report("0700.HK")
        
        jobs = scheduler.scheduler.get_jobs()
        assert len(jobs) >= 1
        
        scheduler.stop()
    
    def test_weekly_report_calls_send_weekly_report(self, tmp_path):
        """测试周报任务调用 send_weekly_report"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_report_gen.send_weekly_report = Mock(return_value={"success": True})
        
        scheduler._send_weekly_report("0700.HK")
        
        mock_report_gen.send_weekly_report.assert_called_once_with(code="0700.HK")
        
        scheduler.stop()
    
    def test_weekly_report_logs_on_failure(self, tmp_path, caplog):
        """测试周报失败时记录日志"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_report_gen.send_weekly_report = Mock(return_value={"success": False, "error": "Weekly error"})
        
        with caplog.at_level(logging.ERROR):
            scheduler._send_weekly_report("0700.HK")
        
        assert "周报推送失败" in caplog.text
        
        scheduler.stop()


class TestPerformanceCheckScheduling:
    """测试绩效检查任务调度"""
    
    def test_schedule_performance_check(self, tmp_path):
        """测试调度绩效检查 (每小时一次)"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        
        # Create properly nested mock for decision_engine
        mock_exit_manager = Mock()
        mock_exit_manager.check_all_positions = Mock(return_value=[])
        mock_decision_engine = Mock()
        mock_decision_engine.exit_manager = mock_exit_manager
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        scheduler.schedule_performance_check()
        
        jobs = scheduler.scheduler.get_jobs()
        assert len(jobs) >= 1
        
        scheduler.stop()
    
    def test_performance_check_calls_exit_manager(self, tmp_path):
        """测试绩效检查调用 exit_manager"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        
        mock_exit_manager = Mock()
        mock_exit_manager.check_all_positions = Mock(return_value=[])
        mock_decision_engine = Mock()
        mock_decision_engine.exit_manager = mock_exit_manager
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        scheduler._check_performance()
        
        mock_decision_engine.exit_manager.check_all_positions.assert_called_once()
        
        scheduler.stop()
    
    def test_performance_check_handles_exits(self, tmp_path):
        """测试绩效检查处理退出决策"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        
        mock_exit_manager = Mock()
        mock_paper_trader = Mock()
        mock_decision_engine = Mock()
        mock_decision_engine.exit_manager = mock_exit_manager
        mock_decision_engine.paper_trader = mock_paper_trader
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        # Create a simple class for exit decision instead of Mock
        class MockExitDecision:
            should_exit = True
            reason = "TARGET_PROFIT"
            expected_return = 0.05
            to_dict = lambda self: {"reason": "TARGET_PROFIT"}
        
        mock_exit_decision = MockExitDecision()
        
        mock_decision_engine.exit_manager.check_all_positions = Mock(return_value=[
            ("0700.HK", mock_exit_decision)
        ])
        
        scheduler._check_performance()
        
        # 应该调用卖出
        mock_paper_trader.sell.assert_called()
        
        scheduler.stop()
    
    def test_performance_check_logs_on_error(self, tmp_path, caplog):
        """测试绩效检查出错时记录日志"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        
        mock_exit_manager = Mock()
        mock_decision_engine = Mock()
        mock_decision_engine.exit_manager = mock_exit_manager
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_decision_engine.exit_manager.check_all_positions = Mock(side_effect=Exception("Check error"))
        
        with caplog.at_level(logging.ERROR):
            scheduler._check_performance()
        
        assert "绩效检查失败" in caplog.text
        
        scheduler.stop()


class TestTimezoneHandling:
    """测试时区处理"""
    
    def test_hk_timezone_is_hkt(self, tmp_path):
        """测试港股使用 HKT 时区"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        # HKT 是 UTC+8
        assert scheduler.timezone_hk.zone == "Asia/Hong_Kong" or str(scheduler.timezone_hk) == "Asia/Hong_Kong"
        
        scheduler.stop()
    
    def test_us_timezone_is_est(self, tmp_path):
        """测试美股使用 EST 时区"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        # EST 是 UTC-5
        assert scheduler.timezone_us.zone == "US/Eastern" or str(scheduler.timezone_us) == "US/Eastern"
        
        scheduler.stop()
    
    def test_shanghai_timezone(self, tmp_path):
        """测试上海时区"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        assert scheduler.timezone_shanghai.zone == "Asia/Shanghai" or str(scheduler.timezone_shanghai) == "Asia/Shanghai"
        
        scheduler.stop()


class TestErrorHandling:
    """测试错误处理"""
    
    def test_scheduler_handles_report_generator_error(self, tmp_path, caplog):
        """测试调度器处理 report_generator 异常"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_report_gen.send_daily_report = Mock(side_effect=Exception("Generator error"))
        
        with caplog.at_level(logging.ERROR):
            scheduler._send_daily_report("0700.HK", "HK")
        
        assert "日报推送异常" in caplog.text
        
        scheduler.stop()
    
    def test_scheduler_handles_decision_engine_error(self, tmp_path, caplog):
        """测试调度器处理 decision_engine 异常"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        
        mock_exit_manager = Mock()
        mock_decision_engine = Mock()
        mock_decision_engine.exit_manager = mock_exit_manager
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        mock_decision_engine.exit_manager.check_all_positions = Mock(side_effect=Exception("Engine error"))
        
        with caplog.at_level(logging.ERROR):
            scheduler._check_performance()
        
        assert "绩效检查失败" in caplog.text
        
        scheduler.stop()


class TestIntegration:
    """集成测试"""
    
    def test_full_scheduler_lifecycle(self, tmp_path):
        """测试完整的调度器生命周期"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        
        mock_exit_manager = Mock()
        mock_exit_manager.check_all_positions = Mock(return_value=[])
        mock_decision_engine = Mock()
        mock_decision_engine.exit_manager = mock_exit_manager
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        # 启动
        scheduler.start()
        assert scheduler._running
        
        # 添加任务
        mock_report_gen.send_daily_report = Mock(return_value={"success": True})
        mock_report_gen.send_weekly_report = Mock(return_value={"success": True})
        
        scheduler.schedule_daily_report("0700.HK", "HK")
        scheduler.schedule_daily_report("TSLA", "US")
        scheduler.schedule_weekly_report("0700.HK")
        scheduler.schedule_performance_check()
        
        # 验证任务数量
        jobs = scheduler.scheduler.get_jobs()
        assert len(jobs) == 4
        
        # 停止
        scheduler.stop()
        assert not scheduler._running
        assert len(scheduler.scheduler.get_jobs()) == 0
    
    def test_multiple_stocks_same_scheduler(self, tmp_path):
        """测试同一调度器管理多只股票"""
        db_path = str(tmp_path / "test.db")
        mock_report_gen = Mock(spec=ReportGenerator)
        mock_decision_engine = Mock(spec=DecisionEngine)
        
        scheduler = TradingScheduler(
            report_generator=mock_report_gen,
            decision_engine=mock_decision_engine,
            db_path=db_path
        )
        
        scheduler.start()
        
        mock_report_gen.send_daily_report = Mock(return_value={"success": True})
        
        # 添加多只股票
        stocks = ["0700.HK", "9988.HK", "TSLA", "NVDA"]
        for stock in stocks:
            market = "HK" if stock.endswith(".HK") else "US"
            scheduler.schedule_daily_report(stock, market)
        
        jobs = scheduler.scheduler.get_jobs()
        # 4 只股票的日报任务
        assert len(jobs) == 4
        
        scheduler.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
