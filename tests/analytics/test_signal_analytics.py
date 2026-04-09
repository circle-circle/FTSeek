"""
信号质量分析模块单元测试 (P1-1)

测试覆盖:
1. 数据库表创建
2. 数据写入
3. 查询 API
4. 分析工具
5. 边界条件
"""
import unittest
import time
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os

from app.storage.database import Database
from app.analytics.signal_analytics import SignalAnalytics
from app.strategy.consensus_engine import SignalOutcome


class TestSignalOutcomeDatabase(unittest.TestCase):
    """测试信号质量记录数据库操作"""
    
    def setUp(self):
        """创建临时数据库"""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ftseek.db")
        self.db = Database(self.db_path)
    
    def tearDown(self):
        """清理临时数据库"""
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)
    
    def test_table_creation(self):
        """测试 signal_outcomes 表创建"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='signal_outcomes'
            """)
            result = cursor.fetchone()
            self.assertIsNotNone(result)
            self.assertEqual(result['name'], 'signal_outcomes')
    
    def test_table_columns(self):
        """测试 signal_outcomes 表字段"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(signal_outcomes)")
            columns = {row['name'] for row in cursor.fetchall()}
            
            expected_columns = {
                'id', 'code', 'action', 'strategy', 'entry_price', 
                'exit_price', 'pnl_pct', 'holding_periods', 'market_regime',
                'adx_value', 'mtf_alignment', 'sentiment_score', 
                'consensus_score', 'quality_score', 'max_drawdown', 
                'max_profit', 'timestamp'
            }
            
            self.assertEqual(columns, expected_columns)
    
    def test_save_signal_outcome(self):
        """测试保存信号记录"""
        outcome_data = {
            'code': 'HK.00700',
            'action': 'BUY',
            'strategy': 'confluence_v2',
            'entry_price': 350.0,
            'exit_price': 360.0,
            'pnl_pct': 2.86,
            'holding_periods': 5,
            'market_regime': 'trending',
            'adx_value': 32.5,
            'mtf_alignment': 0.85,
            'sentiment_score': 0.6,
            'consensus_score': 2.1,
            'quality_score': 1.2,
            'max_drawdown': -0.5,
            'max_profit': 3.2,
            'timestamp': datetime.now().isoformat(),
        }
        
        result = self.db.save_signal_outcome(outcome_data)
        self.assertTrue(result)
        
        # 验证数据已保存
        outcomes = self.db.get_signal_outcomes_by_code('HK.00700', days=1)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]['strategy'], 'confluence_v2')
        self.assertEqual(outcomes[0]['pnl_pct'], 2.86)
    
    def test_save_multiple_outcomes(self):
        """测试保存多条信号记录"""
        for i in range(10):
            outcome_data = {
                'code': 'HK.00700',
                'action': 'BUY' if i % 2 == 0 else 'SELL',
                'strategy': 'confluence_v2',
                'entry_price': 350.0 + i,
                'exit_price': 360.0 + i,
                'pnl_pct': 2.86 if i % 3 != 0 else -1.5,
                'holding_periods': 5,
                'market_regime': 'trending' if i < 5 else 'ranging',
                'adx_value': 32.5,
                'mtf_alignment': 0.85,
                'sentiment_score': 0.6,
                'consensus_score': 2.1,
                'quality_score': 1.2,
                'max_drawdown': -0.5,
                'max_profit': 3.2,
                'timestamp': datetime.now().isoformat(),
            }
            self.db.save_signal_outcome(outcome_data)
        
        outcomes = self.db.get_signal_outcomes_by_code('HK.00700', days=1)
        self.assertEqual(len(outcomes), 10)
    
    def test_query_by_code(self):
        """测试按股票代码查询"""
        # 保存不同股票的记录
        for code in ['HK.00700', 'HK.09988', 'HK.03690']:
            for i in range(5):
                outcome_data = {
                    'code': code,
                    'action': 'BUY',
                    'strategy': 'confluence_v2',
                    'entry_price': 350.0,
                    'exit_price': 360.0,
                    'pnl_pct': 2.86,
                    'holding_periods': 5,
                    'market_regime': 'trending',
                    'adx_value': 32.5,
                    'mtf_alignment': 0.85,
                    'sentiment_score': 0.6,
                    'consensus_score': 2.1,
                    'quality_score': 1.2,
                    'max_drawdown': -0.5,
                    'max_profit': 3.2,
                    'timestamp': datetime.now().isoformat(),
                }
                self.db.save_signal_outcome(outcome_data)
        
        # 查询特定股票
        outcomes = self.db.get_signal_outcomes_by_code('HK.00700', days=1)
        self.assertEqual(len(outcomes), 5)
        
        # 验证其他股票不受影响
        outcomes_9988 = self.db.get_signal_outcomes_by_code('HK.09988', days=1)
        self.assertEqual(len(outcomes_9988), 5)
    
    def test_query_by_strategy(self):
        """测试按策略查询"""
        # 保存不同策略的记录
        for strategy in ['confluence_v2', 'supertrend', 'sentiment_ai']:
            for i in range(3):
                outcome_data = {
                    'code': 'HK.00700',
                    'action': 'BUY',
                    'strategy': strategy,
                    'entry_price': 350.0,
                    'exit_price': 360.0,
                    'pnl_pct': 2.86,
                    'holding_periods': 5,
                    'market_regime': 'trending',
                    'adx_value': 32.5,
                    'mtf_alignment': 0.85,
                    'sentiment_score': 0.6,
                    'consensus_score': 2.1,
                    'quality_score': 1.2,
                    'max_drawdown': -0.5,
                    'max_profit': 3.2,
                    'timestamp': datetime.now().isoformat(),
                }
                self.db.save_signal_outcome(outcome_data)
        
        # 查询特定策略
        outcomes = self.db.get_signal_outcomes_by_strategy('confluence_v2', days=1)
        self.assertEqual(len(outcomes), 3)
    
    def test_query_by_regime(self):
        """测试按市场状态查询"""
        # 保存不同市场状态的记录
        for regime in ['trending', 'ranging', 'neutral']:
            for i in range(4):
                outcome_data = {
                    'code': 'HK.00700',
                    'action': 'BUY',
                    'strategy': 'confluence_v2',
                    'entry_price': 350.0,
                    'exit_price': 360.0,
                    'pnl_pct': 2.86,
                    'holding_periods': 5,
                    'market_regime': regime,
                    'adx_value': 32.5,
                    'mtf_alignment': 0.85,
                    'sentiment_score': 0.6,
                    'consensus_score': 2.1,
                    'quality_score': 1.2,
                    'max_drawdown': -0.5,
                    'max_profit': 3.2,
                    'timestamp': datetime.now().isoformat(),
                }
                self.db.save_signal_outcome(outcome_data)
        
        # 查询特定市场状态
        outcomes = self.db.get_signal_outcomes_by_regime('trending', days=1)
        self.assertEqual(len(outcomes), 4)
    
    def test_summary_statistics(self):
        """测试统计摘要"""
        # 保存混合数据
        for i in range(20):
            outcome_data = {
                'code': 'HK.00700',
                'action': 'BUY',
                'strategy': 'confluence_v2' if i < 10 else 'supertrend',
                'entry_price': 350.0,
                'exit_price': 360.0,
                'pnl_pct': 2.86 if i % 2 == 0 else -1.5,
                'holding_periods': 5,
                'market_regime': 'trending' if i < 10 else 'ranging',
                'adx_value': 32.5,
                'mtf_alignment': 0.85,
                'sentiment_score': 0.6,
                'consensus_score': 2.1,
                'quality_score': 1.2,
                'max_drawdown': -0.5,
                'max_profit': 3.2,
                'timestamp': datetime.now().isoformat(),
            }
            self.db.save_signal_outcome(outcome_data)
        
        # 获取总体统计
        summary = self.db.get_signal_outcomes_summary(days=1)
        self.assertEqual(summary['total_trades'], 20)
        self.assertEqual(summary['winning_trades'], 10)
        self.assertEqual(summary['losing_trades'], 10)
        self.assertAlmostEqual(summary['win_rate'], 0.5, places=2)
        
        # 获取按策略分组统计
        summary_by_strategy = self.db.get_signal_outcomes_summary(days=1, group_by='strategy')
        self.assertEqual(len(summary_by_strategy['data']), 2)
    
    def test_query_with_limit(self):
        """测试限制返回数量"""
        for i in range(50):
            outcome_data = {
                'code': 'HK.00700',
                'action': 'BUY',
                'strategy': 'confluence_v2',
                'entry_price': 350.0,
                'exit_price': 360.0,
                'pnl_pct': 2.86,
                'holding_periods': 5,
                'market_regime': 'trending',
                'adx_value': 32.5,
                'mtf_alignment': 0.85,
                'sentiment_score': 0.6,
                'consensus_score': 2.1,
                'quality_score': 1.2,
                'max_drawdown': -0.5,
                'max_profit': 3.2,
                'timestamp': datetime.now().isoformat(),
            }
            self.db.save_signal_outcome(outcome_data)
        
        # 限制返回 10 条
        outcomes = self.db.get_signal_outcomes_by_code('HK.00700', days=1, limit=10)
        self.assertEqual(len(outcomes), 10)
    
    def test_query_by_period(self):
        """测试按时间段查询"""
        now = datetime.now()
        
        # 保存不同时间的记录
        for days_ago in [1, 5, 10, 20, 30]:
            ts = (now - timedelta(days=days_ago)).isoformat()
            outcome_data = {
                'code': 'HK.00700',
                'action': 'BUY',
                'strategy': 'confluence_v2',
                'entry_price': 350.0,
                'exit_price': 360.0,
                'pnl_pct': 2.86,
                'holding_periods': 5,
                'market_regime': 'trending',
                'adx_value': 32.5,
                'mtf_alignment': 0.85,
                'sentiment_score': 0.6,
                'consensus_score': 2.1,
                'quality_score': 1.2,
                'max_drawdown': -0.5,
                'max_profit': 3.2,
                'timestamp': ts,
            }
            self.db.save_signal_outcome(outcome_data)
        
        # 查询最近 7 天
        end = now.isoformat()
        start = (now - timedelta(days=7)).isoformat()
        
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM signal_outcomes 
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp DESC
            """, (start, end))
            outcomes = [dict(row) for row in cursor.fetchall()]
        
        self.assertEqual(len(outcomes), 2)  # 1 天和 5 天的记录


