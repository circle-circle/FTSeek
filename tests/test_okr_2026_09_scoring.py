"""修尺子批③钉子 (2026-09-06, 用户裁「需要」): OKR 判分面板接现行判据 (OKR-2026-09)。

病灶: `scripts/okr_status.py::calc_okr_score` 判的是 **OKR-2026-08 的 KR 集**
(win_rate 55% / weekly_pnl $1000 / sample 30 / IC(T+3) 0.05 / trigger 50%), 而该 OKR 已于
2026-08-23 **提前结算为 SUPERSEDED**; 现行判据是 `docs/OKR-2026-09.md` 的 KR1 修尺子 /
KR2 参数冻结 / KR3 预注册找 edge / KR4 实盘硬门 / KR5 学费明码 ⇒ 面板头条 `overall_score=60`
与现行判据**不同源**, 且 KR2/KR4 恒 0 是"已废目标"算出来的 (看着像"没做到", 其实是量错了东西)。

治法=**并列不替代** (旧块键名被 `scripts/okr_query.py:58,168` 消费 + `okr_history.jsonl`
有时序连续性, 改键=断消费方+造假时序), 判分逻辑拆到可 import 的纯函数模块
`scripts/okr_2026_09_scoring.py` (okr_status.py 是模块级执行体, import 即跑全部 SQL,
批② 只能抽源码字符串 — 那种钉子钉不住判分逻辑)。

本文件钉住的语义:
  Ⓐ **不新增第四把尺子**: KR2 冻结测量已有两处实现 (周六周检 / 日级守卫两维+豁免对账),
     新块只**读** `data/invariant_check_latest.json` — 再写一份 SQL 即口径漂移;
  Ⓑ 四锁字面值/env 载体/冻结窗起点与 `ops/hermes/ftseek_weekly_checkpoint.py` **逐字同源**;
  Ⓒ **不可测 ≠ 0 ≠ 达成**: 产物缺失/过期/判据要素未落地 → NOT_MEASURABLE + score=None;
     空仓 (读不到四锁) 必须 **alarm**, 不得因"没证据"而放行;
  Ⓓ **待裁项不自评**: OKR-2026-09 KR2 明写「待用户裁决 (不得擅自结算)」⇒ 即使窗内只有
     1 个版本 (字面达成) 也恒 PENDING_USER_RULING, 只报实测事实;
  Ⓔ 日级守卫 `exit_status='ok'` 是**豁免后**口径, 不得冒充 KR2 达成 (原判分式查档案列
     恒 'v2' = 结构性假绿, OKR-2026-09 KR2 勘误 2026-09-03);
  Ⓕ **不给 9 月百分比总分** (五个二元/待裁项取算术平均 = 假精度, 旧块 60% 即此病);
  Ⓖ 旧块 (`kr_scores` 五键 + `overall_score`) **一字不改** — 新块只准追加键;
  Ⓗ 每个 KR 块必带 unit/source/evidence/gate (军规: 新落库字段带单位声明+基数自检)。

回滚: 删本文件 + revert 对应提交 (只读判分, 零退出参数/零四锁/零交易行为变更, 符合 C7)。
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import okr_2026_09_scoring as sc  # noqa: E402

_OKR_STATUS_SRC = (PROJECT_ROOT / "scripts" / "okr_status.py").read_text(encoding="utf-8")
_SCORING_SRC = (PROJECT_ROOT / "scripts" / "okr_2026_09_scoring.py").read_text(encoding="utf-8")
_CHECKPOINT_SRC = (PROJECT_ROOT / "ops" / "hermes" / "ftseek_weekly_checkpoint.py").read_text(encoding="utf-8")
_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


# ───────────────────────── 合成仓 (只喂载体, 不碰生产 data/) ─────────────────────────
def _repo(tmp_path, env=None, cfg="  execute_mode: futu_simulate\n", invariant=None,
          baseline=None, kr3=None, exit_sc=None, prereg_docs=sc.KR3_PREREG_DOCS,
          checkpoint=True, registry_text="REGISTRY = []\n"):
    root = tmp_path
    (root / "config").mkdir(exist_ok=True)
    (root / "data" / "shadow").mkdir(parents=True, exist_ok=True)
    (root / "ops" / "hermes").mkdir(parents=True, exist_ok=True)
    (root / "app" / "playbook").mkdir(parents=True, exist_ok=True)
    if env is not None:
        (root / sc.ENV_FILE).write_text(env, encoding="utf-8")
    if cfg is not None:
        (root / sc.CONFIG_FILE).write_text(cfg, encoding="utf-8")
    for key, doc in (("invariant", invariant), ("freeze_baseline", baseline),
                     ("kr3_verdict", kr3), ("exit_scorecard", exit_sc)):
        if doc is not None:
            (root / sc.ARTIFACTS[key][0]).write_text(json.dumps(doc, ensure_ascii=False),
                                                     encoding="utf-8")
    for rel in prereg_docs:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# prereg\n", encoding="utf-8")
    if checkpoint:
        (root / sc.WEEKLY_CHECKPOINT).write_text("# checkpoint\n", encoding="utf-8")
    (root / sc.SHADOW_REGISTRY).write_text(registry_text, encoding="utf-8")
    return root


_ENV_CLOSED = "AUTO_TRADE_ACCOUNT=sim\nALLOW_REAL_TRADING=false\n"


def _cohort(versions=None, n_unique=None, status="single_version", missing_rows=3):
    """主 cohort 窗块 (日级守卫 2026-09-06 起产出; KR2 修订判据的权威载体)。"""
    v = versions if versions is not None else {
        "exit-de49b1299290": {"n": 23, "first": "2026-09-03", "last": "2026-09-04"}}
    return {"window_start": sc.COHORT_START, "versions": v,
            "n_unique": len(v) if n_unique is None else n_unique,
            "hash_missing_rows": missing_rows, "status": status}


def _invariant(versions=None, exit_status="ok", status="ok", missing_rows=0, age_h=1.0,
               cohort="default"):
    cf = {"window_start": sc.OKR_WINDOW_START,
          "versions": {"v2": 118},
          "exit_versions": versions if versions is not None else {
              "exit-7f5263af3cfd": 58, "exit-2623be9c8879": 21,
              "exit-f837b002896d": 12, "exit-de49b1299290": 24},
          "exit_hash_missing_rows": missing_rows,
          "exit_status": exit_status, "status": status}
    if cohort == "default":
        cf["cohort"] = _cohort()
    elif cohort is not None:
        cf["cohort"] = cohort
    return {"checked_at": (_NOW - timedelta(hours=age_h)).isoformat(), "config_freeze": cf}


def _baseline(exit_versions=None):
    return {"versions": ["v2"],
            "exit_versions": {v: "豁免" for v in (exit_versions or [
                "exit-7f5263af3cfd", "exit-2623be9c8879", "exit-f837b002896d",
                "exit-de49b1299290", "exit-d1120a105711"])}}


_IC_PB_OK = {"ic_t3": -0.0331, "graduation": "ACCUMULATING", "graduation_deprecated": True,
             "day_clustered": {k: {"day_ic_mean": 0.06, "t": 0.5, "mde": 0.35, "n_days": 11}
                               for k in ("intraday", "t1", "t3", "t5")}}
_EXIT_SC_OK = {"size_normalized": {"net_bps_day_clustered": {"t": -0.1, "mde_80pct": 35.41,
                                                             "n_days": 21, "day_mean": -1.32}}}
_TRADE_STATS = {"cnt": 224, "cnt_bps": 221, "fee_bps": 8.95, "avg_notional": 5542.5,
                "bps_suspect": False}


def _score(tmp_path, **kw):
    args = {"env": _ENV_CLOSED, "invariant": _invariant(), "baseline": _baseline(),
            "exit_sc": _EXIT_SC_OK}
    args.update(kw.pop("repo", {}))
    root = _repo(tmp_path, **args)
    params = {"ic_playbook": _IC_PB_OK, "graduation": "ACCUMULATING",
              "graduation_deprecated": True, "trade_stats": _TRADE_STATS,
              "window_days": 30, "now": _NOW, "today": "2026-09-06",
              "cohort_settled": 23}
    params.update(kw)
    return sc.collect_and_score(root, **params)


# ══════════════ Ⓐ 不新增第四把尺子: KR2 只读日级守卫产物 ══════════════
def test_kr2_reads_guard_artifact_instead_of_reimplementing_sql():
    """KR2 冻结测量已有两处实现 (周六周检 + 日级守卫两维), 第三处即口径漂移。"""
    assert "invariant_check_latest.json" in _SCORING_SRC, "必须读日级守卫产物 (SSOT)"
    assert "json_extract(metadata_json" not in _SCORING_SRC, (
        "新判分模块里出现了退出哈希 SQL — KR2 测量已有 weekly_checkpoint.check_freeze 与 "
        "daily_invariant_check._check_config_freeze 两处, 再写一份就是第四把尺子 "
        "(批② 三腿取样面合一的同一教训)")
    # okr_status.py 里那一处 json_extract 是**毕业样本单版本窗口**取样 (line 180, 本批之前就存在),
    # 与 KR2 冻结计数无关; 钉"处数不增"即可 — 接线新块不得顺手加一份冻结 SQL。
    assert _OKR_STATUS_SRC.count("json_extract(metadata_json") == 1, (
        "okr_status.py 的退出哈希 SQL 处数变了 (基线 1 = 毕业样本窗口) — 若为 KR2 新增即第四把尺子")


def test_kr2_evidence_comes_from_guard_block(tmp_path):
    """两个口径的实测值都必须透出 (cohort 判据维 + 全窗留痕维), 且都来自守卫产物。"""
    b = _score(tmp_path)["krs"]["KR2"]
    ev = b["evidence"]
    assert ev["cohort_window_start"] == "2026-09-03" and ev["cohort_n_unique_versions"] == 1
    assert ev["cohort_status"] == "single_version" and ev["cohort_hash_missing_rows"] == 3
    assert ev["full_window_n_unique_versions"] == 4
    assert ev["daily_guard_exit_status"] == "ok"
    assert ev["full_window_unexempted_drift"] == [] and len(ev["full_window_exempt"]) == 4
    assert ev["okr_original_estimate_n"] == 215, "OKR 自算的 215 笔必须留痕 (对照实测 n)"


# ══════════════ Ⓑ 四锁/冻结窗与周六周检逐字同源 ══════════════
def test_lock_literals_match_weekly_checkpoint():
    for key, val in (("AUTO_TRADE_ACCOUNT", "sim"), ("ALLOW_REAL_TRADING", "false"),
                     ("execute_mode", "futu_simulate")):
        assert sc.LOCK_EXPECT[key] == val
        assert re.search(rf'"{key}"', _CHECKPOINT_SRC) or key in _CHECKPOINT_SRC, key
    # 周检的判定字面值必须与本模块一致 (否则两处四锁判据分叉 = 一处红一处绿)
    assert '"AUTO_TRADE_ACCOUNT"] == "sim"' in _CHECKPOINT_SRC.replace("locks[", '"') or \
        'locks["AUTO_TRADE_ACCOUNT"] == "sim"' in _CHECKPOINT_SRC
    assert 'locks["ALLOW_REAL_TRADING"] == "false"' in _CHECKPOINT_SRC
    assert '"execute_mode: futu_simulate" in cfg' in _CHECKPOINT_SRC
    assert sc.ENV_FILE == ".env.us" and ".env.us" in _CHECKPOINT_SRC


def test_freeze_window_start_matches_weekly_checkpoint():
    m = re.search(r'FREEZE_START\s*=\s*"(\d{4}-\d{2}-\d{2})"', _CHECKPOINT_SRC)
    assert m, "周检里的冻结窗起点被删了?"
    assert sc.OKR_WINDOW_START == m.group(1), (
        f"冻结窗起点分叉: 本模块 {sc.OKR_WINDOW_START} vs 周检 {m.group(1)} — "
        f"两处窗口不同则唯一值数不可比")


# ══════════════ Ⓒ 不可测 ≠ 0 ≠ 达成 (fail-close) ══════════════
def test_empty_repo_is_unmeasurable_and_locks_alarm(tmp_path):
    """空仓: 载体全缺 + 四锁读不到 ⇒ 三个 KR 不可测, 且四锁必须 **alarm** (无证据≠放行)。"""
    root = _repo(tmp_path, env=None, cfg=None, checkpoint=False)
    b = sc.collect_and_score(root, ic_playbook=None, trade_stats=None, now=_NOW)
    krs = b["krs"]
    assert krs["KR1"]["verdict"] == sc.VERDICT_NOT_MET      # 尺子没修好 = 未达成, 不是"没数据"
    assert krs["KR2"]["verdict"] == sc.VERDICT_UNMEASURABLE and krs["KR2"]["score"] is None
    assert krs["KR3"]["verdict"] == sc.VERDICT_UNMEASURABLE and krs["KR3"]["score"] is None
    assert krs["KR4"]["alarm"] is True and krs["KR4"]["verdict"] == sc.VERDICT_NOT_MET
    assert krs["KR5"]["verdict"] == sc.VERDICT_UNMEASURABLE
    assert all(krs[k]["score"] is not True for k in krs)     # 不得用 True/1 冒充


def test_stale_guard_artifact_is_unmeasurable(tmp_path):
    inv = _invariant(age_h=200)                              # 阈 96h (cron 05:05 * * 2-6)
    b = _score(tmp_path, repo={"invariant": inv})
    kr2 = b["krs"]["KR2"]
    assert kr2["verdict"] == sc.VERDICT_UNMEASURABLE
    assert "stale" in (kr2["note"] or "")
    assert b["artifacts"]["invariant"]["error"].startswith("stale")


def test_bad_json_artifact_is_explicit_not_silent(tmp_path):
    root = _repo(tmp_path, env=_ENV_CLOSED)
    (root / sc.ARTIFACTS["invariant"][0]).write_text("{不是 JSON", encoding="utf-8")
    art = sc.read_artifact(root, "invariant", now=_NOW)
    assert art["doc"] is None and art["error"].startswith("bad_json")
    kr2 = sc.kr2_freeze(invariant=art, baseline={"doc": _baseline()})
    assert kr2["verdict"] == sc.VERDICT_UNMEASURABLE and "bad_json" in kr2["note"]


def test_missing_timestamp_artifact_is_unmeasurable(tmp_path):
    root = _repo(tmp_path, env=_ENV_CLOSED)
    (root / sc.ARTIFACTS["invariant"][0]).write_text(
        json.dumps({"config_freeze": {"exit_versions": {"exit-x": 1}}}), encoding="utf-8")
    art = sc.read_artifact(root, "invariant", now=_NOW)
    assert art["error"] and "no_timestamp" in art["error"]


# ══════════════ Ⓓ/Ⓔ KR2 cohort 修订判据 (2026-09-06 用户授权代裁) ══════════════
def test_kr2_cohort_start_pinned_to_daily_guard():
    """同源钉子: cohort 窗起点若与日级守卫分叉, 两处判的就不是同一个窗。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dic_under_test", PROJECT_ROOT / "scripts" / "daily_invariant_check.py")
    dic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dic)
    assert sc.COHORT_START == dic._FREEZE_COHORT_START == "2026-09-03"
    assert sc.OKR_WINDOW_START == dic._FREEZE_WINDOW_START == "2026-08-24"


