"""Tests for app.playbook.horizon_return_model."""
import sqlite3
from unittest.mock import MagicMock

import pytest

from app.playbook.horizon_return_model import HorizonReturnModel
from app.playbook.db import PlaybookDB


class TestHorizonReturnModel:
    """HorizonReturnModel 测试."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "test_hrm.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        db = PlaybookDB(conn=conn)
        db.ensure_tables()
        return db

    def _insert_instruction(self, db, symbol="US.TEST", direction="BUY", horizon="short",
                            outcome="win", pnl_pct=2.0, conditions=None, vix=18.0):
        conn = db._conn()
        now = "2026-06-26T10:00:00"
        conditions = conditions or ["volume_expansion"]
        import json
        conn.execute(
            """
            INSERT INTO playbook_instructions
            (date, symbol, direction, entry_price, stop_loss, take_profit, position_size,
             confidence, outcome, horizon, conditions_json, vix_at_generation,
             executed, pnl, pnl_pct, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-20", symbol, direction, 100.0, 98.0, 104.0, 10, 70,
             outcome, horizon, json.dumps(conditions), vix, 1, 100.0, pnl_pct, now, now),
        )
        conn.commit()

    def test_update_creates_horizon_stats(self, db):
        model = HorizonReturnModel(db)
        # 插入 5 条 short 胜 + 5 条 long 负
        for i in range(5):
            self._insert_instruction(db, symbol=f"US.S{i}", horizon="short", outcome="win", pnl_pct=2.0)
        for i in range(5):
            self._insert_instruction(db, symbol=f"US.L{i}", horizon="long", outcome="loss", pnl_pct=-2.0)

        result = model.update(days_back=90)
        assert result["updated"] > 0

        stats = db.get_horizon_stats()
        assert len(stats) > 0

    def test_get_existing_stat(self, db):
        model = HorizonReturnModel(db)
        for i in range(5):
            self._insert_instruction(db, symbol=f"US.S{i}", horizon="short", outcome="win", pnl_pct=2.0)
        model.update(days_back=90)

        stat = model.get("volume_expansion:short", "short")
        assert stat is not None
        assert stat.total_trades == 5

    def test_update_filters_missed_and_void(self, db):
        """missed / void 不应进入 horizon 统计。"""
        model = HorizonReturnModel(db)
        for i in range(3):
            self._insert_instruction(db, symbol=f"US.W{i}", horizon="short", outcome="win", pnl_pct=2.0)
        for i in range(3):
            self._insert_instruction(db, symbol=f"US.M{i}", horizon="short", outcome="missed", pnl_pct=0.0)
        for i in range(3):
            self._insert_instruction(db, symbol=f"US.V{i}", horizon="short", outcome="void", pnl_pct=0.0)

        model.update(days_back=90)
        stats = db.get_horizon_stats()
        # 只有 win/loss 才会生成统计，每个 feature_key 对应 3 条 win 记录
        for s in stats:
            assert s["total_trades"] == 3
        assert len(stats) > 0
