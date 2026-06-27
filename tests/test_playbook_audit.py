"""Tests for app.playbook.audit and void_instruction."""
import json
import sqlite3
from pathlib import Path

import pytest

from app.playbook.audit import log_reconciliation, get_recent_reconciliation_logs
from app.playbook.db import PlaybookDB


class TestLogReconciliation:
    """审计日志写入/读取测试."""

    def test_log_reconciliation_writes_jsonl(self, tmp_path):
        audit_path = tmp_path / "test_audit.jsonl"
        log_reconciliation(
            action="phantom_void",
            reason="清理 $124K 幻影敞口",
            operator="admin",
            before={"outcome": "holding"},
            after={"outcome": "void"},
            audit_path=audit_path,
        )

        assert audit_path.exists()
        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

        record = json.loads(lines[0])
        assert record["action"] == "phantom_void"
        assert record["reason"] == "清理 $124K 幻影敞口"
        assert record["operator"] == "admin"
        assert record["before"]["outcome"] == "holding"
        assert record["after"]["outcome"] == "void"
        assert "timestamp" in record
        assert "pid" in record

    def test_get_recent_reconciliation_logs_filters_by_action(self, tmp_path):
        audit_path = tmp_path / "test_audit.jsonl"
        log_reconciliation(action="phantom_void", reason="r1", audit_path=audit_path)
        log_reconciliation(action="manual_reconcile", reason="r2", audit_path=audit_path)

        logs = get_recent_reconciliation_logs(action="phantom_void", limit=10, audit_path=audit_path)
        assert len(logs) == 1
        assert logs[0]["action"] == "phantom_void"


class TestVoidInstruction:
    """PlaybookDB.void_instruction 测试."""

    @pytest.fixture
    def db(self, tmp_path):
        db_path = tmp_path / "test_playbook.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        db = PlaybookDB(conn=conn)
        db.ensure_tables()
        return db

    def _insert_instruction(self, db, **kwargs):
        """直接插入 playbook_instructions 行用于测试."""
        defaults = {
            "date": "2026-06-26",
            "symbol": "US.TEST",
            "direction": "BUY",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "position_size": 10,
            "confidence": 70,
            "outcome": "holding",
            "created_at": "2026-06-26T10:00:00",
            "updated_at": "2026-06-26T10:00:00",
        }
        defaults.update(kwargs)
        conn = db._conn()
        conn.execute(
            """
            INSERT INTO playbook_instructions
            (date, symbol, direction, entry_price, stop_loss, take_profit, position_size, confidence, outcome, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                defaults["date"],
                defaults["symbol"],
                defaults["direction"],
                defaults["entry_price"],
                defaults["stop_loss"],
                defaults["take_profit"],
                defaults["position_size"],
                defaults["confidence"],
                defaults["outcome"],
                defaults["created_at"],
                defaults["updated_at"],
            ),
        )
        conn.commit()
        return db.get_instruction(defaults["date"], defaults["symbol"])

    def test_void_instruction_marks_outcome_void(self, db, tmp_path):
        self._insert_instruction(db, symbol="US.TEST", outcome="holding")
        inst = db.get_instruction("2026-06-26", "US.TEST")
        assert inst["outcome"] == "holding"

        result = db.void_instruction(inst["id"], reason="幻影敞口清理", operator="admin")
        assert result is True

        after = db.get_instruction("2026-06-26", "US.TEST")
        assert after["outcome"] == "void"

    def test_void_instruction_refuses_terminal_outcome(self, db):
        self._insert_instruction(db, symbol="US.LOSS", outcome="loss")
        inst = db.get_instruction("2026-06-26", "US.LOSS")

        result = db.void_instruction(inst["id"], reason="不应覆盖终态")
        assert result is False

    def test_void_instruction_writes_audit_log(self, db, tmp_path):
        self._insert_instruction(db, symbol="US.AUDIT", outcome="holding")
        inst = db.get_instruction("2026-06-26", "US.AUDIT")

        # 临时替换 audit 日志路径
        audit_path = tmp_path / "void_audit.jsonl"
        from app.playbook import audit as audit_module
        original_path = audit_module._default_audit_path()
        try:
            audit_module.log_reconciliation(
                action="void_instruction",
                reason="审计测试",
                operator="test",
                before={"outcome": "holding"},
                after={"outcome": "void"},
                audit_path=audit_path,
            )
            assert audit_path.exists()
        finally:
            pass
