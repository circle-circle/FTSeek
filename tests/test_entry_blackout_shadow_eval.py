"""RFC-200 黑窗影子评估器契约 (2026-11-12, 用户裁「A: 只加观测」).

钉住的读数口径:
  · cycle 覆盖 / 首末时间 / 取价异常 (全失败 vs 部分失败 vs 无指令);
  · hit 按 (date, inst_id) **最早 ts** 去重 + duplicate_hit 可见;
  · decision_id 源分桶 DA / MOM / OTHER;
  · `band_would_trigger` 只作上限, 不计算收益;
  · 无 hit 不报错, 无文件/无 cycle 必须非零;
  · 注册表条目 + 产出缺失的停摆哨兵 (eval_on_demand 不报悬空)。
"""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import scripts.entry_blackout_shadow_eval as ev


def _cycle(date_s: str, ts: str, et_min: int, n_inst: int, n_priced: int,
           n_would: int = 0, n_new: int = 0, hhmm: int = 945) -> dict:
    return {
        "kind": "cycle", "ts": ts, "date": date_s, "et_min": et_min,
        "n_inst": n_inst, "n_priced": n_priced,
        "n_would_trigger": n_would, "n_new_hits": n_new,
        "blackout_hhmm": hhmm,
    }


def _hit(date_s: str, ts: str, inst_id: int, decision_id: str = "DA-20261112090000-US.AAA-x",
         symbol: str = "US.AAA", entry_path: str = "pullback") -> dict:
    return {
        "kind": "hit", "ts": ts, "date": date_s, "et_min": 0,
        "inst_id": inst_id, "decision_id": decision_id, "symbol": symbol,
        "direction": "BUY", "entry_path": entry_path,
        "band_would_trigger": True, "vol_assumed_ok": True,
        "caveat": "band geometry only; later gates unobserved",
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _run_json(capsys, path: Path, *extra: str) -> tuple[int, dict]:
    code = ev.main(["--path", str(path), "--json", "--no-write", *extra])
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


# ── 读数口径 ─────────────────────────────────────────────────────────
def test_cycles_hits_dedup_and_source_buckets():
    d = "2026-11-12"
    rows = [
        _cycle(d, f"{d}T09:35:00-05:00", 575, 4, 3, n_would=1, n_new=1),
        _cycle(d, f"{d}T09:40:00-05:00", 580, 4, 4, n_would=2, n_new=1),
        _hit(d, f"{d}T09:37:00-05:00", 1),                       # 晚于下一条 ⇒ 丢弃
        _hit(d, f"{d}T09:36:00-05:00", 1),                       # 最早 ts 胜出
        _hit(d, f"{d}T09:38:00-05:00", 2, "MOM-ETF-V1-20261112-US.QQQ"),
        _hit(d, f"{d}T09:39:00-05:00", 3, ""),                   # 空 decision_id
        _hit(d, f"{d}T09:41:00-05:00", 4, "universe_expansion:abc"),
    ]
    res = ev.evaluate(rows)
    assert res["status"] == ev.STATUS_OK
    assert res["date"] == d
    cyc = res["cycle"]
    assert cyc["n_cycles"] == 2
    assert cyc["first_ts"] == f"{d}T09:35:00-05:00"
    assert cyc["last_ts"] == f"{d}T09:40:00-05:00"
    assert cyc["first_et"] == "09:35" and cyc["last_et"] == "09:40"
    assert cyc["et_min_span"] == 5
    assert cyc["gap_min"] == [5]
    assert cyc["n_inst_sum"] == 8 and cyc["n_priced_sum"] == 7
    assert cyc["n_would_trigger_sum"] == 3 and cyc["n_new_hits_sum"] == 2
    assert cyc["blackout_hhmm_seen"] == [945] and cyc["blackout_hhmm_mixed"] is False

    hits = res["hits"]
    assert hits["n_rows_raw"] == 5
    assert hits["n_dedup"] == 4
    assert hits["duplicate_hit"] == 1
    assert hits["first_hit_ts"] == f"{d}T09:36:00-05:00"
    assert hits["n_by_decision_source"] == {"DA": 1, "MOM": 1, "OTHER": 2}
    assert hits["n_empty_decision_id"] == 1
    assert hits["n_by_entry_path"] == {"pullback": 4}
    assert hits["n_band_would_trigger"] == 4

    assert res["quote"]["all_failed"] is False
    assert res["quote"]["anomaly"] is False
    assert "band_would_trigger 只是带几何上限" in " ".join(res["caveats"])
    assert "不计算任何收益" in " ".join(res["caveats"])
    # 明确钉住: 本器不产出收益口径 (caveat 里可以出现 PnL 字样说明"不算")
    assert not any("pnl" in key.lower() for key in res)
    dump = json.dumps(res, ensure_ascii=False).lower()
    assert "pnl_net" not in dump and "realized_pnl" not in dump


def test_no_hits_is_ok(capsys, tmp_path):
    p = tmp_path / "shadow.jsonl"
    _write(p, [_cycle("2026-11-12", "2026-11-12T09:35:00-05:00", 575, 3, 2)])
    code, res = _run_json(capsys, p, "--date", "2026-11-12")
    assert code == 0
    assert res["status"] == ev.STATUS_OK
    assert res["hits"]["n_dedup"] == 0
    assert res["quote"]["all_failed"] is False


def test_r1_no_candidates_is_not_quote_failure():
    row = _cycle("2026-11-12", "2026-11-12T09:35:00-05:00", 575, 7, 0)
    row.update(status="NO_CANDIDATES", n_eligible=0, n_quote_error=0,
               n_price_invalid=0, n_eval_error=0)
    res = ev.evaluate([row])
    assert res["status"] == ev.STATUS_OK
    assert res["quote"]["all_failed"] is False
    assert res["quote"]["writer_status_counts"] == {"NO_CANDIDATES": 1}


def test_r1_no_router_is_explicit_quote_failure():
    row = _cycle("2026-11-12", "2026-11-12T09:35:00-05:00", 575, 1, 0)
    row.update(status="NO_ROUTER", n_eligible=0)
    res = ev.evaluate([row])
    assert res["status"] == ev.STATUS_QUOTE_ALL_FAIL
    assert res["quote"]["all_failed"] is True


def test_r1_partial_status_is_incomplete_and_uses_eligible_ratio():
    row = _cycle("2026-11-12", "2026-11-12T09:35:00-05:00", 575, 5, 1)
    row.update(status="PARTIAL_QUOTE", n_eligible=2, n_price_invalid=1,
               n_quote_error=1, n_eval_error=0)
    res = ev.evaluate([row])
    assert res["status"] == ev.STATUS_INCOMPLETE
    assert res["quote"]["partial_failure"] is True
    assert res["quote"]["priced_ratio_by_cycle"] == [0.5]
    assert res["quote"]["n_quote_error_sum"] == 1


def test_quote_all_fail_is_identified(capsys, tmp_path):
    d = "2026-11-12"
    rows = [
        _cycle(d, f"{d}T09:35:00-05:00", 575, 3, 0),
        _cycle(d, f"{d}T09:40:00-05:00", 580, 3, 0, hhmm=935),
    ]
    p = tmp_path / "shadow.jsonl"
    _write(p, rows)
    code, res = _run_json(capsys, p, "--date", d)
    assert code == 1
    assert res["status"] == ev.STATUS_QUOTE_ALL_FAIL
    assert res["quote"]["all_failed"] is True
    assert res["quote"]["n_cycles_zero_priced_with_inst"] == 2
    assert res["cycle"]["blackout_hhmm_mixed"] is True   # 盘中改配置也可见
    assert res["quote"]["priced_ratio_by_cycle"] == [0.0, 0.0]


def test_partial_quote_failure_is_incomplete(capsys, tmp_path):
    d = "2026-11-12"
    p = tmp_path / "shadow.jsonl"
    _write(p, [
        _cycle(d, f"{d}T09:35:00-05:00", 575, 2, 0),
        _cycle(d, f"{d}T09:40:00-05:00", 580, 2, 2),
    ])
    code, res = _run_json(capsys, p, "--date", d)
    assert code == 2
    assert res["status"] == ev.STATUS_INCOMPLETE
    assert res["quote"]["partial_failure"] is True
    assert res["quote"]["n_cycles_priced"] == 1


def test_missing_counts_is_incomplete_not_all_fail(capsys, tmp_path):
    """缺 n_inst/n_priced 字段 = 读数不完整, 不能当成取价全失败。"""
    d = "2026-11-12"
    row = _cycle(d, f"{d}T09:35:00-05:00", 575, 3, 2)
    row.pop("n_priced")
    p = tmp_path / "shadow.jsonl"
    _write(p, [row])
    code, res = _run_json(capsys, p, "--date", d)
    assert code == 2
    assert res["status"] == ev.STATUS_INCOMPLETE
    assert res["quote"]["n_cycles_missing_counts"] == 1
    assert res["quote"]["all_failed"] is False
    assert res["quote"]["partial_failure"] is True


def test_zero_instructions_is_not_quote_failure(capsys, tmp_path):
    d = "2026-11-12"
    p = tmp_path / "shadow.jsonl"
    _write(p, [_cycle(d, f"{d}T09:35:00-05:00", 575, 0, 0),
               _cycle(d, f"{d}T09:40:00-05:00", 580, 0, 0)])
    code, res = _run_json(capsys, p, "--date", d)
    assert code == 0
    assert res["status"] == ev.STATUS_OK
    assert res["quote"]["all_failed"] is False
    assert res["quote"]["n_cycles_with_inst"] == 0


def test_missing_file_is_no_data_and_nonzero(capsys, tmp_path):
    code, res = _run_json(capsys, tmp_path / "does_not_exist.jsonl",
                          "--date", "2026-11-12")
    assert code == 1
    assert res["status"] == ev.STATUS_NO_DATA
    assert res["mounting"]["data_path_exists"] is False
    assert res["quote"]["unobservable_no_rows"] is True
    assert "无任何行" in res["status_detail"]
    assert "停摆" in res["status_detail"]
    assert "每轮" in res["status_detail"]


def test_no_cycle_for_date_is_no_cycles_and_nonzero(capsys, tmp_path):
    d = "2026-11-12"
    p = tmp_path / "shadow.jsonl"
    _write(p, [_hit(d, f"{d}T09:37:00-05:00", 1)])
    code, res = _run_json(capsys, p, "--date", d)
    assert code == 1
    assert res["status"] == ev.STATUS_NO_CYCLES


def test_default_date_is_latest_in_file():
    rows = [
        _cycle("2026-11-11", "2026-11-11T09:35:00-05:00", 575, 1, 1),
        _cycle("2026-11-12", "2026-11-12T09:35:00-05:00", 575, 1, 1),
    ]
    assert ev.evaluate(rows)["date"] == "2026-11-12"


def test_bad_lines_are_counted_not_silent(tmp_path):
    p = tmp_path / "shadow.jsonl"
    p.write_text(
        json.dumps(_cycle("2026-11-12", "2026-11-12T09:35:00-05:00", 575, 1, 1)) + "\n"
        + "{not-json\n"
        + json.dumps(["not", "an", "object"]) + "\n",
        encoding="utf-8")
    rows, meta = ev.load_rows(p)
    assert len(rows) == 1
    assert meta["lines_total"] == 3
    assert meta["bad_lines"] == 1
    assert meta["non_dict_lines"] == 1
    assert meta["bad_line_samples"]
    res = ev.evaluate(rows, requested_date="2026-11-12", meta=meta)
    assert res["status"] == ev.STATUS_OK
    assert res["data_quality"]["bad_lines"] == 1


# ── 单一真源 / 注册表 / 停摆哨兵 ─────────────────────────────────────
def test_decision_prefix_constants_match_sources():
    """本器为纯 stdlib 不 import app.*; 前缀必须与真源同值 (防漂移)。"""
    from app.playbook.pilot_policy import MOM_ETF_V1_PREFIX
    from app.playbook.pipeline_parts.supply_gate import DA_DECISION_PREFIX
    assert ev.MOM_ETF_V1_PREFIX == MOM_ETF_V1_PREFIX
    assert ev.DA_DECISION_PREFIX == DA_DECISION_PREFIX


def test_registered_with_permanent_measure_contract():
    from app.playbook.shadow_registry import REGISTRY, ShadowStatus
    item = next((s for s in REGISTRY if s.id == "entry_blackout_shadow"), None)
    assert item is not None, "RFC-200 黑窗影子必须进注册表 (否则无停摆守卫)"
    assert item.status == ShadowStatus.OBSERVING
    assert item.data_path == "data/entry_blackout_shadow.jsonl"
    assert item.config_key == "playbook.execution_parameters.entry_blackout_shadow_enabled"
    assert item.eval_script == "scripts/entry_blackout_shadow_eval.py"
    assert item.eval_on_demand is True
    assert item.count_mode == "distinct_where:kind=hit"
    assert item.no_adjudication_reason.startswith("PERMANENT_MEASURE")
    assert item.created == "2026-11-12"


def test_mount_sentinel_surfaces_missing_output(monkeypatch, tmp_path):
    """停摆检查: eval 按需不报悬空, 但产出缺失 (过初生豁免) 必须报。"""
    import app.playbook.shadow_registry as sr
    from app.playbook.shadow_registry import REGISTRY
    item = copy.deepcopy(next(s for s in REGISTRY if s.id == "entry_blackout_shadow"))
    item.data_path = str(tmp_path / "missing.jsonl")
    item.created = (date.today() - timedelta(days=sr._GRACE_DAYS)).isoformat()
    monkeypatch.setattr(sr, "REGISTRY", [item])
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    warnings = sr.check_mounting()
    assert any("产出缺失" in w and "entry_blackout_shadow" in w for w in warnings)
    assert not any("悬空观察" in w for w in warnings), (
        "eval_on_demand=True 的按需评估器不应被报 eval 悬空")