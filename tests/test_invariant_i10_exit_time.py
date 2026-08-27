"""daily_invariant_check I10 不变量回归测试 (2026-08-28)。

背景: 自 2026-08-27/28 起, 全仓"当日"口径一律按 substr(exit_time,1,10) 归集
(熔断/日亏损门/再入场门/冷却门/日报/复盘验证/面板/心跳)。这把新依赖暴露出来:
已了结行 (exit_price>0) 若缺 exit_time, 不再是"归错一天"而是从所有当日数字里
静默消失 (钱不见了且无人报警)。I10 每晚机械守卫这一前提。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py"
)
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

_COLS = (
    "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, source_type TEXT, "
    "trade_date TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL, "
    "exit_price REAL, quantity REAL, realized_pnl REAL, pnl_net REAL, "
    "fees_est REAL, pnl_pct REAL, mfe_pct REAL, mae_pct REAL, "
    "signal_confidence REAL, metadata_json TEXT"
)


def _mk_db(tmp_path):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE trade_records ({_COLS})")
    conn.commit()
    return db


def _row(db, *, symbol, exit_time, exit_price=11.0, exit_day_pnl=10.0):
    """一条物理自洽的已了结行 (I1/I2/I7/I8 全过)"""
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, source_type, trade_date, "
        "entry_time, exit_time, entry_price, exit_price, quantity, realized_pnl, "
        "pnl_net, fees_est, pnl_pct, mfe_pct, mae_pct, signal_confidence, "
        "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, "LONG", "PLAYBOOK_SIM", "2026-08-27",
         "2026-08-27T09:40:00-04:00", exit_time, 10.0, exit_price, 10.0,
         exit_day_pnl, exit_day_pnl - 1.0, 1.0, 10.0, 12.0, 2.0, 0.6, "{}"),
    )
    conn.commit()
    conn.close()


def _run(tmp_path, monkeypatch, db):
    out = tmp_path / "invariant_out.json"
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "OUT", out)
    rc = dic.main()
    return rc, json.loads(out.read_text())


def test_closed_row_missing_exit_time_is_flagged(tmp_path, monkeypatch):
    """核心: 已了结但 exit_time 为 NULL → I10 报警 (旧口径下该缺陷静默)。"""
    db = _mk_db(tmp_path)
    _row(db, symbol="US.GHOST", exit_time=None)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert rc == 1
    assert result["violations"]["I10_closed_missing_exit_time"] == 1
    assert "US.GHOST" in result["examples"]["I10_closed_missing_exit_time"]


def test_empty_string_exit_time_also_flagged(tmp_path, monkeypatch):
    """exit_time='' (历史脏值形态) 同罪 — substr('') 不匹配任何日期。"""
    db = _mk_db(tmp_path)
    _row(db, symbol="US.GHOST2", exit_time="")
    rc, result = _run(tmp_path, monkeypatch, db)
    assert rc == 1
    assert result["violations"]["I10_closed_missing_exit_time"] == 1


def test_clean_db_passes(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _row(db, symbol="US.OK", exit_time="2026-08-27T15:40:00-04:00")
    rc, result = _run(tmp_path, monkeypatch, db)
    assert rc == 0, result["violations"]
    assert "I10_closed_missing_exit_time" not in result["violations"]


def test_open_row_not_false_positive(tmp_path, monkeypatch):
    """在仓行 (exit_price=0) 无 exit_time 属正常态 → 不得报警。"""
    db = _mk_db(tmp_path)
    _row(db, symbol="US.OPEN", exit_time=None, exit_price=0.0, exit_day_pnl=0.0)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert "I10_closed_missing_exit_time" not in result["violations"], result
    assert rc == 0