def test_kr2_accumulating_while_window_open(tmp_path):
    """窗未闭合 (< 9/30) 且 cohort 单版本 ⇒ ACCUMULATING, **不得提前判 MET** (乐观自评)。"""
    kr2 = _score(tmp_path)["krs"]["KR2"]
    assert kr2["verdict"] == sc.VERDICT_ACCUMULATING and kr2["score"] is None
    ev = kr2["evidence"]
    assert ev["cohort_n_unique_versions"] == 1 and ev["window_closed"] is False
    assert ev["cohort_settled_n"] == 23 and ev["cohort_settled_target"] == 200
    assert ev["change_budget_remaining"] == 0 and ev["exempt_channel_for_cohort"] == "CLOSED"
    assert "零换版" in kr2["note"] and "不提前自评" in kr2["note"]
    assert "尚差 177 笔" in kr2["note"], "功效提示必须给差额, 不给数字的进度条是装饰"


def test_kr2_met_only_after_window_closes(tmp_path):
    kr2 = _score(tmp_path, today="2026-09-30")["krs"]["KR2"]
    assert kr2["verdict"] == sc.VERDICT_MET and kr2["score"] == 100.0
    assert kr2["evidence"]["window_closed"] is True
    assert "多版本期" in kr2["note"], "MET 也必须留 8/24→9/2 四版的痕 (不隐瞒)"


