#!/opt/ftseek/FTSeek-US/venv/bin/python3
# -*- coding: utf-8 -*-
"""OKR-2026-09 KR 集判分 (纯函数模块, 与 8 月旧块**并列不替代**) — 修尺子批③。

病灶 (2026-09-06 实测): `scripts/okr_status.py::calc_okr_score` 判的是 **OKR-2026-08 的
KR 集** (kr1_win_rate 目标 55% / kr2_weekly_pnl 目标 $1000/周 / kr3_sample 30 笔 /
kr4_ic IC(T+3)≥0.05 / kr5_trigger 50%), 而 `docs/OKR-2026-08.md` 已于 **2026-08-23 提前
结算为 SUPERSEDED**; 现行判据是 `docs/OKR-2026-09.md` 的 KR1 修尺子 / KR2 参数冻结 /
KR3 预注册找 edge / KR4 实盘硬门 / KR5 学费明码 ⇒ 面板头条 `overall_score=60` 与现行
判据**不同源** (且 KR2/KR4 恒 0 是"目标 $1000/周、IC≥0.05"这两个已废目标算出来的)。

治法不是把旧块改掉, 而是**并列**:
  · 旧块 (`kr_scores` / `overall_score`) 原样续算 — `scripts/okr_query.py:58,168` 依赖其
    键名, `data/okr_history.jsonl` 有时序连续性; 改键 = 断消费方 + 造假时序。
  · 新块 `okr_2026_09` 按现行判据判, 并在两处头条都标清口径 (旧块是 SUPERSEDED 续算值)。

为什么单独一个模块: `scripts/okr_status.py` 是**模块级执行**脚本 (import 即跑全部 SQL),
契约测试无法 import 它 — 批②只能用"从源码抽字符串"的弱钉子
(`tests/test_ruler_bps_and_g4_reconstruction.py:53`)。本模块零副作用、只吃 dict/文本/
tmp 目录 ⇒ 判分逻辑本身可被单测钉死 (`tests/test_okr_2026_09_scoring.py`)。

三条硬规矩 (承修尺子批②):
  1. **不新增第四把尺子**: KR2 冻结测量已有两处实现 —
     `ops/hermes/ftseek_weekly_checkpoint.py::check_freeze` (周六 09:05) 与
     `scripts/daily_invariant_check.py::_check_config_freeze` (每日 05:05, 档案列+退出哈希
     两维 + `data/config_freeze_baseline.json` 豁免对账)。本模块只**读**日级守卫的产物
     `data/invariant_check_latest.json`, 不再写一份 SQL — 再加即口径漂移。
  2. **不可测 ≠ 0 ≠ 达成**: 产物缺失/过期/判据要素未落地 → `NOT_MEASURABLE` + `score=None`
     (fail-close, 同批② `UNKNOWN_FAIL_CLOSE`)。
  3. **待裁项不自评**: `docs/OKR-2026-09.md` KR2 明写「**待用户裁决 (不得擅自结算)**」
     (a) 按 cohort 分段判分 + 变更预算 / (b) 9/3 起硬冻结至 9/30 二选一 ⇒ KR2 verdict 恒
     `PENDING_USER_RULING`, 只报实测事实 (窗内退出哈希唯一值数 + 是否全在豁免清单内)。

**不给 9 月总分百分比**: OKR-2026-09 §结算判据是"KR1+KR2+KR4 全达成 且 KR3 给出干净的
确认或证伪"(二元合取), 且明写「本 OKR 不设任何盈利目标」⇒ 五个二元/待裁项取算术平均是
假精度 (旧块 60% 就是这么来的)。新块只出 verdict 计数 + 结算状态。

单位/口径声明: 每个 KR 块带 `unit` + `source` (载体路径) + `evidence`; KR5 学费单位
USD/周 (由 30d 窗口 fee_bps × avg_notional × 笔频外推, 非实测周账)。
回滚: 删本文件 + revert `scripts/okr_status.py` 的接线 (只读判分, 零退出参数、零四锁、
零交易行为变更 — 符合判据字典 C7 冻结期)。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# ── 判据常量: 全部出自 docs/OKR-2026-09.md 原文。改这里 = 改判据 = 须先改 OKR + 用户裁 ──
OKR_DOC = "docs/OKR-2026-09.md"
OKR_WINDOW_START = "2026-08-24"        # KR2 冻结窗起点 (= weekly_checkpoint.FREEZE_START)
OKR_SETTLEMENT_DATE = "2026-09-30"
KR2_TARGET_UNIQUE_VERSIONS = 1         # KR2 原文: "唯一值数 = 1 (换版即 KR2 破)"
KR3_HYPOTHESIS_LIMIT = 1               # KR3 原文: "只许一个假设进入检验"
# KR3 预注册判据三要素 (原文: 按日聚类 t>2 + MDE 覆盖声称效应 + 样本外窗口验证) 在产物里
# 的机器可读落点 — 任一候选键名命中即算该要素已落地。
KR3_PREREG_ELEMENTS = (
    ("day_clustered_t", ("day_clustered", "day_clustered_t", "t_day_clustered", "day_t")),
    ("mde", ("mde", "mde_80pct", "mde_95pct")),
    ("out_of_sample", ("oos", "out_of_sample", "is_oos", "oos_window")),
)
KR3_PREREG_DOCS = (                    # 入场侧预注册载体 (显式清单, 可审计)
    "docs/decisions/RFC-072-kr3-direction-runup-gate-preregistration.md",
)
# 四锁 (KR4 原文 + ops/hermes/ftseek_weekly_checkpoint.py::check_locks 同一真源同一字面值)
LOCK_EXPECT = {
    "AUTO_TRADE_ACCOUNT": "sim",
    "ALLOW_REAL_TRADING": "false",
    "execute_mode": "futu_simulate",
}
ENV_FILE = ".env.us"
CONFIG_FILE = "config/unified_config.yaml"
WEEKLY_CHECKPOINT = "ops/hermes/ftseek_weekly_checkpoint.py"
SHADOW_REGISTRY = "app/playbook/shadow_registry.py"

# 测量产物 + 过期阈 (小时)。过期即 NOT_MEASURABLE — 陈旧产物冒充当期判据是假绿的一种。
#   invariant: daily_invariant_check.py 挂 crontab 05:05 * * 2-6 ⇒ 周日/周一最长 ~72h 空档, 取 96h。
#   exit_scorecard: 周六 06:20 cron ⇒ 取 8 天。
#   kr3 verdict: kr3_runup_report.py 无 cron (手动) ⇒ 不设过期, 但显性报 age。
ARTIFACTS = {
    "invariant": ("data/invariant_check_latest.json", 96),
    "exit_scorecard": ("data/exit_scorecard_latest.json", 192),
    "freeze_baseline": ("data/config_freeze_baseline.json", None),
    "kr3_verdict": ("data/shadow/kr3_runup_verdict_latest.json", None),
}

VERDICT_MET = "MET"
VERDICT_NOT_MET = "NOT_MET"
VERDICT_PENDING = "PENDING_USER_RULING"
VERDICT_UNMEASURABLE = "NOT_MEASURABLE"


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _parse_ts(text):
    """产物时间戳解析 (带/不带时区都吃); 解析失败返回 None (调用方按不可测处置)。"""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_artifact(repo_root, key, now=None):
    """读一个测量产物 → {"doc", "error", "age_hours", "path", "stale"}。

    失败与过期**显性** (不静默当空 dict — "零数据"与"载体坏了"必须可区分, 同批② R5 模式)。
    """
    rel, max_age = ARTIFACTS[key]
    path = Path(repo_root) / rel
    out = {"path": rel, "doc": None, "error": None, "age_hours": None, "stale": False}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001 — 显性留痕, 不 pass
        out["error"] = f"read_failed: {type(exc).__name__}: {exc}"
        return out
    try:
        out["doc"] = json.loads(raw)
    except Exception as exc:                       # noqa: BLE001
        out["error"] = f"bad_json: {type(exc).__name__}: {exc}"
        return out
    doc = out["doc"] if isinstance(out["doc"], dict) else {}
    ts = _parse_ts(doc.get("checked_at") or doc.get("generated_at") or doc.get("timestamp"))
    if ts:
        out["age_hours"] = round((_now(now) - ts).total_seconds() / 3600.0, 1)
        if max_age is not None and out["age_hours"] > max_age:
            out["stale"] = True
            out["error"] = f"stale: age {out['age_hours']}h > 阈 {max_age}h (载体断流?)"
    elif max_age is not None:
        out["error"] = "no_timestamp: 产物无可解析时间戳 ⇒ 无法判新鲜度"
    return out


def _block(kr, title, verdict, unit, source, evidence, gate, score=None, note=None,
           alarm=False):
    """统一 KR 块形状 — 单位/来源/证据/门禁窗口是**必填**, 缺一个就是没落档的判据。"""
    return {"kr": kr, "title": title, "verdict": verdict, "score": score, "unit": unit,
            "source": source, "evidence": evidence, "gate": gate, "note": note,
            "alarm": alarm}


def _all_keys(doc, acc=None):
    """递归收集 JSON 里出现过的全部键名 (判"判据要素有没有落地"用, 不猜嵌套深度)。"""
    acc = set() if acc is None else acc
    if isinstance(doc, dict):
        for k, v in doc.items():
            acc.add(str(k))
            _all_keys(v, acc)
    elif isinstance(doc, list):
        for v in doc:
            _all_keys(v, acc)
    return acc


def measure_locks(repo_root, graduation=None, graduation_deprecated=None):
    """四锁实测 — 与 `ops/hermes/ftseek_weekly_checkpoint.py::check_locks` 同字面值同判定。

    第 4 锁 (毕业门禁 fail-close) 取 okr_status 已算出的 graduation 状态, 不再 subprocess
    调 `_check_graduation_gate()` (周检已每日/每周调; 这里只要"是否已打标"这一个布尔)。
    """
    root = Path(repo_root)
    locks, errors = {}, []
    try:
        env_text = (root / ENV_FILE).read_text(encoding="utf-8")
        for line in env_text.splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                locks[k.strip()] = v.strip()
    except Exception as exc:                       # noqa: BLE001
        errors.append(f"{ENV_FILE}: {type(exc).__name__}: {exc}")
    try:
        cfg_text = (root / CONFIG_FILE).read_text(encoding="utf-8")
        locks["execute_mode"] = ("futu_simulate" if "execute_mode: futu_simulate" in cfg_text
                                 else "NOT_SIMULATE")
    except Exception as exc:                       # noqa: BLE001
        errors.append(f"{CONFIG_FILE}: {type(exc).__name__}: {exc}")
    observed = {k: locks.get(k, "MISSING") for k in LOCK_EXPECT}
    graduated = str(graduation or "").upper() == "GRADUATED"
    closed = {k: (observed[k] == v) for k, v in LOCK_EXPECT.items()}
    closed["graduation_fail_close"] = (graduated is False)
    return {"observed": observed, "expected": dict(LOCK_EXPECT), "closed": closed,
            "all_closed": all(closed.values()), "graduated": graduated,
            "graduation_deprecated": bool(graduation_deprecated), "errors": errors}


def kr1_ruler(ic_playbook=None, exit_scorecard=None, exit_scorecard_error=None):
    """KR1 (修尺子): a) pooled×日聚类成对 b) 头条改日聚类 IC c) 记分牌 MDE 列 d) 停用 confidence IC。

    a/c/d 由数据判; **b 是呈现层** (stdout 头条口径) — 数据里判不了, 由契约测试从源码钉
    (`tests/test_okr_2026_09_scoring.py`), 故 b 记 `ok=None` 且**不参与 verdict**
    (把呈现层项算进数据判分 = 拿测不到的东西充数)。
    """
    pb = ic_playbook or {}
    dc = pb.get("day_clustered") or {}
    paired = [k for k in ("t1", "t3", "t5")
              if isinstance(dc.get(k), dict) and dc[k].get("t") is not None
              and dc[k].get("mde") is not None]
    sn = ((exit_scorecard or {}).get("size_normalized") or {})
    sn_dc = sn.get("net_bps_day_clustered") or {}
    checks = [
        {"id": "a_ic_pooled_and_day_clustered_paired", "ok": len(paired) == 3,
         "evidence": f"IC 日聚类成对 {len(paired)}/3 档 (t1/t3/t5), 每档带 t + MDE"},
        {"id": "b_headline_is_day_clustered_not_subset_ic", "ok": None,
         "evidence": "呈现层项 (stdout 头条 [Playbook SSOT] + 日聚类, [Signal diag] 降级)",
         "verified_by": "tests/test_okr_2026_09_scoring.py::test_kr1_headline_caliber_pinned_in_source"},
        {"id": "c_scorecard_has_mde_column",
         "ok": (sn_dc.get("t") is not None and sn_dc.get("mde_80pct") is not None),
         "evidence": (f"exit_scorecard size_normalized 日聚类 t={sn_dc.get('t')} "
                      f"MDE={sn_dc.get('mde_80pct')}bps"
                      if sn_dc else f"exit_scorecard 产物不可用: {exit_scorecard_error}")},
        {"id": "d_confidence_ic_retired_as_graduation_gate",
         "ok": bool(pb.get("graduation_deprecated")),
         "evidence": f"ic.playbook.graduation_deprecated={pb.get('graduation_deprecated')}"},
    ]
    judged = [c for c in checks if c["ok"] is not None]
    ok_all = bool(judged) and all(c["ok"] for c in judged)
    return _block(
        "KR1", "修尺子: 全部统计输出符合测量标准", VERDICT_MET if ok_all else VERDICT_NOT_MET,
        unit="二元 (子项 a/c/d 数据判定; b 呈现层由契约测试钉)",
        source=f"{OKR_DOC} KR1 + data/exit_scorecard_latest.json + 本报告 ic.playbook",
        evidence={"checks": checks, "passed": sum(1 for c in judged if c["ok"]),
                  "judged": len(judged)},
        gate="第 1-2 周 (2026-08-24→09-06); 判分=脚本输出格式核查 + 落档",
        score=100.0 if ok_all else 0.0,
        note="a/c/d 任一 False 即 NOT_MET — 尺子没修好时下游所有结论都不可采信 (宪章第 9 律)")


def kr2_freeze(invariant=None, baseline=None):
    """KR2 (参数冻结): 只**读**日级守卫产物, 不自算 SQL; verdict 恒 PENDING_USER_RULING。

    两个口径**并列**, 不得互相冒充:
      · KR2 原文口径 = 窗内退出哈希**唯一值数 = 1** (换版即破) ⇒ 实测 4 版 = 按字面已破;
      · 日级守卫口径 = `exit_status` (豁免清单对账后有无**未豁免**漂移) ⇒ 'ok' 不等于 KR2 达成。
    OKR-2026-09 KR2 勘误段已定性: 原判分式查 `trade_records.config_version` **列** (恒 'v2')
    ⇒ 唯一值数永远=1 = **结构性假绿**; 真口径在 `metadata_json.config_version` (`exit-*`)。
    """
    inv = invariant or {}
    doc, err = inv.get("doc"), inv.get("error")
    cf = (doc or {}).get("config_freeze") or {}
    base = (baseline or {}).get("doc") or {}
    base_err = (baseline or {}).get("error")
    exit_versions = cf.get("exit_versions") or {}
    exempt = set((base.get("exit_versions") or {}).keys())
    observed = set(exit_versions)
    unexempted = sorted(observed - exempt)
    evidence = {
        "window_start": cf.get("window_start") or OKR_WINDOW_START,
        "n_unique_exit_versions": len(observed),
        "target_unique_versions": KR2_TARGET_UNIQUE_VERSIONS,
        "exit_versions": exit_versions,
        "exempt_in_baseline": sorted(observed & exempt),
        "unexempted_drift": unexempted,
        "exit_hash_missing_rows": cf.get("exit_hash_missing_rows"),
        "daily_guard_exit_status": cf.get("exit_status"),
        "daily_guard_status": cf.get("status"),
        "artifact_age_hours": inv.get("age_hours"),
    }
    if err or not cf or not observed:
        return _block(
            "KR2", "参数冻结: 9 月退出参数 config_version 唯一值数 = 1", VERDICT_UNMEASURABLE,
            unit="唯一值数 (个) — 退出哈希 metadata_json.config_version",
            source=f"{OKR_DOC} KR2 + {ARTIFACTS['invariant'][0]} (config_freeze 块)",
            evidence=evidence, gate="全月 (2026-08-24→09-30)", score=None,
            note=f"不可测 (fail-close, 不当 0 也不当达成): {err or 'config_freeze 块为空/窗内零行'}")
    return _block(
        "KR2", "参数冻结: 9 月退出参数 config_version 唯一值数 = 1", VERDICT_PENDING,
        unit="唯一值数 (个) — 退出哈希 metadata_json.config_version",
        source=f"{OKR_DOC} KR2 + {ARTIFACTS['invariant'][0]} (config_freeze 块) + "
               f"{ARTIFACTS['freeze_baseline'][0]} (豁免清单)",
        evidence=evidence, gate="全月 (2026-08-24→09-30)", score=None,
        alarm=bool(unexempted),
        note=(f"实测唯一值数 = {len(observed)} (判据原文要求 = {KR2_TARGET_UNIQUE_VERSIONS}, "
              f"「换版即 KR2 破」⇒ 按字面已破); 其中 {len(observed & exempt)}/{len(observed)} 在豁免清单内"
              + (f", **未豁免漂移 {unexempted}** (日级守卫应已告警)" if unexempted else
                 " (4 版均经 DEB + 用户裁走例外通道)")
              + "。OKR-2026-09 KR2 明写「**待用户裁决 (不得擅自结算)**」: (a) 改按 cohort 分段判分 + "
                "明写变更预算 / (b) 宣布 9/3 起硬冻结至 9/30 ⇒ 本块只报实测事实, **不自评达成/未达成**。"
                "日级守卫 exit_status='ok' 是**豁免后**口径, 不得冒充 KR2 达成。"
              + (f" 豁免基线载体异常: {base_err}" if base_err else "")))


def kr3_prereg(kr3=None, repo_root=None, prereg_docs=KR3_PREREG_DOCS):
    """KR3 (找 edge): ①只许一个预注册假设 ②判据三要素是否已在产物落地。

    三要素 = 按日聚类 t>2 + MDE 覆盖声称效应 + 样本外窗口验证 (原文, 期间禁改阈值)。
    现产物 `kr3_runup_verdict_latest.json` 给的是**分组均值/中位/胜率/符号检验** ⇒ 三要素
    一个都没有 ⇒ 既不能报"确认"也不能报"证伪", 只能 `NOT_MEASURABLE` (判据未落地)。
    这正是 KR1 修尺子未完成时 KR3 无法结算的因果链, 不是"数据不够"。
    """
    art = kr3 or {}
    doc, err = art.get("doc"), art.get("error")
    keys = _all_keys(doc) if doc else set()
    landed, missing = {}, []
    for name, candidates in KR3_PREREG_ELEMENTS:
        hit = sorted(k for k in keys if k in candidates)
        landed[name] = hit or None
        if not hit:
            missing.append(name)
    present, absent = [], []
    for rel in prereg_docs:
        (present if repo_root and (Path(repo_root) / rel).exists() else absent).append(rel)
    registry_mentions = None
    if repo_root:
        try:
            reg = (Path(repo_root) / SHADOW_REGISTRY).read_text(encoding="utf-8")
            registry_mentions = any(tok in reg for tok in ("RFC-072", "runup", "kr3"))
        except Exception as exc:                   # noqa: BLE001
            registry_mentions = f"read_failed: {type(exc).__name__}: {exc}"
    hypothesis_count = len(present)
    evidence = {
        "hypothesis_count": hypothesis_count,
        "hypothesis_limit": KR3_HYPOTHESIS_LIMIT,
        "prereg_docs_present": present,
        "prereg_docs_missing": absent,
        "prereg_elements_landed": landed,
        "prereg_elements_missing": missing,
        "n_collected": (doc or {}).get("n_collected"),
        "n_eval": (doc or {}).get("n_eval"),
        "prereg_text": (doc or {}).get("prereg"),
        "verdict_by_direction": (doc or {}).get("verdict_by_direction"),
        "artifact_age_hours": art.get("age_hours"),
        "registered_in_shadow_registry": registry_mentions,
    }
    count_ok = hypothesis_count == KR3_HYPOTHESIS_LIMIT
    if missing:
        return _block(
            "KR3", "找 edge: 预注册入场侧假设, 只许一个", VERDICT_UNMEASURABLE,
            unit="假设数 (个) + 判据要素落地数 (个/3)",
            source=f"{OKR_DOC} KR3 + {ARTIFACTS['kr3_verdict'][0]} + {'/'.join(prereg_docs)}",
            evidence=evidence, gate="第 2-4 周; 判据预注册, 期间禁改阈值 (宪章第 6 律)",
            score=None,
            note=(f"假设数 {hypothesis_count}/{KR3_HYPOTHESIS_LIMIT} "
                  f"{'✅' if count_ok else '❌ 超一个 = 七路并发改动重演'}; 但预注册判据三要素缺 "
                  f"{missing} ⇒ 产物给的是分组均值/中位/符号检验, **既不得报确认也不得报证伪** "
                  f"(9/4 首判悬置未裁, 见 handoff 📏 节)。"
                  + ("" if registry_mentions is True else
                     " 另: KR3 影子**未在 `app/playbook/shadow_registry.py` 注册** (注册表纪律"
                     "「新增影子必须在此注册, 否则无人跟踪 = 等于没做」) — 待裁是否补登。")
                  + (f" 载体异常: {err}" if err else "")))
    return _block(
        "KR3", "找 edge: 预注册入场侧假设, 只许一个",
        VERDICT_MET if count_ok else VERDICT_NOT_MET,
        unit="假设数 (个) + 判据要素落地数 (个/3)",
        source=f"{OKR_DOC} KR3 + {ARTIFACTS['kr3_verdict'][0]}",
        evidence=evidence, gate="第 2-4 周", score=100.0 if count_ok else 0.0,
        note=("三要素齐备 ⇒ 按原文判: 日聚类 t>2 ∧ MDE 覆盖声称效应 ∧ OOS 同号 → 确认; 否则干净证伪 "
              "(证伪同样算达成 — 本 KR 的产出是证据不是盈利)"
              if count_ok else
              f"🚨 预注册假设 **{hypothesis_count} 个 > 上限 {KR3_HYPOTHESIS_LIMIT}** "
              f"({present}) ⇒ 违反 KR3「只许一个假设进入检验」(防七路并发改动重演)。"
              f"判分不取劣充数: 多假设下的任何显著结果都是多重比较产物, 不得当 edge 呈裁 — "
              f"须先由用户裁撤回哪一个, 再继续攒样"))


def kr4_hard_gate(locks=None, gate_redefined=False, weekly_checkpoint_present=None):
    """KR4 (实盘硬门): 四锁零松动 + 毕业门禁重定义。四锁任一松 = 硬门破 (alarm)。"""
    lk = locks or {}
    closed = lk.get("closed") or {}
    all_closed = bool(lk.get("all_closed"))
    clauses = [
        {"id": "four_locks_all_closed", "ok": all_closed,
         "evidence": f"observed={lk.get('observed')} closed={closed}"},
        {"id": "graduation_gate_redefined_preregistered", "ok": bool(gate_redefined),
         "evidence": ("新判据已预注册 + 用户批准" if gate_redefined else
                      "旧 confidence IC 判据已 DEPRECATED, 新判据**尚未**预注册/批准 ⇒ 门禁保持现状 "
                      "(fail-close = 安全侧)")},
        {"id": "weekly_checkpoint_has_lock_item", "ok": weekly_checkpoint_present,
         "evidence": f"{WEEKLY_CHECKPOINT} "
                     f"{'存在 (周六 09:05 Hermes 0e87d094dcdf 跑四锁+冻结+证伪门)' if weekly_checkpoint_present else '缺失'}"},
    ]
    judged = [c for c in clauses if c["ok"] is not None]
    verdict = VERDICT_MET if (judged and all(c["ok"] for c in judged)) else VERDICT_NOT_MET
    return _block(
        "KR4", "实盘硬门: 四锁零松动 + 毕业门禁重定义", verdict,
        unit="二元 (三条款取劣)",
        source=f"{OKR_DOC} KR4 + {ENV_FILE} + {CONFIG_FILE} + {WEEKLY_CHECKPOINT}",
        evidence={"clauses": clauses, "passed": sum(1 for c in judged if c["ok"]),
                  "judged": len(judged), "lock_errors": lk.get("errors")},
        gate="全月 (2026-08-24→09-30); 打标前严格不切实盘 = 用户明令",
        score=None, alarm=not all_closed,
        note=("四锁全闭 ✅ (硬门未破; 规则=任一松动即 alarm 呈裁, 不是扣分项)。门禁重定义未完成 "
              "⇒ KR4 记 NOT_MET, 但系统仍在安全侧 (graduated=False ⇒ fail-close)。"
              if all_closed else
              "🚨 **四锁松动** (alarm=True) — 立即呈裁, 不得继续跑任何结算"))


def kr5_tuition(cnt=None, window_days=None, fee_bps=None, avg_notional=None,
                bps_suspect=False, okr_estimate_weekly_usd=210.0):
    """KR5 (成本意识, **非门禁**): 学费明码标价。单位 USD/周 (30d 窗口外推, 非实测周账)。"""
    unit = "USD/周 (由窗口 fee_bps × avg_notional × 笔频外推)"
    source = f"{OKR_DOC} KR5 + 本报告 trade_records (cnt_bps/fee_bps/avg_notional)"
    gate = "非门禁 (guardrail: 禁加仓/提频换样本, 禁追盈亏数字)"
    if bps_suspect or not cnt or not window_days or fee_bps is None or not avg_notional:
        return _block("KR5", "成本意识: 学费明码标价", VERDICT_UNMEASURABLE, unit=unit,
                      source=source, gate=gate, score=None,
                      evidence={"cnt": cnt, "window_days": window_days, "fee_bps": fee_bps,
                                "avg_notional": avg_notional, "bps_suspect": bool(bps_suspect)},
                      note=("bps 哨兵报警 (|bps|>1e4 ⇒ 量纲/括号错, 数字不可用)" if bps_suspect
                            else "输入缺失 ⇒ 不可测 (fail-close, 不当 0)"))
    fee_per_trade = fee_bps / 10000.0 * avg_notional
    trades_per_week = cnt / window_days * 7.0
    weekly = fee_per_trade * trades_per_week
    return _block(
        "KR5", "成本意识: 学费明码标价", VERDICT_MET, unit=unit, source=source, gate=gate,
        score=None,
        evidence={"fee_per_trade_usd": round(fee_per_trade, 2),
                  "trades_per_week": round(trades_per_week, 1),
                  "weekly_tuition_usd": round(weekly, 2),
                  "okr_estimate_weekly_usd": okr_estimate_weekly_usd,
                  "cnt": cnt, "window_days": window_days, "fee_bps": fee_bps,
                  "avg_notional": avg_notional},
        note=(f"${fee_per_trade:.2f}/笔 × {trades_per_week:.1f} 笔/周 ≈ **${weekly:,.0f}/周** "
              f"(OKR 预估 ${okr_estimate_weekly_usd:,.0f}/周 = $4.54 × 46 笔)。差额主因是 2026-08-06 "
              f"sizing 变更抬升名义额 (中位 $3,307→$5,737) ⇒ 每笔佣金不变而笔均名义额变大, "
              f"**不是提频换样本**; 约束项 (禁加仓/提频、禁追盈亏) 非机械可判, 属人工审计面。"))


def settle(krs):
    """OKR-2026-09 §结算判据 (2026-09-30): KR1+KR2+KR4 全达成 ∧ KR3 给出干净的确认或证伪。

    任一必需项 PENDING ⇒ 结算状态 PENDING_USER_RULING (**不得自评**);
    任一 NOT_MEASURABLE ⇒ 结算不可判 (尺子没落地); 不给百分比总分 (假精度)。
    """
    required = ("KR1", "KR2", "KR4")
    verdicts = {k: (v or {}).get("verdict") for k, v in krs.items()}
    counts = {}
    for v in verdicts.values():
        counts[v] = counts.get(v, 0) + 1
    blocking = sorted(k for k in required if verdicts.get(k) != VERDICT_MET)
    kr3 = verdicts.get("KR3")
    if any(verdicts.get(k) == VERDICT_PENDING for k in required):
        status = VERDICT_PENDING
        why = (f"{[k for k in required if verdicts.get(k) == VERDICT_PENDING]} 挂用户裁决 "
               f"⇒ {OKR_SETTLEMENT_DATE} 结算不可自评 (OKR 原文「不得擅自结算」)")
    elif blocking or kr3 in (VERDICT_UNMEASURABLE, VERDICT_NOT_MET):
        status = VERDICT_NOT_MET
        why = f"必需项未达成 {blocking}; KR3={kr3}"
    else:
        status = VERDICT_MET
        why = "KR1+KR2+KR4 全 MET 且 KR3 给出干净的确认或证伪"
    return {"rule": (f"{OKR_DOC} §结算判据: 达标 = KR1 + KR2 + KR4 全达成, 且 KR3 给出干净的"
                     f"确认或证伪 (两者都算达成); 未达标 = KR2 破 (又换参) 或 KR1 未完成"),
            "settlement_date": OKR_SETTLEMENT_DATE, "status": status, "why": why,
            "verdicts": verdicts, "counts": counts, "blocking": blocking,
            "no_numeric_total": ("本 OKR 不设盈利目标, 五个二元/待裁项取算术平均是假精度 "
                                 "(旧块 overall_score 即此病) ⇒ 只出 verdict 计数")}


def collect_and_score(repo_root, ic_playbook=None, graduation=None,
                      graduation_deprecated=None, trade_stats=None, window_days=30,
                      gate_redefined=False, now=None):
    """读全部载体 → 判五个 KR → 结算状态。只读, 零副作用 (不写任何文件)。"""
    root = Path(repo_root)
    arts = {k: read_artifact(root, k, now=now) for k in ARTIFACTS}
    locks = measure_locks(root, graduation=graduation,
                          graduation_deprecated=graduation_deprecated)
    ts = trade_stats or {}
    krs = {
        "KR1": kr1_ruler(ic_playbook=ic_playbook, exit_scorecard=arts["exit_scorecard"]["doc"],
                         exit_scorecard_error=arts["exit_scorecard"]["error"]),
        "KR2": kr2_freeze(invariant=arts["invariant"], baseline=arts["freeze_baseline"]),
        "KR3": kr3_prereg(kr3=arts["kr3_verdict"], repo_root=root),
        "KR4": kr4_hard_gate(locks=locks, gate_redefined=gate_redefined,
                             weekly_checkpoint_present=(root / WEEKLY_CHECKPOINT).exists()),
        "KR5": kr5_tuition(cnt=ts.get("cnt_bps") or ts.get("cnt"), window_days=window_days,
                           fee_bps=ts.get("fee_bps"), avg_notional=ts.get("avg_notional"),
                           bps_suspect=bool(ts.get("bps_suspect"))),
    }
    return {
        "okr": "OKR-2026-09",
        "okr_doc": OKR_DOC,
        "window": f"{OKR_WINDOW_START}→{OKR_SETTLEMENT_DATE}",
        "generated_at": _now(now).isoformat(timespec="seconds"),
        "caliber": ("现行判据 (OKR-2026-09)。同文件 `kr_scores`/`overall_score` 是 "
                    "**OKR-2026-08 KR 集**的续算值 — 该 OKR 已于 2026-08-23 提前结算为 "
                    "SUPERSEDED, 旧块仅为 kr_scores 消费方 (scripts/okr_query.py) 与 "
                    "okr_history.jsonl 时序连续性保留, **不代表现行判据**。"),
        "krs": krs,
        "settlement": settle(krs),
        "locks": locks,
        "artifacts": {k: {"path": v["path"], "age_hours": v["age_hours"], "error": v["error"]}
                      for k, v in arts.items()},
    }


def render_lines(block):
    """新块的 stdout 呈现 (与旧块并列打印, 口径写在标题上)。"""
    if not block or block.get("error"):
        return [f"⚠️ OKR-2026-09 判分不可用: {(block or {}).get('error')}"]
    st = block.get("settlement") or {}
    lines = [f"🎯 OKR-2026-09 (现行判据 · 窗口 {block.get('window')}) — 与上方 8 月旧块**并列不替代**",
             f"  结算状态: {st.get('status')} — {st.get('why')}"]
    for kr in ("KR1", "KR2", "KR3", "KR4", "KR5"):
        b = (block.get("krs") or {}).get(kr) or {}
        flag = "🚨" if b.get("alarm") else {"MET": "✅", "NOT_MET": "❌",
                                            "PENDING_USER_RULING": "⏳",
                                            "NOT_MEASURABLE": "❓"}.get(b.get("verdict"), "·")
        lines.append(f"  {flag} {kr} {b.get('title','?'):<28} {b.get('verdict','?')}")
        note = b.get("note")
        if note:
            lines.append(f"      └ {note}")
    lines.append("  ⚠️ 上方 OVERALL % 是 OKR-2026-08 KR 集 (2026-08-23 SUPERSEDED) 的续算值, "
                 "不代表现行判据")
    return lines