class TestSignalAnalytics(unittest.TestCase):
    """测试信号分析器"""
    
    def setUp(self):
        """创建临时数据库和分析器"""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_ftseek.db")
        
        # Mock get_database to use temp db
        self.db_patcher = patch('app.analytics.signal_analytics.get_database')
        self.mock_get_db = self.db_patcher.start()
        self.mock_db = Database(self.db_path)
        self.mock_get_db.return_value = self.mock_db
        
        self.analytics = SignalAnalytics()
    
    def tearDown(self):
        """清理临时数据库"""
        self.db_patcher.stop()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)
    
    def test_record_outcome(self):
        """测试记录信号结果"""
        outcome = SignalOutcome(
            code='HK.00700',
            action='BUY',
            strategy='confluence_v2',
            entry_price=350.0,
            exit_price=360.0,
            pnl_pct=2.86,
            holding_periods=5,
            market_regime='trending',
            adx_value=32.5,
            mtf_alignment=0.85,
            sentiment_score=0.6,
            consensus_score=2.1,
            quality_score=1.2,
            max_drawdown=-0.5,
            max_profit=3.2,
            timestamp=time.time(),
        )
        
        result = self.analytics.record_outcome(outcome)
        self.assertTrue(result)
        
        # 验证数据已保存
        outcomes = self.analytics.query_by_code('HK.00700', days=1)
        self.assertEqual(len(outcomes), 1)
    
    def test_record_outcome_dict(self):
        """测试记录信号结果 (字典版本)"""
        outcome_data = {
            'code': 'HK.00700',
            'action': 'BUY',
            'strategy': 'confluence_v2',
            'entry_price': 350.0,
            'exit_price': 360.0,
            'pnl_pct': 2.86,
            'holding_periods': 5,
            'market_regime': 'trending',
            'adx_value': 32.5,
            'mtf_alignment': 0.85,
            'sentiment_score': 0.6,
            'consensus_score': 2.1,
            'quality_score': 1.2,
            'max_drawdown': -0.5,
            'max_profit': 3.2,
            'timestamp': datetime.now().isoformat(),
        }
        
        result = self.analytics.record_outcome_dict(outcome_data)
        self.assertTrue(result)
    
    def test_analyze_strategy_performance(self):
        """测试策略表现分析"""
        # 准备测试数据
        for strategy in ['confluence_v2', 'supertrend']:
            for i in range(10):
                outcome_data = {
                    'code': 'HK.00700',
                    'action': 'BUY',
                    'strategy': strategy,
                    'entry_price': 350.0,
                    'exit_price': 360.0,
                    'pnl_pct': 2.86 if i % 2 == 0 else -1.5,
                    'holding_periods': 5,
                    'market_regime': 'trending',
                    'adx_value': 32.5,
                    'mtf_alignment': 0.85,
                    'sentiment_score': 0.6,
                    'consensus_score': 2.1,
                    'quality_score': 1.2,
                    'max_drawdown': -0.5,
                    'max_profit': 3.2,
                    'timestamp': datetime.now().isoformat(),
                }
                self.analytics.record_outcome_dict(outcome_data)
        
        # 分析策略表现
        perf = self.analytics.analyze_strategy_performance(days=1)
        
        self.assertIn('confluence_v2', perf)
        self.assertIn('supertrend', perf)
        self.assertEqual(perf['confluence_v2']['total_trades'], 10)
        self.assertAlmostEqual(perf['confluence_v2']['win_rate'], 0.5, places=2)
    
    def test_analyze_regime_performance(self):
        """测试市场状态表现分析"""
        # 准备测试数据
        for regime in ['trending', 'ranging']:
            for i in range(8):
                outcome_data = {
                    'code': 'HK.00700',
                    'action': 'BUY',
                    'strategy': 'confluence_v2',
                    'entry_price': 350.0,
                    'exit_price': 360.0,
                    'pnl_pct': 2.86 if i % 2 == 0 else -1.5,
                    'holding_periods': 5,
                    'market_regime': regime,
                    'adx_value': 32.5,
                    'mtf_alignment': 0.85,
                    'sentiment_score': 0.6,
                    'consensus_score': 2.1,
                    'quality_score': 1.2,
                    'max_drawdown': -0.5,
                    'max_profit': 3.2,
                    'timestamp': datetime.now().isoformat(),
                }
                self.analytics.record_outcome_dict(outcome_data)
        
        # 分析市场状态表现
        perf = self.analytics.analyze_regime_performance(days=1)
        
        self.assertIn('trending', perf)
        self.assertIn('ranging', perf)
        self.assertEqual(perf['trending']['total_trades'], 8)
    
    def test_generate_report(self):
        """测试生成分析报告"""
        # 准备测试数据
        for i in range(20):
            outcome_data = {
                'code': 'HK.00700',
                'action': 'BUY',
                'strategy': 'confluence_v2',
                'entry_price': 350.0,
                'exit_price': 360.0,
                'pnl_pct': 2.86 if i % 2 == 0 else -1.5,
                'holding_periods': 5,
                'market_regime': 'trending' if i < 10 else 'ranging',
                'adx_value': 32.5,
                'mtf_alignment': 0.85,
                'sentiment_score': 0.6,
                'consensus_score': 2.1,
                'quality_score': 1.2,
                'max_drawdown': -0.5,
                'max_profit': 3.2,
                'timestamp': datetime.now().isoformat(),
            }
            self.analytics.record_outcome_dict(outcome_data)
        
        # 生成报告
        report = self.analytics.generate_report(days=1)
        
        self.assertIn('信号质量分析报告', report)
        self.assertIn('总体统计', report)
        self.assertIn('策略表现', report)
        self.assertIn('市场状态表现', report)


