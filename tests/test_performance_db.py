"""
测试绩效追踪数据库扩展

验证新增的 trade_pairs 和 performance_summary 表结构
"""
import pytest
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta
from uuid import uuid4

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.database import Database


@pytest.fixture
def test_db():
    """创建测试数据库"""
    db_path = f"/tmp/ftseek_test_{uuid4()}.db"
    db = Database(db_path)
    yield db
    # 清理测试数据库 (Python 3.6 compatible)
    try:
        Path(db_path).unlink()
    except FileNotFoundError:
        pass


class TestTradePairsTable:
    """测试 trade_pairs 表"""
    
    def test_table_exists(self, test_db):
        """验证 trade_pairs 表已创建"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='trade_pairs'
            """)
            row = cursor.fetchone()
            assert row is not None, "trade_pairs 表不存在"
    
    def test_table_schema(self, test_db):
        """验证 trade_pairs 表结构"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(trade_pairs)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}
            
            # 验证必需字段
            expected_columns = {
                'id': 'TEXT',
                'buy_signal_id': 'TEXT',
                'sell_signal_id': 'TEXT',
                'code': 'TEXT',
                'buy_price': 'REAL',
                'sell_price': 'REAL',
                'quantity': 'INTEGER',
                'return_rate': 'REAL',
                'profit': 'REAL',
                'open_time': 'DATETIME',
                'close_time': 'DATETIME',
                'status': 'TEXT',
                'created_at': 'DATETIME'
            }
            
            for col, col_type in expected_columns.items():
                assert col in columns, f"缺少字段：{col}"
                assert columns[col].upper() == col_type.upper(), \
                    f"字段 {col} 类型错误：期望 {col_type}, 实际 {columns[col]}"
    
    def test_indexes_exist(self, test_db):
        """验证 trade_pairs 索引已创建"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND tbl_name='trade_pairs'
            """)
            indexes = {row[0] for row in cursor.fetchall()}
            
            expected_indexes = {
                'idx_trade_pairs_code',
                'idx_trade_pairs_status',
                'idx_trade_pairs_close_time'
            }
            
            for idx in expected_indexes:
                assert idx in indexes, f"缺少索引：{idx}"
    
    def test_insert_trade_pair(self, test_db):
        """测试插入交易配对记录"""
        trade_id = f"trade_{uuid4()}"
        buy_signal_id = f"buy_{uuid4()}"
        sell_signal_id = f"sell_{uuid4()}"
        
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trade_pairs 
                (id, buy_signal_id, sell_signal_id, code, buy_price, 
                 sell_price, quantity, return_rate, profit, 
                 open_time, close_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                buy_signal_id,
                sell_signal_id,
                'HK0700',
                300.0,
                330.0,
                100,
                0.10,
                3000.0,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                'closed'
            ))
        
        # 验证插入成功
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trade_pairs WHERE id = ?", (trade_id,))
            row = cursor.fetchone()
            assert row is not None, "插入失败"
            assert row['code'] == 'HK0700'
            assert row['buy_price'] == 300.0
            assert row['sell_price'] == 330.0
            assert row['return_rate'] == 0.10
            assert row['profit'] == 3000.0
    
    def test_foreign_key_constraints(self, test_db):
        """测试外键约束 (可选)"""
        # SQLite 默认不启用外键约束
        # 这个测试验证系统能正常工作即使没有外键
        trade_id = f"trade_{uuid4()}"
        
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            # 插入一个不存在的 signal_id (应该成功，因为外键可选)
            cursor.execute("""
                INSERT INTO trade_pairs 
                (id, buy_signal_id, sell_signal_id, code, buy_price, 
                 sell_price, quantity, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                'nonexistent_buy',
                'nonexistent_sell',
                'HK0700',
                300.0,
                330.0,
                100,
                'closed'
            ))
        
        # 验证插入成功
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trade_pairs WHERE id = ?", (trade_id,))
            count = cursor.fetchone()[0]
            assert count == 1
    
    def test_query_by_code(self, test_db):
        """测试按股票代码查询"""
        # 插入多条记录
        for i in range(5):
            trade_id = f"trade_{i}"
            with test_db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trade_pairs 
                    (id, buy_signal_id, sell_signal_id, code, buy_price, 
                     sell_price, quantity, status, close_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    f'buy_{i}',
                    f'sell_{i}',
                    'HK0700' if i < 3 else 'HK9988',
                    300.0,
                    330.0,
                    100,
                    'closed',
                    datetime.now().isoformat()
                ))
        
        # 查询特定股票
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trade_pairs 
                WHERE code = 'HK0700'
            """)
            rows = cursor.fetchall()
            assert len(rows) == 3
    
    def test_query_by_status(self, test_db):
        """测试按状态查询"""
        # 插入不同状态的记录
        for status in ['open', 'closed', 'closed']:
            trade_id = f"trade_{uuid4()}"
            with test_db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trade_pairs 
                    (id, buy_signal_id, sell_signal_id, code, buy_price, 
                     sell_price, quantity, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    'buy',
                    'sell',
                    'HK0700',
                    300.0,
                    330.0,
                    100,
                    status
                ))
        
        # 查询 closed 状态
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trade_pairs 
                WHERE status = 'closed'
            """)
            rows = cursor.fetchall()
            assert len(rows) == 2