def test_kr2_broken_and_alarms_when_second_hash_enters_cohort(tmp_path):
    """预注册回滚线: cohort 窗内出现第 2 个哈希 ⇒ BROKEN + alarm, 豁免清单**不适用**本维。"""
    two = _cohort(versions={"exit-de49b1299290": {"n": 23, "first": "2026-09-03", "last": "2026-09-04"},
                            "exit-cccccccccccc": {"n": 2, "first": "2026-09-08", "last": "2026-09-08"}},
                  status="broken")
    kr2 = _score(tmp_path, repo={"invariant": _invariant(cohort=two)})["krs"]["KR2"]
    assert kr2["verdict"] == sc.VERDICT_NOT_MET and kr2["alarm"] is True and kr2["score"] == 0.0
    assert kr2["evidence"]["cohort_n_unique_versions"] == 2
    assert "BROKEN" in kr2["note"] and "豁免清单**不适用**" in kr2["note"]
    assert "样本作废" in kr2["note"] and "10 月开跑会" in kr2["note"]


def test_kr2_missing_cohort_block_is_unmeasurable(tmp_path):
    """旧版守卫产物 (无 cohort 块) ⇒ 不可测 + 指路重跑, 不得用全窗维冒充 cohort 判分。"""
    kr2 = _score(tmp_path, repo={"invariant": _invariant(cohort=None)})["krs"]["KR2"]
    assert kr2["verdict"] == sc.VERDICT_UNMEASURABLE and kr2["score"] is None
    assert "cohort" in kr2["note"] and "daily_invariant_check.py" in kr2["note"]


