"""
回测对比模块单元测试

测试 BacktestComparer 类的核心功能：
- compare_returns: 收益率对比
- compare_win_rates: 胜率对比
- compare_trades: 交易次数对比
- generate_report: 生成对比报告
"""
import pytest
import sqlite3
import os
from datetime import datetime, timedelta
from typing import Dict, List

# 导入被测试模块
from app.performance.backtest_compare import BacktestComparer


class TestBacktestComparer:
    """BacktestComparer 测试类"""

    @pytest.fixture
    def test_db(self, tmp_path):
        """创建测试数据库"""
        db_path = str(tmp_path / "test_ftseek.db")
        self._create_test_tables(db_path)
        self._insert_test_data(db_path)
        return db_path

    def _create_test_tables(self, db_path: str):
        """创建测试表结构"""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 创建 signals 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                quantity INTEGER DEFAULT 1,
                strategy TEXT,
                technical_score REAL,
                sentiment_score REAL,
                confidence REAL,
                reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                expire_at DATETIME,
                confirmed_at DATETIME,
                executed_at DATETIME,
                status TEXT DEFAULT 'pending'
            )
        """)

        # 创建 trade_pairs 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_pairs (
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

        # 创建 backtest_trades 表 (回测数据)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                strategy TEXT,
                buy_price REAL NOT NULL,
                sell_price REAL NOT NULL,
                quantity INTEGER DEFAULT 1,
                return_rate REAL,
                profit REAL,
                open_time DATETIME,
                close_time DATETIME,
                status TEXT DEFAULT 'closed'
            )
        """)

        conn.commit()
        conn.close()

    def _insert_test_data(self, db_path: str):
        """插入测试数据"""
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        now = datetime.now()
        past_30d = now - timedelta(days=30)

        # 插入实盘交易配对数据 (10 笔交易，7 笔盈利)
        live_trades = [
            # 盈利交易 (return_rate > 0)
            ('pair_001', 'HK0700', 300.0, 330.0, 100, 10.0, 3000.0, 'closed'),
            ('pair_002', 'HK0700', 320.0, 350.0, 100, 9.375, 3000.0, 'closed'),
            ('pair_003', 'HK9988', 100.0, 110.0, 200, 10.0, 2000.0, 'closed'),
            ('pair_004', 'HK9988', 105.0, 115.0, 200, 9.52, 2000.0, 'closed'),
            ('pair_005', 'HK0700', 310.0, 340.0, 100, 9.68, 3000.0, 'closed'),
            ('pair_006', 'HK9988', 98.0, 108.0, 200, 10.2, 2000.0, 'closed'),
            ('pair_007', 'HK0700', 305.0, 335.0, 100, 9.84, 3000.0, 'closed'),
            # 亏损交易 (return_rate < 0)
            ('pair_008', 'HK0700', 340.0, 320.0, 100, -5.88, -2000.0, 'closed'),
            ('pair_009', 'HK9988', 112.0, 105.0, 200, -6.25, -1400.0, 'closed'),
            ('pair_010', 'HK0700', 330.0, 315.0, 100, -4.55, -1500.0, 'closed'),
        ]

        for trade in live_trades:
            cursor.execute("""
                INSERT INTO trade_pairs 
                (id, code, buy_price, sell_price, quantity, return_rate, profit, status, close_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (*trade, now.isoformat()))

        # 插入回测交易数据 (10 笔交易，8 笔盈利)
        backtest_trades = [
            # 盈利交易
            ('bt_001', 'HK0700', 'momentum', 300.0, 335.0, 100, 11.67, 3500.0, 'closed'),
            ('bt_002', 'HK0700', 'momentum', 320.0, 355.0, 100, 10.94, 3500.0, 'closed'),
            ('bt_003', 'HK9988', 'momentum', 100.0, 112.0, 200, 12.0, 2400.0, 'closed'),
            ('bt_004', 'HK9988', 'momentum', 105.0, 118.0, 200, 12.38, 2600.0, 'closed'),
            ('bt_005', 'HK0700', 'momentum', 310.0, 345.0, 100, 11.29, 3500.0, 'closed'),
            ('bt_006', 'HK9988', 'momentum', 98.0, 110.0, 200, 12.24, 2400.0, 'closed'),
            ('bt_007', 'HK0700', 'momentum', 305.0, 340.0, 100, 11.48, 3500.0, 'closed'),
            ('bt_008', 'HK9988', 'momentum', 102.0, 115.0, 200, 12.75, 2600.0, 'closed'),
            # 亏损交易
            ('bt_009', 'HK0700', 'momentum', 340.0, 325.0, 100, -4.41, -1500.0, 'closed'),
            ('bt_010', 'HK9988', 'momentum', 112.0, 108.0, 200, -3.57, -800.0, 'closed'),
        ]

        for trade in backtest_trades:
            cursor.execute("""
                INSERT INTO backtest_trades 
                (id, code, strategy, buy_price, sell_price, quantity, return_rate, profit, status, close_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (*trade, now.isoformat()))

        conn.commit()
        conn.close()

    def test_init(self, test_db):
        """测试初始化"""
        comparer = BacktestComparer(db_path=test_db)
        assert comparer.db_path == test_db
        assert comparer.live_db_path == test_db

    def test_compare_returns(self, test_db):
        """测试收益率对比"""
        comparer = BacktestComparer(db_path=test_db)
        result = comparer.compare_returns(period='30d')

        # 验证返回结构
        assert 'live_return' in result
        assert 'backtest_return' in result
        assert 'difference' in result

        # 验证数据类型
        assert isinstance(result['live_return'], float)
        assert isinstance(result['backtest_return'], float)
        assert isinstance(result['difference'], float)

        # 验证收益率计算 (实盘平均收益率)
        # 实盘：(10.0 + 9.375 + 10.0 + 9.52 + 9.68 + 10.2 + 9.84 - 5.88 - 6.25 - 4.55) / 10 = 5.193
        assert result['live_return'] > 0
        assert result['backtest_return'] > 0

        # 回测收益率应该高于实盘 (测试数据设定)
        assert result['backtest_return'] > result['live_return']

    def test_compare_returns_by_strategy(self, test_db):
        """测试按策略对比收益率"""
        comparer = BacktestComparer(db_path=test_db)
        result = comparer.compare_returns(period='30d', strategy='momentum')

        # 验证返回结构
        assert 'live_return' in result
        assert 'backtest_return' in result
        assert 'difference' in result

    def test_compare_win_rates(self, test_db):
        """测试胜率对比"""
        comparer = BacktestComparer(db_path=test_db)
        result = comparer.compare_win_rates(period='30d')

        # 验证返回结构
        assert 'live_win_rate' in result
        assert 'backtest_win_rate' in result
        assert 'difference' in result

        # 验证数据类型
        assert isinstance(result['live_win_rate'], float)
        assert isinstance(result['backtest_win_rate'], float)
        assert isinstance(result['difference'], float)

        # 验证胜率范围 (0-1)
        assert 0 <= result['live_win_rate'] <= 1
        assert 0 <= result['backtest_win_rate'] <= 1

        # 实盘胜率：7/10 = 0.7
        assert result['live_win_rate'] == pytest.approx(0.7, rel=0.01)

        # 回测胜率：8/10 = 0.8
        assert result['backtest_win_rate'] == pytest.approx(0.8, rel=0.01)

    def test_compare_trades(self, test_db):
        """测试交易次数对比"""
        comparer = BacktestComparer(db_path=test_db)
        result = comparer.compare_trades(period='30d')

        # 验证返回结构
        assert 'live_trades' in result
        assert 'backtest_trades' in result
        assert 'difference' in result

        # 验证数据类型
        assert isinstance(result['live_trades'], int)
        assert isinstance(result['backtest_trades'], int)

        # 验证交易次数
        assert result['live_trades'] == 10
        assert result['backtest_trades'] == 10
        assert result['difference'] == 0

    def test_compare_trades_by_code(self, test_db):
        """测试按股票代码对比交易"""
        comparer = BacktestComparer(db_path=test_db)
        result = comparer.compare_trades(period='30d', code='HK0700')

        # 验证返回结构
        assert 'live_trades' in result
        assert 'backtest_trades' in result

        # HK0700 实盘有 6 笔交易
        assert result['live_trades'] == 6

    def test_generate_report(self, test_db):
        """测试生成对比报告"""
        comparer = BacktestComparer(db_path=test_db)
        report = comparer.generate_report(period='30d')

        # 验证报告结构
        assert 'period' in report
        assert 'returns' in report
        assert 'win_rates' in report
        assert 'trades' in report
        assert 'summary' in report

        # 验证时间段
        assert report['period'] == '30d'

        # 验证收益率对比
        assert 'live_return' in report['returns']
        assert 'backtest_return' in report['returns']
        assert 'difference' in report['returns']

        # 验证胜率对比
        assert 'live_win_rate' in report['win_rates']
        assert 'backtest_win_rate' in report['win_rates']

        # 验证交易对比
        assert 'live_trades' in report['trades']
        assert 'backtest_trades' in report['trades']

    def test_generate_report_with_strategy(self, test_db):
        """测试生成带策略的对比报告"""
        comparer = BacktestComparer(db_path=test_db)
        report = comparer.generate_report(period='30d', strategy='momentum')

        # 验证报告结构
        assert 'period' in report
        assert 'strategy' in report
        assert report['strategy'] == 'momentum'

    def test_empty_data(self, tmp_path):
        """测试空数据情况"""
        db_path = str(tmp_path / "empty.db")
        self._create_test_tables(db_path)
        # 不插入任何数据

        comparer = BacktestComparer(db_path=db_path)
        report = comparer.generate_report(period='30d')

        # 验证空数据处理
        assert report['returns']['live_return'] == 0.0
        assert report['returns']['backtest_return'] == 0.0
        assert report['win_rates']['live_win_rate'] == 0.0
        assert report['trades']['live_trades'] == 0

    def test_get_live_trades(self, test_db):
        """测试获取实盘交易"""
        comparer = BacktestComparer(db_path=test_db)
        trades = comparer._get_live_trades(period='30d')

        assert len(trades) == 10
        assert all('return_rate' in t for t in trades)

    def test_get_backtest_trades(self, test_db):
        """测试获取回测交易"""
        comparer = BacktestComparer(db_path=test_db)
        trades = comparer._get_backtest_trades(period='30d')

        assert len(trades) == 10
        assert all('return_rate' in t for t in trades)

    def test_calculate_avg_return(self, test_db):
        """测试平均收益率计算"""
        comparer = BacktestComparer(db_path=test_db)
        trades = comparer._get_live_trades(period='30d')
        avg_return = comparer._calculate_avg_return(trades)

        # 验证平均收益率计算
        assert avg_return > 0


class TestBacktestComparerIntegration:
    """BacktestComparer 集成测试"""

    @pytest.fixture
    def production_db(self):
        """使用生产数据库 (如果存在)"""
        db_path = '/opt/ftseek/FTSeek/data/ftseek.db'
        if os.path.exists(db_path):
            return db_path
        pytest.skip("生产数据库不存在")

    def test_compare_with_production_data(self, production_db):
        """测试使用生产数据对比"""
        comparer = BacktestComparer(db_path=production_db)
        report = comparer.generate_report(period='30d')

        # 验证报告生成成功
        assert isinstance(report, dict)
        assert 'period' in report
