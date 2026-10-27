"""[DEB-20261027 N4-C] 覆盖仲裁留痕契约测试 (record-only)。

守两件事:
1) 被仲裁拒绝的写入必须在 data/shadow/instruction_arbitration.jsonl 留一行 —
   否则「生成轮判 SKIP / 实际被 bridge 行覆盖」这个装饰门现象永远不可见;
2) 留痕不得改变任何返回值/仲裁结果 — 拒绝仍返回现存行 id, 正常写入不产生留痕。
"""
import json
import re
import sqlite3
from pathlib import Path

import pytest

from app.playbook.db import PlaybookDB

REPO = Path(__file__).resolve().parents[1]
DB_SRC = (REPO / "app" / "playbook" / "db.py").read_text(encoding="utf-8")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    inst = PlaybookDB(conn=conn)
    monkeypatch.setattr(PlaybookDB, "_ARBITRATION_SHADOW",
                        str(tmp_path / "instruction_arbitration.jsonl"))
    inst.ensure_tables()
    return inst


def _row(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


BASE = {
    "date": "2026-10-27", "symbol": "US.TEST", "direction": "BUY",
    "entry_price": 500.0, "stop_loss": 475.0, "take_profit": 550.0,
    "confidence": 35, "position_size": 10, "strategy_id": "s1",
    "conditions": [], "risks": [], "reason": "t",
}


def test_empty_did_skip_against_da_row_is_logged_and_kept(db, tmp_path):
    da = dict(BASE, decision_id="DA-20261027120000-US.TEST-abc")
    kept_id = db.save_instruction(da)
    # 生成轮的 SKIP 票: 空 decision_id, 低置信
    skip = dict(BASE, decision_id="", direction="SKIP", skip_reason="low_confidence")
    returned = db.save_instruction(skip)

    assert returned == kept_id, "仲裁必须原样返回现存行 id (行为不变)"
    recs = _row(tmp_path / "instruction_arbitration.jsonl")
    assert len(recs) == 1, recs
    rec = recs[0]
    assert rec["reason"] == "empty_did_vs_sourced"
    assert rec["symbol"] == "US.TEST" and rec["date"] == "2026-10-27"
    assert rec["attempted"]["did"] in ("", None)
    assert rec["attempted"]["direction"] == "SKIP"
    assert str(rec["kept"]["did"]).startswith("DA-")
    assert rec["kept"]["direction"] == "BUY"


def test_plain_insert_writes_no_arbitration_row(db, tmp_path):
    db.save_instruction(dict(BASE, decision_id="DA-20261027120000-US.TEST-abc"))
    assert _row(tmp_path / "instruction_arbitration.jsonl") == []


def test_all_refusal_sites_are_instrumented():
    """源码契约: 四个拒绝点必须都调用留痕 (漏一个 = 该路径的装饰门不可见)。"""
    assert DB_SRC.count("self._emit_arbitration(") >= 4
    for reason in ("ue_vs_da", "lower_priority", "stale_did",
                   "empty_did_vs_sourced"):
        assert f'"{reason}"' in DB_SRC, reason
    # 留痕不得出现在 return 之后 (必须 before-return 落行)
    for m in re.finditer(r"self\._emit_arbitration\([^)]*\)\s*\n\s*return", DB_SRC):
        assert m, "留痕顺序异常"