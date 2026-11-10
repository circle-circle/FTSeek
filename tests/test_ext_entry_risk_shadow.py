"""EXT 入场风险影子契约测试 (DEB-20261109 Batch 1.2/1.3)。

被测对象: scripts/extended_hours_entry_executor.py 的 `_ext_entry_risk_shadow`。
契约:
  1. **只记录, 不拦单** —— 任何内部异常都不得冒泡到下单路径。
  2. 落盘行含全部必需键, 且 `direction_conflict` 与 DA 方向/EXT 方向一致。
  3. 开关 EXT_ENTRY_RISK_SHADOW=false ⇒ 不写文件。
  4. `has_tp=False` 与 EXT 现状一致 (EXT 写入路径不落 TP) —— 一旦 EXT 开始落 TP, 本测试提醒更新。
  5. `da_available=false` 时 `direction_conflict` 必为 false (没有方向就不可能冲突)。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "scripts" / "extended_hours_entry_executor.py"


def _load_module(monkeypatch, tmp_path, db_path):
    spec = importlib.util.spec_from_file_location("ext_exec_under_test", EXT)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setenv("EXT_ENTRY_RISK_SHADOW", "true")
    spec.loader.exec_module(mod)
    mod.SHADOW_DIR = tmp_path / "shadow"
    mod.DB = str(db_path)
    return mod


def _mk_db(tmp_path, rows):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE playbook_instructions (
        date TEXT, symbol TEXT, row_kind TEXT, decision_id TEXT,
        direction TEXT, created_at TEXT)""")
    for r in rows:
        conn.execute("INSERT INTO playbook_instructions VALUES (?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()
    return db


def test_shadow_writes_required_keys(monkeypatch, tmp_path):
    db = _mk_db(tmp_path, [])
    mod = _load_module(monkeypatch, tmp_path, db)
    mod._ext_entry_risk_shadow("US.TEST", 10, 100.0, "premarket", "LONG", "EXT-x", 95.0)
    out = tmp_path / "shadow" / "ext_entry_risk_shadow.jsonl"
    assert out.exists(), "影子文件未生成"
    row = json.loads(out.read_text().strip().splitlines()[-1])
    for k in ("ts", "date", "symbol", "side", "ext_direction", "session", "entry_px",
              "qty", "notional", "sl", "sl_pct", "max_loss_usd", "cross_night_candidate",
              "has_tp", "da_available", "da_direction", "direction_conflict", "decision_id"):
        assert k in row, f"缺键 {k}"
    assert row["symbol"] == "US.TEST"
    assert row["ext_direction"] == "LONG"
    assert row["notional"] == pytest.approx(1000.0)
    assert row["max_loss_usd"] == pytest.approx(50.0)
    assert row["sl_pct"] == pytest.approx(-5.0)
    assert row["has_tp"] is False
    assert row["cross_night_candidate"] is True


def test_direction_conflict_detected(monkeypatch, tmp_path):
    today = "2026-11-09"
    db = _mk_db(tmp_path, [
        (today, "US.SHRT", "instruction", "DA-1", "SELL_SHORT", "2026-11-09T13:33:48Z"),
    ])
    mod = _load_module(monkeypatch, tmp_path, db)
    monkeypatch.setattr(mod, "_today_et", lambda: today)
    mod._ext_entry_risk_shadow("US.SHRT", 10, 100.0, "premarket", "LONG", "EXT-x", 95.0)
    row = json.loads((tmp_path / "shadow" / "ext_entry_risk_shadow.jsonl")
                     .read_text().strip().splitlines()[-1])
    assert row["da_available"] is True
    assert row["da_direction"] == "SHORT"
    assert row["direction_conflict"] is True


def test_same_direction_is_not_conflict(monkeypatch, tmp_path):
    today = "2026-11-09"
    db = _mk_db(tmp_path, [
        (today, "US.LONG", "instruction", "DA-2", "BUY", "2026-11-09T13:33:48Z"),
    ])
    mod = _load_module(monkeypatch, tmp_path, db)
    monkeypatch.setattr(mod, "_today_et", lambda: today)
    mod._ext_entry_risk_shadow("US.LONG", 10, 100.0, "premarket", "LONG", "EXT-x", 95.0)
    row = json.loads((tmp_path / "shadow" / "ext_entry_risk_shadow.jsonl")
                     .read_text().strip().splitlines()[-1])
    assert row["da_available"] is True
    assert row["direction_conflict"] is False


def test_no_da_card_means_no_conflict(monkeypatch, tmp_path):
    today = "2026-11-09"
    db = _mk_db(tmp_path, [])
    mod = _load_module(monkeypatch, tmp_path, db)
    monkeypatch.setattr(mod, "_today_et", lambda: today)
    mod._ext_entry_risk_shadow("US.NOD", 10, 100.0, "premarket", "LONG", "EXT-x", 95.0)
    row = json.loads((tmp_path / "shadow" / "ext_entry_risk_shadow.jsonl")
                     .read_text().strip().splitlines()[-1])
    assert row["da_available"] is False
    assert row["da_direction"] is None
    assert row["direction_conflict"] is False


def test_switch_off_writes_nothing(monkeypatch, tmp_path):
    db = _mk_db(tmp_path, [])
    mod = _load_module(monkeypatch, tmp_path, db)
    monkeypatch.setenv("EXT_ENTRY_RISK_SHADOW", "false")
    mod._ext_entry_risk_shadow("US.OFF", 10, 100.0, "premarket", "LONG", "EXT-x", 95.0)
    assert not (tmp_path / "shadow" / "ext_entry_risk_shadow.jsonl").exists()


def test_shadow_failure_never_raises(monkeypatch, tmp_path):
    """影子内部故障不得冒泡 —— 下单路径必须照常走。"""
    db = _mk_db(tmp_path, [])
    mod = _load_module(monkeypatch, tmp_path, db)

    def _boom(*_a, **_k):
        raise RuntimeError("shadow internals exploded")

    monkeypatch.setattr(mod, "_today_et", _boom)
    mod._ext_entry_risk_shadow("US.BOOM", 10, 100.0, "premarket", "LONG", "EXT-x", 95.0)
    # 没抛异常即通过
