"""P3 恢复补录：指令 SL/TP 真源与 synthetic 显式降级契约。"""
import sqlite3

import pytest

from app.core.position_sync import _instruction_stop_truth


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "truth.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE playbook_instructions (id INTEGER PRIMARY KEY, stop_loss REAL, take_profit REAL)")
    conn.execute("INSERT INTO playbook_instructions VALUES (5666,669.47,757.56)")
    conn.execute("INSERT INTO playbook_instructions VALUES (5667,0,123.0)")
    conn.commit()
    conn.close()
    return str(path)


def test_instruction_truth_wins_and_tp_zero_is_legal(db):
    sl, tp, source = _instruction_stop_truth(db, 5666, 690.90, 972.64)
    assert sl == pytest.approx(669.47)
    assert tp == pytest.approx(757.56)
    assert source == "instruction"


def test_missing_instruction_id_uses_synthetic(db):
    sl, tp, source = _instruction_stop_truth(db, None, 690.90, 972.64)
    assert (sl, tp, source) == (690.90, 972.64, "synthetic")


def test_missing_or_invalid_sl_keeps_synthetic(db):
    assert _instruction_stop_truth(db, 9999, 690.90, 972.64) == (690.90, 972.64, "synthetic")
    assert _instruction_stop_truth(db, 5667, 690.90, 972.64) == (690.90, 972.64, "synthetic")


def test_query_failure_is_explicit_synthetic(tmp_path):
    missing = tmp_path / "missing.db"
    sl, tp, source = _instruction_stop_truth(str(missing), 5666, 690.90, 972.64)
    assert (sl, tp, source) == (690.90, 972.64, "synthetic")
