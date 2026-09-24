"""ETH 影子⑤ eth_relative_volume_gate_shadow hermetic 测试.

覆盖:
1. 自门控 dormant (PASS 日数 < 7) / active (≥ 7) 两路
2. 判据计算 (relative_ratio, would_pass_relative)
3. 幂等重跑 (同一 key 不重复追加)
4. median 不足时 ratio=null + basis="accumulating"
5. cf_net_usd 不可构造时 null + basis
6. 死线到期停采
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# 延迟加载, 避免 import-time 副作用
def _load_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "eth_relative_volume_gate_shadow",
        ROOT / "scripts" / "eth_relative_volume_gate_shadow.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_mod()


@pytest.fixture
def tmp_env(tmp_path):
    """创建临时数据目录结构。"""
    shadow_dir = tmp_path / "data" / "shadow"
    shadow_dir.mkdir(parents=True)
    plan_dir = tmp_path / "data" / "loops" / "ext_entry_plan"
    plan_dir.mkdir(parents=True)
    return tmp_path


def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, ensure_ascii=False)


def _make_reconcile(pass_days: int, base_date: str = "2026-09-20") -> list[dict]:
    """生成 reconcile_ledger 行 (PASS 日数)。"""
    from datetime import timedelta
    rows = []
    d = date.fromisoformat(base_date)
    for i in range(pass_days):
        day = (d + timedelta(days=i)).isoformat()
        rows.append({
            "et_date": day,
            "session": "premarket",
            "verdict": "PASS",
            "coverage_pct": 100.0,
            "plan_rows": 16,
            "event_tickets": 0,
            "missed_count": 0,
            "reconciled_at": f"{day}T05:08:10-04:00",
        })
    return rows


def _make_plan(symbols: list[str], et_date: str, session: str) -> list[dict]:
    return [
        {
            "symbol": s,
            "et_date": et_date,
            "session": session,
            "phase": "unknown",
            "sources": [{"name": "finnhub", "reportDate": et_date}],
            "gate_snapshot": {},
            "state": "ambiguous",
        }
        for s in symbols
    ]


def _make_rejects(ts: str, session: str, rejects: list[dict]) -> dict:
    return {"ts": ts, "session": session, "n": len(rejects), "rejects": rejects}


# ── 测试 1: 自门控 dormant ──

def test_dormant_when_insufficient_pass_days(mod, tmp_env):
    """reconcile PASS 日数 < 7 → dormant, 不采集。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(3))

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"), \
         patch.object(mod, "REJECTS_PATH", tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"), \
         patch.object(mod, "PLAN_DIR", tmp_env / "data" / "loops" / "ext_entry_plan"), \
         patch.object(mod, "EXECUTIONS_PATH", tmp_env / "data" / "extended_hours_entry_executions.jsonl"):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert result == []


def test_active_when_sufficient_pass_days(mod, tmp_env):
    """reconcile PASS 日数 ≥ 7 → active (但无匹配数据时返回空)。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [])

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["AAPL"], "2026-09-24", "premarket"))

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", tmp_env / "data" / "extended_hours_entry_executions.jsonl"):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert isinstance(result, list)


# ── 测试 2: 判据计算 ──

def test_ratio_computation_with_sufficient_history(mod, tmp_env):
    """有 20+ 日历史时计算 relative_ratio 和 would_pass_relative。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["TEST"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.TEST", "decision_price": 10.0, "reason": "turnover_too_low",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "market_cap": 2e9, "spread_pct": 2.0}}
        ])
    ])

    # 构造 20 日历史
    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
    history = []
    from datetime import timedelta
    base = date(2026, 9, 1)
    for i in range(20):
        d = (base + timedelta(days=i)).isoformat()
        history.append({
            "et_date": d,
            "symbol": "US.TEST",
            "session": "premarket",
            "round_ts": f"{d}T12:00:00Z",
            "turnover_approx": 500.0,  # 中位数 = 500
            "relative_ratio": None,
            "median_baseline": None,
        })
    _write_jsonl(out_path, history)

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 1
        rec = result[0]
        # turnover_approx = decision_price × ask_volume = 10.0 × 100.0 = 1000
        assert rec["turnover_approx"] == 1000.0
        # median = 500 (20 日都是 500)
        assert rec["median_baseline"] == 500.0
        # ratio = 1000 / 500 = 2.0
        assert rec["relative_ratio"] == 2.0
        # would_pass = 2.0 >= 1.5 → True
        assert rec["would_pass_relative"] is True


