"""Contracts for the execution-time portfolio guardrail."""

from __future__ import annotations

import json
import sqlite3

from app.playbook.db import PlaybookDB
from app.playbook.playbook_service import PlaybookService
from app.playbook.portfolio_execution_guardrail import evaluate_portfolio_guardrail


def _row(row_id, symbol, direction, confidence, sector=""):
    return {
        "id": row_id,
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "sector": sector,
        "skip_reason": "",
    }


def test_direction_guardrail_selects_global_low_confidence_rows():
    rows = [
        _row(1, "US.AAA", "BUY", 70),
        _row(2, "US.BBB", "BUY", 70),
        _row(3, "US.CCC", "BUY", 70),
        _row(4, "US.DDD", "BUY", 35),
        _row(5, "US.EEE", "BUY", 50),
        _row(6, "US.FFF", "SELL_SHORT", 70),
    ]

    low = evaluate_portfolio_guardrail(rows, 4)
    high = evaluate_portfolio_guardrail(rows, 3)

    assert low["should_skip"] is True
    assert "组合方向偏斜" in low["reason"]
    assert low["direction_counts"] == {"BUY": 5, "SELL_SHORT": 1}
    assert 4 in low["skip_ids"]
    assert high["should_skip"] is False


def test_sector_guardrail_uses_balanced_direction_portfolio():
    rows = [
        _row(1, "US.AAA", "BUY", 70, "Technology"),
        _row(2, "US.BBB", "SELL_SHORT", 70, "Technology"),
        _row(3, "US.CCC", "BUY", 35, "Technology"),
    ]

    result = evaluate_portfolio_guardrail(rows, 3)

    assert result["should_skip"] is True
    assert "组合行业集中度" in result["reason"]
    assert result["direction_counts"] == {"BUY": 2, "SELL_SHORT": 1}


def test_terminal_skip_rows_leave_denominator():
    rows = [
        _row(1, "US.AAA", "BUY", 70),
        _row(2, "US.BBB", "BUY", 70),
        {
            **_row(3, "US.CCC", "BUY", 35),
            "skip_reason": "thesis_drift_invalidate",
        },
    ]

    result = evaluate_portfolio_guardrail(rows, 3)

    assert result["should_skip"] is False
    assert result["active_count"] == 2


def _qty_above_floor(entry_price: float) -> int:
    """按**当下** admission 的 min_notional_usd 算足额股数 (夹具必须跟着活配置走)。

    2026-11-03: 该键 2026-10-31 由 2500 抬到 5500 (用户裁「P3上」) ⇒ 旧夹具
    (100 股 × $50 = $5,000 / 50 股 × $100 = $5,000) 在**落库时**就被 admission 闸
    改成 SKIP (skip_reason=NOTIONAL_TOO_SMALL) ⇒ `get_recent_instructions` 的
    `WHERE direction != 'SKIP'` 查不到 ⇒ `execute_instruction` 恒返回「指令不存在」
    ⇒ 守卫**静默失效**(实测 5 处红, 同一根因)。
    夹具写死股数 ⇒ 每次调阈值都会把守卫打红 ⇒ 改为从活配置派生。
    """
    from app.playbook.admission import get_admission_config
    floor = float(get_admission_config().get("min_notional_usd") or 0)
    return int(floor / float(entry_price)) + 2


