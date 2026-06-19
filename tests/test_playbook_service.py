"""Tests for app.playbook.playbook_service — 业务编排层"""
import pytest
import sqlite3
from unittest.mock import MagicMock, patch

from app.playbook.db import PlaybookDB
from app.playbook.playbook_service import PlaybookService


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def svc(db):
    return PlaybookService(playbook_db=db)


def _save_inst(db, **kwargs):
    defaults = {
        "date": "2026-06-18",
        "symbol": "US.NTES",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": 50,
        "confidence": 72,
        "conditions": ["above_ma20", "insider_cluster_buy"],
    }
    defaults.update(kwargs)
    return db.save_instruction(defaults)


class TestQueryMethods:
    def test_get_today_instructions(self, svc, db):
        _save_inst(db, symbol="US.NTES")
        _save_inst(db, symbol="US.ARM")
        result = svc.get_today_instructions("2026-06-18")
        assert len(result) == 2

    def test_get_today_empty(self, svc):
        result = svc.get_today_instructions("2020-01-01")
        assert result == []

    def test_get_summary(self, svc, db):
        _save_inst(db)
        summary = svc.get_playbook_summary(days=30)
        assert "today_date" in summary
        assert "today_instructions_count" in summary

    def test_get_factor_stats(self, svc, db):
        db.upsert_factor_stat({
            "factor_key": "insider_cluster_buy",
            "analysis_level": "single",
            "verdict": "positive",
        })
        stats = svc.get_factor_stats()
        assert "single" in stats
        assert len(stats["single"]) == 1

    def test_get_patterns(self, svc, db):
        db.upsert_pattern({
            "pattern_key": "hash_abc",
            "conditions": ["a", "b"],
            "status": "active",
        })
        patterns = svc.get_patterns(status="active")
        assert len(patterns) == 1


class TestSkipInstruction:
    def test_skip_basic(self, svc, db):
        inst_id = _save_inst(db)
        result = svc.skip_instruction(inst_id, reason="risk_too_high")
        assert result["success"] is True

        # Verify DB updated
        inst = db.get_instruction("2026-06-18", "US.NTES")
        assert inst["outcome"] == "skipped"

        # Verify feedback saved
        feedbacks = db.get_user_feedback(inst_id)
        assert len(feedbacks) == 1
        assert feedbacks[0]["action"] == "skipped"

    def test_skip_with_custom_reason(self, svc, db):
        inst_id = _save_inst(db)
        result = svc.skip_instruction(inst_id, custom_reason="我有内部消息")
        assert result["success"] is True

        feedbacks = db.get_user_feedback(inst_id)
        assert "内部消息" in feedbacks[0]["skip_reason"]

    def test_skip_nonexistent(self, svc):
        result = svc.skip_instruction(99999, reason="test")
        assert result["success"] is False


class TestModifyInstruction:
    def test_modify_stop_loss(self, svc, db):
        inst_id = _save_inst(db, stop_loss=95.0)
        result = svc.modify_instruction(inst_id, "stop_loss", "92.0")
        assert result["success"] is True

        feedbacks = db.get_user_feedback(inst_id)
        assert len(feedbacks) == 1
        assert feedbacks[0]["action"] == "modified"
        assert feedbacks[0]["modified_field"] == "stop_loss"
        assert feedbacks[0]["original_value"] == "95.0"
        assert feedbacks[0]["modified_value"] == "92.0"

    def test_modify_nonexistent(self, svc):
        result = svc.modify_instruction(99999, "stop_loss", "90")
        assert result["success"] is False


class TestExecuteInstruction:
    @patch("app.playbook.playbook_service.PlaybookService._find_instruction_by_id")
    def test_execute_skip_fails(self, mock_find, svc, db):
        """SKIP 指令不可执行"""
        inst_id = _save_inst(db, direction="SKIP")
        mock_find.return_value = db.get_instruction("2026-06-18", "US.NTES")
        result = svc.execute_instruction(inst_id)
        assert result["success"] is False
        assert "SKIP" in result["message"]

    @patch("app.playbook.playbook_service.PlaybookService._find_instruction_by_id")
    def test_execute_already_executed(self, mock_find, svc, db):
        """已执行的指令不可重复执行"""
        inst_id = _save_inst(db)
        db.update_instruction_tracking(inst_id, {"executed": 1})
        mock_find.return_value = db.get_instruction("2026-06-18", "US.NTES")
        result = svc.execute_instruction(inst_id)
        assert result["success"] is False
        assert "已执行" in result["message"]

    def test_execute_nonexistent(self, svc):
        result = svc.execute_instruction(99999)
        assert result["success"] is False


class TestInstructionDetail:
    def test_detail_with_feedback(self, svc, db):
        inst_id = _save_inst(db)
        db.save_user_feedback({"instruction_id": inst_id, "action": "executed"})

        detail = svc.get_instruction_detail(inst_id)
        assert detail is not None
        assert len(detail["user_feedback"]) == 1

    def test_detail_not_found(self, svc):
        detail = svc.get_instruction_detail(99999)
        assert detail is None
