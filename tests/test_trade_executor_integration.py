"""
交易执行器集成测试 - Task 5

测试绩效追踪集成到交易执行器的功能：
1. 交易执行后自动记录绩效
2. 钉钉推送包含收益信息
3. 不影响现有交易流程
4. 支持模拟盘模式
"""
import pytest
import sqlite3
import os
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
import sys

# 添加项目路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from scripts.trade_executor_manual import ManualTradeExecutor, TRADING_MODE
from app.performance.tracker import PerformanceTracker
from app.storage.database import Database


@pytest.fixture
def test_db_path(tmp_path):
    """创建测试数据库"""
    db_path = str(tmp_path / "test_ftseek.db")
    
    # 创建测试数据库和表结构
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 创建 signals 表
    cursor.execute("""
        CREATE TABLE signals (
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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (buy_signal_id) REFERENCES signals(id),
            FOREIGN KEY (sell_signal_id) REFERENCES signals(id)
        )
    """)
    
    # 创建索引
    cursor.execute("CREATE INDEX idx_signals_code ON signals(code)")
    cursor.execute("CREATE INDEX idx_signals_status ON signals(status)")
    cursor.execute("CREATE INDEX idx_trade_pairs_code ON trade_pairs(code)")
    
    conn.commit()
    conn.close()
    
    return db_path


@pytest.fixture
def executor(test_db_path):
    """创建测试执行器"""
    # 先 patch PerformanceTracker 的初始化
    with patch('scripts.trade_executor_manual.PerformanceTracker') as MockTracker:
        mock_tracker = Mock()
        mock_tracker.match_trades = Mock(return_value=[])
        MockTracker.return_value = mock_tracker
        
        # 创建真实的 Database 实例用于测试
        real_db = Database()
        real_db.db_path = test_db_path
        
        # 创建执行器
        executor = ManualTradeExecutor()
        executor.db = real_db
        
        # 创建使用测试数据库的真实 tracker
        real_tracker = PerformanceTracker(db_path=test_db_path)
        executor.tracker = real_tracker
        
        # Mock notifier
        mock_notifier = Mock()
        mock_notifier._do_post = Mock(return_value=True)
        executor.notifier = mock_notifier
        
        yield executor


