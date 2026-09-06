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


def _invariant(versions=None, exit_status="ok", status="ok", missing_rows=0, age_h=1.0):
    return {"checked_at": (_NOW - timedelta(hours=age_h)).isoformat(),
            "config_freeze": {"window_start": sc.OKR_WINDOW_START,
                              "versions": {"v2": 118},
                              "exit_versions": versions if versions is not None else {
                                  "exit-7f5263af3cfd": 58, "exit-2623be9c8879": 21,
                                  "exit-f837b002896d": 12, "exit-de49b1299290": 24},
                              "exit_hash_missing_rows": missing_rows,
                              "exit_status": exit_status, "status": status}}


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
              "window_days": 30, "now": _NOW}
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
    b = _score(tmp_path)["krs"]["KR2"]
    ev = b["evidence"]
    assert ev["n_unique_exit_versions"] == 4 and ev["window_start"] == "2026-08-24"
    assert ev["daily_guard_exit_status"] == "ok" and ev["exit_hash_missing_rows"] == 0
    assert ev["unexempted_drift"] == [] and len(ev["exempt_in_baseline"]) == 4


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


# ══════════════ Ⓓ/Ⓔ KR2 待裁不自评 + 两口径不得互相冒充 ══════════════
def test_kr2_never_self_settles_even_when_literally_met(tmp_path):
    """窗内只有 1 个退出哈希 (= 字面达成) 也恒 PENDING — OKR 原文「不得擅自结算」。"""
    inv = _invariant(versions={"exit-de49b1299290": 24})
    kr2 = _score(tmp_path, repo={"invariant": inv})["krs"]["KR2"]
    assert kr2["verdict"] == sc.VERDICT_PENDING and kr2["score"] is None
    assert kr2["evidence"]["n_unique_exit_versions"] == 1
    assert "待用户裁决" in kr2["note"] and "(a)" in kr2["note"] and "(b)" in kr2["note"]


def test_kr2_unexempted_drift_alarms_and_names_the_version(tmp_path):
    inv = _invariant(versions={"exit-de49b1299290": 24, "exit-NEW0000000": 3})
    kr2 = _score(tmp_path, repo={"invariant": inv})["krs"]["KR2"]
    assert kr2["alarm"] is True and kr2["evidence"]["unexempted_drift"] == ["exit-NEW0000000"]
    assert kr2["verdict"] == sc.VERDICT_PENDING, "有未豁免漂移也只报事实 + alarm, 裁决权属用户"


def test_kr2_guard_ok_must_not_impersonate_kr2_met(tmp_path):
    """守卫 exit_status='ok' 是**豁免后**口径 (原判分式查档案列恒 v2 = 结构性假绿)。"""
    kr2 = _score(tmp_path)["krs"]["KR2"]
    assert kr2["evidence"]["daily_guard_exit_status"] == "ok"
    assert kr2["verdict"] != sc.VERDICT_MET
    assert "不得冒充 KR2 达成" in kr2["note"]
    assert "唯一值数 = 4" in kr2["note"] and "要求 = 1" in kr2["note"]


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
def test_kr3_unmeasurable_when_prereg_elements_not_landed(tmp_path):
    """现产物给的是分组均值/中位/符号检验 ⇒ 三要素全缺 ⇒ 不得报确认也不得报证伪。"""
    kr3doc = {"generated_at": _NOW.isoformat(), "n_collected": 44, "n_eval": 41,
              "prereg": "RFC-072: would-block 子集净期望显著更差 (均值口径)",
              "groups": {"LONG|blocked": {"mean": 6.19, "median": 14.88,
                                          "sign_test_p_median_gt0": 0.0592}},
              "verdict_by_direction": {"LONG": "DIRECTIONAL", "SHORT": "DIRECTIONAL"}}
    kr3 = _score(tmp_path, repo={"kr3": kr3doc})["krs"]["KR3"]
    assert kr3["verdict"] == sc.VERDICT_UNMEASURABLE and kr3["score"] is None
    assert sorted(kr3["evidence"]["prereg_elements_missing"]) == \
        ["day_clustered_t", "mde", "out_of_sample"]
    assert kr3["evidence"]["hypothesis_count"] == 1
    assert "既不得报确认也不得报证伪" in kr3["note"]


def test_kr3_met_when_one_hypothesis_and_elements_landed(tmp_path):
    kr3doc = {"generated_at": _NOW.isoformat(), "day_clustered_t": 2.4, "mde_80pct": 12.0,
              "oos_window": "2026-09-01→09-05"}
    kr3 = _score(tmp_path, repo={"kr3": kr3doc})["krs"]["KR3"]
    assert kr3["evidence"]["prereg_elements_missing"] == []
    assert kr3["verdict"] == sc.VERDICT_MET


def test_kr3_more_than_one_hypothesis_is_not_met(tmp_path):
    two = ("docs/decisions/RFC-072-kr3-direction-runup-gate-preregistration.md",
           "docs/decisions/RFC-099-second-hypothesis-preregistration.md")
    kr3doc = {"generated_at": _NOW.isoformat(), "day_clustered_t": 2.4, "mde": 1.0, "oos": "x"}
    root = _repo(tmp_path, env=_ENV_CLOSED, prereg_docs=two)
    art = sc.read_artifact(root, "kr3_verdict", now=_NOW)     # 载体缺 → 直接喂判据要素齐备的 doc
    kr3 = sc.kr3_prereg(kr3={"doc": kr3doc, "error": None, "age_hours": 0.0}, repo_root=root,
                        prereg_docs=two)
    assert art["error"], "本例不依赖产物载体"
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
    assert st["status"] == sc.VERDICT_PENDING and st["blocking"] == ["KR2", "KR4"]
    assert st["counts"][sc.VERDICT_PENDING] == 1


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
                                  sc.VERDICT_UNMEASURABLE}
        assert sc.OKR_DOC in blk["source"], f"{kr} 的来源必须指回判据原文"


def test_render_lines_marks_caliber_and_flags(tmp_path):
    lines = sc.render_lines(_score(tmp_path))
    text = "\n".join(lines)
    assert "OKR-2026-09" in text and "并列不替代" in text
    assert "SUPERSEDED" in text, "必须提醒读者上方百分比是废判据续算值"
    assert "PENDING_USER_RULING" in text and "⏳" in text and "❓" in text
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
    assert b["settlement"]["status"] == sc.VERDICT_PENDING
    assert b["krs"]["KR2"]["evidence"]["n_unique_exit_versions"] >= 1
    assert b["krs"]["KR4"]["alarm"] is False and b["locks"]["all_closed"] is True, \
        "生产四锁必须全闭 — 若本断言红, 是实盘硬门真破了, 立即呈裁"
    assert b["krs"]["KR1"]["verdict"] == sc.VERDICT_MET, "批② 已修的尺子应让 KR1 达成"