class TestSignalOutcomeDataClass(unittest.TestCase):
    """测试 SignalOutcome 数据类"""
    
    def test_create_outcome(self):
        """测试创建 SignalOutcome 对象"""
        outcome = SignalOutcome(
            code='HK.00700',
            action='BUY',
            strategy='confluence_v2',
            entry_price=350.0,
            exit_price=360.0,
            pnl_pct=2.86,
            holding_periods=5,
            market_regime='trending',
            adx_value=32.5,
            mtf_alignment=0.85,
            sentiment_score=0.6,
            consensus_score=2.1,
            quality_score=1.2,
            max_drawdown=-0.5,
            max_profit=3.2,
            timestamp=time.time(),
        )
        
        self.assertEqual(outcome.code, 'HK.00700')
        self.assertEqual(outcome.action, 'BUY')
        self.assertEqual(outcome.strategy, 'confluence_v2')
        self.assertEqual(outcome.pnl_pct, 2.86)
        self.assertEqual(outcome.market_regime, 'trending')
    
    def test_default_values(self):
        """测试默认值"""
        outcome = SignalOutcome(
            code='HK.00700',
            action='BUY',
            strategy='confluence_v2',
            entry_price=350.0,
            exit_price=360.0,
            pnl_pct=2.86,
            holding_periods=0,
            market_regime='neutral',
            adx_value=0.0,
            mtf_alignment=0.0,
            sentiment_score=0.0,
            consensus_score=0.0,
            quality_score=1.0,
            max_drawdown=0.0,
            max_profit=0.0,
        )
        
        self.assertEqual(outcome.holding_periods, 0)
        self.assertEqual(outcome.market_regime, 'neutral')
        self.assertEqual(outcome.quality_score, 1.0)


if __name__ == '__main__':
    unittest.main()
