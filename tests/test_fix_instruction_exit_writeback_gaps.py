"""Contract tests for scripts/fix_instruction_exit_writeback_gaps.py.

2026-10-01 全链路审计: 9/17 盘前 tighten/lock_in 临时阈值(9/18 用户勘误已废)平仓
未带 instruction_id → pi 回写断链 (2550/2771 两行 outcome='holding' + 陈旧未实现
pnl 快照, NOK 符号翻转 -112.13 vs 权威净 +108.43)。本脚本为一次性历史回填,
测试钉住: 只回填 holding/NULL、跳过终态/ghost、净额=毛-费、幂等。
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fix_gaps", ROOT / "scripts" / "fix_instruction_exit_writeback_gaps.py")
fix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix)


def _make_db(tmp_path) -> Path:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE playbook_instructions (
        id INTEGER PRIMARY KEY, symbol TEXT, outcome TEXT, pnl REAL, pnl_pct REAL,
        exit_price REAL, exit_time TEXT, exit_reason TEXT, holding_period_hours REAL)""")
    conn.execute("""CREATE TABLE trade_records (
        id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, entry_price REAL,
        exit_price REAL, exit_time TEXT, exit_reason TEXT, realized_pnl REAL,
        fees_est REAL, holding_hours REAL, quantity REAL, metadata_json TEXT,
        source_type TEXT)""")
    conn.commit()
    conn.close()
    return db


def _add_case(db, pid, outcome, tr_exit, gross, fee, qty=100, px=50.0,
              source="PLAYBOOK_SIM", exit_time="2026-09-17T04:00:00-04:00",
              exit_price=51.0, pi_pnl=-10.0):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?,?,?,?)",
                 (pid, "US.TEST", outcome, pi_pnl, None, None, None, None, None))
    md = json.dumps({"playbook_instruction_id": pid, "quantity": qty})
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid * 10, "US.TEST", "LONG", px, exit_price, exit_time, tr_exit,
         gross, fee, 3.0, qty, md, source))
    conn.commit()
    conn.close()


class TestFindGaps:
    def test_holding_row_is_gap(self, tmp_path):
        db = _make_db(tmp_path)
        _add_case(db, 1, "holding", "tighten", 123.62, 15.19)
        gaps = fix.find_gaps(db)
        assert len(gaps) == 1
        bf = gaps[0]["backfill"]
        assert bf["pnl"] == pytest.approx(108.43)   # 净 = 毛 - 费
        assert bf["outcome"] == "win"
        assert bf["exit_reason"] == "tighten"
        assert bf["pnl_pct"] == pytest.approx(108.43 / 5000.0, abs=1e-6)

    def test_null_outcome_is_gap(self, tmp_path):
        db = _make_db(tmp_path)
        _add_case(db, 2, None, "lock_in", -94.77, 4.6)
        gaps = fix.find_gaps(db)
        assert len(gaps) == 1
        assert gaps[0]["backfill"]["outcome"] == "loss"
        assert gaps[0]["backfill"]["pnl"] == pytest.approx(-99.37)

    def test_terminal_outcomes_untouched(self, tmp_path):
        db = _make_db(tmp_path)
        for i, oc in enumerate(("win", "loss", "void", "closed", "already_held")):
            _add_case(db, 10 + i, oc, "tighten", 10.0, 1.0)
        assert fix.find_gaps(db) == []

    def test_ghost_exit_skipped(self, tmp_path):
        """E2 契约: GHOST/ORPHAN/PHANTOM 平仓 → outcome=void 语义, 不得回填 win/loss。"""
        db = _make_db(tmp_path)
        _add_case(db, 20, "holding", "BROKER_GHOST_CLOSE", 39.59, 0.0)
        assert fix.find_gaps(db) == []

    def test_unclosed_trade_skipped(self, tmp_path):
        db = _make_db(tmp_path)
        _add_case(db, 30, "holding", None, None, None,
                  exit_time="", exit_price=0.0)
        assert fix.find_gaps(db) == []

    def test_position_sync_source_skipped(self, tmp_path):
        db = _make_db(tmp_path)
        _add_case(db, 40, "holding", "external_close", 5.0, 1.0,
                  source="POSITION_SYNC")
        assert fix.find_gaps(db) == []


class TestApply:
    def test_apply_writes_and_is_idempotent(self, tmp_path):
        db = _make_db(tmp_path)
        _add_case(db, 1, "holding", "tighten", 123.62, 15.19)
        audit = tmp_path / "audits" / "a.jsonl"
        gaps = fix.find_gaps(db)
        fix.apply_backfill(db, gaps, audit)
        # audit 留痕
        assert audit.exists()
        assert len(audit.read_text().strip().splitlines()) == 1
        # 幂等: 再找为 0
        assert fix.find_gaps(db) == []
        # 回写值正确
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT outcome, pnl, exit_reason FROM playbook_instructions WHERE id=1"
        ).fetchone()
        conn.close()
        assert row == ("win", pytest.approx(108.43), "tighten")

    def test_apply_skips_rows_settled_between_find_and_apply(self, tmp_path):
        """find 与 apply 之间被主链结算的行不得覆盖 (竞态防护 = UPDATE WHERE 守卫)。"""
        db = _make_db(tmp_path)
        _add_case(db, 1, "holding", "tighten", 123.62, 15.19)
        gaps = fix.find_gaps(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE playbook_instructions SET outcome='loss', pnl=-1.0 WHERE id=1")
        conn.commit()
        conn.close()
        fix.apply_backfill(db, gaps, tmp_path / "a.jsonl")
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT outcome, pnl FROM playbook_instructions WHERE id=1").fetchone()
        conn.close()
        assert row == ("loss", -1.0)  # 未被回填覆盖

    def test_quantity_falls_back_to_metadata(self, tmp_path):
        """trade_records.quantity 为 NULL 时退回 metadata.quantity 算 pnl_pct。"""
        db = _make_db(tmp_path)
        _add_case(db, 1, "holding", "tighten", 100.0, 10.0)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE trade_records SET quantity=NULL WHERE symbol='US.TEST'")
        conn.commit()
        conn.close()
        gaps = fix.find_gaps(db)
        assert gaps[0]["backfill"]["pnl_pct"] == pytest.approx(90.0 / 5000.0, abs=1e-6)
