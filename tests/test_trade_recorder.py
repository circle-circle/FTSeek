"""
交易记录器测试
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime
import tempfile
import os
import json

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.trade_recorder import TradeRecorder, TradeRecord, get_trade_recorder


class TestTradeRecorder:
    """交易记录器测试"""
    
    @pytest.fixture
    def recorder(self):
        """创建测试记录器"""
        # 使用临时目录
        import app.storage.trade_recorder as tr
        original_dir = tr.TRADE_RECORDS_DIR
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tr.TRADE_RECORDS_DIR = Path(tmpdir)
            rec = TradeRecorder()
            yield rec
            tr.TRADE_RECORDS_DIR = original_dir
    
    def test_record_open(self, recorder):
        """测试记录开仓"""
        record = recorder.record_open(
            code='00700.HK',
            strategy='confluence_v2',
            action='BUY',
            entry_price=380.5,
            quantity=100,
            signal_confidence=0.8,
            decision_reason='technical',
            decision_confidence=8,
            emotion_state='calm'
        )
        
        assert record.code == '00700.HK'
        assert record.action == 'BUY'
        assert record.entry_price == 380.5
        assert record.decision_reason == 'technical'
        assert record.emotion_state == 'calm'
        assert record.trade_id is not None
    
    def test_record_close(self, recorder):
        """测试记录平仓"""
        # 先开仓
        open_record = recorder.record_open(
            code='00700.HK',
            strategy='confluence_v2',
            action='BUY',
            entry_price=380.5,
            quantity=100,
            signal_confidence=0.8
        )
        
        # 平仓
        close_record = recorder.record_close(
            trade_id=open_record.trade_id,
            exit_price=400.0,
            exit_reason='止盈',
            exit_timing_score=8,
            confidence_exit=7
        )
        
        assert close_record is not None
        assert close_record.exit_price == 400.0
        assert close_record.pnl > 0
        assert close_record.exit_timing_score == 8
    
    def test_update_review(self, recorder):
        """测试更新复盘"""
        # 先开仓
        open_record = recorder.record_open(
            code='00700.HK',
            strategy='confluence_v2',
            action='BUY',
            entry_price=380.5,
            quantity=100,
            signal_confidence=0.8
        )
        
        # 更新复盘
        updated = recorder.update_review(
            trade_id=open_record.trade_id,
            entry_timing_score=7,
            stop_loss_executed=True,
            psychological_notes='心态平稳'
        )
        
        assert updated is not None
        assert updated.entry_timing_score == 7
        assert updated.stop_loss_executed is True
        assert updated.psychological_notes == '心态平稳'
    
    def test_get_record(self, recorder):
        """测试获取记录"""
        record = recorder.record_open(
            code='00700.HK',
            strategy='confluence_v2',
            action='BUY',
            entry_price=380.5,
            quantity=100,
            signal_confidence=0.8
        )
        
        retrieved = recorder.get_record(record.trade_id)
        assert retrieved is not None
        assert retrieved.trade_id == record.trade_id
    
    def test_get_records_by_date(self, recorder):
        """测试按日期获取记录"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        recorder.record_open(
            code='00700.HK',
            strategy='confluence_v2',
            action='BUY',
            entry_price=380.5,
            quantity=100,
            signal_confidence=0.8
        )
        
        records = recorder.get_records_by_date(today)
        assert len(records) > 0
    
    def test_get_review_statistics(self, recorder):
        """测试获取复盘统计"""
        # 添加多笔交易
        for i in range(5):
            record = recorder.record_open(
                code='00700.HK',
                strategy='confluence_v2',
                action='BUY',
                entry_price=380.5,
                quantity=100,
                signal_confidence=0.8,
                emotion_state='calm' if i < 4 else 'anxious'
            )
            
            # 平仓
            recorder.record_close(
                trade_id=record.trade_id,
                exit_price=400.0 if i < 3 else 360.0,
                exit_reason='止盈' if i < 3 else '止损'
            )
        
        stats = recorder.get_review_statistics()
        
        assert stats['total_trades'] == 5
        assert stats['winning_trades'] == 3
        assert stats['losing_trades'] == 2
        assert 'emotion_distribution' in stats
    
    def test_backward_compatibility(self, recorder):
        """测试向后兼容性"""
        # 使用旧版接口 (不带复盘字段)
        record = recorder.record_open(
            code='00700.HK',
            strategy='fusion_v2',
            action='BUY',
            entry_price=380.5,
            quantity=100,
            signal_confidence=0.8
        )
        
        # 应该有默认值
        assert record.emotion_state == 'calm'
        assert record.decision_confidence == 5
        assert record.entry_timing_score == 5
    
    def test_data_validation(self, recorder):
        """测试数据验证"""
        # 测试默认值
        record = recorder.record_open(
            code='00700.HK',
            strategy='test',
            action='BUY',
            entry_price=380.5,
            quantity=100,
            signal_confidence=0.8
        )
        
        # 验证默认值合理
        assert 1 <= record.decision_confidence <= 10
        assert 1 <= record.entry_timing_score <= 10
        assert record.emotion_state in ['calm', 'anxious', 'excited', 'fearful', 'greedy', 'frustrated']


class TestTradeRecord:
    """交易记录数据类测试"""
    
    def test_default_values(self):
        """测试默认值"""
        record = TradeRecord()
        
        assert record.emotion_state == 'calm'
        assert record.decision_confidence == 5
        assert record.entry_timing_score == 5
        assert record.stop_loss_executed is True
    
    def test_to_dict(self):
        """测试转换为字典"""
        record = TradeRecord(
            trade_id='test123',
            code='00700.HK',
            action='BUY',
            entry_price=380.5
        )
        
        data = record.to_dict()
        assert data['trade_id'] == 'test123'
        assert data['code'] == '00700.HK'
    
    def test_from_dict(self):
        """测试从字典创建"""
        data = {
            'trade_id': 'test456',
            'code': '00700.HK',
            'action': 'BUY',
            'entry_price': 380.5,
            'decision_confidence': 8,
            'emotion_state': 'calm'
        }
        
        record = TradeRecord.from_dict(data)
        assert record.trade_id == 'test456'
        assert record.decision_confidence == 8


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
