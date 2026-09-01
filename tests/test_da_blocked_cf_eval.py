"""da_blocked_cf (B14) 行为级测试 + 注册表钉子。

红绿纪律: 判定函数对构造数据必须给出机械一致结论 (防多窗拣选=仅主窗 T+1 参裁);
三观察点注册项必须在册 (防散落), eval_script 路径必须真实存在。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.da_blocked_signal_cf_eval as m  # noqa: E402
from app.playbook.shadow_registry import REGISTRY, ShadowStatus  # noqa: E402


def _write_jsonl(path: Path, rows: list):
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_sign_test_boundaries():
    assert m.sign_test_one_sided(0, 0) == 1.0
    assert abs(m.sign_test_one_sided(30, 30) - 1 / 2 ** 30) < 1e-12
    assert m.sign_test_one_sided(20, 30) < 0.05 < m.sign_test_one_sided(19, 30)


def test_load_filters_and_dedups(tmp_path, monkeypatch):
    jl = tmp_path / "gate.jsonl"
    _write_jsonl(jl, [
        {"datetime": "2026-07-13T09:02-04:00", "trade_date": "2026-07-13", "symbol": "US.A",
         "direction": "BUY", "decision": "BLOCK", "quality_score": 10},
        {"datetime": "2026-07-13T11:00-04:00", "trade_date": "2026-07-13", "symbol": "US.A",
         "direction": "BUY", "decision": "BLOCK", "quality_score": 12},   # 同日同向重复 → 去重
        {"datetime": "2026-07-14T09:00-04:00", "trade_date": "2026-07-14", "symbol": "US.B",
         "direction": "SKIP", "decision": "BLOCK", "quality_score": 5},   # 非方向性 → 排除
        {"datetime": "2026-07-15T09:00-04:00", "trade_date": "2026-07-15", "symbol": "US.C",
         "direction": "SELL_SHORT", "decision": "WARN", "quality_score": 55},  # 非BLOCK → 排除
        {"datetime": "2026-07-16T09:00-04:00", "trade_date": "2026-07-16", "symbol": "US.D",
         "direction": "SELL_SHORT", "decision": "BLOCK", "quality_score": 70},
    ])
    monkeypatch.setattr(m, "GATE_JSONL", jl)
    sig = m.load_blocked_signals()
    keys = [(s["trade_date"], s["symbol"], s["direction"]) for s in sig]
    assert keys == [("2026-07-13", "US.A", "BUY"), ("2026-07-16", "US.D", "SELL_SHORT")]


def _bars(dates, closes, open_=100.0):
    return [[d, open_, open_ + 2, open_ - 2, c] for d, c in zip(dates, closes)]


def test_evaluate_signed_returns_and_censoring(tmp_path, monkeypatch):
    daily = tmp_path / "daily"
    daily.mkdir()
    # US.A: 7/13 锚日 → T+1 收 90 (BUY: -10%); 7/14 锚日 → 窗口不足 (右缘删失)
    (daily / "US_A.json").write_text(json.dumps(
        _bars(["2026-07-13", "2026-07-14"], [90.0, 95.0])))
    monkeypatch.setattr(m, "DAILY_DIR", daily)

    signals = [
        {"trade_date": "2026-07-13", "symbol": "US.A", "direction": "BUY", "quality_score": 10},
        {"trade_date": "2026-07-14", "symbol": "US.A", "direction": "BUY", "quality_score": 10},
        {"trade_date": "2026-07-13", "symbol": "US.NOFILE", "direction": "BUY", "quality_score": 10},
        {"trade_date": "2026-07-13", "symbol": "US.A", "direction": "SELL_SHORT", "quality_score": 40},
    ]
    rep = m.evaluate(signals)
    assert rep["censored"]["no_bars"] == 1          # US.NOFILE
    assert rep["censored"]["window"][1] == 1        # 7/14 锚日 T+1 右缘
    t1 = rep["horizons"]["T+1"]
    assert t1["n_eval"] == 2                        # BUY 7/13 + SHORT 7/13
    # BUY: 90/100-1 = -10%; SELL_SHORT: -(90/100-1) = +10% → 一正一负
    assert t1["mean_signed_pct"] == 0.0 and t1["wins"] == 1
    assert rep["verdict"] == "KEEP_SAMPLING"        # n<50 一律攒样


def _verdict_case(tmp_path, monkeypatch, direction, close):
    daily = tmp_path / "daily"
    daily.mkdir(exist_ok=True)
    dates = [f"2026-{(i // 28) + 7:02d}-{(i % 28) + 1:02d}" for i in range(60)]
    (daily / "US_X.json").write_text(json.dumps(_bars(dates, [close] * 60)))
    monkeypatch.setattr(m, "DAILY_DIR", daily)
    signals = [{"trade_date": dates[i], "symbol": "US.X", "direction": direction,
                "quality_score": 20} for i in range(50)]
    return m.evaluate(signals)


def test_verdict_gate_effective(tmp_path, monkeypatch):
    rep = _verdict_case(tmp_path, monkeypatch, "BUY", 90.0)   # 放行全亏 -10%
    assert rep["horizons"]["T+1"]["n_eval"] == 50
    assert rep["verdict"] == "GATE_EFFECTIVE"


def test_verdict_over_blocking(tmp_path, monkeypatch):
    rep = _verdict_case(tmp_path, monkeypatch, "BUY", 110.0)  # 放行全赚 +10%
    assert rep["verdict"] == "OVER_BLOCKING_SUSPECT"


def test_registry_pins_phase_c():
    items = {it.id: it for it in REGISTRY}
    for iid, script, min_n in [
        ("pullback_limit_shadow", "scripts/cf_pullback_limit_replay.py", 20),
        ("gap_alignment", "scripts/gap_alignment_shadow.py", 30),
        ("da_blocked_cf", "scripts/da_blocked_signal_cf_eval.py", 50),
    ]:
        it = items.get(iid)
        assert it is not None, f"{iid} 未注册 (防散落纪律)"
        assert it.status in (ShadowStatus.OBSERVING, ShadowStatus.READY_TO_EVALUATE)
        assert it.min_n == min_n and it.eval_script == script
        assert (Path(__file__).resolve().parents[1] / script).exists()
    assert items["gap_alignment"].count_mode == "distinct:trade_id"
    assert "B14" in items["da_blocked_cf"].graduation_criteria