def test_execute_instruction_blocks_low_confidence_direct_write(monkeypatch, tmp_path):
    """[DEB-20261008 F1] 摘闸后默认为关 — 本用例显式打开开关, 钉住「开关打开时
    行为不变」; 关闸零拦截 + 影子照写见 test_gate_off_allows_but_shadows。"""
    monkeypatch.setattr(
        "app.playbook.pipeline.portfolio_guardrail_enabled", lambda: True
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    service = PlaybookService(playbook_db=db)
    audit_path = tmp_path / "portfolio_execution_guardrail.jsonl"
    monkeypatch.setattr(
        "app.playbook.playbook_service._PORTFOLIO_GUARDRAIL_AUDIT_PATH", audit_path
    )

    high_id = db.save_instruction({
        "date": "2026-10-08",
        "symbol": "US.AAA",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": _qty_above_floor(100.0),
        "confidence": 70,
        "conditions": [],
    })
    low_id = db.save_instruction({
        "date": "2026-10-08",
        "symbol": "US.BBB",
        "direction": "BUY",
        "entry_price": 50.0,
        "stop_loss": 47.0,
        "take_profit": 55.0,
        "position_size": _qty_above_floor(50.0),
        "confidence": 35,
        "conditions": [],
    })
    mid_id = db.save_instruction({
        "date": "2026-10-08",
        "symbol": "US.CCC",
        "direction": "BUY",
        "entry_price": 75.0,
        "stop_loss": 71.0,
        "take_profit": 82.0,
        "position_size": _qty_above_floor(75.0),
        "confidence": 50,
        "conditions": [],
    })

    result = service.execute_instruction(low_id)

    assert result["success"] is False
    assert result["skipped_portfolio_guardrail"] is True
    assert "组合方向偏斜" in result["message"]
    row = next(row for row in db.get_instructions_by_date("2026-10-08") if row["id"] == low_id)
    assert row["direction"] == "SKIP"
    assert "组合方向偏斜" in row["skip_reason"]

    second_result = service.execute_instruction(mid_id)
    assert second_result["success"] is False
    assert second_result["skipped_portfolio_guardrail"] is True
    second_row = next(
        row for row in db.get_instructions_by_date("2026-10-08")
        if row["id"] == mid_id
    )
    assert second_row["direction"] == "SKIP"
    assert "组合方向偏斜" in second_row["skip_reason"]
    audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["instruction_id"] == mid_id
    assert audit["decision"] == "SKIP"
    assert high_id not in audit["skip_ids"]

    conn.close()


def test_guardrail_db_failure_is_fail_close():
    class FailingDB:
        def ensure_tables(self):
            return None

        def get_instructions_by_date(self, date):
            raise RuntimeError("database unavailable")

    service = PlaybookService(playbook_db=FailingDB())
    allowed, reason, result = service._check_execution_portfolio_guardrail(
        {"date": "2026-10-08", "symbol": "US.AAA", "direction": "BUY"}, 1
    )

    assert allowed is False
    assert "PORTFOLIO_GUARDRAIL_FAIL_CLOSE" in reason
    assert result["status"] == "error"


def test_guardrail_skip_does_not_overwrite_booked_row():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    instruction_id = db.save_instruction({
        "date": "2026-10-08",
        "symbol": "US.AAA",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": _qty_above_floor(100.0),
        "confidence": 35,
        "conditions": [],
    })
    db.update_instruction_tracking(instruction_id, {"executed": 1})

    marked = db.mark_instruction_guardrail_skip(
        instruction_id, "组合方向偏斜 guardrail 拒绝执行"
    )

    assert marked is False
    row = db.get_instruction("2026-10-08", "US.AAA")
    assert row["direction"] == "BUY"
    assert row["skip_reason"] == ""
    conn.close()


def test_gate_off_allows_but_shadows(monkeypatch, tmp_path):
    """[DEB-20261008 F1, 2026-10-08 用户裁决摘闸] 开关关闭 (默认): 全域 100% BUY
    也不再拦截 (0 空头日断崖消除), 但评估+审计照跑 — would-skip 落影子
    (enforced=false) 供「闸拦对没拦对」回测 (F4 配套)。"""
    monkeypatch.setattr(
        "app.playbook.pipeline.portfolio_guardrail_enabled", lambda: False
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    service = PlaybookService(playbook_db=db)
    audit_path = tmp_path / "portfolio_execution_guardrail.jsonl"
    monkeypatch.setattr(
        "app.playbook.playbook_service._PORTFOLIO_GUARDRAIL_AUDIT_PATH", audit_path
    )

    low_id = db.save_instruction({
        "date": "2026-10-08",
        "symbol": "US.BBB",
        "direction": "BUY",
        "entry_price": 50.0,
        "stop_loss": 47.0,
        "take_profit": 55.0,
        "position_size": _qty_above_floor(50.0),
        "confidence": 35,
        "conditions": [],
    })
    db.save_instruction({
        "date": "2026-10-08",
        "symbol": "US.AAA",
        "direction": "BUY",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 110.0,
        "position_size": _qty_above_floor(100.0),
        "confidence": 70,
        "conditions": [],
    })

    allowed, reason, result = service._check_execution_portfolio_guardrail(
        {"date": "2026-10-08", "symbol": "US.BBB", "direction": "BUY"}, low_id
    )

    # 摘闸: would-skip 但不拦截, 行不被改 SKIP
    assert allowed is True
    assert reason == ""
    assert result["shadow_would_skip"] is True
    row = next(r for r in db.get_instructions_by_date("2026-10-08") if r["id"] == low_id)
    assert row["direction"] == "BUY"
    # 影子照写: 审计行含 would-skip 判定 + enforced=false
    audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["instruction_id"] == low_id
    assert audit["decision"] == "SKIP"
    assert audit["enforced"] is False
    assert "组合方向偏斜" in audit["reason"]
    conn.close()
