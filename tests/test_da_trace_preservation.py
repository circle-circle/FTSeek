import json
import sqlite3
from types import SimpleNamespace

from app.playbook.db import PlaybookDB
from app.playbook.pipeline import _instruction_to_db_dict


def test_da_instruction_mapping_preserves_attribution():
    inst = SimpleNamespace(
        symbol="US.MU",
        direction="BUY",
        entry_price=1075.0,
        stop_loss=1015.0,
        take_profit=1220.0,
        position_size=4,
        risk_amount=240.0,
        risk_reward=2.41,
        confidence=70,
        conditions=["pe_below_industry"],
        thesis="test",
        sector="Technology",
        risks=[],
        data_quality={"technical": "ok"},
        decision_id="DA-20261002202120-US.MU-0c80",
    )
    regime = SimpleNamespace(regime=SimpleNamespace(value="RISK_ON"), vix=18.0,
                             spy_ma20=500.0, spy_ma50=490.0)

    result = _instruction_to_db_dict(inst, regime, "2026-10-02")

    assert result["raw_instruction"]["source"] == "deep_analysis"
    assert result["model_used"] is None


def test_same_da_reupsert_merges_raw_trace_and_keeps_model():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    decision_id = "DA-20261002202120-US.MU-0c80"
    base = {
        "date": "2026-10-02",
        "symbol": "US.MU",
        "direction": "BUY",
        "entry_price": 1075.0,
        "stop_loss": 1015.0,
        "take_profit": 1220.0,
        "confidence": 70,
        "decision_id": decision_id,
    }

    db.save_instruction({**base, "model_used": "qwen3.7-max", "raw_instruction": {
        "source": "deep_analysis",
        "score": 6.0,
        "debate_outcome": "2-model agreement",
        "planned_entry": 1075.0,
    }})
    db.save_instruction({**base, "model_used": None, "raw_instruction": {
        "source": "deep_analysis",
        "entry_price_source": "reprice",
        "admission": {"verdict": "PASS"},
    }})

    row = conn.execute(
        "SELECT model_used, raw_instruction_json FROM playbook_instructions WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    raw = json.loads(row["raw_instruction_json"])
    assert raw["source"] == "deep_analysis"
    assert raw["score"] == 6.0
    assert raw["debate_outcome"] == "2-model agreement"
    assert raw["planned_entry"] == 1075.0
    assert raw["entry_price_source"] == "reprice"
    assert raw["admission"]["verdict"] == "PASS"
    assert row["model_used"] == "qwen3.7-max"