def test_kr2_unexempted_drift_alarms_and_names_the_version(tmp_path):
    inv = _invariant(versions={"exit-de49b1299290": 24, "exit-NEW0000000": 3})
    kr2 = _score(tmp_path, repo={"invariant": inv})["krs"]["KR2"]
    assert kr2["alarm"] is True
    assert kr2["evidence"]["full_window_unexempted_drift"] == ["exit-NEW0000000"]
    assert "exit-NEW0000000" in kr2["note"]
    assert kr2["verdict"] == sc.VERDICT_ACCUMULATING, (
        "全窗漂移属多版本期留痕 + alarm; cohort 判据本身仍单版本 ⇒ 不混判 (两口径并列)")


def test_kr2_guard_ok_must_not_impersonate_kr2_met(tmp_path):
    """守卫 exit_status='ok' 是**豁免后**口径 (原判分式查档案列恒 v2 = 结构性假绿)。"""
    kr2 = _score(tmp_path)["krs"]["KR2"]
    assert kr2["evidence"]["daily_guard_exit_status"] == "ok"
    assert kr2["verdict"] != sc.VERDICT_MET
    assert "不得冒充本判据达成" in kr2["note"]
    assert kr2["evidence"]["full_window_n_unique_versions"] == 4
    assert len(kr2["evidence"]["full_window_exempt"]) == 4


# ══════════════ KR1 修尺子 (a/c/d 数据判, b 呈现层由源码钉) ══════════════
def test_kr1_met_when_data_checks_pass(tmp_path):
    kr1 = _score(tmp_path)["krs"]["KR1"]
    assert kr1["verdict"] == sc.VERDICT_MET
    ids = {c["id"]: c for c in kr1["evidence"]["checks"]}
    assert ids["a_ic_pooled_and_day_clustered_paired"]["ok"] is True
    assert ids["c_scorecard_has_mde_column"]["ok"] is True
    assert ids["d_confidence_ic_retired_as_graduation_gate"]["ok"] is True
    assert ids["b_headline_is_day_clustered_not_subset_ic"]["ok"] is None, (
        "呈现层项不得由数据判分 (拿测不到的东西充数)")
    assert kr1["evidence"]["judged"] == 3


def test_kr1_not_met_when_mde_column_missing(tmp_path):
    kr1 = _score(tmp_path, repo={"exit_sc": {"size_normalized": {}}})["krs"]["KR1"]
    assert kr1["verdict"] == sc.VERDICT_NOT_MET and kr1["score"] == 0.0


def test_kr1_not_met_when_day_clustered_absent(tmp_path):
    ic = {"ic_t3": -0.03, "graduation_deprecated": True}      # 只有 pooled = 宪章第 9 律违例
    kr1 = _score(tmp_path, ic_playbook=ic)["krs"]["KR1"]
    assert kr1["verdict"] == sc.VERDICT_NOT_MET


def test_kr1_headline_caliber_pinned_in_source():
    """b 项 (头条口径) 的钉子: 旧块头条必须标 SUPERSEDED, signal IC 必须降级为 diag。"""
    assert "SUPERSEDED" in _OKR_STATUS_SRC and "OVERALL PROGRESS" in _OKR_STATUS_SRC
    assert "[Signal diag]" in _OKR_STATUS_SRC and "[Playbook SSOT]" in _OKR_STATUS_SRC
    assert "okr_2026_09" in _OKR_STATUS_SRC and "kr_scores_caliber" in _OKR_STATUS_SRC


