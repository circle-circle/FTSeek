"""
绩效追踪器单元测试

测试 PerformanceTracker 类的核心功能：
- match_trades: 配对买卖交易 (FIFO 原则)
- calculate_return: 计算单笔收益率
- get_position: 获取当前持仓
- get_total_return: 计算总收益率
"""
import pytest
import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict
import sys

# 添加项目根目录到路径
sys.path.insert(0, '/opt/ftseek/FTSeek')

from app.performance.tracker import PerformanceTracker


@pytest.fixture
def test_db_path(tmp_path):
    """创建测试数据库"""
    db_path = str(tmp_path / "test_ftseek.db")
    
    # 创建测试数据库和 signals 表
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 创建 signals 表 (与生产环境一致)
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
    
    # 创建索引
    cursor.execute("CREATE INDEX idx_signals_code ON signals(code)")
    cursor.execute("CREATE INDEX idx_signals_status ON signals(status)")
    cursor.execute("CREATE INDEX idx_signals_created_at ON signals(created_at)")
    
    conn.commit()
    conn.close()
    
    return db_path


@pytest.fixture
def tracker(test_db_path):
    """创建 PerformanceTracker 实例"""
    return PerformanceTracker(db_path=test_db_path)


@pytest.fixture
def sample_signals(test_db_path):
    """插入样本交易信号"""
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    
    base_time = datetime.now()
    
    # 插入测试数据：HK0700 的买卖信号
    signals = [
        # 第一笔交易：买入 100 股 @ 300，卖出 100 股 @ 330
        ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum', 
         0.8, 0.7, 0.75, '突破阻力位', 
         (base_time - timedelta(days=5)).isoformat(), None, None, 
         (base_time - timedelta(days=5)).isoformat(), 'executed'),
        
        ('sig_002', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
         0.7, 0.8, 0.75, '达到目标价',
         (base_time - timedelta(days=3)).isoformat(), None, None,
         (base_time - timedelta(days=3)).isoformat(), 'executed'),
        
        # 第二笔交易：买入 200 股 @ 310，卖出 100 股 @ 340 (部分卖出)
        ('sig_003', 'HK0700', '腾讯控股', 'buy', 310.0, 200, 'value',
         0.75, 0.65, 0.7, '估值合理',
         (base_time - timedelta(days=4)).isoformat(), None, None,
         (base_time - timedelta(days=4)).isoformat(), 'executed'),
        
        ('sig_004', 'HK0700', '腾讯控股', 'sell', 340.0, 100, 'value',
         0.8, 0.75, 0.78, '部分止盈',
         (base_time - timedelta(days=2)).isoformat(), None, None,
         (base_time - timedelta(days=2)).isoformat(), 'executed'),
        
        # 第三笔交易：另一只股票 HK9988
        ('sig_005', 'HK9988', '阿里巴巴', 'buy', 80.0, 500, 'mean_reversion',
         0.6, 0.7, 0.65, '超卖反弹',
         (base_time - timedelta(days=6)).isoformat(), None, None,
         (base_time - timedelta(days=6)).isoformat(), 'executed'),
        
        ('sig_006', 'HK9988', '阿里巴巴', 'sell', 88.0, 500, 'mean_reversion',
         0.7, 0.8, 0.75, '均值回归完成',
         (base_time - timedelta(days=1)).isoformat(), None, None,
         (base_time - timedelta(days=1)).isoformat(), 'executed'),
    ]
    
    cursor.executemany("""
        INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                            technical_score, sentiment_score, confidence, reason,
                            created_at, expire_at, confirmed_at, executed_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, signals)
    
    conn.commit()
    conn.close()
    
    return signals


class TestCalculateReturn:
    """测试 calculate_return 方法"""
    
    def test_positive_return(self, tracker):
        """测试盈利情况"""
        result = tracker.calculate_return(
            buy_price=300.0,
            sell_price=330.0,
            quantity=100
        )
        
        assert 'return_rate' in result
        assert 'profit' in result
        assert 'cost' in result
        
        # 收益率 = (330 - 300) / 300 * 100% = 10%
        assert result['return_rate'] == pytest.approx(10.0, rel=1e-5)
        # 收益 = (330 - 300) * 100 = 3000
        assert result['profit'] == pytest.approx(3000.0, rel=1e-5)
        # 成本 = 300 * 100 = 30000
        assert result['cost'] == pytest.approx(30000.0, rel=1e-5)
    
    def test_negative_return(self, tracker):
        """测试亏损情况"""
        result = tracker.calculate_return(
            buy_price=300.0,
            sell_price=270.0,
            quantity=100
        )
        
        # 收益率 = (270 - 300) / 300 * 100% = -10%
        assert result['return_rate'] == pytest.approx(-10.0, rel=1e-5)
        # 收益 = (270 - 300) * 100 = -3000
        assert result['profit'] == pytest.approx(-3000.0, rel=1e-5)
    
    def test_zero_return(self, tracker):
        """测试保本情况"""
        result = tracker.calculate_return(
            buy_price=300.0,
            sell_price=300.0,
            quantity=100
        )
        
        assert result['return_rate'] == pytest.approx(0.0, rel=1e-5)
        assert result['profit'] == pytest.approx(0.0, rel=1e-5)
    
    def test_high_return(self, tracker):
        """测试高收益情况"""
        result = tracker.calculate_return(
            buy_price=100.0,
            sell_price=200.0,
            quantity=50
        )
        
        # 收益率 = (200 - 100) / 100 * 100% = 100%
        assert result['return_rate'] == pytest.approx(100.0, rel=1e-5)
        # 收益 = (200 - 100) * 50 = 5000
        assert result['profit'] == pytest.approx(5000.0, rel=1e-5)


class TestMatchTrades:
    """测试 match_trades 方法"""
    
    def test_match_single_pair(self, tracker, sample_signals, test_db_path):
        """测试单对买卖匹配"""
        # 只插入一对信号
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        
        base_time = datetime.now()
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                technical_score, sentiment_score, confidence, reason,
                                created_at, expire_at, confirmed_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum',
             0.8, 0.7, 0.75, '突破阻力位',
             (base_time - timedelta(days=5)).isoformat(), None, None,
             (base_time - timedelta(days=5)).isoformat(), 'executed'),
            ('sig_002', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             0.7, 0.8, 0.75, '达到目标价',
             (base_time - timedelta(days=3)).isoformat(), None, None,
             (base_time - timedelta(days=3)).isoformat(), 'executed'),
        ])
        conn.commit()
        conn.close()
        
        pairs = tracker.match_trades(code='HK0700')
        
        assert len(pairs) == 1
        pair = pairs[0]
        
        assert pair['code'] == 'HK0700'
        assert pair['buy_price'] == 300.0
        assert pair['sell_price'] == 330.0
        assert pair['quantity'] == 100
        assert pair['return_rate'] == pytest.approx(10.0, rel=1e-5)
        assert pair['profit'] == pytest.approx(3000.0, rel=1e-5)
        assert pair['status'] == 'closed'
    
    def test_match_multiple_pairs_fifo(self, tracker, test_db_path):
        """测试 FIFO 原则匹配多笔交易"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        
        base_time = datetime.now()
        
        # 两笔买入，一笔卖出 (卖出匹配第一笔买入 - FIFO)
        signals = [
            # 买入 1: 100 股 @ 300 (第 1 天)
            ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum',
             0.8, 0.7, 0.75, '突破',
             (base_time - timedelta(days=5)).isoformat(), None, None,
             (base_time - timedelta(days=5)).isoformat(), 'executed'),
            
            # 买入 2: 100 股 @ 310 (第 3 天)
            ('sig_002', 'HK0700', '腾讯控股', 'buy', 310.0, 100, 'value',
             0.75, 0.65, 0.7, '估值',
             (base_time - timedelta(days=3)).isoformat(), None, None,
             (base_time - timedelta(days=3)).isoformat(), 'executed'),
            
            # 卖出：100 股 @ 330 (第 5 天) - 应该匹配买入 1
            ('sig_003', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             0.7, 0.8, 0.75, '目标',
             (base_time - timedelta(days=1)).isoformat(), None, None,
             (base_time - timedelta(days=1)).isoformat(), 'executed'),
        ]
        
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                technical_score, sentiment_score, confidence, reason,
                                created_at, expire_at, confirmed_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, signals)
        conn.commit()
        conn.close()
        
        pairs = tracker.match_trades(code='HK0700')
        
        assert len(pairs) == 1
        pair = pairs[0]
        
        # FIFO: 应该匹配第一笔买入 (300)
        assert pair['buy_price'] == 300.0
        assert pair['sell_price'] == 330.0
        # 收益率应该基于 300 的成本
        assert pair['return_rate'] == pytest.approx(10.0, rel=1e-5)
    
    def test_match_partial_sell(self, tracker, test_db_path):
        """测试部分卖出匹配"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        
        base_time = datetime.now()
        
        signals = [
            # 买入：200 股 @ 300
            ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 200, 'momentum',
             0.8, 0.7, 0.75, '突破',
             (base_time - timedelta(days=5)).isoformat(), None, None,
             (base_time - timedelta(days=5)).isoformat(), 'executed'),
            
            # 卖出：100 股 @ 330 (部分卖出)
            ('sig_002', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             0.7, 0.8, 0.75, '部分止盈',
             (base_time - timedelta(days=3)).isoformat(), None, None,
             (base_time - timedelta(days=3)).isoformat(), 'executed'),
        ]
        
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                technical_score, sentiment_score, confidence, reason,
                                created_at, expire_at, confirmed_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, signals)
        conn.commit()
        conn.close()
        
        pairs = tracker.match_trades(code='HK0700')
        
        assert len(pairs) == 1
        pair = pairs[0]
        
        # 部分卖出，匹配 100 股
        assert pair['quantity'] == 100
        assert pair['buy_price'] == 300.0
        assert pair['sell_price'] == 330.0
    
    def test_match_multiple_stocks(self, tracker, sample_signals):
        """测试多股票同时追踪"""
        # 匹配所有股票
        all_pairs = tracker.match_trades()
        
        # 应该至少有两支股票的配对
        codes = set(pair['code'] for pair in all_pairs)
        assert 'HK0700' in codes
        assert 'HK9988' in codes
        
        # 分别查询
        hk0700_pairs = tracker.match_trades(code='HK0700')
        hk9988_pairs = tracker.match_trades(code='HK9988')
        
        assert len(hk0700_pairs) >= 1
        assert len(hk9988_pairs) == 1
    
    def test_match_no_signals(self, tracker, test_db_path):
        """测试没有信号的情况"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        conn.commit()
        conn.close()
        
        pairs = tracker.match_trades()
        
        assert pairs == []
    
    def test_match_only_buy_signals(self, tracker, test_db_path):
        """测试只有买入信号 (未平仓)"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        
        base_time = datetime.now()
        cursor.execute("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                technical_score, sentiment_score, confidence, reason,
                                created_at, expire_at, confirmed_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum',
              0.8, 0.7, 0.75, '突破',
              base_time.isoformat(), None, None, base_time.isoformat(), 'executed'))
        
        conn.commit()
        conn.close()
        
        pairs = tracker.match_trades(code='HK0700')
        
        # 没有卖出信号，无法配对
        assert pairs == []


class TestGetPosition:
    """测试 get_position 方法"""
    
    def test_get_position_with_open_position(self, tracker, test_db_path):
        """测试获取持仓 (有未平仓头寸)"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        
        base_time = datetime.now()
        
        # 买入 200 股，卖出 100 股，剩余 100 股
        signals = [
            ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 200, 'momentum',
             0.8, 0.7, 0.75, '突破',
             (base_time - timedelta(days=5)).isoformat(), None, None,
             (base_time - timedelta(days=5)).isoformat(), 'executed'),
            ('sig_002', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             0.7, 0.8, 0.75, '部分止盈',
             (base_time - timedelta(days=3)).isoformat(), None, None,
             (base_time - timedelta(days=3)).isoformat(), 'executed'),
        ]
        
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                technical_score, sentiment_score, confidence, reason,
                                created_at, expire_at, confirmed_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, signals)
        conn.commit()
        conn.close()
        
        position = tracker.get_position(code='HK0700')
        
        assert position['code'] == 'HK0700'
        assert position['quantity'] == 100  # 200 - 100
        assert position['avg_cost'] == pytest.approx(300.0, rel=1e-5)
    
    def test_get_position_closed(self, tracker, test_db_path):
        """测试获取持仓 (已平仓)"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        
        base_time = datetime.now()
        
        signals = [
            ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum',
             0.8, 0.7, 0.75, '突破',
             (base_time - timedelta(days=5)).isoformat(), None, None,
             (base_time - timedelta(days=5)).isoformat(), 'executed'),
            ('sig_002', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             0.7, 0.8, 0.75, '止盈',
             (base_time - timedelta(days=3)).isoformat(), None, None,
             (base_time - timedelta(days=3)).isoformat(), 'executed'),
        ]
        
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                technical_score, sentiment_score, confidence, reason,
                                created_at, expire_at, confirmed_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, signals)
        conn.commit()
        conn.close()
        
        position = tracker.get_position(code='HK0700')
        
        assert position['code'] == 'HK0700'
        assert position['quantity'] == 0
        assert position['avg_cost'] == 0.0
    
    def test_get_position_no_signals(self, tracker, test_db_path):
        """测试没有信号的情况"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        conn.commit()
        conn.close()
        
        position = tracker.get_position(code='HK0700')
        
        assert position['code'] == 'HK0700'
        assert position['quantity'] == 0
        assert position['avg_cost'] == 0.0


