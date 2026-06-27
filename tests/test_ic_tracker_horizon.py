"""Tests for ic_tracker.py by_horizon."""
import sqlite3
import json
from unittest.mock import MagicMock

import pytest

from app.playbook.db import PlaybookDB
from app.playbook.ic_tracker import compute_playbook_ic


class TestICTrackerHorizon:
    """IC Tracker 按 horizon 分组测试."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "test_ic_horizon.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        db = PlaybookDB(conn=conn)
        db.ensure_tables()
        return db

    def _insert_instruction(self, db, symbol, horizon, fwd_t3, confidence=70):
        conn = db._conn()
        now = "2026-06-26T10:00:00"
        conn.execute(
            """
            INSERT INTO playbook_instructions
            (date, symbol, direction, entry_price, stop_loss, take_profit, position_size,
             confidence, outcome, horizon, conditions_json, market_regime, fwd_return_t3,
             executed, pnl, pnl_pct, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-06-20", symbol, "BUY", 100.0, 98.0, 104.0, 10, confidence,
             "win" if fwd_t3 > 0 else "loss", horizon, json.dumps(["volume_expansion"]),
             "RISK_ON", fwd_t3, 1, 100.0, 0.02, now, now),
        )
        conn.commit()

    def test_compute_playbook_ic_by_horizon(self, db):
        # 插入 5 条 short + 5 条 long，confidence 和 fwd_return 正相关
        for i in range(5):
            self._insert_instruction(db, f"US.S{i}", "short", 0.01 * (i + 1), confidence=60 + i * 10)
        for i in range(5):
            self._insert_instruction(db, f"US.L{i}", "long", 0.02 * (i + 1), confidence=60 + i * 10)

        report = compute_playbook_ic(db, days=30)

        assert "by_horizon" in report
        assert "short" in report["by_horizon"]
        assert "long" in report["by_horizon"]
        assert report["by_horizon"]["short"]["count"] == 5
        assert report["by_horizon"]["long"]["count"] == 5

    def test_check_graduation_prefers_long_horizon(self, db):
        from app.playbook.ic_tracker import _check_graduation
        report = {
            "overall_ic_t3": 0.01,
            "sample_t3": 5,
            "by_horizon": {
                "long": {"ic": 0.08, "count": 35},
                "short": {"ic": -0.05, "count": 20},
            },
        }
        grad = _check_graduation(report)
        assert grad["graduated"] is True
        assert grad["horizon_used"] == "long"
        assert grad["ic_t3"] == 0.08