# ══════════════ KR3 预注册 (只许一个 + 判据三要素落地) ══════════════
def test_kr3_elements_absent_is_by_design_not_a_ruler_defect(tmp_path):
    """**归因更正**: 三要素缺席是 RFC-072 §六.3 明令 (判分代码不得预先实现), 不是尺子病灶。"""
    kr3doc = {"generated_at": _NOW.isoformat(), "n_collected": 44, "n_eval": 41,
              "prereg": "RFC-072: would-block 子集净期望显著更差 (均值口径)",
              "groups": {"LONG|blocked": {"n_eval": 15, "mean": 6.19, "median": 14.88},
                         "LONG|pass": {"n_eval": 15, "mean": 20.88},
                         "SHORT|blocked": {"n_eval": 8, "mean": -25.42},
                         "SHORT|pass": {"n_eval": 3, "mean": 50.43}},
              "verdict_by_direction": {"LONG": "DIRECTIONAL", "SHORT": "DIRECTIONAL"}}
    kr3 = _score(tmp_path, repo={"kr3": kr3doc})["krs"]["KR3"]
    ev = kr3["evidence"]
    assert sorted(ev["adjudication_elements_pending"]) == ["day_clustered_t", "mde", "out_of_sample"]
    assert "§六.3" in ev["elements_absent_by_design"] and "不是病灶" in ev["elements_absent_by_design"]
    assert "不实现判分代码" in kr3["note"] or "本批不实现判分代码" in kr3["note"]
    assert kr3["verdict"] == sc.VERDICT_ACCUMULATING, "样本门未达 ⇒ 攒样, 不是不可测"


def test_kr3_sample_gate_counts_blocked_subset_only(tmp_path):
    """§三.5 的 n 数的是 **would-block 子集**已结算笔数, 不是总行数 (44 行会假满足)。"""
    kr3doc = {"generated_at": _NOW.isoformat(), "n_collected": 44, "n_eval": 41,
              "groups": {"LONG|blocked": {"n_eval": 15}, "SHORT|blocked": {"n_eval": 8},
                         "LONG|pass": {"n_eval": 15}, "SHORT|pass": {"n_eval": 3}}}
    kr3 = _score(tmp_path, repo={"kr3": kr3doc})["krs"]["KR3"]
    sg = kr3["evidence"]["sample_gate"]
    assert sg == {"blocked_n": 23, "min_n": 30, "met": False, "pass_n": 18,
                  "rule": sg["rule"]}
    assert kr3["verdict"] == sc.VERDICT_ACCUMULATING and "n=23 < 30" in kr3["note"]
    assert "延长观察" in kr3["note"]


def test_kr3_met_when_final_adjudication_artifact_present(tmp_path):
    """期末裁决批产物存在 (LONG=FALSIFIED) ⇒ KR3=MET, 不得再报 READY「可开裁决批」。

    实锤 2026-10-06: 10/4 裁决批已落地 (kr3_final_adjudication.json), 面板仍报
    READY_TO_ADJUDICATE 催「可开独立裁决批」= 陈旧催裁。OKR-2026-09 §结算判据:
    干净的确认或证伪都算达成。
    """
    kr3doc = {"generated_at": _NOW.isoformat(),
              "groups": {"LONG|blocked": {"n_eval": 43}, "SHORT|blocked": {"n_eval": 23},
                         "LONG|pass": {"n_eval": 74}, "SHORT|pass": {"n_eval": 19}}}
    finaldoc = {"generated_at": "2026-10-04T21:47:51",
                "legs": [
                    {"direction": "LONG", "n_blocked": 43, "verdict": "FALSIFIED"},
                    {"direction": "SHORT", "n_blocked": 23,
                     "verdict": "INSUFFICIENT_SAMPLE_DO_NOT_ADJUDICATE"}],
                "window": {"end": "2026-09-30"}}
    root = _repo(tmp_path, kr3=kr3doc)
    (root / sc.ARTIFACTS["kr3_final"][0]).write_text(json.dumps(finaldoc), encoding="utf-8")
    b = sc.collect_and_score(repo_root=root, now=_NOW)
    kr3 = b["krs"]["KR3"]
    assert kr3["verdict"] == sc.VERDICT_MET and kr3["score"] == 100.0
    assert "FALSIFIED" in kr3["note"] and "达成" in kr3["note"]
    assert kr3["evidence"]["final_adjudication"]["adjudicated"] is True
    # settlement: KR3 MET 后不再进 accumulating
    assert "KR3" not in b["settlement"]["accumulating"]


def test_kr3_final_artifact_insufficient_only_stays_accumulating(tmp_path):
    """产物存在但两腿都无干净裁决 (如全 INSUFFICIENT) ⇒ 不算达成, 走原样本门分支。"""
    kr3doc = {"generated_at": _NOW.isoformat(),
              "groups": {"LONG|blocked": {"n_eval": 22}, "SHORT|blocked": {"n_eval": 9}}}
    finaldoc = {"generated_at": "2026-10-04T21:47:51",
                "legs": [{"direction": "LONG", "n_blocked": 22,
                          "verdict": "INSUFFICIENT_SAMPLE_DO_NOT_ADJUDICATE"},
                         {"direction": "SHORT", "n_blocked": 9,
                          "verdict": "INSUFFICIENT_SAMPLE_DO_NOT_ADJUDICATE"}]}
    root = _repo(tmp_path, kr3=kr3doc)
    (root / sc.ARTIFACTS["kr3_final"][0]).write_text(json.dumps(finaldoc), encoding="utf-8")
    kr3 = sc.collect_and_score(repo_root=root, now=_NOW)["krs"]["KR3"]
    assert kr3["verdict"] == sc.VERDICT_READY, "无干净裁决事件 ⇒ 维持原 READY 分支"


def test_kr3_ready_to_adjudicate_when_sample_gate_met(tmp_path):
    kr3doc = {"generated_at": _NOW.isoformat(),
              "groups": {"LONG|blocked": {"n_eval": 22}, "SHORT|blocked": {"n_eval": 9},
                         "LONG|pass": {"n_eval": 20}, "SHORT|pass": {"n_eval": 5}}}
    kr3 = _score(tmp_path, repo={"kr3": kr3doc})["krs"]["KR3"]
    assert kr3["evidence"]["sample_gate"]["met"] is True
    assert kr3["verdict"] == sc.VERDICT_READY and kr3["score"] is None
    assert "现写现验" in kr3["note"] and "干净证伪" in kr3["note"]
    assert "不立即换假设" in kr3["note"], "证伪后防链式 fishing 是 OKR 明令, 必须写在脸上"