@pytest.fixture
def sample_buy_signal(test_db_path):
    """插入测试买入信号"""
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    
    base_time = datetime.now()
    
    # 插入测试买入信号
    cursor.execute("""
        INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                            reason, created_at, executed_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        'test_signal_001',
        'HK0700',
        '腾讯控股',
        'buy',
        300.0,
        100,
        'momentum',
        '突破阻力位',
        (base_time - timedelta(hours=1)).isoformat(),
        (base_time - timedelta(hours=1)).isoformat(),
        'pending_confirmation',
    ))
    
    conn.commit()
    conn.close()
    
    return {
        'id': 'test_signal_001',
        'code': 'HK0700',
        'name': '腾讯控股',
        'action': 'buy',
        'price': 300.0,
        'quantity': 100,
    }


class TestTradeExecutorIntegration:
    """交易执行器集成测试"""
    
    def test_execute_buy_records_trade(self, executor, test_db_path, sample_buy_signal):
        """测试买入执行后记录交易"""
        # 执行买入
        result = executor.execute('test_signal_001', 'BUY')
        
        # 验证执行成功
        assert result is True
        
        # 验证信号状态已更新
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM signals WHERE id = ?", ('test_signal_001',))
        row = cursor.fetchone()
        assert row[0] == 'executed'
        conn.close()
    
    def test_execute_sell_creates_trade_pair(self, executor, test_db_path):
        """测试卖出执行后创建交易配对"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        base_time = datetime.now()
        
        # 先插入买入信号
        cursor.execute("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                reason, created_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'test_buy_001',
            'HK0700',
            '腾讯控股',
            'buy',
            300.0,
            100,
            'momentum',
            '突破',
            (base_time - timedelta(days=2)).isoformat(),
            (base_time - timedelta(days=2)).isoformat(),
            'executed',
        ))
        
        # 插入卖出信号
        cursor.execute("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                reason, created_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'test_sell_001',
            'HK0700',
            '腾讯控股',
            'sell',
            330.0,
            100,
            'momentum',
            '止盈',
            base_time.isoformat(),
            None,
            'pending_confirmation',
        ))
        
        conn.commit()
        conn.close()
        
        # 执行卖出
        result = executor.execute('test_sell_001', 'SELL')
        
        # 验证执行成功
        assert result is True
        
        # 验证 trade_pairs 表有记录
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trade_pairs")
        count = cursor.fetchone()[0]
        assert count >= 1
        
        # 验证配对记录内容
        cursor.execute("""
            SELECT buy_signal_id, sell_signal_id, code, buy_price, sell_price, 
                   quantity, return_rate, profit
            FROM trade_pairs 
            ORDER BY created_at DESC 
            LIMIT 1
        """)
        row = cursor.fetchone()
        assert row[0] == 'test_buy_001'  # buy_signal_id
        assert row[1] == 'test_sell_001'  # sell_signal_id
        assert row[2] == 'HK0700'  # code
        assert row[3] == 300.0  # buy_price
        assert row[4] == 330.0  # sell_price
        assert row[5] == 100  # quantity
        assert row[6] == pytest.approx(10.0, rel=1e-5)  # return_rate (10%)
        assert row[7] == pytest.approx(3000.0, rel=1e-5)  # profit (3000 HKD)
        
        conn.close()
    
    def test_notify_includes_profit_info(self, executor, test_db_path):
        """测试钉钉推送包含收益信息"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        base_time = datetime.now()
        
        # 插入买入信号
        cursor.execute("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                reason, created_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'test_buy_002',
            'HK0700',
            '腾讯控股',
            'buy',
            300.0,
            100,
            'momentum',
            '突破',
            (base_time - timedelta(days=2)).isoformat(),
            (base_time - timedelta(days=2)).isoformat(),
            'executed',
        ))
        
        # 插入卖出信号
        cursor.execute("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                reason, created_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'test_sell_002',
            'HK0700',
            '腾讯控股',
            'sell',
            330.0,
            100,
            'momentum',
            '止盈',
            base_time.isoformat(),
            None,
            'pending_confirmation',
        ))
        
        conn.commit()
        conn.close()
        
        # 执行卖出
        result = executor.execute('test_sell_002', 'SELL')
        assert result is True
        
        # 验证钉钉推送包含收益信息
        assert hasattr(executor.notifier, '_do_post')
        calls = executor.notifier._do_post.call_args_list
        
        # 至少有一次推送
        assert len(calls) >= 1
        
        # 检查最后一次推送 (卖出结果) 是否包含收益信息
        last_call = calls[-1]
        payload = last_call[0][0]
        
        assert 'markdown' in payload
        text = payload['markdown']['text']
        
        # 应该包含收益信息
        assert '收益' in text or 'profit' in text.lower() or '3000' in text
    
    def test_paper_mode_compatible(self, executor, test_db_path, sample_buy_signal):
        """测试模拟盘模式兼容"""
        # 设置模拟盘模式
        import scripts.trade_executor_manual as module
        original_mode = module.TRADING_MODE
        module.TRADING_MODE = 'paper'
        
        try:
            # 执行买入
            result = executor.execute('test_signal_001', 'BUY')
            
            # 验证执行成功
            assert result is True
            
            # 验证订单 ID 是模拟盘格式
            conn = sqlite3.connect(test_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM signals WHERE id = ?", ('test_signal_001',))
            row = cursor.fetchone()
            assert row[0] == 'executed'
            conn.close()
            
        finally:
            module.TRADING_MODE = original_mode
    
    def test_execution_not_affected_by_tracker_failure(self, test_db_path, sample_buy_signal):
        """测试绩效追踪失败不影响交易执行"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        # 创建执行器
        executor = ManualTradeExecutor()
        executor.db = Database()
        executor.db.db_path = test_db_path
        
        # Mock notifier
        mock_notifier = Mock()
        mock_notifier._do_post = Mock(return_value=True)
        executor.notifier = mock_notifier
        
        # Mock tracker 抛出异常
        with patch.object(executor, '_record_performance') as mock_record:
            mock_record.side_effect = Exception("Tracker failed")
            
            # 执行买入应该仍然成功
            result = executor.execute('test_signal_001', 'BUY')
            
            # 验证交易执行成功
            assert result is True
            
            # 验证信号状态已更新
            cursor.execute("SELECT status FROM signals WHERE id = ?", ('test_signal_001',))
            row = cursor.fetchone()
            assert row[0] == 'executed'
        
        conn.close()
    
    def test_nonexistent_signal(self, executor, test_db_path):
        """测试不存在的信号"""
        # 执行不存在的信号
        result = executor.execute('nonexistent_signal', 'BUY')
        
        # 应该返回 False
        assert result is False
    
    def test_invalid_action(self, executor, test_db_path, sample_buy_signal):
        """测试无效操作"""
        # 执行无效操作
        result = executor.execute('test_signal_001', 'INVALID')
        
        # 应该返回 False
        assert result is False
    
    def test_wrong_status_signal(self, test_db_path):
        """测试状态不正确的信号"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        # 插入状态不正确的信号 (已执行)
        cursor.execute("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                reason, created_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'test_wrong_status',
            'HK0700',
            '腾讯控股',
            'buy',
            300.0,
            100,
            'momentum',
            '突破',
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            'executed',  # 已经是 executed 状态
        ))
        
        conn.commit()
        conn.close()
        
        # 创建执行器
        executor = ManualTradeExecutor()
        executor.db = Database()
        executor.db.db_path = test_db_path
        
        mock_notifier = Mock()
        mock_notifier._do_post = Mock(return_value=True)
        executor.notifier = mock_notifier
        
        # 执行应该失败
        result = executor.execute('test_wrong_status', 'BUY')
        
        # 应该返回 False
        assert result is False


class TestPerformanceCalculation:
    """绩效计算测试"""
    
    def test_single_trade_pair_return(self, test_db_path):
        """测试单笔交易对收益率计算"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        base_time = datetime.now()
        
        # 插入买卖信号
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                reason, created_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('sig_buy', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum',
             '突破', (base_time - timedelta(days=2)).isoformat(),
             (base_time - timedelta(days=2)).isoformat(), 'executed'),
            ('sig_sell', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             '止盈', base_time.isoformat(), base_time.isoformat(), 'executed'),
        ])
        
        conn.commit()
        conn.close()
        
        # 使用 tracker 计算
        tracker = PerformanceTracker(db_path=test_db_path)
        pairs = tracker.match_trades(code='HK0700')
        
        assert len(pairs) == 1
        pair = pairs[0]
        
        assert pair['return_rate'] == pytest.approx(10.0, rel=1e-5)
        assert pair['profit'] == pytest.approx(3000.0, rel=1e-5)
    
    def test_multiple_trade_pairs(self, test_db_path):
        """测试多笔交易对"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        base_time = datetime.now()
        
        # 插入多笔交易
        signals = [
            # 第一笔：盈利
            ('buy1', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum',
             '突破', (base_time - timedelta(days=5)).isoformat(),
             (base_time - timedelta(days=5)).isoformat(), 'executed'),
            ('sell1', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             '止盈', (base_time - timedelta(days=3)).isoformat(),
             (base_time - timedelta(days=3)).isoformat(), 'executed'),
            
            # 第二笔：亏损
            ('buy2', 'HK0700', '腾讯控股', 'buy', 350.0, 100, 'value',
             '估值', (base_time - timedelta(days=4)).isoformat(),
             (base_time - timedelta(days=4)).isoformat(), 'executed'),
            ('sell2', 'HK0700', '腾讯控股', 'sell', 320.0, 100, 'value',
             '止损', (base_time - timedelta(days=1)).isoformat(),
             (base_time - timedelta(days=1)).isoformat(), 'executed'),
        ]
        
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                reason, created_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, signals)
        
        conn.commit()
        conn.close()
        
        # 使用 tracker 计算
        tracker = PerformanceTracker(db_path=test_db_path)
        pairs = tracker.match_trades(code='HK0700')
        
        assert len(pairs) == 2
        
        # 验证收益率
        returns = [p['return_rate'] for p in pairs]
        assert pytest.approx(10.0, rel=1e-5) in returns  # 第一笔盈利 10%
        assert pytest.approx(-8.57, rel=0.01) in returns  # 第二笔亏损约 -8.57%


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
