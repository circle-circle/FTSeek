"""
测试 ReportGenerator - 报告生成器

测试覆盖:
1. 日报生成 - 当日交易明细、收益、持仓、累计收益
2. 周报生成 - 周收益率、胜率、最大回撤、夏普比率
3. 钉钉推送 - 格式正确、失败日志
4. 多股票汇总
"""
import pytest
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sqlite3
import tempfile

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.v4.report.report_generator import ReportGenerator


class TestReportGeneratorInit:
    """测试初始化"""
    
    def test_init_with_db_path(self):
        """测试使用数据库路径初始化"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            generator = ReportGenerator(db_path=db_path)
            assert generator.db_path == db_path
            assert generator.dingtalk_webhook is None
        finally:
            os.unlink(db_path)
    
    def test_init_with_dingtalk_webhook(self):
        """测试使用钉钉 webhook 初始化"""
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"
        generator = ReportGenerator(db_path=":memory:", dingtalk_webhook=webhook)
        assert generator.dingtalk_webhook == webhook
    
    def test_init_creates_tables(self):
        """测试初始化时创建必要的表"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            generator = ReportGenerator(db_path=db_path)
            
            # 检查表是否存在
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            
            assert 'report_trades' in tables
            assert 'report_positions' in tables
            assert 'report_performance' in tables
            conn.close()
        finally:
            os.unlink(db_path)


class TestDailyReportGeneration:
    """测试日报生成"""
    
    @pytest.fixture
    def generator(self):
        """创建测试用生成器"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        gen = ReportGenerator(db_path=db_path)
        
        # 插入测试数据
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 插入交易记录
        cursor.execute("""
            INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('0700.HK', 'BUY', 100, 380.5, 0, 0, datetime.now().isoformat()))
        
        cursor.execute("""
            INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('0700.HK', 'SELL', 100, 385.2, 470, 0.0123, (datetime.now() - timedelta(hours=1)).isoformat()))
        
        cursor.execute("""
            INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('MU', 'BUY', 50, 85.3, 0, 0, (datetime.now() - timedelta(minutes=30)).isoformat()))
        
        # 插入持仓记录
        cursor.execute("""
            INSERT INTO report_positions (code, quantity, avg_cost, current_price, market_type)
            VALUES (?, ?, ?, ?, ?)
        """, ('0700.HK', 100, 385.2, 388.5, 'HK'))
        
        cursor.execute("""
            INSERT INTO report_positions (code, quantity, avg_cost, current_price, market_type)
            VALUES (?, ?, ?, ?, ?)
        """, ('MU', 50, 85.3, 85.3, 'US'))
        
        # 插入绩效记录
        cursor.execute("""
            INSERT INTO report_performance (code, date, daily_return, cumulative_return, trade_count)
            VALUES (?, ?, ?, ?, ?)
        """, ('0700.HK', today, 0.032, 0.058, 2))
        
        cursor.execute("""
            INSERT INTO report_performance (code, date, daily_return, cumulative_return, trade_count)
            VALUES (?, ?, ?, ?, ?)
        """, ('MU', today, 0, 0, 1))
        
        conn.commit()
        conn.close()
        
        yield gen
        
        os.unlink(db_path)
    
    def test_generate_daily_report_all_stocks(self, generator):
        """测试生成全部股票的日报"""
        report = generator.generate_daily_report()
        
        assert '交易日报' in report
        assert '0700.HK' in report or '腾讯' in report
        assert 'MU' in report
        assert '当日交易' in report
        assert '累计收益' in report
    
    def test_generate_daily_report_single_stock(self, generator):
        """测试生成单只股票的日报"""
        report = generator.generate_daily_report(code='0700.HK')
        
        assert '0700.HK' in report or '腾讯' in report
        assert 'MU' not in report
    
    def test_generate_daily_report_with_date(self, generator):
        """测试生成指定日期的日报"""
        today = datetime.now().strftime('%Y-%m-%d')
        report = generator.generate_daily_report(date=today)
        
        assert today in report or '交易日报' in report
    
    def test_daily_report_format(self, generator):
        """测试日报格式正确"""
        report = generator.generate_daily_report()
        
        # 检查格式元素
        assert '━━' in report  # 分隔线
        assert '├─' in report or '└─' in report  # 树形结构
        assert '📊' in report  # emoji
    
    def test_daily_report_includes_pnl(self, generator):
        """测试日报包含收益信息"""
        report = generator.generate_daily_report(code='0700.HK')
        
        # 应该包含收益信息
        assert '收益' in report


