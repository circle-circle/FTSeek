"""ETH 影子⑥ eth_patient_maker_shadow hermetic 测试.

覆盖:
1. 自门控 dormant (PASS 日数 < 7) / active (≥ 7) 两路
2. 判据计算 (fill_rate, adverse, spread_captured, net_econ)
3. 幂等重跑 (同一 key 不重复追加)
4. 成交判定 (穿越逻辑)
5. 撤单逻辑 (不利漂移 > 0.5%)
6. 毕业裁定 (三条全满足 / 任一不满足)
7. 死线到期停采
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _load_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "eth_patient_maker_shadow",
        ROOT / "scripts" / "eth_patient_maker_shadow.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_mod()


@pytest.fixture
def tmp_env(tmp_path):
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
         patch.object(mod, "OUT_PATH", tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"), \
         patch.object(mod, "REJECTS_PATH", tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"), \
         patch.object(mod, "PLAN_DIR", tmp_env / "data" / "loops" / "ext_entry_plan"), \
         patch.object(mod, "EXECUTIONS_PATH", tmp_env / "data" / "extended_hours_entry_executions.jsonl"):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert result == []


def test_active_when_sufficient_pass_days(mod, tmp_env):
    """reconcile PASS 日数 ≥ 7 → active。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [])

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["AAPL"], "2026-09-24", "premarket"))

    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", tmp_env / "data" / "extended_hours_entry_executions.jsonl"):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert isinstance(result, list)


# ── 测试 2: 成交判定 (穿越) ──

