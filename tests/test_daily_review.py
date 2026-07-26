"""
每日复盘生成器测试 (美股版)
"""
import pytest
import sys
from pathlib import Path
from datetime import datetime
import os

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.review.daily_review import DailyReviewGenerator


class TestDailyReviewGenerator:
    """每日复盘生成器测试 (美股版)"""
    
    @pytest.fixture
    def setup_data(self, monkeypatch, tmp_path):
        """DailyReviewGenerator 现读 ftseek_us.db (TradeStore 口径, 经 get_trade_records_db).

        密封: 构造临时 DB (含 generate() 查询引用的列) 并 monkeypatch 路径,
        不再经由 legacy TradeRecorder (那是另一个 DB, 复盘生成器已不读它).
        """
        import sqlite3
        import app.review.daily_review as dr_mod

        db_path = tmp_path / "ftseek_us_test.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE trade_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT, symbol TEXT, direction TEXT,
                entry_time TEXT, exit_time TEXT,
                entry_price REAL, exit_price REAL, quantity INTEGER,
                realized_pnl REAL, pnl_pct REAL, exit_reason TEXT,
                holding_hours REAL, signal_strength INTEGER,
                signal_confidence REAL, voting_result TEXT,
                decision_reason TEXT, source_type TEXT DEFAULT 'MAIN_PIPELINE',
                trade_mode TEXT DEFAULT 'simulate', mode TEXT DEFAULT 'normal',
                owner_strategy TEXT, trace_id TEXT, mfe_pct REAL, mae_pct REAL,
                created_at TEXT
            )
            """
        )
        today = datetime.now().strftime('%Y-%m-%d')
        for i in range(5):
            win = i < 3
            conn.execute(
                "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, exit_time, "
                "entry_price, exit_price, quantity, realized_pnl, pnl_pct, exit_reason, created_at) "
                "VALUES (?, 'AAPL', 'LONG', ?, ?, 175.5, ?, 100, ?, ?, ?, ?)",
                (
                    today,
                    f"{today}T10:0{i}:00",
                    f"{today}T11:0{i}:00",
                    180.0 if win else 170.0,
                    450.0 if win else -550.0,
                    0.0256 if win else -0.0313,
                    'TP' if win else 'SL',
                    f"{today}T11:0{i}:00",
                ),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr(dr_mod, "get_trade_records_db", lambda: db_path)
        yield db_path
    
    def test_generate(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        
        assert report is not None
        assert report['report_type'] == 'daily'
        assert 'summary' in report
    
    def test_summary_generation(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        summary = report['summary']
        
        assert summary['total_trades'] == 5
        assert summary['closed_trades'] == 5
        assert summary['winning_trades'] == 3
    
    def test_suggestions_generation(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        report = generator.generate(date=today)
        suggestions = report['improvement_suggestions']
        
        assert isinstance(suggestions, list)
    
    def test_save_report(self, setup_data):
        generator = DailyReviewGenerator()
        today = datetime.now().strftime('%Y-%m-%d')
        
        generator.generate(date=today)
        
        filename = generator.output_dir / f"daily_review_{today}.json"
        assert filename.exists()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
