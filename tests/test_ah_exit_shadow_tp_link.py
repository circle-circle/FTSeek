"""AH影子 TP 关联链路契约测试 (2026-08-31)。

背景: find_tp 原用 open_positions.entry_time[:19] == substr(trade_records.entry_time,1,19)
精确秒匹配经 decision_id 关联 playbook_instructions。实测 8/27 US.AVGO 两表入场时间
秒级错位 (:34 vs :33) → 关联失败 → touch_tp 观测臂对该仓永久失明 (tp=None 恒记未触及)。

本测试锁: ①秒级错位下仍取到 TP (在仓行语义关联) ②在仓行已平 (评估与平仓竞态) 时
退回秒级精确匹配兜底 ③无指令行 → None 不阻塞。

回滚: 删本文件 + revert scripts/ah_exit_shadow.py find_tp 改动。
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ah_exit_shadow_tp", PROJECT_ROOT / "scripts" / "ah_exit_shadow.py")
aes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aes)


@pytest.fixture
def tp_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trade_records (symbol TEXT, direction TEXT, "
                "entry_time TEXT, exit_time TEXT, decision_id TEXT)")
    con.execute("CREATE TABLE playbook_instructions (decision_id TEXT, "
                "take_profit REAL, updated_at TEXT)")
    yield con, db
    con.close()


def _bind(con, db, monkeypatch):
    monkeypatch.setattr(aes, "DB", db)
    return con


def test_second_skew_still_links_tp(tp_db, monkeypatch):
    """open_positions :34 vs trade_records :33 (实测 AVGO) → 仍取到 TP。"""
    con, db = tp_db
    _bind(con, db, monkeypatch)
    con.execute("INSERT INTO trade_records VALUES "
                "('US.AVGO','SHORT','2026-08-27T09:48:33.895652-04:00',NULL,'D-1')")
    con.execute("INSERT INTO playbook_instructions VALUES "
                "('D-1', 333.6, '2026-08-28T23:53:14+00:00')")
    con.commit()
    # pos_key 取自 open_positions 口径 (秒级错位 :34)
    assert aes.find_tp(("US.AVGO", "SHORT", "2026-08-27T09:48:34")) == 333.6


def test_closed_position_falls_back_to_second_match(tp_db, monkeypatch):
    """评估与平仓竞态: 在仓行语义查不到 (已平) → 秒级精确匹配兜底。"""
    con, db = tp_db
    _bind(con, db, monkeypatch)
    con.execute("INSERT INTO trade_records VALUES "
                "('US.PDD','LONG','2026-08-27T09:40:00.000000-04:00',"
                "'2026-08-28T10:00:00-04:00','D-2')")
    con.execute("INSERT INTO playbook_instructions VALUES "
                "('D-2', 92.25, '2026-08-27T10:00:00+00:00')")
    con.commit()
    assert aes.find_tp(("US.PDD", "LONG", "2026-08-27T09:40:00")) == 92.25


def test_no_instructions_returns_none(tp_db, monkeypatch):
    """无指令/TP≤0 → None (touch_tp 臂记未触及不阻塞)。"""
    con, db = tp_db
    _bind(con, db, monkeypatch)
    con.execute("INSERT INTO trade_records VALUES "
                "('US.X','LONG','2026-08-27T09:40:00.000000-04:00',NULL,'D-3')")
    con.commit()
    assert aes.find_tp(("US.X", "LONG", "2026-08-27T09:40:00")) is None


def test_latest_instruction_wins(tp_db, monkeypatch):
    """多条指令取 updated_at 最新 (改单后 TP 跟进)。"""
    con, db = tp_db
    _bind(con, db, monkeypatch)
    con.execute("INSERT INTO trade_records VALUES "
                "('US.M','LONG','2026-08-27T09:40:00.000000-04:00',NULL,'D-4')")
    con.execute("INSERT INTO playbook_instructions VALUES "
                "('D-4', 100.0, '2026-08-27T10:00:00+00:00')")
    con.execute("INSERT INTO playbook_instructions VALUES "
                "('D-4', 105.0, '2026-08-28T10:00:00+00:00')")
    con.commit()
    assert aes.find_tp(("US.M", "LONG", "2026-08-27T09:40:00")) == 105.0