def test_kr3_missing_artifact_is_unmeasurable(tmp_path):
    kr3 = _score(tmp_path)["krs"]["KR3"]
    assert kr3["verdict"] == sc.VERDICT_UNMEASURABLE and "载体不可用" in kr3["note"]


def test_kr3_more_than_one_hypothesis_is_not_met(tmp_path):
    two = ("docs/decisions/RFC-072-kr3-direction-runup-gate-preregistration.md",
           "docs/decisions/RFC-099-second-hypothesis-preregistration.md")
    kr3doc = {"generated_at": _NOW.isoformat(),
              "groups": {"LONG|blocked": {"n_eval": 22}, "SHORT|blocked": {"n_eval": 9}}}
    root = _repo(tmp_path, env=_ENV_CLOSED, prereg_docs=two)
    kr3 = sc.kr3_prereg(kr3={"doc": kr3doc, "error": None, "age_hours": 0.0}, repo_root=root,
                        prereg_docs=two)
    assert kr3["evidence"]["hypothesis_count"] == 2 and kr3["verdict"] == sc.VERDICT_NOT_MET
    assert "七路并发改动重演" in kr3["note"]


def test_kr3_surfaces_missing_shadow_registry_entry(tmp_path):
    """注册表纪律: 新增影子必须注册, 否则无人跟踪 = 等于没做 (实测 KR3 影子 0 命中)。"""
    kr3 = _score(tmp_path, repo={"registry_text": "REGISTRY = []\n"})["krs"]["KR3"]
    assert kr3["evidence"]["registered_in_shadow_registry"] is False
    assert "未在 `app/playbook/shadow_registry.py` 注册" in kr3["note"]
    ok = _score(tmp_path, repo={"registry_text": "# RFC-072 runup gate\n"})["krs"]["KR3"]
    assert ok["evidence"]["registered_in_shadow_registry"] is True


# ══════════════ KR4 实盘硬门 (四锁任一松 = alarm) ══════════════
def test_kr4_lock_breach_alarms(tmp_path):
    b = _score(tmp_path, repo={"env": "AUTO_TRADE_ACCOUNT=sim\nALLOW_REAL_TRADING=true\n"})
    kr4 = b["krs"]["KR4"]
    assert kr4["alarm"] is True and kr4["verdict"] == sc.VERDICT_NOT_MET
    assert kr4["evidence"]["clauses"][0]["ok"] is False
    assert "四锁松动" in kr4["note"]


def test_kr4_non_simulate_execute_mode_alarms(tmp_path):
    b = _score(tmp_path, repo={"cfg": "  execute_mode: futu_live\n"})
    assert b["locks"]["observed"]["execute_mode"] == "NOT_SIMULATE"
    assert b["krs"]["KR4"]["alarm"] is True


def test_kr4_graduated_true_breaks_fail_close(tmp_path):
    b = _score(tmp_path, graduation="GRADUATED")
    assert b["locks"]["closed"]["graduation_fail_close"] is False
    assert b["krs"]["KR4"]["alarm"] is True


def test_kr4_met_only_when_all_clauses_pass(tmp_path):
    kr4 = _score(tmp_path, gate_redefined=True)["krs"]["KR4"]
    assert kr4["verdict"] == sc.VERDICT_MET and kr4["alarm"] is False
    assert kr4["evidence"]["passed"] == kr4["evidence"]["judged"] == 3
    assert "四锁全闭" in kr4["note"], "全闭时的 note 不得读起来像报警"


def test_kr4_gate_not_redefined_is_not_met_but_safe(tmp_path):
    kr4 = _score(tmp_path)["krs"]["KR4"]
    assert kr4["verdict"] == sc.VERDICT_NOT_MET and kr4["alarm"] is False
    assert "fail-close" in kr4["note"]


# ══════════════ KR5 学费 (非门禁, 单位声明) ══════════════
def test_kr5_tuition_is_priced_with_unit(tmp_path):
    kr5 = _score(tmp_path)["krs"]["KR5"]
    ev = kr5["evidence"]
    assert kr5["verdict"] == sc.VERDICT_MET and "非门禁" in kr5["gate"]
    assert ev["fee_per_trade_usd"] == pytest.approx(8.95 / 10000 * 5542.5, abs=0.01)
    assert ev["trades_per_week"] == pytest.approx(221 / 30 * 7, abs=0.1)
    assert ev["weekly_tuition_usd"] == pytest.approx(ev["fee_per_trade_usd"] * ev["trades_per_week"],
                                                     abs=0.5)
    assert "USD/周" in kr5["unit"]


def test_kr5_bps_sentinel_blocks_pricing(tmp_path):
    """批② 的 bps 哨兵: 量纲错时不得把天文数字当学费标价出去。"""
    ts = dict(_TRADE_STATS, bps_suspect=True, fee_bps=1.5e6)
    kr5 = _score(tmp_path, trade_stats=ts)["krs"]["KR5"]
    assert kr5["verdict"] == sc.VERDICT_UNMEASURABLE and "哨兵" in kr5["note"]


