"""
交易记录器测试 (美股版)
"""
import pytest
import sys
from pathlib import Path
import tempfile
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.trade_recorder import TradeRecorder, get_trade_recorder


class TestTradeRecorder:
    """交易记录器测试 (美股版)"""
    
    @pytest.fixture
    def recorder(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        rec = TradeRecorder(db_path)
        yield rec
        os.unlink(db_path)
    
    def test_record(self, recorder):
        trade_id = recorder.record(
            stock_code='AAPL',
            side='buy',
            quantity=100,
            price=175.5,
            decision_reason='technical',
            decision_confidence=8,
            emotion_state='calm'
        )
        
        assert trade_id is not None
        
        record = recorder.get_record(trade_id)
        assert record is not None
        assert record['symbol'] == 'AAPL'
        assert record['decision_confidence'] == 8
    
    def test_update_review(self, recorder):
        trade_id = recorder.record(
            stock_code='AAPL',
            side='buy',
            quantity=100,
            price=175.5,
            decision_reason='technical'
        )
        
        success = recorder.update_review(
            trade_id=trade_id,
            entry_timing_score=7,
            stop_loss_executed=True,
            psychological_notes='Good mindset'
        )
        
        assert success is True
        
        record = recorder.get_record(trade_id)
        assert record['entry_timing_score'] == 7
    
    def test_record_close(self, recorder):
        trade_id = recorder.record(
            stock_code='AAPL',
            side='buy',
            quantity=100,
            price=175.5
        )
        
        success = recorder.record_close(
            trade_id=trade_id,
            exit_price=180.0,
            exit_reason='TP',
            exit_timing_score=8
        )
        
        assert success is True
        
        record = recorder.get_record(trade_id)
        assert record['exit_price'] == 180.0
        assert record['realized_pnl'] > 0
    
    def test_get_records_by_date(self, recorder):
        today = recorder._get_conn().execute("SELECT date('now')").fetchone()[0]
        
        recorder.record(
            stock_code='AAPL',
            side='buy',
            quantity=100,
            price=175.5
        )
        
        records = recorder.get_records_by_date(today)
        assert len(records) > 0
    
    def test_get_review_statistics(self, recorder):
        for i in range(5):
            trade_id = recorder.record(
                stock_code='AAPL',
                side='buy',
                quantity=100,
                price=175.5,
                emotion_state='calm' if i < 4 else 'anxious'
            )
            
            recorder.record_close(
                trade_id=trade_id,
                exit_price=180.0 if i < 3 else 170.0,
                exit_reason='TP' if i < 3 else 'SL'
            )
        
        stats = recorder.get_review_statistics()
        
        assert stats['total_trades'] == 5
        assert stats['winning_trades'] == 3
        assert 'emotion_distribution' in stats


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
