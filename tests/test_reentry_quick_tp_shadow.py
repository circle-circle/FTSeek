"""再入场快TP门禁吞掉影子测试钉 (2026-08-31, DEB-20260831-reentry-quick-tp-gate-conflict).

钉四件事:
  ① note_gate_swallowed_touch: 触达记 meta / 未触达不记 / 重复触达去重 / 脏输入不炸。
  ② settle_on_close: 再入场臂+触达 → 落行且反事实差方向正确; 非再入场无触达 → 不落;
     开关关 → 不落。
  ③ 集成: _check_playbook_exit_rules 被吞 TP 场景 — 退出行为不变 (无 TAKE_PROFIT),
     meta 记到触达 (LONG/SHORT 双向)。
  ④ 对照组: RR≥1.5 的 TP 照常止盈, 无触达标记。
"""
import json
import sqlite3

import pytest

from app.playbook import reentry_tp_shadow as rts
from app.playbook.pipeline import _check_playbook_exit_rules


def _base_meta(**kw):
    meta = {"_log_tag": "test", "_symbol": "TEST", "cohort": "A", "horizon": "short"}
    meta.update(kw)
    return meta


# ── ① note_gate_swallowed_touch 单元 ──────────────────────────────────────

def test_note_touch_long_records_once(monkeypatch):
    # 2026-10-03 清理批: 生产开关已随判据窗满 KILLED 置 false (退役),
    # 本文件钉的是「写方机制」而非开关默认值 ⇒ 显式强开。
    monkeypatch.setattr(rts, "_enabled", lambda: True)
    meta = _base_meta()
    rts.note_gate_swallowed_touch(meta, "LONG", 101.5, 101.4, peak_profit_pct=1.5, held_minutes=30)
    assert rts.TOUCH_KEY in meta
    first = meta[rts.TOUCH_KEY]
    assert first["price"] == 101.5
    assert first["held_minutes"] == 30.0
    # 重复触达 → 去重, 原记录不变
    rts.note_gate_swallowed_touch(meta, "LONG", 102.0, 101.4, peak_profit_pct=2.0, held_minutes=40)
    assert meta[rts.TOUCH_KEY] is first


def test_note_touch_short_and_no_touch(monkeypatch):
    monkeypatch.setattr(rts, "_enabled", lambda: True)
    meta = _base_meta()
    # SHORT: 价格 ≤ TP 才算触达
    rts.note_gate_swallowed_touch(meta, "SHORT", 98.5, 98.6, peak_profit_pct=1.4, held_minutes=20)
    assert rts.TOUCH_KEY in meta
    meta2 = _base_meta()
    rts.note_gate_swallowed_touch(meta2, "LONG", 101.0, 101.4)  # 未达 TP
    assert rts.TOUCH_KEY not in meta2
    # 脏输入不炸
    rts.note_gate_swallowed_touch(meta2, "LONG", None, "bad")
    rts.note_gate_swallowed_touch(None, "LONG", 1.0, 1.0)
    assert rts.TOUCH_KEY not in meta2


# ── ② settle_on_close 单元 ────────────────────────────────────────────────

def _make_db(tmp_path, decision_id, meta_extra):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, "
        "entry_price REAL, quantity INTEGER, entry_time TEXT, decision_id TEXT, "
        "metadata_json TEXT, source_type TEXT)")
    meta = {"take_profit": 101.4, "stop_loss": 97.5}
    meta.update(meta_extra)
    conn.execute(
        "INSERT INTO trade_records (symbol, direction, entry_price, quantity, entry_time, "
        "decision_id, metadata_json, source_type) VALUES (?,?,?,?,?,?,?,?)",
        ("US.TEST", "LONG", 100.0, 10, "2026-09-01T10:00:00-04:00", decision_id,
         json.dumps(meta), "PLAYBOOK_SIM"))
    conn.commit()
    return conn


def test_settle_reentry_with_touch_writes_row(tmp_path, monkeypatch):
    monkeypatch.setattr(rts, "_enabled", lambda: True)
    conn = _make_db(tmp_path, "intraday_reentry_20260901_US.TEST",
                    {rts.TOUCH_KEY: {"price": 101.4, "ts": "t0", "held_minutes": 25.0,
                                     "mfe_pct": 1.4}})
    out = tmp_path / "shadow.jsonl"
    rts.settle_on_close(conn, 1, exit_price=100.6, exit_reason="SL_TRAIL_LOCK", shadow_path=out)
    conn.close()
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["is_reentry"] is True and rec["tp_touched"] is True
    assert rec["tp_rr"] == pytest.approx(1.4 / 2.5, abs=1e-3)
    # TP 出场比实际好 0.8pp; 毛盈利 0.6pp
    assert rec["tp_vs_actual_pct"] == pytest.approx(0.8, abs=1e-4)
    assert rec["gross_pnl_pct"] == pytest.approx(0.6, abs=1e-4)