class TestPerformanceSummaryTable:
    """测试 performance_summary 表"""
    
    def test_table_exists(self, test_db):
        """验证 performance_summary 表已创建"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='performance_summary'
            """)
            row = cursor.fetchone()
            assert row is not None, "performance_summary 表不存在"
    
    def test_table_schema(self, test_db):
        """验证 performance_summary 表结构"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(performance_summary)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}
            
            expected_columns = {
                'id': 'TEXT',
                'period': 'TEXT',
                'start_date': 'DATETIME',
                'end_date': 'DATETIME',
                'total_trades': 'INTEGER',
                'winning_trades': 'INTEGER',
                'losing_trades': 'INTEGER',
                'win_rate': 'REAL',
                'total_return': 'REAL',
                'avg_return': 'REAL',
                'total_profit': 'REAL',
                'total_loss': 'REAL',
                'max_drawdown': 'REAL',
                'created_at': 'DATETIME'
            }
            
            for col, col_type in expected_columns.items():
                assert col in columns, f"缺少字段：{col}"
                assert columns[col].upper() == col_type.upper(), \
                    f"字段 {col} 类型错误：期望 {col_type}, 实际 {columns[col]}"
    
    def test_indexes_exist(self, test_db):
        """验证 performance_summary 索引已创建"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='index' AND tbl_name='performance_summary'
            """)
            indexes = {row[0] for row in cursor.fetchall()}
            
            expected_indexes = {
                'idx_performance_summary_period',
                'idx_performance_summary_created_at'
            }
            
            for idx in expected_indexes:
                assert idx in indexes, f"缺少索引：{idx}"
    
    def test_insert_performance_summary(self, test_db):
        """测试插入绩效汇总记录"""
        summary_id = f"summary_{uuid4()}"
        now = datetime.now()
        
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO performance_summary 
                (id, period, start_date, end_date, total_trades, 
                 winning_trades, losing_trades, win_rate, total_return, 
                 avg_return, total_profit, total_loss, max_drawdown)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                summary_id,
                'weekly',
                (now - timedelta(days=7)).isoformat(),
                now.isoformat(),
                10,
                6,
                4,
                0.60,
                0.15,
                0.015,
                15000.0,
                -5000.0,
                -0.08
            ))
        
        # 验证插入成功
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM performance_summary WHERE id = ?
            """, (summary_id,))
            row = cursor.fetchone()
            assert row is not None, "插入失败"
            assert row['period'] == 'weekly'
            assert row['total_trades'] == 10
            assert row['winning_trades'] == 6
            assert row['losing_trades'] == 4
            assert row['win_rate'] == 0.60
            assert row['total_return'] == 0.15
    
    def test_query_by_period(self, test_db):
        """测试按期类型查询"""
        # 插入不同周期的记录
        for period in ['daily', 'weekly', 'weekly', 'monthly']:
            summary_id = f"summary_{uuid4()}"
            now = datetime.now()
            with test_db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO performance_summary 
                    (id, period, start_date, end_date, total_trades, 
                     winning_trades, losing_trades, win_rate, total_return, 
                     avg_return, total_profit, total_loss, max_drawdown)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    summary_id,
                    period,
                    (now - timedelta(days=7)).isoformat(),
                    now.isoformat(),
                    10,
                    6,
                    4,
                    0.60,
                    0.15,
                    0.015,
                    15000.0,
                    -5000.0,
                    -0.08
                ))
        
        # 查询 weekly 周期
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM performance_summary 
                WHERE period = 'weekly'
            """)
            rows = cursor.fetchall()
            assert len(rows) == 2


class TestBackwardCompatibility:
    """测试向后兼容性"""
    
    def test_existing_signals_table_unchanged(self, test_db):
        """验证 signals 表未被修改"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(signals)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}
            
            # 验证原有字段仍然存在
            expected_columns = {
                'id', 'code', 'name', 'action', 'price', 'quantity',
                'strategy', 'technical_score', 'sentiment_score',
                'confidence', 'reason', 'created_at', 'expire_at',
                'confirmed_at', 'executed_at', 'status'
            }
            
            for col in expected_columns:
                assert col in columns, f"signals 表缺少字段：{col}"
    
    def test_existing_trades_table_unchanged(self, test_db):
        """验证 trades 表未被修改"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(trades)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}
            
            expected_columns = {
                'id', 'signal_id', 'code', 'name', 'action',
                'entry_price', 'exit_price', 'quantity', 'pnl',
                'pnl_percent', 'fees', 'strategy', 'entry_time',
                'exit_time', 'exit_reason', 'order_id', 'status'
            }
            
            for col in expected_columns:
                assert col in columns, f"trades 表缺少字段：{col}"
    
    def test_existing_performance_snapshots_unchanged(self, test_db):
        """验证 performance_snapshots 表未被修改"""
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(performance_snapshots)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}
            
            expected_columns = {
                'id', 'period', 'start_date', 'end_date', 'total_trades',
                'winning_trades', 'losing_trades', 'win_rate',
                'profit_factor', 'max_drawdown', 'sharpe_ratio',
                'sortino_ratio', 'total_return', 'avg_win', 'avg_loss',
                'created_at'
            }
            
            for col in expected_columns:
                assert col in columns, f"performance_snapshots 表缺少字段：{col}"
    
    def test_database_operations_still_work(self, test_db):
        """验证现有数据库操作仍然正常工作"""
        # 测试保存信号
        signal_id = f"signal_{uuid4()}"
        test_db.save_signal({
            'id': signal_id,
            'code': 'HK0700',
            'name': '腾讯控股',
            'action': 'buy',
            'price': 300.0,
            'quantity': 100,
            'strategy': 'test_strategy',
            'confidence': 0.8
        })
        
        # 验证信号已保存
        signal = test_db.get_signal(signal_id)
        assert signal is not None
        assert signal['code'] == 'HK0700'
        assert signal['action'] == 'buy'
        
        # 测试保存交易
        trade_id = f"trade_{uuid4()}"
        test_db.save_trade({
            'id': trade_id,
            'signal_id': signal_id,
            'code': 'HK0700',
            'action': 'buy',
            'entry_price': 300.0,
            'quantity': 100,
            'status': 'open'
        })
        
        # 验证交易已保存
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row['code'] == 'HK0700'


class TestDatabaseMethods:
    """测试新增的数据库操作方法"""
    
    def test_save_trade_pair(self, test_db):
        """测试保存交易配对"""
        trade_id = f"trade_{uuid4()}"
        
        result = test_db.save_trade_pair({
            'id': trade_id,
            'buy_signal_id': f'buy_{uuid4()}',
            'sell_signal_id': f'sell_{uuid4()}',
            'code': 'HK0700',
            'buy_price': 300.0,
            'sell_price': 330.0,
            'quantity': 100,
            'return_rate': 0.10,
            'profit': 3000.0,
            'open_time': datetime.now().isoformat(),
            'close_time': datetime.now().isoformat(),
            'status': 'closed'
        })
        
        assert result is True
        
        # 验证数据已保存
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trade_pairs WHERE id = ?", (trade_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row['profit'] == 3000.0
    
    def test_get_trade_pairs_by_code(self, test_db):
        """测试按代码查询交易配对"""
        # 插入测试数据
        for i in range(3):
            trade_id = f"trade_{i}"
            test_db.save_trade_pair({
                'id': trade_id,
                'buy_signal_id': f'buy_{i}',
                'sell_signal_id': f'sell_{i}',
                'code': 'HK0700',
                'buy_price': 300.0,
                'sell_price': 330.0,
                'quantity': 100,
                'return_rate': 0.10,
                'profit': 3000.0,
                'open_time': datetime.now().isoformat(),
                'close_time': datetime.now().isoformat(),
                'status': 'closed'
            })
        
        # 查询
        pairs = test_db.get_trade_pairs_by_code('HK0700')
        assert len(pairs) == 3
    
    def test_get_trade_pairs_by_status(self, test_db):
        """测试按状态查询交易配对"""
        # 插入测试数据
        for status in ['open', 'closed', 'closed']:
            trade_id = f"trade_{uuid4()}"
            test_db.save_trade_pair({
                'id': trade_id,
                'buy_signal_id': 'buy',
                'sell_signal_id': 'sell',
                'code': 'HK0700',
                'buy_price': 300.0,
                'sell_price': 330.0,
                'quantity': 100,
                'return_rate': 0.10,
                'profit': 3000.0,
                'open_time': datetime.now().isoformat(),
                'close_time': datetime.now().isoformat(),
                'status': status
            })
        
        # 查询 closed 状态
        pairs = test_db.get_trade_pairs_by_status('closed')
        assert len(pairs) == 2
    
    def test_save_performance_summary(self, test_db):
        """测试保存绩效汇总"""
        summary_id = f"summary_{uuid4()}"
        now = datetime.now()
        
        result = test_db.save_performance_summary({
            'id': summary_id,
            'period': 'weekly',
            'start_date': (now - timedelta(days=7)).isoformat(),
            'end_date': now.isoformat(),
            'total_trades': 10,
            'winning_trades': 6,
            'losing_trades': 4,
            'win_rate': 0.60,
            'total_return': 0.15,
            'avg_return': 0.015,
            'total_profit': 15000.0,
            'total_loss': -5000.0,
            'max_drawdown': -0.08
        })
        
        assert result is True
        
        # 验证数据已保存
        with test_db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM performance_summary WHERE id = ?
            """, (summary_id,))
            row = cursor.fetchone()
            assert row is not None
            assert row['win_rate'] == 0.60
    
    def test_get_performance_summaries(self, test_db):
        """测试查询绩效汇总"""
        # 插入测试数据
        for period in ['daily', 'weekly', 'monthly']:
            summary_id = f"summary_{period}"
            now = datetime.now()
            test_db.save_performance_summary({
                'id': summary_id,
                'period': period,
                'start_date': (now - timedelta(days=7)).isoformat(),
                'end_date': now.isoformat(),
                'total_trades': 10,
                'winning_trades': 6,
                'losing_trades': 4,
                'win_rate': 0.60,
                'total_return': 0.15,
                'avg_return': 0.015,
                'total_profit': 15000.0,
                'total_loss': -5000.0,
                'max_drawdown': -0.08
            })
        
        # 查询所有
        summaries = test_db.get_performance_summaries()
        assert len(summaries) == 3
        
        # 按周期查询
        weekly = test_db.get_performance_summaries(period='weekly')
        assert len(weekly) == 1
        assert weekly[0]['period'] == 'weekly'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
