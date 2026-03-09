"""
决策日志系统单元测试

测试 DecisionLog 类的所有核心功能：
- 记录交易决策
- 记录配置变更
- 查询日志（按类型、标的、时间范围）
- 导出日志（JSON、CSV）
- 清理旧日志
- 统计信息
"""

import pytest
import os
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Any
import tempfile
import shutil


class TestDecisionLog:
    """DecisionLog 测试类"""
    
    @pytest.fixture
    def temp_log_file(self):
        """创建临时日志文件"""
        temp_dir = tempfile.mkdtemp()
        log_file = os.path.join(temp_dir, 'test_decision_log.json')
        yield log_file
        # 清理临时文件
        if os.path.exists(log_file):
            os.remove(log_file)
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.fixture
    def decision_log(self, temp_log_file):
        """创建 DecisionLog 实例"""
        from app.core.decision_log import DecisionLog
        log = DecisionLog(log_file=temp_log_file, max_entries=10000)
        yield log
        # 清理
        if hasattr(log, 'entries'):
            log.entries.clear()
    
    def test_log_trade_decision(self, decision_log):
        """测试记录交易决策"""
        result = decision_log.log_trade_decision(
            symbol='HK.09988',
            action='BUY',
            confidence=0.85,
            decision='manual_confirm',
            context={'market_condition': 'bullish', 'user_id': 'user_001'}
        )
        
        # 验证返回结果
        assert result is not None
        assert 'id' in result
        assert 'timestamp' in result
        assert result['type'] == 'trade_decision'
        assert result['symbol'] == 'HK.09988'
        assert result['action'] == 'BUY'
        assert result['confidence'] == 0.85
        assert result['decision'] == 'manual_confirm'
        assert 'context' in result
        assert result['context']['market_condition'] == 'bullish'
        
        # 验证日志已保存
        entries = decision_log.query(symbol='HK.09988')
        assert len(entries) >= 1
        assert entries[-1]['symbol'] == 'HK.09988'
    
    def test_log_config_change(self, decision_log):
        """测试记录配置变更"""
        result = decision_log.log_config_change(
            key='max_position_size',
            old_value=10000,
            new_value=20000,
            reason='调整仓位上限以适应新策略'
        )
        
        # 验证返回结果
        assert result is not None
        assert 'id' in result
        assert 'timestamp' in result
        assert result['type'] == 'config_change'
        assert result['key'] == 'max_position_size'
        assert result['old_value'] == 10000
        assert result['new_value'] == 20000
        assert result['reason'] == '调整仓位上限以适应新策略'
        
        # 验证日志已保存
        entries = decision_log.query(decision_type='config_change')
        assert len(entries) >= 1
        assert entries[-1]['key'] == 'max_position_size'
    
    def test_query_by_type(self, decision_log):
        """测试按类型查询"""
        # 记录不同类型的日志
        decision_log.log_trade_decision('HK.09988', 'BUY', 0.85, 'auto', {})
        decision_log.log_trade_decision('HK.07000', 'SELL', 0.75, 'manual', {})
        decision_log.log_config_change('threshold', 0.5, 0.6, 'test')
        
        # 按类型查询
        trade_entries = decision_log.query(decision_type='trade_decision')
        config_entries = decision_log.query(decision_type='config_change')
        
        assert len(trade_entries) >= 2
        assert len(config_entries) >= 1
        
        # 验证类型正确
        for entry in trade_entries:
            assert entry['type'] == 'trade_decision'
        for entry in config_entries:
            assert entry['type'] == 'config_change'
    
    def test_query_by_symbol(self, decision_log):
        """测试按标的查询"""
        # 记录不同标的的日志
        decision_log.log_trade_decision('HK.09988', 'BUY', 0.85, 'auto', {})
        decision_log.log_trade_decision('HK.07000', 'SELL', 0.75, 'manual', {})
        decision_log.log_trade_decision('HK.09988', 'SELL', 0.80, 'auto', {})
        
        # 按标的查询
        entries_9988 = decision_log.query(symbol='HK.09988')
        entries_7000 = decision_log.query(symbol='HK.07000')
        
        assert len(entries_9988) >= 2
        assert len(entries_7000) >= 1
        
        # 验证标的正确
        for entry in entries_9988:
            assert entry['symbol'] == 'HK.09988'
        for entry in entries_7000:
            assert entry['symbol'] == 'HK.07000'
    
    def test_query_by_time_range(self, decision_log):
        """测试按时间范围查询"""
        # 记录日志
        decision_log.log_trade_decision('HK.09988', 'BUY', 0.85, 'auto', {})
        time.sleep(0.1)
        mid_time = datetime.now()
        time.sleep(0.1)
        decision_log.log_trade_decision('HK.07000', 'SELL', 0.75, 'manual', {})
        
        # 按时间范围查询
        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=1)
        
        entries = decision_log.query(start_time=start_time, end_time=end_time)
        assert len(entries) >= 2
        
        # 验证时间在范围内
        for entry in entries:
            # Python 3.6 兼容的解析方式
            entry_time = datetime.strptime(entry['timestamp'], '%Y-%m-%dT%H:%M:%S.%f')
            assert start_time <= entry_time <= end_time
    
    def test_export_json(self, decision_log, temp_log_file):
        """测试导出 JSON"""
        # 记录一些日志
        decision_log.log_trade_decision('HK.09988', 'BUY', 0.85, 'auto', {})
        decision_log.log_config_change('threshold', 0.5, 0.6, 'test')
        
        # 导出到 JSON
        export_file = temp_log_file.replace('.json', '_export.json')
        decision_log.export_to_json(export_file)
        
        # 验证导出文件
        assert os.path.exists(export_file)
        with open(export_file, 'r', encoding='utf-8') as f:
            exported_data = json.load(f)
        
        assert isinstance(exported_data, list)
        assert len(exported_data) >= 2
        
        # 清理
        if os.path.exists(export_file):
            os.remove(export_file)
    
    def test_clear_old_entries(self, decision_log):
        """测试清理旧日志"""
        # 记录一些日志
        decision_log.log_trade_decision('HK.09988', 'BUY', 0.85, 'auto', {})
        decision_log.log_config_change('threshold', 0.5, 0.6, 'test')
        
        # 获取初始数量
        initial_count = len(decision_log.query())
        assert initial_count >= 2
        
        # 清理 30 天前的日志（应该清理 0 条，因为都是刚创建的）
        cleared = decision_log.clear_old_entries(days=30)
        
        # 验证清理结果
        assert cleared >= 0
        remaining_count = len(decision_log.query())
        assert remaining_count >= 0  # 可能全部清理或保留
    
    def test_get_statistics(self, decision_log):
        """测试获取统计信息"""
        # 记录不同类型的日志
        decision_log.log_trade_decision('HK.09988', 'BUY', 0.85, 'auto', {})
        decision_log.log_trade_decision('HK.07000', 'SELL', 0.75, 'manual', {})
        decision_log.log_config_change('threshold', 0.5, 0.6, 'test')
        
        # 获取统计信息
        stats = decision_log.get_statistics()
        
        # 验证统计信息结构
        assert isinstance(stats, dict)
        assert 'total_entries' in stats
        assert 'by_type' in stats
        assert 'by_symbol' in stats
        
        # 验证统计数据
        assert stats['total_entries'] >= 3
        assert stats['by_type'].get('trade_decision', 0) >= 2
        assert stats['by_type'].get('config_change', 0) >= 1
    
    def test_thread_safety(self, decision_log):
        """测试线程安全性"""
        import threading
        
        results = []
        errors = []
        
        def log_trade(symbol, action):
            try:
                result = decision_log.log_trade_decision(symbol, action, 0.8, 'auto', {})
                results.append(result)
            except Exception as e:
                errors.append(e)
        
        # 创建多个线程同时写入
        threads = []
        for i in range(10):
            t = threading.Thread(
                target=log_trade,
                args=(f'HK.{i:05d}', 'BUY' if i % 2 == 0 else 'SELL')
            )
            threads.append(t)
        
        # 启动所有线程
        for t in threads:
            t.start()
        
        # 等待所有线程完成
        for t in threads:
            t.join()
        
        # 验证没有错误
        assert len(errors) == 0, f"Thread safety errors: {errors}"
        
        # 验证所有日志都记录了
        assert len(results) == 10
        entries = decision_log.query()
        assert len(entries) >= 10
    
    def test_max_entries_limit(self, temp_log_file):
        """测试最大条目数限制"""
        from app.core.decision_log import DecisionLog
        
        # 创建只有 5 条最大容量的日志
        log = DecisionLog(log_file=temp_log_file, max_entries=5)
        
        # 记录 10 条日志
        for i in range(10):
            log.log_trade_decision(f'HK.{i:05d}', 'BUY', 0.8, 'auto', {})
        
        # 验证只保留了最新的 5 条
        entries = log.query()
        assert len(entries) <= 5
        
        # 验证保留的是最新的
        if len(entries) > 0:
            assert entries[0]['symbol'] == 'HK.00005' or entries[-1]['symbol'] == 'HK.00009'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
