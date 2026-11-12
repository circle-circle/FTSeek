"""RFC-155 前瞻读数契约: 只收成功行, join 实际交易, 复用美元口径。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

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


def _r2_row(day, pnl):
    return {
        "id": 1, "symbol": "US.AAPL", "trade_date": day, "source": "DA",
        "atr_pct": 1.0, "session_min": 5, "pnl": float(pnl), "notional": 10000.0,
        "left_tail": pnl < 0,
    }


def _r2_days(n=10):
    start = date(2026, 11, 11)
    return [(start + timedelta(days=offset)).isoformat() for offset in range(n)]


def test_r2_confirmation_excludes_history_and_requires_all_frozen_gates():
    rows = [_r2_row(day, -10.0) for day in _r2_days() for _ in range(3)]
    rows.extend(_r2_row("2026-11-10", -10000.0) for _ in range(20))
    report = ev.compute_r2_open15_confirmation(rows, resolution_coverage=1.0)
    assert report["affected_n"] == 30
    assert report["sample"]["distinct_days"] == 10
    assert report["actual_net"] == -300.0
    assert report["cf_net"] == -150.0
    assert report["delta_net"] == 150.0
    assert report["delta_excluding_best_day"] == 135.0
    assert report["day_cluster"]["ci80"] == [15.0, 15.0]
    assert report["day_cluster"]["mde80"] == 0.0
    assert all(report["criteria"].values())
    assert report["verdict"] == "R2_PROMOTE_CANDIDATE_REQUIRES_RFC_AND_USER_APPROVAL"
    assert report["automatic_trading_change"] is False


def test_r2_confirmation_rejects_single_best_day_dependency():
    rows = []
    for day in _r2_days()[:9]:
        rows.extend([_r2_row(day, 20.0), _r2_row(day, -10.0), _r2_row(day, 0.0)])
    rows.extend(_r2_row(_r2_days()[9], -100.0) for _ in range(3))
    report = ev.compute_r2_open15_confirmation(rows, resolution_coverage=1.0)
    assert report["affected_n"] == 30
    assert report["delta_net"] == 105.0
    assert report["delta_excluding_best_day"] == -45.0
    assert report["criteria"]["excluding_best_day>0"] is False
    assert report["verdict"] == "NO_DEPLOY"


def test_r2_confirmation_stays_too_early_before_affected_sample_gate():
    rows = [_r2_row(day, -10.0) for day in _r2_days(9) for _ in range(3)]
    rows.extend(_r2_row(_r2_days()[9], -10.0) for _ in range(2))
    report = ev.compute_r2_open15_confirmation(rows, resolution_coverage=1.0)
    assert report["affected_n"] == 29
    assert report["sample"]["distinct_days"] == 10
    assert report["criteria"]["sample_affected_n>=30"] is False
    assert report["verdict"] == "TOO_EARLY"


def test_r2_confirmation_rejects_unresolved_survivor_bias():
    rows = [_r2_row(day, -10.0) for day in _r2_days() for _ in range(3)]
    report = ev.compute_r2_open15_confirmation(rows, resolution_coverage=0.94)
    assert report["criteria"]["resolution_coverage>=0.95"] is False
    assert report["verdict"] == "NO_DEPLOY"


def test_r2_resolution_coverage_counts_pending_and_excludes_old_rows():
    forward = [
        _forward_row(decision_id="new1", date="2026-11-11"),
        _forward_row(decision_id="new2", date="2026-11-11"),
        _forward_row(decision_id="pending", date="2026-11-12"),
        _forward_row(decision_id="old", date="2026-11-10"),
        _forward_row(decision_id="ext", date="2026-11-11", source="EXT"),
    ]
    outcomes = {
        "new1": {"trade_date": "2026-11-11", "pnl": 1.0},
        "new2": {"trade_date": "2026-11-11", "pnl": 1.0},
        "old": {"trade_date": "2026-11-10", "pnl": -100.0},
    }
    report = ev._r2_resolution_coverage(forward, outcomes)
    assert report["eligible_n"] == 3
    assert report["resolved_n"] == 2
    assert report["pending_n"] == 1
    assert report["pending_decision_ids"] == ["pending"]
    assert report["rate"] == 0.6667


def test_r2_confirmation_excludes_non_da_sources():
    rows = [_r2_row(day, -10.0) for day in _r2_days() for _ in range(3)]
    ext_rows = [dict(_r2_row(day, -1000.0), source="EXT") for day in _r2_days()]
    report = ev.compute_r2_open15_confirmation(rows + ext_rows, resolution_coverage=1.0)
    assert report["source"] == "DA"
    assert report["sample"]["n"] == 30
    assert report["actual_net"] == -300.0