def test_settle_non_reentry_no_touch_skips(tmp_path):
    conn = _make_db(tmp_path, "da_bridge_xxx", {})
    out = tmp_path / "shadow.jsonl"
    rts.settle_on_close(conn, 1, exit_price=100.6, exit_reason="SL_INITIAL", shadow_path=out)
    conn.close()
    assert not out.exists()


def test_settle_disabled_flag_writes_nothing(tmp_path, monkeypatch):
    import app.playbook.config as pbcfg
    monkeypatch.setattr(pbcfg, "get_playbook_config",
                        lambda: {"reentry_quick_tp_shadow_enabled": False})
    conn = _make_db(tmp_path, "intraday_reentry_x",
                    {rts.TOUCH_KEY: {"price": 101.4, "ts": "t0", "held_minutes": 1.0,
                                     "mfe_pct": 1.4}})
    out = tmp_path / "shadow.jsonl"
    rts.settle_on_close(conn, 1, exit_price=100.6, exit_reason="SL_INITIAL", shadow_path=out)
    meta = {}
    rts.note_gate_swallowed_touch(meta, "LONG", 102.0, 101.4)
    conn.close()
    assert not out.exists()
    assert rts.TOUCH_KEY not in meta


# ── ③④ 集成: 退出行为不变 + 触达记录 ──────────────────────────────────────

def test_gate_swallowed_tp_no_exit_but_touch_recorded_long(monkeypatch):
    monkeypatch.setattr(rts, "_enabled", lambda: True)
    # RR = 1.4/2.5 = 0.56 < 1.5 → TP 被门禁吞; 价格已越过 TP
    meta = _base_meta(take_profit=101.4, stop_loss=97.5)
    reason, exit_price, peak, be = _check_playbook_exit_rules(
        direction="LONG", entry_price=100.0, current_price=101.5,
        initial_sl=97.5, peak_price=101.5, meta=meta,
        held_minutes=30, minutes_to_close=300, minutes_from_open=30, entry_time_str="")
    assert reason != "TAKE_PROFIT", "被吞TP不应触发止盈 (行为不变钉)"
    assert reason == "", f"该场景应无退出, got {reason}"
    assert rts.TOUCH_KEY in meta, "触达应记入 meta"


def test_gate_swallowed_tp_no_exit_but_touch_recorded_short(monkeypatch):
    monkeypatch.setattr(rts, "_enabled", lambda: True)
    meta = _base_meta(take_profit=98.6, stop_loss=102.5)
    reason, *_ = _check_playbook_exit_rules(
        direction="SHORT", entry_price=100.0, current_price=98.5,
        initial_sl=102.5, peak_price=98.5, meta=meta,
        held_minutes=30, minutes_to_close=300, minutes_from_open=30, entry_time_str="")
    assert reason != "TAKE_PROFIT"
    assert rts.TOUCH_KEY in meta


def test_gate_swallowed_tp_below_tp_no_touch():
    meta = _base_meta(take_profit=101.4, stop_loss=97.5)
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100.0, current_price=100.8,
        initial_sl=97.5, peak_price=100.8, meta=meta,
        held_minutes=30, minutes_to_close=300, minutes_from_open=30, entry_time_str="")
    assert reason == ""
    assert rts.TOUCH_KEY not in meta


def test_good_rr_tp_still_fires_no_touch_flag():
    # RR = 4/2 = 2.0 ≥ 1.5 → TP 照常生效 (对照组)
    meta = _base_meta(take_profit=104.0, stop_loss=98.0)
    reason, *_ = _check_playbook_exit_rules(
        direction="LONG", entry_price=100.0, current_price=104.2,
        initial_sl=98.0, peak_price=104.2, meta=meta,
        held_minutes=30, minutes_to_close=300, minutes_from_open=30, entry_time_str="")
    assert reason == "TAKE_PROFIT"
    assert rts.TOUCH_KEY not in meta