class TestWeeklyReportGeneration:
    """测试周报生成"""
    
    @pytest.fixture
    def generator(self):
        """创建测试用生成器"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        gen = ReportGenerator(db_path=db_path)
        
        # 插入测试数据
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        now = datetime.now()
        
        # 插入多笔交易记录（模拟一周）
        for i in range(5):
            trade_date = (now - timedelta(days=i)).isoformat()
            pnl = 500 if i % 2 == 0 else -200  # 交替盈亏
            
            cursor.execute("""
                INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ('0700.HK', 'SELL' if i % 2 == 0 else 'BUY', 100, 380.5 + i, pnl, pnl/38000, trade_date))
        
        for i in range(3):
            trade_date = (now - timedelta(days=i)).isoformat()
            pnl = 300 if i < 2 else -100
            
            cursor.execute("""
                INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ('MU', 'SELL' if i < 2 else 'BUY', 50, 85.3 + i, pnl, pnl/4265, trade_date))
        
        # 插入绩效记录
        cursor.execute("""
            INSERT INTO report_performance (code, date, daily_return, cumulative_return, trade_count, win_count, max_drawdown, sharpe_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ('0700.HK', now.strftime('%Y-%m-%d'), 0.085, 0.085, 5, 4, -0.021, 1.85))
        
        cursor.execute("""
            INSERT INTO report_performance (code, date, daily_return, cumulative_return, trade_count, win_count, max_drawdown, sharpe_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ('MU', now.strftime('%Y-%m-%d'), 0.032, 0.032, 3, 2, -0.015, 1.42))
        
        conn.commit()
        conn.close()
        
        yield gen
        
        os.unlink(db_path)
    
    def test_generate_weekly_report_all_stocks(self, generator):
        """测试生成全部股票的周报"""
        report = generator.generate_weekly_report()
        
        assert '交易周报' in report
        assert '0700.HK' in report or '腾讯' in report
        assert 'MU' in report
        assert '周收益率' in report
        assert '胜率' in report
    
    def test_generate_weekly_report_single_stock(self, generator):
        """测试生成单只股票的周报"""
        report = generator.generate_weekly_report(code='0700.HK')
        
        assert '0700.HK' in report or '腾讯' in report
        assert 'MU' not in report
    
    def test_weekly_report_format(self, generator):
        """测试周报格式正确"""
        report = generator.generate_weekly_report()
        
        # 检查格式元素
        assert '━━' in report  # 分隔线
        assert '├─' in report or '└─' in report  # 树形结构
        assert '📈' in report  # emoji
    
    def test_weekly_report_includes_metrics(self, generator):
        """测试周报包含关键指标"""
        report = generator.generate_weekly_report(code='0700.HK')
        
        # 应该包含关键指标
        assert '收益率' in report
        assert '胜率' in report
        assert '回撤' in report or '夏普' in report


class TestDingTalkPush:
    """测试钉钉推送"""
    
    @pytest.fixture
    def generator(self):
        """创建测试用生成器"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        webhook = "https://oapi.dingtalk.com/robot/send?access_token=test"
        gen = ReportGenerator(db_path=db_path, dingtalk_webhook=webhook)
        
        # 插入基本测试数据
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        cursor.execute("""
            INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('0700.HK', 'BUY', 100, 380.5, 0, 0, datetime.now().isoformat()))
        
        cursor.execute("""
            INSERT INTO report_positions (code, quantity, avg_cost, current_price, market_type)
            VALUES (?, ?, ?, ?, ?)
        """, ('0700.HK', 100, 380.5, 385.2, 'HK'))
        
        cursor.execute("""
            INSERT INTO report_performance (code, date, daily_return, cumulative_return, trade_count)
            VALUES (?, ?, ?, ?, ?)
        """, ('0700.HK', today, 0.032, 0.058, 1))
        
        conn.commit()
        conn.close()
        
        yield gen
        
        os.unlink(db_path)
    
    @patch('app.v4.report.report_generator.requests.post')
    def test_send_daily_report_success(self, mock_post, generator):
        """测试发送日报成功"""
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 0}
        mock_post.return_value = mock_response
        
        result = generator.send_daily_report(code='0700.HK')
        
        assert result['success'] is True
        mock_post.assert_called_once()
    
    @patch('app.v4.report.report_generator.requests.post')
    def test_send_daily_report_failure(self, mock_post, generator):
        """测试发送日报失败"""
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 1, "errmsg": "error"}
        mock_post.return_value = mock_response
        
        result = generator.send_daily_report(code='0700.HK')
        
        assert result['success'] is False
        assert 'error' in str(result)
    
    @patch('app.v4.report.report_generator.requests.post')
    def test_send_weekly_report_success(self, mock_post, generator):
        """测试发送周报成功"""
        mock_response = Mock()
        mock_response.json.return_value = {"errcode": 0}
        mock_post.return_value = mock_response
        
        # 添加更多数据用于周报
        conn = sqlite3.connect(generator.db_path)
        cursor = conn.cursor()
        for i in range(3):
            cursor.execute("""
                INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ('0700.HK', 'SELL', 100, 380.5 + i, 500, 0.013, (datetime.now() - timedelta(days=i)).isoformat()))
        conn.commit()
        conn.close()
        
        result = generator.send_weekly_report(code='0700.HK')
        
        assert result['success'] is True
        mock_post.assert_called()
    
    def test_send_without_webhook(self):
        """测试没有 webhook 时发送失败"""
        gen = ReportGenerator(db_path=":memory:")
        
        result = gen.send_daily_report(code='0700.HK')
        
        assert result['success'] is False


class TestMultiStockSummary:
    """测试多股票汇总"""
    
    @pytest.fixture
    def generator(self):
        """创建测试用生成器"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        gen = ReportGenerator(db_path=db_path)
        
        # 插入多只股票数据
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        # 腾讯
        cursor.execute("""
            INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('0700.HK', 'BUY', 100, 380.5, 0, 0, datetime.now().isoformat()))
        
        cursor.execute("""
            INSERT INTO report_positions (code, quantity, avg_cost, current_price, market_type)
            VALUES (?, ?, ?, ?, ?)
        """, ('0700.HK', 100, 380.5, 385.2, 'HK'))
        
        cursor.execute("""
            INSERT INTO report_performance (code, date, daily_return, cumulative_return, trade_count)
            VALUES (?, ?, ?, ?, ?)
        """, ('0700.HK', today, 0.032, 0.058, 1))
        
        # MU
        cursor.execute("""
            INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ('MU', 'BUY', 50, 85.3, 0, 0, datetime.now().isoformat()))
        
        cursor.execute("""
            INSERT INTO report_positions (code, quantity, avg_cost, current_price, market_type)
            VALUES (?, ?, ?, ?, ?)
        """, ('MU', 50, 85.3, 85.3, 'US'))
        
        cursor.execute("""
            INSERT INTO report_performance (code, date, daily_return, cumulative_return, trade_count)
            VALUES (?, ?, ?, ?, ?)
        """, ('MU', today, 0, 0, 1))
        
        conn.commit()
        conn.close()
        
        yield gen
        
        os.unlink(db_path)
    
    def test_multi_stock_daily_report(self, generator):
        """测试多股票日报汇总"""
        report = generator.generate_daily_report()
        
        assert '0700.HK' in report
        assert 'MU' in report
        assert '汇总' in report
    
    def test_multi_stock_weekly_report(self, generator):
        """测试多股票周报汇总"""
        # 添加更多数据
        conn = sqlite3.connect(generator.db_path)
        cursor = conn.cursor()
        
        for code in ['0700.HK', 'MU']:
            for i in range(3):
                cursor.execute("""
                    INSERT INTO report_trades (code, action, quantity, price, pnl, pnl_percent, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (code, 'SELL', 100, 380.5 + i, 500, 0.013, (datetime.now() - timedelta(days=i)).isoformat()))
        
        conn.commit()
        conn.close()
        
        report = generator.generate_weekly_report()
        
        assert '0700.HK' in report
        assert 'MU' in report
        assert '汇总' in report or '建议' in report


class TestEdgeCases:
    """测试边界情况"""
    
    @pytest.fixture
    def generator(self):
        """创建测试用生成器"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        yield ReportGenerator(db_path=db_path)
        
        os.unlink(db_path)
    
    def test_no_trades_today(self, generator):
        """测试当日无交易"""
        report = generator.generate_daily_report()
        
        assert '交易日报' in report
        # 应该显示 0 笔交易或类似信息
    
    def test_no_data_for_week(self, generator):
        """测试本周无数据"""
        report = generator.generate_weekly_report()
        
        assert '交易周报' in report
    
    def test_invalid_stock_code(self, generator):
        """测试无效股票代码"""
        report = generator.generate_daily_report(code='INVALID')
        
        # 应该返回空报告或错误提示
        assert isinstance(report, str)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--cov=app/v4/report/report_generator', '--cov-report=term-missing'])
