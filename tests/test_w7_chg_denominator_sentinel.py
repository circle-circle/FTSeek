"""W7 昨收分母哨兵钉子 (2026-09-11, DEB-20260911 共识 N1/N3, 用户裁「按建议执行」)。

病灶 (BIDU 2026-09-08 实锤, DEB C6): 同一标的同一时刻, 两条取数路径对「chg vs 昨收」
给出分叉读数 — premarket_invalidation 影子走 futu get_market_snapshot 原始
prev_close_price (干净侧, SMTC 8/26 真值 127.52 已证), ctx 路径走 get_quote push 缓存
prev_close (陈旧侧): 7 秒内 -7.65% vs +4.07%, 差 11.72pp 且 change_percent 全日冻结
(BIDU 09:35:25 与 10:47:00 同值) ⇒ 所有 ctx["change_percent"] 消费者当日读数不可信。
两个影子各自"零违反" ≠ 口径一致 — 此前无守卫盯分母维度。

本文件钉住接入 daily_invariant_check 后的语义:
  ① 基线外新增分叉 → 当晚 W7_chg_denominator_divergence (BIDU 形态即真目标);
  ② 基线内存量 (BIDU 9/8 等立档实锤) → silenced_known 计数, 零告警 (存量在修, 不刷噪声);
  ③ 愈合只报告不自动改写基线 (棘轮只降不升 — 守卫不得自己改判据);
  ④ 影子档缺失 = 部署形态差异 → skipped_no_shadow 静默 (与 W1 skipped_old_schema 同纪律);
  ⑤ 首跑自建基线不告警 (立档当刻的分叉即已知集, BIDU 9/8 被锁);
  ⑥ 时间窗纪律: 窗外 (±10min) 行不可比 — 日内价格漂移不得误报为分母分叉;
     窗内取最小差 (分母一致 ⇒ 最接近时刻读数 ≈0; 分母分叉 ⇒ 全日冻结必全发散);
  ⑦ join key: inst_id 精确优先, pmi 侧无 inst_id 才退 (symbol,date);
     有 inst_id 的行不得经 (symbol,date) 兜底跨指令串染;
  ⑧ 整档不可读 → W7_detector_error ("零违反"与"守卫崩了"必须可区分, 8/28 D1/D2 同课);
  ⑨ 单行坏行跳过计数不灭灯 (容忍个别损坏行, 哨兵面只缩不瞎)。

回滚: 删本文件 + revert 对应提交即可, 零生产代码依赖。
"""
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py"
)
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

# BIDU 2026-09-08 立档实锤形态 (数字取自真实数据, 见 DEB-20260911 C6)
_BIDU_PMI = {
    "ts": "2026-09-08T09:35:17-04:00", "date": "2026-09-08", "symbol": "US.BIDU",
    "direction": "SELL_SHORT", "inst_id": 2353, "chg_pct": -7.653,
}
_BIDU_ETS_FROZEN = {  # push 缓存昨收 → change_percent +4.07 (真实 09:35:25 行)
    "ts": "2026-09-08T09:35:25-04:00", "symbol": "US.BIDU", "inst_id": 2353,
    "features": {"change_percent": 4.07},
}
_DDOG_PMI = {  # 健康对照: 两路径同昨收, Δ=0.17pp (真实 9/3 数据)
    "ts": "2026-09-03T09:35:30-04:00", "date": "2026-09-03", "symbol": "US.DDOG",
    "direction": "SELL_SHORT", "inst_id": 2172, "chg_pct": 3.267,
}
_DDOG_ETS = {
    "ts": "2026-09-03T09:35:35-04:00", "symbol": "US.DDOG", "inst_id": 2172,
    "features": {"change_percent": 3.44},
}


