"""DEB-20260816-fullchain 批次E 契约测试: #20 (BUY配额排除already_held幻影) /
#22 (生成幂等=完成标记非任意行) / #27 (decision_conf pipeline 写入)."""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from app.playbook.db import PlaybookDB  # noqa: E402


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


def _seed_executed(db, symbol, direction, outcome):
    db.save_instruction({
        "date": "2026-08-17", "symbol": symbol, "direction": direction,
        "entry_price": 100.0, "decision_id": f"DEC-x-{symbol}",
    })
    conn = db._conn()
    conn.execute(
        "UPDATE playbook_instructions SET executed=1, outcome=? WHERE symbol=?",
        (outcome, symbol),
    )
    conn.commit()


# ── #20: BUY 日配额排除 already_held 幻影行 ──────────────────────────────

def test_20_already_held_phantom_not_counted(db):
    _seed_executed(db, "US.AAA", "BUY", "already_held")  # 幻影: broker已有持仓, 无新单
    _seed_executed(db, "US.BBB", "BUY", "win")            # 真实成交
    _seed_executed(db, "US.CCC", "BUY", None)             # 真实成交 (outcome未回填)
    assert db.count_executed_by_direction("2026-08-17", "BUY") == 2


def test_20_real_holding_still_counted(db):
    _seed_executed(db, "US.DDD", "BUY", "holding")
    assert db.count_executed_by_direction("2026-08-17", "BUY") == 1


# ── #22: 生成完成标记 (与"是否有行"解耦) ─────────────────────────────────

def test_22_marker_roundtrip(db):
    assert db.is_generate_completed("2026-08-17") is False
    db.mark_generate_completed("2026-08-17")
    assert db.is_generate_completed("2026-08-17") is True
    assert db.is_generate_completed("2026-08-18") is False  # 日级隔离


def test_22_marker_table_created_by_ensure_tables():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "playbook_generate_state" in tables


def test_22_gate_uses_completion_marker_not_row_existence():
    src = (PROJECT_ROOT / "app" / "playbook" / "pipeline_parts" / "generation_flow.py").read_text(
        encoding="utf-8"
    )
    assert "is_generate_completed(today)" in src, "幂等门须查完成标记 (#22 回归)"
    assert "mark_generate_done(today)" in src
    # 异常路径不写标记: 完成/空表终态共 3 处调用, 不含 except 分支
    assert src.count("mark_generate_done(today)") == 3


def test_22_rows_without_marker_do_not_block():
    # 行存在但无标记 → is_generate_completed=False (生成可继续, UE 先写不再锁死)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    pdb.save_instruction({"date": "2026-08-17", "symbol": "US.UE1",
                          "direction": "BUY", "entry_price": 50.0,
                          "decision_id": "universe_expansion:2026-08-17:UE1"})
    assert pdb.is_generate_completed("2026-08-17") is False


# ── #27: decision_conf pipeline 写入 (与 DA 同口径 confidence/100) ───────

def _mk_inst(confidence):
    return SimpleNamespace(
        symbol="US.XOM", direction="BUY", entry_price=100.0, stop_loss=95.0,
        take_profit=110.0, position_size=10, confidence=confidence,
        conditions=[], risks=[], data_quality={}, thesis="t", sector="s",
        entry_price_source="", atr_source="", da_thesis="", decision_id="DEC-t",
        invalidation="", anchor_dev_pct=None, model_used=None, horizon="short",
        horizon_confidence=0.0, horizon_reasoning={}, skip_reason="",
        risk_amount=0.0, risk_reward=0.0,
    )


def _mk_regime():
    return SimpleNamespace(regime=SimpleNamespace(value="RISK_ON"), vix=15.0,
                           spy_ma20=500.0, spy_ma50=490.0)


def test_27_decision_conf_written_normalized():
    from app.playbook.pipeline import _instruction_to_db_dict
    d = _instruction_to_db_dict(_mk_inst(70), _mk_regime(), "2026-08-17")
    assert d["decision_conf"] == pytest.approx(0.7)


def test_27_decision_conf_none_when_no_confidence():
    from app.playbook.pipeline import _instruction_to_db_dict
    d = _instruction_to_db_dict(_mk_inst(0), _mk_regime(), "2026-08-17")
    assert d["decision_conf"] is None


def test_27_roundtrip_persists():
    from app.playbook.pipeline import _instruction_to_db_dict
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    pdb.save_instruction(_instruction_to_db_dict(_mk_inst(65), _mk_regime(), "2026-08-17"))
    row = pdb.get_instruction("2026-08-17", "US.XOM")
    assert row["decision_conf"] == pytest.approx(0.65)
