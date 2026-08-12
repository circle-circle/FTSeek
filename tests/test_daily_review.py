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

        输出隔离 (2026-08-13 修复): 原 fixture 只隔离 DB 读路径, 未隔离输出 —
        generate() → _save_report() 会把假数据 (AAPL 175.5 组, 5笔/0.6/+250) 写进
        生产 data/daily_reviews/*.json 与 Obsidian vault (write_daily_postmarket),
        每次 pytest 跑到本文件即污染"当前日期"的生产报告。实锤: 14 篇 postmarket
        笔记 + 7 个 JSON 带假签名 (指纹 total_pnl: 250.0)。修复 = 输出三路径
        (reviews_dir/obsidian vault/writer 单例) 全部指向 tmp_path。
        """
        import sqlite3
        import app.review.daily_review as dr_mod
        import app.core.obsidian_writer as ow_mod

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
                decision_reason TEXT, source_type TEXT DEFAULT 'PLAYBOOK_SIM',
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

        # ── 输出隔离: 生产 data/daily_reviews/ 与 Obsidian vault 一律不碰 ──
        import app.core.paths as paths_mod
        out_dir = tmp_path / "reviews"
        out_dir.mkdir(exist_ok=True)
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(dr_mod, "get_reviews_dir", lambda: out_dir)
        # get_obsidian_vault 在 _save_report 内 from app.core.paths 导入 → patch 源模块
        monkeypatch.setattr(paths_mod, "get_obsidian_vault", lambda: vault_dir)
        # obsidian_writer 单例缓存 vault_path → 清空, 强制按 tmp vault 重建
        monkeypatch.setattr(ow_mod, "_writer_instances", {}, raising=False)
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