def test_fill_on_crossing(mod, tmp_env):
    """后续 ask ≤ buy_limit → fill_buy = True。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["FILL"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    # 第一轮: spread_too_wide, bid=10.0, ask=10.5 → buy_limit=10.01, sell_limit=10.49
    # 第二轮: ask 跌到 9.99 (≤ buy_limit 10.01) → 穿越成交
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.FILL", "decision_price": 10.25, "qty": 100,
             "reason": "spread_too_wide",
             "quote": {"bid": 10.0, "ask": 10.5, "ask_volume": 50.0, "spread_pct": 4.88}}
        ]),
        _make_rejects("2026-09-24T12:05:00Z", "premarket", [
            {"symbol": "US.FILL", "decision_price": 10.0, "qty": 100,
             "reason": "market_cap_too_low",
             "quote": {"bid": 9.95, "ask": 9.99, "ask_volume": 60.0, "spread_pct": 0.4}}
        ]),
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    _write_jsonl(out_path, [])

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
        # buy_limit = bid + 0.01 = 10.01
        assert rec["buy_limit"] == 10.01
        # sell_limit = ask - 0.01 = 10.49
        assert rec["sell_limit"] == 10.49
        # 后续 ask=9.99 ≤ buy_limit=10.01 → fill_buy = True
        assert rec["fill_buy"] is True
        # 后续 bid=9.95 < sell_limit=10.49 → fill_sell = False
        assert rec["fill_sell"] is False


# ── 测试 3: 撤单逻辑 (不利漂移) ──

def test_cancel_on_adverse_drift(mod, tmp_env):
    """不利漂移 > 0.5% → cancel_buy/cancel_sell。"""
    # 构造一个 ask 上涨 > 0.5% 的场景 (对买方不利)
    rj = {
        "symbol": "US.DRIFT",
        "decision_price": 100.0,
        "reason": "spread_too_wide",
        "quote": {"bid": 99.5, "ask": 100.0, "ask_volume": 50.0, "spread_pct": 0.5},
        "_ts": "2026-09-24T12:00:00Z",
        "_session": "premarket",
        "_et_date": "2026-09-24",
    }
    # 后续: ask 从 100.0 涨到 101.0 → drift = (101.0 - 100.0) / 100.0 * 100 = 1.0% > 0.5%
    subsequent = [
        {
            "symbol": "US.DRIFT",
            "quote": {"bid": 100.5, "ask": 101.0, "ask_volume": 60.0, "spread_pct": 0.5},
            "_ts": "2026-09-24T12:05:00Z",
        }
    ]
    result = mod._simulate_maker(rj, subsequent)
    assert result["simulated"] is True
    # ask 从 100.0 → 101.0, drift = 1.0% > 0.5% → cancel_buy
    assert result["cancel_buy_drift"] is True
    # bid 从 99.5 → 100.5, drift_sell = (99.5 - 100.5) / 99.5 * 100 = -1.0% (有利, 不撤)
    assert result["cancel_sell_drift"] is False


# ── 测试 4: 幂等重跑 ──

def test_idempotent_rerun(mod, tmp_env):
    """同一 (et_date, symbol, session, round_ts) 不重复追加。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["IDM"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.IDM", "decision_price": 10.0, "qty": 100,
             "reason": "spread_too_wide",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "spread_pct": 2.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    _write_jsonl(out_path, [{
        "et_date": "2026-09-24",
        "symbol": "US.IDM",
        "session": "premarket",
        "round_ts": "2026-09-24T12:00:00Z",
    }])

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 0


# ── 测试 5: 毕业裁定 ──

def test_graduation_check_probe_recommended(mod, tmp_env):
    """三条全满足 → PROBE_RECOMMENDED。"""
    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    # 20 条记录, fill_rate=1.0, adverse<capture, net>0
    records = []
    for i in range(20):
        records.append({
            "et_date": f"2026-09-{i+1:02d}",
            "symbol": f"US.GRAD{i}",
            "session": "premarket",
            "round_ts": f"2026-09-{i+1:02d}T12:00:00Z",
            "filled_sides": 2,
            "total_sides": 2,
            "fill_rate": 1.0,
            "adverse_buy_pct": 0.1,
            "spread_captured_pct": 0.5,
            "net_econ_pct": 0.3,
        })
    _write_jsonl(out_path, records)

    with patch.object(mod, "OUT_PATH", out_path):
        result = mod.graduation_check()
        assert result["verdict"] == "PROBE_RECOMMENDED"
        assert result["n_filled"] == 20


def test_graduation_check_reject_low_fill(mod, tmp_env):
    """fill_rate < 40% → REJECT。"""
    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    records = []
    for i in range(20):
        records.append({
            "et_date": f"2026-09-{i+1:02d}",
            "symbol": f"US.LOWF{i}",
            "session": "premarket",
            "round_ts": f"2026-09-{i+1:02d}T12:00:00Z",
            "filled_sides": 1,
            "total_sides": 2,
            "fill_rate": 0.3,  # < 0.4
            "adverse_buy_pct": 0.1,
            "spread_captured_pct": 0.5,
            "net_econ_pct": 0.3,
        })
    _write_jsonl(out_path, records)

    with patch.object(mod, "OUT_PATH", out_path):
        result = mod.graduation_check()
        assert result["verdict"] == "REJECT"
        assert not result["criteria"]["fill_rate_gte_40pct"]["pass"]


def test_graduation_check_reject_adverse_gt_capture(mod, tmp_env):
    """逆行中位数 > 捕获价差 → REJECT。"""
    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    records = []
    for i in range(20):
        records.append({
            "et_date": f"2026-09-{i+1:02d}",
            "symbol": f"US.ADV{i}",
            "session": "premarket",
            "round_ts": f"2026-09-{i+1:02d}T12:00:00Z",
            "filled_sides": 2,
            "total_sides": 2,
            "fill_rate": 1.0,
            "adverse_buy_pct": 0.8,  # > spread_captured
            "spread_captured_pct": 0.3,
            "net_econ_pct": -0.5,
        })
    _write_jsonl(out_path, records)

    with patch.object(mod, "OUT_PATH", out_path):
        result = mod.graduation_check()
        assert result["verdict"] == "REJECT"


def test_graduation_check_insufficient_samples(mod, tmp_env):
    """n < 20 → INSUFFICIENT_SAMPLES。"""
    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    records = []
    for i in range(10):
        records.append({
            "et_date": f"2026-09-{i+1:02d}",
            "symbol": f"US.FEW{i}",
            "session": "premarket",
            "round_ts": f"2026-09-{i+1:02d}T12:00:00Z",
            "filled_sides": 1,
            "total_sides": 2,
            "fill_rate": 0.5,
        })
    _write_jsonl(out_path, records)

    with patch.object(mod, "OUT_PATH", out_path):
        result = mod.graduation_check()
        assert result["verdict"] == "INSUFFICIENT_SAMPLES"
        assert result["n"] == 10


# ── 测试 6: 死线到期停采 ──

def test_deadline_expiry(mod, tmp_env):
    """死线过后返回空列表。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"), \
         patch.object(mod, "PLAN_DIR", tmp_env / "data" / "loops" / "ext_entry_plan"), \
         patch.object(mod, "EXECUTIONS_PATH", tmp_env / "data" / "extended_hours_entry_executions.jsonl"), \
         patch.object(mod, "_et_today", return_value=date(2026, 12, 1)):
        result = mod.collect(et_date="2026-12-01", session="premarket")
        assert result == []


# ── 测试 7: 无有效报价不模拟 ──

def test_no_simulation_without_quote(mod, tmp_env):
    """quote 缺 bid/ask → simulated=False。"""
    rj = {
        "symbol": "US.NOQUOTE",
        "decision_price": 10.0,
        "reason": "spread_too_wide",
        "quote": {},
        "_ts": "2026-09-24T12:00:00Z",
    }
    result = mod._simulate_maker(rj, [])
    assert result["simulated"] is False
    assert result["sim_basis"] == "no_valid_quote"


# ── 测试 8: 非计划票不采集 ──

def test_non_plan_ticket_skipped(mod, tmp_env):
    """非计划内事件票不被采集。"""
    reconcile = tmp_env / "data" / "loops" / "ext_entry_plan" / "reconcile_ledger.jsonl"
    _write_jsonl(reconcile, _make_reconcile(7))

    plan_dir = tmp_env / "data" / "loops" / "ext_entry_plan"
    _write_json(plan_dir / "2026-09-24_premarket.json", _make_plan(["AAPL"], "2026-09-24", "premarket"))

    rejects_path = tmp_env / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
    _write_jsonl(rejects_path, [
        _make_rejects("2026-09-24T12:00:00Z", "premarket", [
            {"symbol": "US.NOTPLAN", "decision_price": 10.0, "qty": 100,
             "reason": "spread_too_wide",
             "quote": {"bid": 9.9, "ask": 10.1, "ask_volume": 100.0, "spread_pct": 2.0}}
        ])
    ])

    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    _write_jsonl(out_path, [])

    exec_path = tmp_env / "data" / "extended_hours_entry_executions.jsonl"
    _write_jsonl(exec_path, [])

    with patch.object(mod, "RECONCILE_PATH", reconcile), \
         patch.object(mod, "OUT_PATH", out_path), \
         patch.object(mod, "REJECTS_PATH", rejects_path), \
         patch.object(mod, "PLAN_DIR", plan_dir), \
         patch.object(mod, "EXECUTIONS_PATH", exec_path):
        result = mod.collect(et_date="2026-09-24", session="premarket")
        assert len(result) == 0


# ── 测试 9: main 入口 ──

def test_main_returns_zero(mod):
    """main() 正常返回 0。"""
    with patch.object(mod, "collect", return_value=[]):
        assert mod.main(["--dry", "--date", "2026-09-24"]) == 0


def test_main_check_flag(mod, tmp_env, capsys):
    """--check 标志运行毕业裁定。"""
    out_path = tmp_env / "data" / "shadow" / "eth_patient_maker_shadow.jsonl"
    _write_jsonl(out_path, [])

    with patch.object(mod, "OUT_PATH", out_path):
        ret = mod.main(["--check"])
        assert ret == 0
        captured = capsys.readouterr()
        # 空台账 → n=0 < SAMPLING_GATE_N=20 → INSUFFICIENT_SAMPLES
        assert "INSUFFICIENT_SAMPLES" in captured.out
