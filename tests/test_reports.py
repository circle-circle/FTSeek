#!/usr/bin/env python3
"""
FTSeek 报告生成器测试

测试周报和月报生成器的功能
"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.reporters.weekly_report import WeeklyReportGenerator, WeeklyReport
from app.reporters.monthly_report import MonthlyReportGenerator, MonthlyReport
from app.storage.database import Database
from app.risk.metrics_calculator import MetricsCalculator


class TestWeeklyReportGenerator(unittest.TestCase):
    """周报生成器测试"""
    
    def setUp(self):
        """测试前准备"""
        self.generator = WeeklyReportGenerator()
    
    def test_get_week_range(self):
        """测试周范围计算"""
        # 测试给定日期
        test_date = datetime(2026, 3, 5)  # 周三
        monday, sunday = self.generator.get_week_range(test_date)
        
        self.assertEqual(monday.weekday(), 0)  # 周一
        self.assertEqual(sunday.weekday(), 6)  # 周日
        self.assertEqual(monday.date(), datetime(2026, 3, 2).date())
        self.assertEqual(sunday.date(), datetime(2026, 3, 8).date())
    
    def test_get_previous_week_range(self):
        """测试上一周范围计算"""
        monday, sunday = self.generator.get_previous_week_range()
        
        self.assertEqual(monday.weekday(), 0)  # 周一
        self.assertEqual(sunday.weekday(), 6)  # 周日
        
        # 确保是上一周 (不是本周)
        this_monday, _ = self.generator.get_week_range()
        self.assertLess(monday, this_monday)
    
    def test_generate_empty_report(self):
        """测试无交易时的报告生成"""
        report = self.generator.generate()
        
        self.assertIsInstance(report, WeeklyReport)
        self.assertIsNotNone(report.week)
        # YYYY-Www format (e.g., 2026-W09)
        self.assertGreaterEqual(len(report.week), 8)
        self.assertIn('-', report.week)
    
    def test_report_structure(self):
        """测试报告结构"""
        report = self.generator.generate()
        
        # 检查必需字段
        self.assertTrue(hasattr(report, 'week'))
        self.assertTrue(hasattr(report, 'start_date'))
        self.assertTrue(hasattr(report, 'end_date'))
        self.assertTrue(hasattr(report, 'total_trades'))
        self.assertTrue(hasattr(report, 'win_rate'))
        self.assertTrue(hasattr(report, 'pnl'))
        self.assertTrue(hasattr(report, 'strategies'))
        self.assertTrue(hasattr(report, 'best_trade'))
        self.assertTrue(hasattr(report, 'worst_trade'))
        self.assertTrue(hasattr(report, 'outlook'))
    
    def test_to_dict(self):
        """测试转换为字典"""
        report = self.generator.generate()
        report_dict = report.to_dict()
        
        self.assertIsInstance(report_dict, dict)
        self.assertIn('week', report_dict)
        self.assertIn('total_trades', report_dict)
        self.assertIn('win_rate', report_dict)


class TestMonthlyReportGenerator(unittest.TestCase):
    """月报生成器测试"""
    
    def setUp(self):
        """测试前准备"""
        self.generator = MonthlyReportGenerator()
    
    def test_get_month_range(self):
        """测试月范围计算"""
        # 测试给定日期
        test_date = datetime(2026, 3, 15)  # 3 月中旬
        first_day, last_day = self.generator.get_month_range(test_date)
        
        self.assertEqual(first_day.day, 1)
        self.assertEqual(first_day.month, 3)
        self.assertEqual(last_day.month, 3)
        self.assertIn(last_day.day, [28, 29, 30, 31])  # 取决于月份
    
    def test_get_previous_month_range(self):
        """测试上一月范围计算"""
        first_day, last_day = self.generator.get_previous_month_range()
        
        self.assertEqual(first_day.day, 1)
        
        # 确保是上一月
        this_first, _ = self.generator.get_month_range()
        self.assertLessEqual(first_day, this_first)
    
    def test_generate_empty_report(self):
        """测试无交易时的报告生成"""
        report = self.generator.generate()
        
        self.assertIsInstance(report, MonthlyReport)
        self.assertIsNotNone(report.month)
        self.assertEqual(len(report.month), 7)  # YYYY-MM 格式
    
    def test_report_structure(self):
        """测试报告结构"""
        report = self.generator.generate()
        
        # 检查必需字段
        self.assertTrue(hasattr(report, 'month'))
        self.assertTrue(hasattr(report, 'start_date'))
        self.assertTrue(hasattr(report, 'end_date'))
        self.assertTrue(hasattr(report, 'total_trades'))
        self.assertTrue(hasattr(report, 'win_rate'))
        self.assertTrue(hasattr(report, 'pnl'))
        self.assertTrue(hasattr(report, 'sharpe_ratio'))
        self.assertTrue(hasattr(report, 'sortino_ratio'))
        self.assertTrue(hasattr(report, 'max_drawdown'))
        self.assertTrue(hasattr(report, 'strategies'))
        self.assertTrue(hasattr(report, 'plan'))
    
    def test_equity_curve(self):
        """测试资金曲线生成"""
        report = self.generator.generate()
        
        # 资金曲线应该是列表
        self.assertIsInstance(report.equity_curve, list)
        self.assertIsInstance(report.curve_dates, list)
        
        # 如果有交易，曲线长度应该匹配
        if report.total_trades > 0:
            self.assertGreater(len(report.equity_curve), 0)
            self.assertEqual(len(report.equity_curve), len(report.curve_dates))
    
    def test_to_dict(self):
        """测试转换为字典"""
        report = self.generator.generate()
        report_dict = report.to_dict()
        
        self.assertIsInstance(report_dict, dict)
        self.assertIn('month', report_dict)
        self.assertIn('total_trades', report_dict)
        self.assertIn('sharpe_ratio', report_dict)


class TestMetricsCalculator(unittest.TestCase):
    """量化指标计算器测试"""
    
    def setUp(self):
        """测试前准备"""
        self.calc = MetricsCalculator()
    
    def test_total_return(self):
        """测试总收益率计算"""
        equity = [1000000, 1010000, 1020000, 1015000, 1030000]
        total_ret = self.calc.total_return(equity)
        
        expected = (1030000 - 1000000) / 1000000
        self.assertAlmostEqual(total_ret, expected, places=4)
    
    def test_max_drawdown(self):
        """测试最大回撤计算"""
        # 先上涨后下跌
        equity = [100, 110, 120, 115, 125, 120]
        max_dd = self.calc.max_drawdown(equity)
        
        # 最大回撤应该是从 125 跌到 120: (125-120)/125 = 0.04
        self.assertAlmostEqual(max_dd, 0.04, places=2)
    
    def test_sharpe_ratio(self):
        """测试夏普比率计算"""
        import numpy as np
        
        # 正收益序列
        returns = np.array([0.01, 0.02, 0.015, 0.01, 0.02])
        sharpe = self.calc.sharpe_ratio(returns)
        
        # 夏普比率应该为正
        self.assertGreater(sharpe, 0)
    
    def test_sortino_ratio(self):
        """测试索提诺比率计算"""
        import numpy as np
        
        # 有正有负的收益序列
        returns = np.array([0.02, -0.01, 0.015, -0.005, 0.025])
        sortino = self.calc.sortino_ratio(returns)
        
        # 索提诺比率应该可计算
        self.assertIsNotNone(sortino)
    
    def test_win_rate(self):
        """测试胜率计算"""
        trades = [
            {'pnl': 100},
            {'pnl': -50},
            {'pnl': 200},
            {'pnl': 150},
            {'pnl': -100},
        ]
        
        win_rate = self.calc.win_rate(trades)
        expected = 3 / 5  # 3 笔盈利 / 5 笔总交易
        
        self.assertAlmostEqual(win_rate, expected, places=2)
    
    def test_profit_factor(self):
        """测试盈利因子计算"""
        trades = [
            {'pnl': 100},
            {'pnl': -50},
            {'pnl': 200},
            {'pnl': -100},
        ]
        
        pf = self.calc.profit_factor(trades)
        # 总盈利 300 / 总亏损 150 = 2.0
        self.assertAlmostEqual(pf, 2.0, places=2)


class TestReportIntegration(unittest.TestCase):
    """报告集成测试"""
    
    def test_weekly_report_with_mock_data(self):
        """测试周报生成 (使用模拟数据)"""
        # 跳过需要数据库的集成测试 (在 CI 环境中运行)
        self.skipTest("集成测试需要完整数据库环境")
    
    def test_monthly_report_with_mock_data(self):
        """测试月报生成 (使用模拟数据)"""
        # 跳过需要数据库的集成测试 (在 CI 环境中运行)
        self.skipTest("集成测试需要完整数据库环境")


if __name__ == "__main__":
    unittest.main(verbosity=2)