def test_would_pass_false_when_below_threshold(mod, tmp_env):
    """ratio < 1.5 → would_pass_relative = False。"""
    # 复用上一个测试的框架, 但 turnover_approx < 1.5 × median
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["TEST"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.TEST", "decision_price": 5.0, "reason": "turnover_too_low",
             "quote": {"bid": 4.9, "ask": 5.1, "ask_volume": 50.0, "market_cap": 2e9, "spread_pct": 4.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
    history = []
    from datetime import timedelta
    base = date(2026, 9, 1)
    for i in range(20):
        d = (base + timedelta(days=i)).isoformat()
        history.append({
            "et_date": d,
            "symbol": "US.TEST",
            "session": "premarket",
            "round_ts": f"{d}T12:00:00Z",
            "turnover_approx": 1000.0,  # 中位数 = 1000
        })
    _write_jsonl(out_path, history)

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 1
        rec = result[0]
        # turnover_approx = 5.0 × 50.0 = 250
        assert rec["turnover_approx"] == 250.0
        # ratio = 250 / 1000 = 0.25
        assert rec["relative_ratio"] == 0.25
        assert rec["would_pass_relative"] is False


# ── 测试 3: 幂等重跑 ──

def test_idempotent_rerun(mod, tmp_env):
    """同一 (et_date, symbol, session, round_ts) 不重复追加。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["IDM"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.IDM", "decision_price": 10.0, "reason": "turnover_too_low",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "market_cap": 2e9, "spread_pct": 2.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
    # 已有记录 (同 key)
    _write_jsonl(out_path, [{
        "et_date": "2026-09-24",
        "symbol": "US.IDM",
        "session": "premarket",
        "round_ts": "2026-09-24T12:00:00Z",
        "turnover_approx": 1000.0,
    }])

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 0  # 幂等, 不重复


# ── 测试 4: median 不足时 ratio=null ──

def test_ratio_null_when_insufficient_history(mod, tmp_env):
    """历史 < 20 日 → ratio=null, basis='accumulating'。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["NEW"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.NEW", "decision_price": 10.0, "reason": "turnover_too_low",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "market_cap": 2e9, "spread_pct": 2.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
    # 仅 5 日历史
    history = []
    from datetime import timedelta
    base = date(2026, 9, 19)
    for i in range(5):
        d = (base + timedelta(days=i)).isoformat()
        history.append({
            "et_date": d,
            "symbol": "US.NEW",
            "session": "premarket",
            "round_ts": f"{d}T12:00:00Z",
            "turnover_approx": 500.0,
        })
    _write_jsonl(out_path, history)

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 1
        rec = result[0]
        assert rec["relative_ratio"] is None
        assert rec["would_pass_relative"] is None
        assert rec["median_baseline"] is None
        assert "accumulating" in rec["median_basis"]


# ── 测试 5: cf_net_usd 不可构造 ──

def test_cf_null_when_no_execution(mod, tmp_env):
    """无后续执行记录 → cf_net_usd=null, cf_basis 说明原因。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["NOCF"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.NOCF", "decision_price": 10.0, "reason": "turnover_too_low",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "market_cap": 2e9, "spread_pct": 2.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
    _write_jsonl(out_path, [])

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])  # 空执行 → 无反事实

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 1
        rec = result[0]
        assert rec["cf_net_usd"] is None
        assert "no_subsequent_execution" in rec["cf_basis"]


def test_cf_computed_when_execution_exists(mod, tmp_env):
    """有后续执行记录 → cf_net_usd 有值。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["HASC"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.HASC", "decision_price": 10.0, "reason": "turnover_too_low",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "market_cap": 2e9, "spread_pct": 2.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
    _write_jsonl(out_path, [])

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [
        {"symbol": "US.HASC", "session": "premarket", "ts": "2026-09-24T12:30:00Z",
         "price": 10.5, "fill": {"filled": True, "avg_fill_price": 10.5, "filled_qty": 100}}
    ])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 1
        rec = result[0]
        assert rec["cf_net_usd"] is not None
        assert rec["cf_basis"] is not None


# ── 测试 6: 死线到期停采 ──

def test_deadline_expiry(mod, tmp_env):
    """死线过后返回空列表。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"), \
         patch.object(mod, "PLAN_DIR", tmp_env / "data" / "loops" / "ext_entry_plan"), \
         patch.object(mod, "EXECUTIONS_PATH", tmp_env / "data" / "extended_hours_entry_executions.jsonl"), \
         patch.object(mod, "_et_today", return_value=date(2026, 12, 1)):
        result = mod.collect(et_date="2026-12-01", session="premarket")
        assert result == []


# ── 测试 7: 非计划票不采集 ──

def test_non_plan_ticket_skipped(mod, tmp_env):
    """非计划内事件票不被采集。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["AAPL"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.NOTPLAN", "decision_price": 10.0, "reason": "turnover_too_low",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "market_cap": 2e9, "spread_pct": 2.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"
    _write_jsonl(out_path, [])

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 0  # NOTPLAN 不在计划中


# ── 测试 8: main 入口 ──

def test_main_returns_zero(mod):
    """main() 正常返回 0。"""
    with patch.object(mod, "collect", return_value=[]):
        assert mod.main(["--dry", "--date", "2026-09-24"]) == 0
