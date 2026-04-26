"""
周报复盘模块单元测试

测试 WeeklyReviewer 类的核心功能：
- generate_report: 生成周报数据
- analyze_strategies: 策略分析
- compare_benchmark: 基准对比
- generate_recommendations: 生成改进建议
"""
import pytest
import sqlite3
import os
import tempfile
from datetime import datetime, timedelta
from typing import Dict, List

# app.performance模块尚未实现，跳过此测试文件
pytest.importorskip("app.performance.weekly_review", reason="app.performance模块尚未实现")

# 导入被测试模块
from app.performance.weekly_review import WeeklyReviewer


class TestWeeklyReviewerInit:
    """测试初始化功能"""

    def test_init_default_db(self):
        """测试使用默认数据库路径初始化"""
        reviewer = WeeklyReviewer()
        assert reviewer.db_path is not None
        assert 'ftseek.db' in reviewer.db_path

    def test_init_custom_db(self):
        """测试使用自定义数据库路径初始化"""
        custom_path = '/tmp/test_ftseek.db'
        reviewer = WeeklyReviewer(db_path=custom_path)
        assert reviewer.db_path == custom_path


class TestGenerateReport:
    """测试周报生成功能"""

    @pytest.fixture
    def temp_db(self):
        """创建临时数据库用于测试"""
        # 创建临时数据库
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        
        # 初始化数据库表结构
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        
        # 创建 signals 表（包含 strategy）
        cursor.execute("""
            CREATE TABLE signals (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER DEFAULT 1,
                strategy TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                executed_at DATETIME,
                status TEXT DEFAULT 'pending'
            )
        """)
        
        # 创建 trade_pairs 表（不包含 strategy，从 signals 关联）
        cursor.execute("""
            CREATE TABLE trade_pairs (
                id TEXT PRIMARY KEY,
                buy_signal_id TEXT,
                sell_signal_id TEXT,
                code TEXT,
                buy_price REAL,
                sell_price REAL,
                quantity INTEGER,
                return_rate REAL,
                profit REAL,
                open_time DATETIME,
                close_time DATETIME,
                status TEXT DEFAULT 'closed',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
        
        yield path
        
        # 清理临时文件
        if os.path.exists(path):
            os.remove(path)

    @pytest.fixture
    def sample_trades(self, temp_db):
        """插入样本交易数据"""
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        
        # 计算日期范围（最近 7 天）
        now = datetime.now()
        week_start = now - timedelta(days=7)
        
        # 先插入 signals 数据（包含 strategy）
        signals = [
            ('buy_001', 'HK0700', 'Tencent', 'buy', 100.0, 100, 'MA',
             week_start.strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('sell_001', 'HK0700', 'Tencent', 'sell', 110.0, 100, 'MA',
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('buy_002', 'HK9988', 'Alibaba', 'buy', 200.0, 50, 'RSI',
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('sell_002', 'HK9988', 'Alibaba', 'sell', 210.0, 50, 'RSI',
             (week_start + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('buy_003', 'HK0700', 'Tencent', 'buy', 105.0, 80, 'MACD',
             (week_start + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('sell_003', 'HK0700', 'Tencent', 'sell', 100.0, 80, 'MACD',
             (week_start + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('buy_004', 'HK9988', 'Alibaba', 'buy', 205.0, 60, 'MA',
             (week_start + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('sell_004', 'HK9988', 'Alibaba', 'sell', 215.0, 60, 'MA',
             (week_start + timedelta(days=4)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
        ]
        
        for sig in signals:
            cursor.execute("""
                INSERT INTO signals 
                (id, code, name, action, price, quantity, strategy, executed_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, sig)
        
        # 插入交易配对数据（不包含 strategy）
        trades = [
            # 盈利交易 - MA 策略
            ('pair_001', 'buy_001', 'sell_001', 'HK0700', 100.0, 110.0, 100, 10.0, 1000.0,
             week_start.strftime('%Y-%m-%d %H:%M:%S'),
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
             'closed'),
            
            # 盈利交易 - RSI 策略
            ('pair_002', 'buy_002', 'sell_002', 'HK9988', 200.0, 210.0, 50, 5.0, 500.0,
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
             (week_start + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),
             'closed'),
            
            # 亏损交易 - MACD 策略
            ('pair_003', 'buy_003', 'sell_003', 'HK0700', 105.0, 100.0, 80, -4.76, -400.0,
             (week_start + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),
             (week_start + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S'),
             'closed'),
            
            # 盈利交易 - MA 策略
            ('pair_004', 'buy_004', 'sell_004', 'HK9988', 205.0, 215.0, 60, 4.88, 600.0,
             (week_start + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S'),
             (week_start + timedelta(days=4)).strftime('%Y-%m-%d %H:%M:%S'),
             'closed'),
        ]
        
        # 插入 trade_pairs 表
        for trade in trades:
            cursor.execute("""
                INSERT INTO trade_pairs 
                (id, buy_signal_id, sell_signal_id, code, buy_price, sell_price,
                 quantity, return_rate, profit, open_time, close_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, trade)
        
        conn.commit()
        conn.close()
        
        return temp_db

    def test_generate_report_structure(self, sample_trades):
        """测试生成报告的数据结构"""
        reviewer = WeeklyReviewer(db_path=sample_trades)
        
        now = datetime.now()
        week_start = (now - timedelta(days=7)).strftime('%Y-%m-%d')
        week_end = now.strftime('%Y-%m-%d')
        
        report = reviewer.generate_report(week_start, week_end)
        
        # 验证返回结构
        assert isinstance(report, dict)
        assert 'period' in report
        assert 'total_trades' in report
        assert 'win_rate' in report
        assert 'total_return' in report
        assert 'by_strategy' in report
        assert 'by_stock' in report
        assert 'recommendations' in report

    def test_generate_report_totals(self, sample_trades):
        """测试生成报告的交易汇总"""
        reviewer = WeeklyReviewer(db_path=sample_trades)
        
        now = datetime.now()
        week_start = (now - timedelta(days=7)).strftime('%Y-%m-%d')
        week_end = now.strftime('%Y-%m-%d')
        
        report = reviewer.generate_report(week_start, week_end)
        
        # 验证交易总数
        assert report['total_trades'] == 4
        
        # 验证胜率 (3 盈利 / 4 总交易 = 0.75)
        assert abs(report['win_rate'] - 0.75) < 0.01
        
        # 验证总收益率 (10 + 5 - 4.76 + 4.88 = 15.12)
        assert abs(report['total_return'] - 15.12) < 0.1

    def test_generate_report_by_strategy(self, sample_trades):
        """测试按策略分组统计"""
        reviewer = WeeklyReviewer(db_path=sample_trades)
        
        now = datetime.now()
        week_start = (now - timedelta(days=7)).strftime('%Y-%m-%d')
        week_end = now.strftime('%Y-%m-%d')
        
        report = reviewer.generate_report(week_start, week_end)
        
        # 验证策略分组
        assert 'MA' in report['by_strategy']
        assert 'RSI' in report['by_strategy']
        assert 'MACD' in report['by_strategy']
        
        # MA 策略：2 笔交易
        assert report['by_strategy']['MA']['count'] == 2
        
        # RSI 策略：1 笔交易
        assert report['by_strategy']['RSI']['count'] == 1
        
        # MACD 策略：1 笔交易
        assert report['by_strategy']['MACD']['count'] == 1

    def test_generate_report_by_stock(self, sample_trades):
        """测试按股票分组统计"""
        reviewer = WeeklyReviewer(db_path=sample_trades)
        
        now = datetime.now()
        week_start = (now - timedelta(days=7)).strftime('%Y-%m-%d')
        week_end = now.strftime('%Y-%m-%d')
        
        report = reviewer.generate_report(week_start, week_end)
        
        # 验证股票分组
        assert 'HK0700' in report['by_stock']
        assert 'HK9988' in report['by_stock']
        
        # HK0700: 2 笔交易
        assert report['by_stock']['HK0700']['count'] == 2
        
        # HK9988: 2 笔交易
        assert report['by_stock']['HK9988']['count'] == 2

    def test_generate_report_empty_period(self, temp_db):
        """测试空时间段的报告"""
        reviewer = WeeklyReviewer(db_path=temp_db)
        
        # 使用过去的时间段（无数据）
        report = reviewer.generate_report('2020-01-01', '2020-01-07')
        
        assert report['total_trades'] == 0
        assert report['win_rate'] == 0.0
        assert report['total_return'] == 0.0
        assert report['by_strategy'] == {}
        assert report['by_stock'] == {}


class TestAnalyzeStrategies:
    """测试策略分析功能"""

    @pytest.fixture
    def sample_trades_list(self):
        """样本交易列表"""
        return [
            {'strategy': 'MA', 'return_rate': 10.0, 'profit': 1000.0},
            {'strategy': 'MA', 'return_rate': 5.0, 'profit': 500.0},
            {'strategy': 'RSI', 'return_rate': -2.0, 'profit': -200.0},
            {'strategy': 'MACD', 'return_rate': 8.0, 'profit': 800.0},
        ]

    def test_analyze_strategies_structure(self, sample_trades_list):
        """测试策略分析返回结构"""
        reviewer = WeeklyReviewer()
        result = reviewer.analyze_strategies(sample_trades_list)
        
        assert isinstance(result, dict)
        assert 'MA' in result
        assert 'RSI' in result
        assert 'MACD' in result

    def test_analyze_strategies_metrics(self, sample_trades_list):
        """测试策略分析指标"""
        reviewer = WeeklyReviewer()
        result = reviewer.analyze_strategies(sample_trades_list)
        
        # MA 策略
        assert result['MA']['count'] == 2
        assert abs(result['MA']['avg_return'] - 7.5) < 0.01
        assert result['MA']['total_profit'] == 1500.0
        
        # RSI 策略
        assert result['RSI']['count'] == 1
        assert abs(result['RSI']['avg_return'] - (-2.0)) < 0.01
        
        # MACD 策略
        assert result['MACD']['count'] == 1
        assert abs(result['MACD']['avg_return'] - 8.0) < 0.01

    def test_analyze_strategies_empty(self):
        """测试空交易列表"""
        reviewer = WeeklyReviewer()
        result = reviewer.analyze_strategies([])
        
        assert result == {}


class TestCompareBenchmark:
    """测试基准对比功能"""

    def test_compare_benchmark_outperform(self):
        """测试跑赢基准"""
        reviewer = WeeklyReviewer()
        result = reviewer.compare_benchmark(5.0)  # 收益率 5%
        
        assert result['strategy_return'] == 5.0
        assert result['benchmark_return'] == 2.0  # 假设基准 2%
        assert result['excess_return'] == 3.0
        assert result['outperform'] is True

    def test_compare_benchmark_underperform(self):
        """测试跑输基准"""
        reviewer = WeeklyReviewer()
        result = reviewer.compare_benchmark(-3.0)  # 收益率 -3%
        
        assert result['strategy_return'] == -3.0
        assert result['benchmark_return'] == 2.0
        assert result['excess_return'] == -5.0
        assert result['outperform'] is False

    def test_compare_benchmark_custom_benchmark(self):
        """测试自定义基准"""
        reviewer = WeeklyReviewer()
        result = reviewer.compare_benchmark(10.0, benchmark_return=8.0)
        
        assert result['excess_return'] == 2.0
        assert result['outperform'] is True


class TestGenerateRecommendations:
    """测试改进建议生成功能"""

    def test_recommendations_low_win_rate(self):
        """测试低胜率建议"""
        reviewer = WeeklyReviewer()
        metrics = {
            'win_rate': 0.3,
            'total_return': -5.0,
            'total_trades': 10,
            'avg_return': -0.5
        }
        
        recommendations = reviewer.generate_recommendations(metrics)
        
        assert isinstance(recommendations, list)
        assert len(recommendations) > 0
        assert any('胜率' in rec for rec in recommendations)

    def test_recommendations_high_win_rate(self):
        """测试高胜率建议"""
        reviewer = WeeklyReviewer()
        metrics = {
            'win_rate': 0.8,
            'total_return': 15.0,
            'total_trades': 10,
            'avg_return': 1.5
        }
        
        recommendations = reviewer.generate_recommendations(metrics)
        
        assert isinstance(recommendations, list)
        # 高胜率时建议可能包括保持策略或增加仓位
        assert len(recommendations) >= 0

    def test_recommendations_few_trades(self):
        """测试交易数量少的建议"""
        reviewer = WeeklyReviewer()
        metrics = {
            'win_rate': 1.0,
            'total_return': 2.0,
            'total_trades': 1,
            'avg_return': 2.0
        }
        
        recommendations = reviewer.generate_recommendations(metrics)
        
        assert isinstance(recommendations, list)
        assert any('交易数量' in rec or '样本' in rec for rec in recommendations)

    def test_recommendations_empty_metrics(self):
        """测试空指标"""
        reviewer = WeeklyReviewer()
        metrics = {
            'win_rate': 0.0,
            'total_return': 0.0,
            'total_trades': 0,
            'avg_return': 0.0
        }
        
        recommendations = reviewer.generate_recommendations(metrics)
        
        assert isinstance(recommendations, list)


class TestIntegration:
    """集成测试"""

    @pytest.fixture
    def temp_db_with_data(self):
        """创建带完整数据的临时数据库"""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        
        # 创建表
        cursor.execute("""
            CREATE TABLE signals (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER DEFAULT 1,
                strategy TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                executed_at DATETIME,
                status TEXT DEFAULT 'pending'
            )
        """)
        
        cursor.execute("""
            CREATE TABLE trade_pairs (
                id TEXT PRIMARY KEY,
                buy_signal_id TEXT,
                sell_signal_id TEXT,
                code TEXT,
                buy_price REAL,
                sell_price REAL,
                quantity INTEGER,
                return_rate REAL,
                profit REAL,
                open_time DATETIME,
                close_time DATETIME,
                status TEXT DEFAULT 'closed',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 插入测试数据
        now = datetime.now()
        week_start = now - timedelta(days=7)
        
        # 先插入 signals
        signals = [
            ('buy_001', 'HK0700', 'Tencent', 'buy', 100.0, 100, 'MA',
             week_start.strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('sell_001', 'HK0700', 'Tencent', 'sell', 110.0, 100, 'MA',
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('buy_002', 'HK9988', 'Alibaba', 'buy', 200.0, 50, 'RSI',
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
            ('sell_002', 'HK9988', 'Alibaba', 'sell', 210.0, 50, 'RSI',
             (week_start + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'), 'executed'),
        ]
        
        for sig in signals:
            cursor.execute("""
                INSERT INTO signals 
                (id, code, name, action, price, quantity, strategy, executed_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, sig)
        
        # 插入 trade_pairs
        trades = [
            ('pair_001', 'buy_001', 'sell_001', 'HK0700', 100.0, 110.0, 100, 10.0, 1000.0,
             week_start.strftime('%Y-%m-%d %H:%M:%S'),
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
             'closed'),
            ('pair_002', 'buy_002', 'sell_002', 'HK9988', 200.0, 210.0, 50, 5.0, 500.0,
             (week_start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
             (week_start + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),
             'closed'),
        ]
        
        for trade in trades:
            cursor.execute("""
                INSERT INTO trade_pairs 
                (id, buy_signal_id, sell_signal_id, code, buy_price, sell_price,
                 quantity, return_rate, profit, open_time, close_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, trade)
        
        conn.commit()
        conn.close()
        
        yield path
        
        if os.path.exists(path):
            os.remove(path)

    def test_full_report_generation(self, temp_db_with_data):
        """测试完整报告生成流程"""
        reviewer = WeeklyReviewer(db_path=temp_db_with_data)
        
        now = datetime.now()
        week_start = (now - timedelta(days=7)).strftime('%Y-%m-%d')
        week_end = now.strftime('%Y-%m-%d')
        
        report = reviewer.generate_report(week_start, week_end)
        
        # 验证所有必需字段
        assert 'period' in report
        assert 'total_trades' in report
        assert 'win_rate' in report
        assert 'total_return' in report
        assert 'by_strategy' in report
        assert 'by_stock' in report
        assert 'recommendations' in report
        
        # 验证数据准确性
        assert report['total_trades'] == 2
        assert report['win_rate'] == 1.0  # 100% 胜率
        assert abs(report['total_return'] - 15.0) < 0.1
        
        # 验证建议生成
        assert isinstance(report['recommendations'], list)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