# ══════════════ Ⓕ 不给 9 月百分比总分 + Ⓗ 单位/来源声明 ══════════════
def test_no_numeric_total_for_2026_09(tmp_path):
    b = _score(tmp_path)
    assert "overall_score" not in b and "overall_score_2026_09" not in b
    st = b["settlement"]
    assert isinstance(st["status"], str) and st["no_numeric_total"]
    # 必需项 (KR1/KR2/KR4) 里非 MET 的都算 blocking: KR2 待裁 + KR4 门禁重定义未完成
    # 当日投影: KR4 门禁条款未完成 = 决定性 NOT_MET; KR2/KR3 窗未闭合列在 accumulating
    assert st["status"] == sc.VERDICT_NOT_MET and st["not_met"] == ["KR4"]
    assert st["blocking"] == ["KR2", "KR4"] and st["accumulating"] == ["KR2"]
    assert st["unmeasurable"] == ["KR3"], "合成仓无 KR3 载体 ⇒ 不可测 (与攒样必须可区分)"
    assert st["window_closed"] is False and st["as_of"] == "2026-09-06"
    assert st["settlement_date"] == "2026-09-30"


def test_settlement_not_met_when_kr1_fails(tmp_path, monkeypatch):
    krs = {"KR1": {"verdict": sc.VERDICT_NOT_MET}, "KR2": {"verdict": sc.VERDICT_MET},
           "KR3": {"verdict": sc.VERDICT_MET}, "KR4": {"verdict": sc.VERDICT_MET},
           "KR5": {"verdict": sc.VERDICT_MET}}
    st = sc.settle(krs)
    assert st["status"] == sc.VERDICT_NOT_MET and "KR1" in st["why"]


def test_every_kr_block_declares_unit_source_evidence_gate(tmp_path):
    b = _score(tmp_path)
    assert set(b["krs"]) == {"KR1", "KR2", "KR3", "KR4", "KR5"}
    for kr, blk in b["krs"].items():
        for field in ("unit", "source", "evidence", "gate", "verdict"):
            assert blk.get(field), f"{kr} 缺 {field} — 没落档的判据不得上门 (军规)"
        assert blk["verdict"] in {sc.VERDICT_MET, sc.VERDICT_NOT_MET, sc.VERDICT_PENDING,
                                  sc.VERDICT_UNMEASURABLE, sc.VERDICT_ACCUMULATING,
                                  sc.VERDICT_READY}, f"{kr} verdict 出了词表: {blk['verdict']}"
        assert sc.OKR_DOC in blk["source"], f"{kr} 的来源必须指回判据原文"


def test_render_lines_marks_caliber_and_flags(tmp_path):
    lines = sc.render_lines(_score(tmp_path))
    text = "\n".join(lines)
    assert "OKR-2026-09" in text and "并列不替代" in text
    assert "SUPERSEDED" in text, "必须提醒读者上方百分比是废判据续算值"
    assert "⏳" in text and ("ACCUMULATING" in text or "NOT_MET" in text)
    assert sc.render_lines({"error": "boom"})[0].startswith("⚠️")


# ══════════════ Ⓖ 旧块一字不改 (消费面契约) + 生产产物烟测 ══════════════
def test_legacy_kr_scores_contract_untouched():
    for key in ("kr1_win_rate", "kr2_weekly_pnl", "kr3_sample", "kr4_ic", "kr5_trigger"):
        assert f'scores["{key}"]' in _OKR_STATUS_SRC, f"旧块键 {key} 被改了 — okr_query.py 会断"
    assert '"overall_score": round(overall_score, 1)' in _OKR_STATUS_SRC
    assert '"kr_scores": {k: round(v, 1) for k, v in kr_scores.items()}' in _OKR_STATUS_SRC


def test_production_artifact_carries_both_blocks():
    """真产物烟测 (只读): 旧块五键原样 + 新块并列 + 现行结算状态为待裁。"""
    path = PROJECT_ROOT / "data" / "okr_latest.json"
    if not path.exists():
        pytest.skip("data/okr_latest.json 不存在 (跑 scripts/okr_status.py 生成)")
    d = json.loads(path.read_text(encoding="utf-8"))
    assert set(d["kr_scores"]) == {"kr1_win_rate", "kr2_weekly_pnl", "kr3_sample",
                                   "kr4_ic", "kr5_trigger"}
    assert isinstance(d["overall_score"], (int, float)) and "SUPERSEDED" in d["kr_scores_caliber"]
    b = d["okr_2026_09"]
    assert b["okr"] == "OKR-2026-09" and set(b["krs"]) == {"KR1", "KR2", "KR3", "KR4", "KR5"}
    assert b["settlement"]["status"] in {sc.VERDICT_NOT_MET, sc.VERDICT_ACCUMULATING,
                                         sc.VERDICT_MET}, "结算状态必须是词表内的值"
    assert b["krs"]["KR2"]["evidence"]["cohort_n_unique_versions"] >= 1
    assert b["krs"]["KR2"]["verdict"] in (sc.VERDICT_ACCUMULATING, sc.VERDICT_MET,
                                          sc.VERDICT_NOT_MET)
    assert b["krs"]["KR3"]["evidence"]["sample_gate"]["min_n"] == 30
    # 生产四锁: 全闭, 或**全部松动锁都被有效预注册破例覆盖** (RFC-135, 用户裁 A
    # 2026-10-06)。破例只抑制 alarm, 不改写 closed/all_closed 的实测真相 ⇒ 这里
    # 不得断言 all_closed is True (那会让破例期恒红, 红灯失去信号价值)。
    # 但仍必须严格: 只要有一把松动锁未被授权精确覆盖 (loose_locks 非空),
    # 或 alarm 竟然响着, 就是实盘硬门真破 ⇒ 断言红 + 立即呈裁。
    lk = b["locks"]
    kr4 = b["krs"]["KR4"]
    assert kr4["alarm"] is False, \
        "生产四锁 alarm 响着 — 实盘硬门真破 (或被未覆盖的松动锁触发), 立即呈裁"
    assert lk["all_closed"] is True or (
        lk.get("alarm_suppressed") is True and not lk.get("loose_locks")), \
        ("生产四锁既非全闭, 也未被有效预注册破例完整覆盖 — 立即呈裁。"
         f" closed={lk.get('closed')} loose_locks={lk.get('loose_locks')} "
         f"waiver={lk.get('waiver')}")
    if not lk["all_closed"]:
        # 破例期: 授权必须可追溯 (有 ID + 有到期), 且到期日未过
        wv = lk.get("waiver") or {}
        assert wv.get("active") is True and wv.get("authorization_id"), \
            f"破例生效但授权不可追溯: {wv}"
        assert wv.get("expires"), f"破例无到期日 = 永久消音器, 拒绝: {wv}"
    assert b["krs"]["KR1"]["verdict"] == sc.VERDICT_MET, "批② 已修的尺子应让 KR1 达成"


