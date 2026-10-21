"""RFC-155 前瞻读数契约: 只收成功行, join 实际交易, 复用美元口径。"""
from __future__ import annotations

import json
import sqlite3

import scripts.risk_size_tier_forward_eval as ev


def _write_forward(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _forward_row(**overrides):
    row = {
        "schema": "risk_size_tier_forward_v1", "success": True, "executed": True,
        "decision_id": "d1", "inst_id": 1, "symbol": "US.AAPL",
        "date": "2026-10-01", "source": "DA", "atr_pct": 6.0, "session_min": 5,
        "actual_size": 100.0, "actual_notional": 10000.0,
    }
    row.update(overrides)
    return row


def _db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trade_records (decision_id TEXT, trade_date TEXT, "
                "pnl_net REAL, exit_reason TEXT, exit_time TEXT)")
    con.execute("INSERT INTO trade_records VALUES ('d1','2026-10-01',-100.0,"
                "'SL_INITIAL','2026-10-01T10:00:00-04:00')")
    con.commit()
    con.close()


def test_forward_eval_joins_and_scales(tmp_path, monkeypatch):
    forward = tmp_path / "fwd.jsonl"
    _write_forward(forward, [
        _forward_row(),
        _forward_row(decision_id="fail", success=False),  # 不应计入
        _forward_row(),                                   # 重复 d1, 去重
    ])
    rows = ev.load_forward(forward)
    assert len(rows) == 1 and rows[0]["decision_id"] == "d1"
    db = tmp_path / "t.db"
    _db(db)
    outcomes = ev.load_trade_outcomes(db, {r["decision_id"] for r in rows})
    report = ev.compute(rows, outcomes)
    assert report["coverage"]["joined"] == 1
    assert report["sample"]["n"] == 1
    atr5 = report["rules"]["atr5"]
    assert atr5["actual_net"] == -100.0
    assert atr5["cf_net"] == -50.0
    assert atr5["delta_net"] == 50.0
    assert atr5["left_tail_actual_net"] == -100.0
    assert report["gate"]["pass"] is False   # n=1 < 30


def test_forward_eval_skips_missing_trade(tmp_path):
    forward = tmp_path / "fwd.jsonl"
    _write_forward(forward, [_forward_row(decision_id="missing")])
    rows = ev.load_forward(forward)
    db = tmp_path / "t.db"
    _db(db)
    outcomes = ev.load_trade_outcomes(db, {"missing"})
    report = ev.compute(rows, outcomes)
    assert report["coverage"]["joined"] == 0
    assert report["coverage"]["skipped_no_trade"] == 1
    assert report["sample"]["n"] == 0


def test_sample_gate_uses_affected_not_total(tmp_path):
    """RFC-155 §3.2 原文 = ≥10 ET 日 ∧ ≥30 笔**受影响**交易。

    30 笔一个旗标都没触发的单 (ATR<5% / 非开盘 15min / 非 UE) ⇒ 一笔记账都不会被缩放,
    不得过样本门。旧实现拿 sample.n (全部 joined) 当门 ⇒ 30 笔无旗标单会假 PASS。
    """
    forward = tmp_path / "fwd.jsonl"
    n = 30
    _write_forward(forward, [
        _forward_row(decision_id=f"z{i}", atr_pct=1.0, session_min=60, source="DA")
        for i in range(n)])
    rows = ev.load_forward(forward)
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trade_records (decision_id TEXT, trade_date TEXT, "
                "pnl_net REAL, exit_reason TEXT, exit_time TEXT)")
    for i in range(n):
        con.execute("INSERT INTO trade_records VALUES (?,?,?,?,?)",
                    (f"z{i}", "2026-10-01", 10.0, "SL_BREAKEVEN",
                     "2026-10-01T10:00:00-04:00"))
    con.commit()
    con.close()
    outcomes = ev.load_trade_outcomes(db, {r["decision_id"] for r in rows})
    report = ev.compute(rows, outcomes)
    assert report["sample"]["n"] == n                 # 总数只印
    assert report["gate"]["sample_affected_n"] == 0   # 受影响 = 0 ⇒ 判据不成立
    assert report["gate"]["sample_total_n_only_printed"] == n
    assert report["gate"]["pass"] is False
