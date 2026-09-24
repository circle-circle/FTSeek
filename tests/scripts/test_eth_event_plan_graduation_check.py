"""scripts/eth_event_plan_graduation_check.py hermetic 测试 (9/24 增强批).

覆盖:
1. 全过 → GRADUATE
2. 覆盖缺 → NOT_READY + gaps
3. 条数超带 → 如实 ❌ + 附注 (诚实纪律)
4. 空台账 → NOT_READY + reason
5. 交易日不足 → NOT_READY + gap 天数
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location(
        "graduation_check", ROOT / "scripts" / "eth_event_plan_graduation_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def grad_mod():
    return _load()


def _write_ledger(tmp_path, records: list[dict], monkeypatch, grad_mod):
    """写 fixture ledger 并 monkeypatch 路径."""
    plan_dir = tmp_path / "ext_entry_plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    ledger = plan_dir / "reconcile_ledger.jsonl"
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    ledger.write_text("\n".join(lines) + "\n" if lines else "")
    monkeypatch.setattr(grad_mod, "PLAN_DIR", plan_dir)
    monkeypatch.setattr(grad_mod, "LEDGER_PATH", ledger)
    monkeypatch.setattr(grad_mod, "OUTPUT_PATH", plan_dir / "graduation_check_latest.json")


def _make_record(et_date: str, plan_rows: int = 8, missed_count: int = 0,
                 coverage_pct: float = 100.0) -> dict:
    return {
        "et_date": et_date,
        "session": "premarket",
        "verdict": "PASS" if missed_count == 0 else "FAIL",
        "coverage_pct": coverage_pct,
        "plan_rows": plan_rows,
        "event_tickets": plan_rows,
        "missed_count": missed_count,
        "reconciled_at": f"{et_date}T21:00:00",
    }


# ── 1. 全过 ──

class TestAllPass:
    """全部判据满足 → GRADUATE + 提请阶段 1 立项."""

    def test_graduate(self, grad_mod, tmp_path, monkeypatch):
        records = [
            _make_record(f"2026-09-{17 + i:02d}", plan_rows=8)
            for i in range(7)  # 7 个交易日
        ]
        _write_ledger(tmp_path, records, monkeypatch, grad_mod)

        report = grad_mod.run_check()
        assert report["verdict"] == "GRADUATE"
        assert "recommendation" in report
        assert "阶段 1" in report["recommendation"]


# ── 2. 覆盖缺 ──

class TestCoverageGap:
    """有漏票日 → NOT_READY + gaps."""

    def test_coverage_fail(self, grad_mod, tmp_path, monkeypatch):
        records = [
            _make_record("2026-09-17", plan_rows=8, missed_count=0),
            _make_record("2026-09-18", plan_rows=8, missed_count=2, coverage_pct=75.0),
            _make_record("2026-09-19", plan_rows=8, missed_count=0),
            _make_record("2026-09-22", plan_rows=8, missed_count=0),
            _make_record("2026-09-23", plan_rows=8, missed_count=0),
            _make_record("2026-09-24", plan_rows=8, missed_count=0),
            _make_record("2026-09-25", plan_rows=8, missed_count=0),
        ]
        _write_ledger(tmp_path, records, monkeypatch, grad_mod)

        report = grad_mod.run_check()
        assert report["verdict"] == "NOT_READY"
        assert not report["criteria"]["coverage_100pct"]["pass"]
        assert "2026-09-18" in report["criteria"]["coverage_100pct"]["fail_dates"]


# ── 3. 条数带 (2026-09-24 用户裁修订: 7-9 → [0,44]) ──

class TestPlanRowsBand:
    """修订带 [0,44]: 16 行入带; 超 44 如实 ❌ (防计划爆炸退化盲扫)."""

    def test_16_rows_in_revised_band(self, grad_mod, tmp_path, monkeypatch):
        records = [
            _make_record(f"2026-09-{17 + i:02d}", plan_rows=16)
            for i in range(7)  # 多源并集实测常态 16 行 — 修订后入带
        ]
        _write_ledger(tmp_path, records, monkeypatch, grad_mod)

        report = grad_mod.run_check()
        band = report["criteria"]["plan_rows_band"]
        assert band["pass"], "16 行应在修订带 [0,44] 内 (原 7-9 带系单源起草估算, 9/24 用户裁修订)"
        assert report["verdict"] == "GRADUATE"

    def test_over_44_band_honest(self, grad_mod, tmp_path, monkeypatch):
        records = [
            _make_record(f"2026-09-{17 + i:02d}", plan_rows=50)
            for i in range(7)  # 超防爆上界 44 (盲扫基线)
        ]
        _write_ledger(tmp_path, records, monkeypatch, grad_mod)

        report = grad_mod.run_check()
        assert report["verdict"] == "NOT_READY"
        band = report["criteria"]["plan_rows_band"]
        assert not band["pass"]
        assert "盲扫" in band["note"]  # 附注点明退化风险
        assert 50 in band["actual"]


# ── 4. 空台账 ──

class TestEmptyLedger:
    """空台账 → NOT_READY + reason."""

    def test_empty_ledger(self, grad_mod, tmp_path, monkeypatch):
        _write_ledger(tmp_path, [], monkeypatch, grad_mod)

        report = grad_mod.run_check()
        assert report["verdict"] == "NOT_READY"
        assert "reason" in report


# ── 5. 交易日不足 ──

class TestTradingDaysInsufficient:
    """交易日 < 7 → NOT_READY + gap 天数."""

    def test_insufficient_days(self, grad_mod, tmp_path, monkeypatch):
        records = [
            _make_record(f"2026-09-{17 + i:02d}", plan_rows=8)
            for i in range(3)  # 只 3 个交易日
        ]
        _write_ledger(tmp_path, records, monkeypatch, grad_mod)

        report = grad_mod.run_check()
        assert report["verdict"] == "NOT_READY"
        td = report["criteria"]["trading_days"]
        assert not td["pass"]
        assert td["gap"] == 4  # 7 - 3 = 4


# ── 6. 终端报告 + JSON 工件 ──

class TestOutput:
    """终端报告 + JSON 工件."""

    def test_json_output(self, grad_mod, tmp_path, monkeypatch):
        records = [_make_record("2026-09-24", plan_rows=8)]
        _write_ledger(tmp_path, records, monkeypatch, grad_mod)

        report = grad_mod.run_check()
        assert "checked_at" in report
        assert "criteria" in report
        assert "verdict" in report

    def test_print_report_no_crash(self, grad_mod, tmp_path, monkeypatch, capsys):
        """_print_report 不崩 (GRADUATE 路)."""
        records = [
            _make_record(f"2026-09-{17 + i:02d}", plan_rows=8)
            for i in range(7)
        ]
        _write_ledger(tmp_path, records, monkeypatch, grad_mod)

        report = grad_mod.run_check()
        grad_mod._print_report(report)
        captured = capsys.readouterr()
        assert "GRADUATE" in captured.out or "阶段 1" in captured.out

    def test_print_report_not_ready(self, grad_mod, tmp_path, monkeypatch, capsys):
        """_print_report 不崩 (NOT_READY 路)."""
        _write_ledger(tmp_path, [], monkeypatch, grad_mod)
        report = grad_mod.run_check()
        grad_mod._print_report(report)
        captured = capsys.readouterr()
        assert "未就绪" in captured.out or "NOT_READY" in captured.out