class TestGetTotalReturn:
    """测试 get_total_return 方法"""
    
    def test_get_total_return_with_trades(self, tracker, sample_signals):
        """测试计算总收益率"""
        result = tracker.get_total_return(days=7)
        
        assert 'total_trades' in result
        assert 'win_rate' in result
        assert 'total_return' in result
        assert 'avg_return' in result
        
        # 应该有交易记录
        assert result['total_trades'] >= 2
        
        # 胜率应该在 0-1 之间
        assert 0 <= result['win_rate'] <= 1
    
    def test_get_total_return_no_trades(self, tracker, test_db_path):
        """测试没有交易的情况"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        conn.commit()
        conn.close()
        
        result = tracker.get_total_return(days=7)
        
        assert result['total_trades'] == 0
        assert result['win_rate'] == 0.0
        assert result['total_return'] == 0.0
        assert result['avg_return'] == 0.0
    
    def test_get_total_return_win_rate_calculation(self, tracker, test_db_path):
        """测试胜率计算"""
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM signals")
        
        base_time = datetime.now()
        
        # 2 笔盈利，1 笔亏损
        signals = [
            # 盈利交易 1: 300 -> 330 (+10%)
            ('sig_001', 'HK0700', '腾讯控股', 'buy', 300.0, 100, 'momentum',
             0.8, 0.7, 0.75, '突破',
             (base_time - timedelta(days=5)).isoformat(), None, None,
             (base_time - timedelta(days=5)).isoformat(), 'executed'),
            ('sig_002', 'HK0700', '腾讯控股', 'sell', 330.0, 100, 'momentum',
             0.7, 0.8, 0.75, '目标',
             (base_time - timedelta(days=3)).isoformat(), None, None,
             (base_time - timedelta(days=3)).isoformat(), 'executed'),
            
            # 盈利交易 2: 80 -> 88 (+10%)
            ('sig_003', 'HK9988', '阿里巴巴', 'buy', 80.0, 100, 'value',
             0.7, 0.7, 0.7, '估值',
             (base_time - timedelta(days=4)).isoformat(), None, None,
             (base_time - timedelta(days=4)).isoformat(), 'executed'),
            ('sig_004', 'HK9988', '阿里巴巴', 'sell', 88.0, 100, 'value',
             0.75, 0.75, 0.75, '目标',
             (base_time - timedelta(days=2)).isoformat(), None, None,
             (base_time - timedelta(days=2)).isoformat(), 'executed'),
            
            # 亏损交易：200 -> 180 (-10%)
            ('sig_005', 'HK0005', '汇丰控股', 'buy', 200.0, 100, 'momentum',
             0.6, 0.6, 0.6, '突破',
             (base_time - timedelta(days=6)).isoformat(), None, None,
             (base_time - timedelta(days=6)).isoformat(), 'executed'),
            ('sig_006', 'HK0005', '汇丰控股', 'sell', 180.0, 100, 'momentum',
             0.5, 0.5, 0.5, '止损',
             (base_time - timedelta(days=1)).isoformat(), None, None,
             (base_time - timedelta(days=1)).isoformat(), 'executed'),
        ]
        
        cursor.executemany("""
            INSERT INTO signals (id, code, name, action, price, quantity, strategy,
                                technical_score, sentiment_score, confidence, reason,
                                created_at, expire_at, confirmed_at, executed_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, signals)
        conn.commit()
        conn.close()
        
        result = tracker.get_total_return(days=7)
        
        assert result['total_trades'] == 3
        # 2 赢 1 输，胜率 = 2/3 = 0.666...
        assert result['win_rate'] == pytest.approx(2/3, rel=1e-5)


class TestEdgeCases:
    """测试边界情况"""
    
    def test_zero_quantity(self, tracker):
        """测试零数量"""
        result = tracker.calculate_return(
            buy_price=300.0,
            sell_price=330.0,
            quantity=0
        )
        
        assert result['profit'] == 0.0
        assert result['cost'] == 0.0
    
    def test_very_small_return(self, tracker):
        """测试极小收益率"""
        result = tracker.calculate_return(
            buy_price=300.0,
            sell_price=300.01,
            quantity=100
        )
        
        # 收益率 = (300.01 - 300) / 300 * 100% = 0.00333...%
        assert result['return_rate'] == pytest.approx(0.00333333, rel=1e-5)
    
    def test_large_quantity(self, tracker):
        """测试大数量"""
        result = tracker.calculate_return(
            buy_price=100.0,
            sell_price=110.0,
            quantity=100000
        )
        
        assert result['return_rate'] == pytest.approx(10.0, rel=1e-5)
        assert result['profit'] == pytest.approx(1000000.0, rel=1e-5)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
