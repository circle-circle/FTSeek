import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "da_entry_quality_trend.py"
SPEC = importlib.util.spec_from_file_location("da_entry_quality_trend", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def _row(day, net, mfe):
    return {"trade_date": day, "entry_price": 100.0, "quantity": 10,
            "pnl_net": net, "realized_pnl": net, "mfe_pct": mfe}


def test_net_fallback_subtracts_fees_from_gross_realized_pnl():
    row = _row("2026-11-10", -6.0, 1.0)
    row["pnl_net"] = None
    row["fees_est"] = 2.0
    assert MODULE._net(row) == pytest.approx(-8.0)


def test_breakeven_formula():
    assert MODULE.breakeven_dead_rate(-100.0, 50.0) == pytest.approx(50.0 / 150.0)
    assert MODULE.breakeven_dead_rate(-147.38, 35.62) == pytest.approx(35.62 / (35.62 + 147.38))


def test_breakeven_none_when_live_nonpositive():
    assert MODULE.breakeven_dead_rate(-100.0, 0.0) is None
    assert MODULE.breakeven_dead_rate(-100.0, -5.0) is None


def test_analyze_dead_rate_and_below_breakeven():
    rows = [_row(f"2026-10-{i + 1:02d}", 30.0, 1.0) for i in range(8)]
    rows += [_row(f"2026-10-{i + 9:02d}", -10.0, 0.1) for i in range(2)]
    report = MODULE.analyze(rows)
    assert report["n"] == 10
    assert report["dead_n"] == 2
    assert report["dead_rate_pct"] == pytest.approx(20.0)
    assert report["live_avg_bps"] == pytest.approx(300.0)
    assert report["dead_avg_bps"] == pytest.approx(-100.0)
    assert report["breakeven_dead_rate_pct"] == pytest.approx(75.0)
    assert report["below_breakeven"] is True


def test_analyze_stratifies_recovery_artifacts_out_of_dead_rate():
    def sig(day, net, mfe):
        return {"trade_date": day, "entry_price": 100.0, "quantity": 10,
                "pnl_net": net, "realized_pnl": net, "mfe_pct": mfe,
                "entry_reason": "playbook_futu_simulate"}
    rows = [sig(f"2026-11-{i + 1:02d}", 30.0, 1.0) for i in range(8)]
    rows += [sig(f"2026-11-{i + 9:02d}", -10.0, 0.1) for i in range(2)]
    rows += [{"trade_date": f"2026-11-{i + 11:02d}", "entry_price": 100.0, "quantity": 10,
              "pnl_net": -20.0, "realized_pnl": -20.0, "mfe_pct": 0.0,
              "entry_reason": "sync_recovery_backfill"} for i in range(5)]
    report = MODULE.analyze(rows)
    assert report["n"] == 10
    assert report["dead_n"] == 2
    assert report["dead_rate_pct"] == pytest.approx(20.0)
    assert report["n_all"] == 15
    assert report["dead_n_all"] == 7
    assert report["dead_rate_all_pct"] == pytest.approx(46.7, abs=0.1)
    assert report["artifact"]["n"] == 5
    assert report["artifact"]["dead_n"] == 5
    assert report["artifact"]["net_usd"] == pytest.approx(-100.0)


# ── 空转指标 (RFC-145 §4 B3 / RFC-215 §4 D4, 2026-11-19) ──────────────────────

import json as _json


def _cc_row(day, dec, rtype="chase_cap_violation"):
    return {"date": day, "type": rtype, "decision_id": dec}


def test_load_chase_cap_rounds_filters_type_date_badlines(tmp_path):
    p = tmp_path / "pb.jsonl"
    lines = [
        _json.dumps(_cc_row("2026-10-01", "DA-a")),
        _json.dumps(_cc_row("2026-10-01", "DA-a")),          # 同指令同日第二轮 ⇒ 2
        _json.dumps(_cc_row("2026-10-01", "DA-b", rtype="trigger")),  # 非 violation 不计
        _json.dumps(_cc_row("2026-09-30", "DA-c")),           # 早于 start 不计
        "{broken json",                                      # 坏行跳过
        _json.dumps({"date": "2026-10-02", "type": "chase_cap_violation"}),  # 无 decision_id 不计
    ]
    p.write_text("\n".join(lines), encoding="utf-8")
    pairs = MODULE.load_chase_cap_rounds("2026-10-01", path=p)
    assert pairs == {("DA-a", "2026-10-01"): 2}


def test_load_chase_cap_rounds_missing_file_returns_none(tmp_path):
    assert MODULE.load_chase_cap_rounds("2026-10-01", path=tmp_path / "nope.jsonl") is None


def test_idle_churn_metrics_ge20_and_executed0():
    from collections import Counter
    pairs = Counter({("DA-a", "2026-10-01"): 25, ("DA-b", "2026-10-02"): 71,
                     ("DA-c", "2026-10-03"): 19, ("DA-d", "2026-10-04"): 30})
    em = {("DA-a", "2026-10-01"): False, ("DA-b", "2026-10-02"): True,
          ("DA-c", "2026-10-03"): False, ("DA-d", "2026-10-04"): False}
    m = MODULE.idle_churn_metrics(pairs, em)
    assert m["rounds_total"] == 145
    assert m["pairs_total"] == 4
    assert m["median_rounds"] == 30  # 上中位: sorted [19,25,30,71] -> values[n//2]
    assert m["max_rounds"] == 71
    assert m["pairs_ge_min"] == 3
    assert m["idle_pairs"] == 2          # DA-a(25,未执行) + DA-d(30,未执行)
    assert m["idle_rounds"] == 55
    assert m["executed_pairs_ge_min"] == 1
    assert m["unknown_pairs_ge_min"] == 0
    assert m["monthly"] == [{"month": "2026-10", "idle_pairs": 2, "idle_rounds": 55}]


def test_idle_churn_metrics_unknown_instruction_rows_counted_separately():
    from collections import Counter
    pairs = Counter({("DA-x", "2026-11-01"): 40})
    m = MODULE.idle_churn_metrics(pairs, {})  # 指令行缺席 ⇒ unknown, 不当未执行
    assert m["pairs_ge_min"] == 1
    assert m["idle_pairs"] == 0
    assert m["unknown_pairs_ge_min"] == 1


def test_idle_churn_metrics_empty():
    from collections import Counter
    m = MODULE.idle_churn_metrics(Counter(), {})
    assert m["rounds_total"] == 0
    assert m["pairs_total"] == 0
    assert m["median_rounds"] is None
    assert m["max_rounds"] is None
    assert m["monthly"] == []