# ══════════════ 批④ 新增: 注册表补登 + 毕业门禁草案 (真仓只读) ══════════════
def _registry_entry(reg: str, item_id: str) -> str:
    """切出注册表中某个条目的源码块 (到下一个 ShadowItem( 之前)。

    2026-09-14 根治定长窗假红: 原断言写 `[:2500]` / `[:1200]`, 而条目的 verdict_history
    每次裁决都会合法增长 (9/13 二判约 +1.1k 字符) ⇒ `eval_on_demand=True` 这个 kwarg 被
    挤出窗口, 守卫从"条目缺字段"退化成"条目写长了就报"的假红 (真字段一直在, 见
    shadow_registry.py 的 kr3 条目)。按条目边界切片后, 判据回到它本来的意思:
    字段在不在这个条目里。
    """
    return reg.split(f'id="{item_id}"', 1)[1].split("ShadowItem(", 1)[0]


def test_kr3_shadow_now_registered_in_production_registry():
    """补登已生效: 注册表是影子 SSOT, 未登记 = 无人跟踪 = 等于没做。"""
    reg = (PROJECT_ROOT / sc.SHADOW_REGISTRY).read_text(encoding="utf-8")
    assert 'id="kr3_direction_runup_gate"' in reg and "RFC-072" in reg
    entry = _registry_entry(reg, "kr3_direction_runup_gate")
    assert "eval_on_demand=True" in entry, (
        "报告脚本无 cron (只有采集器有) ⇒ 必须标 on_demand, 否则挂载核对哨兵误报")
    assert "min_n=30" in entry
    from app.playbook.shadow_registry import REGISTRY
    item = next(i for i in REGISTRY if i.id == "kr3_direction_runup_gate")
    assert item.min_n == 30 and item.eval_on_demand is True
    assert "would-block 子集" in item.notes, "min_n 数的是子集不是总行数 — 口径必须写在条目里"


def test_kr3_panel_sees_registry_entry(tmp_path):
    """面板归因随之更正: 补登后不得再说"未注册"。"""
    reg_text = (PROJECT_ROOT / sc.SHADOW_REGISTRY).read_text(encoding="utf-8")
    root = _repo(tmp_path, env=_ENV_CLOSED, registry_text=reg_text)
    kr3 = sc.kr3_prereg(kr3={"doc": {"generated_at": _NOW.isoformat(),
                                     "groups": {"LONG|blocked": {"n_eval": 15},
                                                "SHORT|blocked": {"n_eval": 8}}},
                             "error": None, "age_hours": 0.0}, repo_root=root)
    assert kr3["evidence"]["registered_in_shadow_registry"] is True
    assert "未在" not in kr3["note"]


def test_graduation_gate_draft_exists_but_is_not_effective():
    """RFC-077 是**草案**: 批准权与签字权在用户, 面板 gate_redefined 必须仍为 False。"""
    rfc = PROJECT_ROOT / sc.GATE_PREREG_DRAFT
    assert rfc.exists(), "毕业门禁重定义草案未落档 ⇒ KR4 条款无载体可批"
    text = rfc.read_text(encoding="utf-8")
    assert "DRAFT" in text and "未生效" in text and "待用户批准" in text
    assert "用户签字" in text and "三重闸" in text, "四锁+门禁+签字三重闸必须写死在草案里"
    for g in ("G1", "G2", "G3", "G4", "G5", "G6"):
        assert g in text, f"六道判据缺 {g}"
    assert "UNKNOWN_FAIL_CLOSE" in text, "不可测必须 fail-close, 不得以其余五道过而放行"
    src = (PROJECT_ROOT / "scripts" / "okr_status.py").read_text(encoding="utf-8")
    assert "gate_redefined=False" in src, "草案 ≠ 生效: 接线处不得翻 True"


def test_kr4_shows_draft_as_evidence_not_as_completion(tmp_path):
    draft = sc.GATE_PREREG_DRAFT
    root = _repo(tmp_path, env=_ENV_CLOSED)
    (root / draft).parent.mkdir(parents=True, exist_ok=True)
    (root / draft).write_text("# DRAFT\n", encoding="utf-8")
    kr4 = sc.kr4_hard_gate(locks=sc.measure_locks(root, graduation="ACCUMULATING"),
                           gate_redefined=False, weekly_checkpoint_present=True,
                           gate_prereg_draft=draft)
    clause = kr4["evidence"]["clauses"][1]
    assert clause["ok"] is False and kr4["verdict"] == sc.VERDICT_NOT_MET
    assert "草案已出" in clause["evidence"] and "未生效" in clause["evidence"]
    assert "批准权不在代理侧" in clause["evidence"]
