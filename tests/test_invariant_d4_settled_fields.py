"""daily_invariant_check I11~I14 (D4 测量不变量) 回归测试 (2026-08-28)。

背景: 全仓"当日"口径按 substr(exit_time,1,10) + COALESCE(pnl_net, realized_pnl) 聚合后,
了结行缺字段不再是"算错"而是从数字里整笔消失。I1~I10 只看 PLAYBOOK% 且 exit_time 非空的行,
I11~I14 补上"全部来源的了结行 + 字段级缺失", 并要求守卫自身故障必须显性化
(否则"零违反"与"守卫崩了"长得一模一样 — D1/D2 同课)。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py"
)
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

# 生产列集 (比 I10 夹具多 data_quality / exit_reason / created_at)
_COLS = (
    "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, source_type TEXT, "
    "trade_date TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL, "
    "exit_price REAL, quantity REAL, realized_pnl REAL, pnl_net REAL, "
    "fees_est REAL, pnl_pct REAL, mfe_pct REAL, mae_pct REAL, "
    "signal_confidence REAL, metadata_json TEXT, data_quality TEXT, "
    "exit_reason TEXT, created_at TEXT"
)


def _mk_db(tmp_path, cols=_COLS):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE trade_records ({cols})")
    conn.commit()
    return db


def _row(db, *, source="PLAYBOOK_SIM", realized_pnl=10.0, pnl_net=9.0, fees=1.0,
         entry=10.0, exit_=11.0, qty=10.0, exit_reason="sl_hit", data_quality="real",
         exit_day=None, created=None, symbol="US.TEST"):
    """一条物理自洽的了结行 (I1~I10 全过), 只留被测字段可为缺陷态。"""
    day = exit_day or dic.now_et().date().isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, source_type, trade_date, "
        "entry_time, exit_time, entry_price, exit_price, quantity, realized_pnl, "
        "pnl_net, fees_est, pnl_pct, mfe_pct, mae_pct, signal_confidence, "
        "metadata_json, data_quality, exit_reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, "LONG", source, day, f"{day}T09:40:00-04:00", f"{day}T15:40:00-04:00",
         entry, exit_, qty, realized_pnl, pnl_net, fees,
         0.0 if not realized_pnl else 10.0, 12.0, 2.0, 0.6, "{}", data_quality,
         exit_reason, created or f"{day} 15:40:00"),
    )
    conn.commit()
    conn.close()


def _run(tmp_path, monkeypatch, db):
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "OUT", tmp_path / "out.json")
    rc = dic.main()
    return rc, json.loads((tmp_path / "out.json").read_text())


# ── I11 了结无盈亏记账 ──────────────────────────────────────────────────

def test_null_realized_pnl_on_settled_row_is_flagged(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _row(db, realized_pnl=None)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["violations"]["I11_closed_null_pnl"] == 1
    assert rc == 1


def test_clean_settled_rows_pass(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _row(db)
    _row(db, symbol="US.TEST2", source="SCALPING", realized_pnl=-5.0, pnl_net=-6.0)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert rc == 0, result["violations"]
    assert result["settled_all_sources"] == 2
    assert result["settled_exempt_marked"] == 0


# ── I12 占位符伪装盈亏 ──────────────────────────────────────────────────

def test_zero_placeholder_pnl_with_real_spread_is_flagged(tmp_path, monkeypatch):
    """8/28 占位链缺陷的常驻守卫: 价差×量=$100 却记 realized_pnl=0."""
    db = _mk_db(tmp_path)
    _row(db, realized_pnl=0.0, pnl_net=-1.0, fees=1.0)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["violations"]["I12_zero_as_proxy_pnl"] == 1


def test_genuine_flat_exit_is_not_flagged(tmp_path, monkeypatch):
    """真实打平出场 (entry==exit) 不得误判成占位符。"""
    db = _mk_db(tmp_path)
    _row(db, realized_pnl=0.0, pnl_net=0.0, fees=0.0, exit_=10.0)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert "I12_zero_as_proxy_pnl" not in result["violations"], result["violations"]


# ── I13 净额口径静默回落毛额 ────────────────────────────────────────────

def test_missing_net_after_fee_rollout_is_flagged(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _row(db, pnl_net=None)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["violations"]["I13_net_caliber_degraded"] == 1


def test_missing_net_before_fee_rollout_is_exempt(tmp_path, monkeypatch):
    """上线日 (_FEE_ROLLOUT_DATE) 前的历史行无净额属正常态, 不得倒过来报警。"""
    db = _mk_db(tmp_path)
    _row(db, pnl_net=None, fees=None,
         created="2026-07-01 15:40:00", realized_pnl=10.0)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert "I13_net_caliber_degraded" not in result["violations"], result["violations"]


# ── I14 出场归因丢行 ────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [None, "", "   "])
def test_settled_row_without_exit_reason_is_flagged(tmp_path, monkeypatch, bad):
    """NULL 也判: 全库 725 条了结行零 NULL (2026-08-28 实测) ⇒ 出现 NULL = 写入链停止赋值。"""
    db = _mk_db(tmp_path)
    _row(db, exit_reason=bad)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["violations"]["I14_closed_no_exit_reason"] == 1


def test_known_exit_reason_is_silent(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _row(db, exit_reason="PRE_CLOSE_EXIT")
    rc, result = _run(tmp_path, monkeypatch, db)
    assert "I14_closed_no_exit_reason" not in result["violations"]


# ── 覆盖面与豁免 ────────────────────────────────────────────────────────

def test_non_playbook_sources_are_covered(tmp_path, monkeypatch):
    """主循环只看 PLAYBOOK% — broker 同步/手动/剥头皮的了结行必须由 I11~I14 兜住。"""
    db = _mk_db(tmp_path)
    _row(db, source="POSITION_SYNC", realized_pnl=None)
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["sample"] == 0, "主样本确实不含该来源 (证明不是主循环报的)"
    assert result["violations"]["I11_closed_null_pnl"] == 1
    assert result["settled_all_sources"] == 1


def test_marked_bad_row_is_exempt_but_counted(tmp_path, monkeypatch):
    """data_quality='duplicate_external_close' = 写入方声明非真实了结 → 豁免但落数量。"""
    db = _mk_db(tmp_path)
    _row(db, source="SCALPING", realized_pnl=None, pnl_net=None,
         data_quality="duplicate_external_close")
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["violations"] == {}, result["violations"]
    assert result["settled_all_sources"] == 1
    assert result["settled_exempt_marked"] == 1


def test_normal_data_quality_labels_are_not_exempt(tmp_path, monkeypatch):
    """'real'/'clean'/'manual' 是全库常态值 (457/265/1) — 拿它们当豁免 = 守卫零覆盖。"""
    db = _mk_db(tmp_path)
    for i, dq in enumerate(("real", "clean", "manual")):
        _row(db, realized_pnl=None, data_quality=dq, symbol=f"US.T{i}")
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["violations"]["I11_closed_null_pnl"] == 3
    assert result["settled_exempt_marked"] == 0


# ── 自检的自检 ──────────────────────────────────────────────────────────

def test_detector_crash_surfaces_as_violation(tmp_path, monkeypatch):
    """字段守卫内部异常必须变成告警 (旧实现 except: pass → 崩溃读成"全部健康")。"""
    db = _mk_db(tmp_path)
    _row(db, source="POSITION_SYNC")
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE trade_records SET entry_price='abc'")  # 非数值 → float() 抛 ValueError
    conn.commit()
    conn.close()
    rc, result = _run(tmp_path, monkeypatch, db)
    assert "D4_detector_error" in result["violations"], result["violations"]
    assert result["settled_all_sources"] == -1
    assert rc == 1


def test_old_schema_db_is_tolerated_not_blind(tmp_path, monkeypatch):
    """旧库形态 (无 data_quality/exit_reason/净额列, 无 playbook_instructions 表):
    结构性缺表缺列静默跳过, 但 I11/I12 仍须工作 — 不得整段放弃。"""
    cols = (
        "id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, source_type TEXT, "
        "trade_date TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL, "
        "exit_price REAL, quantity REAL, realized_pnl REAL, pnl_pct REAL, "
        "mfe_pct REAL, mae_pct REAL, signal_confidence REAL, metadata_json TEXT"
    )
    db = _mk_db(tmp_path, cols=cols)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, source_type, trade_date, "
        "entry_time, exit_time, entry_price, exit_price, quantity, realized_pnl, "
        "pnl_pct, mfe_pct, mae_pct, signal_confidence, metadata_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("US.OLD", "LONG", "PLAYBOOK_SIM", dic.now_et().date().isoformat(),
         f"{dic.now_et().date().isoformat()}T09:40:00-04:00",
         f"{dic.now_et().date().isoformat()}T15:40:00-04:00",
         10.0, 11.0, 10.0, 0.0, 0.0, 12.0, 2.0, 0.6, "{}"))
    conn.commit()
    conn.close()
    rc, result = _run(tmp_path, monkeypatch, db)
    assert result["violations"]["I12_zero_as_proxy_pnl"] == 1
    assert not any(k.endswith("detector_error") for k in result["violations"]), result["violations"]
