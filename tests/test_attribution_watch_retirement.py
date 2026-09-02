"""DEB-20260903-trade-attribution-zombie-watch 方案A 钉死测试 (2026-09-03, 用户裁"按建议执行").

背景: trade_attribution 表止于 2026-07-14 (ADR-015 策略隔离砍唯一写入方), 8/27 退役⑦
已裁停 review_loop 归因规则, 但读侧两处守卫未跟进 — daily_invariant_check.py J2 三口径
对账每日查询死表恒输出 not_judgeable_attribution_stalled (僵尸黄灯), scheduler_triggers.py
Task #30 SystemicIssueAnalyzer 周五对着空表静默无操作. 本测试钉死退役终态:
  A. invariant 输出 caliber_diff.status == "retired_attribution_20260827",
     死表字段 (trade_attribution_pnl / attribution_last_trade_date / not_judgeable) 全消失,
     且不再查询 trade_attribution (表存在也不读 → 删表零影响)
  B. Task #30 kill switch 显式关停 + 带恢复条件, 防静默复活
回滚: revert 方案A 提交并删本文件即可, 零生产数据风险.
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py"
)
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

_DEB_ID = "DEB-20260903-trade-attribution-zombie-watch"

_COLS = (
    "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, source_type TEXT, "
    "trade_date TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL, "
    "exit_price REAL, quantity REAL, realized_pnl REAL, pnl_net REAL, "
    "fees_est REAL, pnl_pct REAL, mfe_pct REAL, mae_pct REAL, "
    "signal_confidence REAL, metadata_json TEXT, data_quality TEXT, "
    "exit_reason TEXT, created_at TEXT"
)


def _mk_db(tmp_path, with_attribution_table=False):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE trade_records ({_COLS})")
    day = dic.now_et().date().isoformat()
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, source_type, trade_date, "
        "entry_time, exit_time, entry_price, exit_price, quantity, realized_pnl, "
        "pnl_net, fees_est, pnl_pct, mfe_pct, mae_pct, signal_confidence, "
        "metadata_json, data_quality, exit_reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("US.TEST", "LONG", "PLAYBOOK_SIM", day, f"{day}T09:40:00-04:00",
         f"{day}T15:40:00-04:00", 10.0, 11.0, 10.0, 10.0, 9.0, 1.0,
         10.0, 12.0, 2.0, 0.6, "{}", "real", "sl_hit", f"{day} 15:40:00"))
    if with_attribution_table:
        # 死表仍在且含行 — 若退役不彻底 (仍查询), 旧代码路径会读出这些值
        conn.execute(
            "CREATE TABLE trade_attribution (id INTEGER PRIMARY KEY, trade_date TEXT, "
            "pnl REAL, systemic_pattern TEXT)")
        conn.execute(
            "INSERT INTO trade_attribution (trade_date, pnl, systemic_pattern) "
            "VALUES ('2026-07-14', -999.0, 'phantom')")
    conn.commit()
    conn.close()
    return db


def _run(tmp_path, monkeypatch, db):
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "OUT", tmp_path / "out.json")
    rc = dic.main()
    return rc, json.loads((tmp_path / "out.json").read_text())


# ── A. invariant 侧退役终态 ────────────────────────────────────────────

@pytest.mark.parametrize("with_attribution_table", [False, True],
                         ids=["no_table", "table_present_but_untouched"])
def test_caliber_diff_reports_retired_terminal_state(
        tmp_path, monkeypatch, with_attribution_table):
    """无论死表存在与否, 输出都是退役终态; 死表残留数据不得泄入对账字段."""
    db = _mk_db(tmp_path, with_attribution_table)
    rc, result = _run(tmp_path, monkeypatch, db)
    cd = result["caliber_diff"]
    assert cd["status"] == "retired_attribution_20260827"
    assert cd["trade_records_net"] == 9.0
    assert "error" not in cd, cd
    assert "trade_attribution_pnl" not in cd
    assert "attribution_last_trade_date" not in cd
    assert "gap_pct" not in cd
    assert "not_judgeable_attribution_stalled" not in json.dumps(result)
    assert rc == 0, result["violations"]


def test_invariant_script_no_longer_queries_dead_table():
    """死表查询移除: SQL 里不得再出现 FROM trade_attribution (注释提及不算)."""
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text()
    assert "FROM trade_attribution" not in src
    assert "retired_attribution_20260827" in src
    assert _DEB_ID in src


def test_dropping_dead_table_would_not_break_invariant(tmp_path, monkeypatch):
    """极端验证退役彻底性: trade_attribution 表整表 DROP 也能正常出报告."""
    db = _mk_db(tmp_path, with_attribution_table=True)
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE trade_attribution")
    conn.commit()
    conn.close()  # noqa: SIM115 — 测试夹具短连接
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["caliber_diff"]["status"] == "retired_attribution_20260827"
    assert rc == 0, result["violations"]


# ── B. Task #30 kill switch ─────────────────────────────────────────────

def test_task30_systemic_analyzer_disabled_by_default():
    """kill switch 必须显式 False — 复活只能通过改这行 (触发 DEB 恢复条件审阅)."""
    from app.core import scheduler_triggers as st
    assert st._TASK30_SYSTEMIC_ANALYZER_ENABLED is False


def test_task30_gate_is_pinned_in_source():
    """gate 必须是 try 内 RuntimeError (except logger.debug 优雅吞掉, 周五主流程不受影响),
    且注释/异常信息携带 DEB 编号与恢复条件指向."""
    src = (PROJECT_ROOT / "app" / "core" / "scheduler_triggers.py").read_text()
    assert "_TASK30_SYSTEMIC_ANALYZER_ENABLED = False" in src
    assert "if not _TASK30_SYSTEMIC_ANALYZER_ENABLED" in src
    assert "DEB-20260903-trade-attribution-zombie-watch" in src
    assert "已退役" in src and "RuntimeError" in src
