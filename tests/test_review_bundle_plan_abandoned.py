"""日报计划分母: 2 分钟门放弃必须计入 planned, 不能从执行率中消失。

10-19 事故形态: AVGO 被 `entry_confirm_reversed` 改成 SKIP 后, bundle_builder
旧查询只看 BUY/SELL_SHORT ⇒ 计划分母 7、执行率 100%; 正确口径是 7/8=87.5%。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.review.bundle_builder import build_plan_vs_actual  # noqa: E402


def _connection():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE playbook_instructions ("
        "id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, direction TEXT, skip_reason TEXT, "
        "entry_price REAL, raw_instruction_json TEXT, decision_id TEXT)"
    )
    con.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, trade_date TEXT, symbol TEXT, direction TEXT, "
        "source_type TEXT, exit_reason TEXT, decision_id TEXT, metadata_json TEXT)"
    )
    con.executemany(
        "INSERT INTO playbook_instructions (id, date, symbol, direction, skip_reason, "
        "entry_price, raw_instruction_json, decision_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-10-19", "US.EXEC", "BUY", None, 100.0,
             '{"entry_archetype": "PULLBACK_LIMIT", "confirmation": {"price_level": 102.0}}', "DA-EXEC"),
            (2, "2026-10-19", "US.ABANDONED", "SKIP", "entry_confirm_reversed", 100.0, "{}", "DA-ABANDONED"),
            (3, "2026-10-19", "US.OTHER_SKIP", "SKIP", "other_gate", 100.0, None, "DA-OTHER"),
        ],
    )
    con.executemany(
        "INSERT INTO trade_records (id, trade_date, symbol, direction, source_type, exit_reason, decision_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "2026-10-19", "US.EXEC", "LONG", "PLAYBOOK_SIM", "SL_BREAKEVEN", "DA-EXEC"),
            (2, "2026-10-19", "US.UNPLANNED", "LONG", "PLAYBOOK_SIM", "SL_BREAKEVEN", "EXT-UNPLANNED"),
        ],
    )
    return con


def test_abandoned_instruction_enters_planned_denominator():
    out = build_plan_vs_actual(_connection(), "2026-10-19")
    assert out["planned_count"].value == 2
    assert out["executed_count"].value == 1
    assert out["abandoned_count"].value == 1
    assert out["abandoned_symbols"].value == ["US.ABANDONED"]
    assert out["plan_gap_count"].value == 1
    assert out["plan_gap_symbols"].value == ["US.EXEC"]
    assert out["missed_count"].value == 0
    assert out["unplanned_count"].value == 1
    assert out["unplanned_symbols"].value == ["US.UNPLANNED"]
    assert out["execution_rate"].value == 0.5
    assert out["direction_mismatch"].value == []