def _write(path: Path, rows) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def _baseline(tmp_path, divergent, refs=None):
    p = tmp_path / "chg_denominator_baseline.json"
    p.write_text(json.dumps({"divergent": list(divergent), "refs": refs or {}},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _run(monkeypatch, tmp_path, pmi_rows, ets_rows, baseline=None):
    monkeypatch.setattr(dic, "_W7_PMI", _write(tmp_path / "pmi.jsonl", pmi_rows))
    monkeypatch.setattr(dic, "_W7_ETS", _write(tmp_path / "ets.jsonl", ets_rows))
    monkeypatch.setattr(dic, "_W7_BASELINE",
                        baseline or (tmp_path / "absent_baseline.json"))
    hits = []

    def rec(k, rid, sym, msg):
        hits.append((k, str(sym), msg))

    return dic._check_chg_denominator(rec), hits


def test_bidu_shape_new_divergence_alarms_same_night(tmp_path, monkeypatch):
    """①真目标: 基线外新增分叉 (BIDU 9/8 形态, 11.72pp) → 当晚 W7_chg_denominator_divergence。"""
    info, hits = _run(monkeypatch, tmp_path,
                      [_BIDU_PMI, _DDOG_PMI], [_BIDU_ETS_FROZEN, _DDOG_ETS],
                      baseline=_baseline(tmp_path, []))
    assert info["status"] == "drift"
    assert [h[0] for h in hits] == ["W7_chg_denominator_divergence"], "健康对照不得告警"
    assert hits[0][1] == "2026-09-08 US.BIDU#2353"
    assert "11.72" in hits[0][2] and "2pp" in hits[0][2]
    assert info["divergent"] == ["2026-09-08 US.BIDU#2353"]
    assert info["paired"] == 2 and info["silenced_known"] == 0


def test_known_divergence_silenced_not_noise(tmp_path, monkeypatch):
    """②存量 (BIDU 9/8 已裁决留痕) 只计数不告警 — 否则守卫天天红 = 没人看。"""
    info, hits = _run(monkeypatch, tmp_path,
                      [_BIDU_PMI, _DDOG_PMI], [_BIDU_ETS_FROZEN, _DDOG_ETS],
                      baseline=_baseline(tmp_path, ["2026-09-08 US.BIDU#2353"]))
    assert info["status"] == "ok" and hits == []
    assert info["silenced_known"] == 1 and info["divergent_n"] == 1


def test_healed_reported_and_baseline_not_rewritten(tmp_path, monkeypatch):
    """③棘轮只降不升: 愈合项进 healed 供人工收缩, 守卫绝不自动改写判据文件。"""
    healed = _baseline(tmp_path, ["2026-09-08 US.BIDU#2353", "2026-08-18 US.SNDK#1164"])
    before = healed.read_bytes()
    info, hits = _run(monkeypatch, tmp_path,
                      [_BIDU_PMI], [_BIDU_ETS_FROZEN], baseline=healed)
    assert info["status"] == "ok" and hits == []
    assert info["healed"] == ["2026-08-18 US.SNDK#1164"]
    assert healed.read_bytes() == before, "基线被自动改写 = 守卫自己改判据"


def test_first_run_bootstraps_baseline_and_locks_bidu(tmp_path, monkeypatch):
    """⑤首跑自建基线 (与 W1/W2 bootstrap 同款): 立档当刻的分叉即已知集, BIDU 被锁不告警。"""
    info, hits = _run(monkeypatch, tmp_path, [_BIDU_PMI], [_BIDU_ETS_FROZEN])
    assert info["status"] == "baseline_established" and hits == []
    written = json.loads((tmp_path / "absent_baseline.json").read_text(encoding="utf-8"))
    assert written["divergent"] == ["2026-09-08 US.BIDU#2353"]
    assert "只降不升" in written["note"], "豁免纪律必须写在判据文件里 (同 config_freeze)"
    assert "2026-09-08 US.BIDU#2353" in written["refs"], "立档实锤必须带裁决出处"


def test_out_of_window_rows_not_comparable(tmp_path, monkeypatch):
    """⑥窗外行不可比: BIDU 10:47 的冻结行距 09:35 快照 >10min → 日内漂移不得误判分母。"""
    late = {"ts": "2026-09-08T10:47:00-04:00", "symbol": "US.BIDU", "inst_id": 2353,
            "features": {"change_percent": 4.07}}
    info, hits = _run(monkeypatch, tmp_path, [_BIDU_PMI], [late],
                      baseline=_baseline(tmp_path, []))
    assert info["status"] == "ok" and hits == []
    assert info["paired"] == 0 and info["unpaired"] == 1


def test_in_window_min_delta_suppresses_false_positive(tmp_path, monkeypatch):
    """⑥窗内取最小差: 分母一致时最接近时刻的读数 ≈0 — 同窗后行漂移不得当分叉。"""
    later_ok = {"ts": "2026-09-08T09:41:00-04:00", "symbol": "US.BIDU", "inst_id": 2353,
                "features": {"change_percent": -5.0}}  # 6 分钟后价格漂移 2.65pp
    info, hits = _run(monkeypatch, tmp_path, [_BIDU_PMI],
                      [{"ts": "2026-09-08T09:35:20-04:00", "symbol": "US.BIDU",
                        "inst_id": 2353, "features": {"change_percent": -7.60}}, later_ok],
                      baseline=_baseline(tmp_path, []))
    assert info["status"] == "ok" and hits == [], "窗内存在同读数行 ⇒ 分母一致, 不得告警"


def test_inst_id_join_does_not_cross_contaminate(tmp_path, monkeypatch):
    """⑦join 精度: 同 symbol 同 date 但不同 inst_id 的行不得经兜底 key 串染。"""
    other_inst = {"ts": "2026-09-08T09:35:25-04:00", "symbol": "US.BIDU", "inst_id": 2379,
                  "features": {"change_percent": 4.07}}
    info, hits = _run(monkeypatch, tmp_path, [_BIDU_PMI], [other_inst],
                      baseline=_baseline(tmp_path, []))
    assert info["status"] == "ok" and hits == []
    assert info["paired"] == 0 and info["unpaired"] == 1, "inst 2353 ≠ 2379, 不得串"


def test_symbol_date_fallback_when_inst_id_missing(tmp_path, monkeypatch):
    """⑦兜底 key: pmi 行缺 inst_id 时按 (symbol,date) join (spec: inst_id/(symbol,date))。"""
    pmi_noinst = {k: v for k, v in _BIDU_PMI.items() if k != "inst_id"}
    ets_noinst = {"ts": "2026-09-08T09:35:25-04:00", "symbol": "US.BIDU",
                  "features": {"change_percent": 4.07}}
    info, hits = _run(monkeypatch, tmp_path, [pmi_noinst], [ets_noinst],
                      baseline=_baseline(tmp_path, []))
    assert info["status"] == "drift"
    assert hits and hits[0][1] == "2026-09-08 US.BIDU#noinst"


def test_missing_shadow_file_is_deployment_shape_not_detector_failure(tmp_path, monkeypatch):
    """④影子档未部署/退役清场 → skipped_no_shadow 静默 (与 skipped_old_schema 同纪律)。"""
    monkeypatch.setattr(dic, "_W7_PMI", tmp_path / "absent_pmi.jsonl")
    monkeypatch.setattr(dic, "_W7_ETS", tmp_path / "absent_ets.jsonl")
    monkeypatch.setattr(dic, "_W7_BASELINE", tmp_path / "absent_baseline.json")
    hits = []
    info = dic._check_chg_denominator(lambda k, rid, sym, msg: hits.append(k))
    assert info["status"] == "skipped_no_shadow" and hits == []


def test_whole_file_corrupt_is_loud(tmp_path, monkeypatch):
    """⑧整档不可读 ≠ 零违反: 哨兵失明面 100% 必须 W7_detector_error (D1/D2 同课)。"""
    corrupt = tmp_path / "ets.jsonl"
    corrupt.write_text("not json\nalso not json\n", encoding="utf-8")
    monkeypatch.setattr(dic, "_W7_PMI", _write(tmp_path / "pmi.jsonl", [_BIDU_PMI]))
    monkeypatch.setattr(dic, "_W7_ETS", corrupt)
    monkeypatch.setattr(dic, "_W7_BASELINE", tmp_path / "absent_baseline.json")
    hits = []
    info = dic._check_chg_denominator(lambda k, rid, sym, msg: hits.append((k, msg)))
    assert info["status"] == "detector_error"
    assert [h[0] for h in hits] == ["W7_detector_error"]
    assert "不可信" in hits[0][1]


def test_single_bad_line_tolerated_not_blinding(tmp_path, monkeypatch):
    """⑨单行坏行只计数: 哨兵面对 4050 行级别文件不得因个别损坏行整体失明。"""
    ets_path = tmp_path / "ets.jsonl"
    ets_path.write_text(
        "{broken\n" + json.dumps(_BIDU_ETS_FROZEN) + "\n", encoding="utf-8")
    monkeypatch.setattr(dic, "_W7_PMI", _write(tmp_path / "pmi.jsonl", [_BIDU_PMI]))
    monkeypatch.setattr(dic, "_W7_ETS", ets_path)
    monkeypatch.setattr(dic, "_W7_BASELINE", tmp_path / "absent_baseline.json")
    hits = []
    info = dic._check_chg_denominator(lambda k, rid, sym, msg: hits.append(k))
    assert info["status"] == "baseline_established", "好行仍可判, 首跑建基线"
    assert info["bad_lines"] == {"pmi": 0, "ets": 1} and hits == []


def test_healthy_pair_zero_divergence(tmp_path, monkeypatch):
    """健康对照: DDOG 9/3 真实形态 (Δ=0.17pp) 永不进 divergent — 防哨兵本身造假阳。"""
    info, hits = _run(monkeypatch, tmp_path, [_DDOG_PMI], [_DDOG_ETS],
                      baseline=_baseline(tmp_path, []))
    assert info["status"] == "ok" and hits == []
    assert info["divergent"] == [] and info["paired"] == 1
