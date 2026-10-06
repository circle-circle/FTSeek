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
# 毕业门禁重定义 (KR4 未完成条款) 的预注册**草案**载体 — 2026-09-06 用户授权代裁批次起草。
# 草案 ≠ 生效: OKR-2026-09 KR4 原文要求「新判据**预注册 + 用户批准**后才生效」, 且
# 「没有打标前严格不切实盘 = 用户明令」⇒ 批准权不在代理侧, gate_redefined 恒 False 直到签字。
GATE_PREREG_DRAFT = "docs/decisions/RFC-077-graduation-gate-redefinition-preregistration.md"
ENV_FILE = ".env.us"
CONFIG_FILE = "config/unified_config.yaml"
# 四锁破例预注册载体 (RFC-135, 2026-10-06 用户裁 A)。data/* 是 gitignored ⇒
# 属运维文件, 预注册留痕落在 docs/decisions/RFC-135-*.md (同 RFC-082 知情破例先例)。
WAIVER_FILE = "data/live_probe_lock_waiver.json"
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

# ── KR2 修订判据 (2026-09-06, 用户授权代裁「交给你来做决策」; 落档 docs/OKR-2026-09.md 变更记录) ──
# 判分单位 = **主 cohort 窗** (现行退出参数版本的连续窗), 不再是 8/24 起的全窗。
COHORT_START = "2026-09-03"          # = daily_invariant_check._FREEZE_COHORT_START (钉子钉住分叉即红)
COHORT_SETTLED_TARGET = 200          # 非判据, 只作功效提示: OKR 自算 19 交易日 × 11.3 笔/日 ≈ 215
KR2_CHANGE_BUDGET = 0                # 9/6→9/30 允许的退出参数变更次数 = 零
KR2_EXEMPT_CHANNEL = "CLOSED"        # P0 豁免通道对本维关闭 (前 4 版全走豁免 ⇒ 再开口子=判据名存实亡)
# KR3 样本门 (RFC-072 §三.5 原文: would-block 子集 n<30 → 不裁决, 延长观察)
KR3_MIN_BLOCKED_N = 30

VERDICT_MET = "MET"
VERDICT_NOT_MET = "NOT_MET"
VERDICT_PENDING = "PENDING_USER_RULING"
VERDICT_UNMEASURABLE = "NOT_MEASURABLE"
VERDICT_ACCUMULATING = "ACCUMULATING"            # 窗未闭合/样本未达门 ⇒ 判据尚未到期 (不是没做到)
VERDICT_READY = "READY_TO_ADJUDICATE"            # 样本门已达 ⇒ 可开裁决批 (判分口径现写现验)


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

    破例语义 (RFC-135, 2026-10-06 用户裁 A):
      `closed` 永远是**实测真相** — 锁松着就记 False, 不因有授权而改写 (改写=撒谎,
      且会让下一次真松动无法被发现)。授权体现在独立的 `waived` 段 +
      `alarm_suppressed`: 只有当"松动的每一把锁都被有效预注册授权精确覆盖"时,
      KR4 才不 alarm。破例文件缺失/过期/坏 JSON ⇒ 读不到 ⇒ 告警自动复响。
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
    all_closed = all(closed.values())

    waiver, alarm_suppressed, uncovered = _measure_lock_waiver(root, observed, closed, errors)
    return {"observed": observed, "expected": dict(LOCK_EXPECT), "closed": closed,
            "all_closed": all_closed, "graduated": graduated,
            "graduation_deprecated": bool(graduation_deprecated), "errors": errors,
            "waiver": waiver, "alarm_suppressed": alarm_suppressed,
            "loose_locks": uncovered}


def _measure_lock_waiver(root, observed, closed, errors):
    """破例覆盖判定 (RFC-135). 返回 (waiver段, alarm_suppressed, 未被覆盖的松动锁).

    复用 `app.playbook.live_arm_gate.read_live_probe_waiver` = 与 gate 到期判定**同一真源**,
    不在审计器另写一份日期解析 (那会分叉 — 正是 test_lock_literals_match_weekly_checkpoint
    所防的病)。传显式 path 以保持合成仓测试 hermetic (不读生产 data/)。

    fail-close 方向: 任何读不到/解析失败 ⇒ 无破例 ⇒ alarm 照响。
    """
    loose = [k for k, ok in closed.items() if not ok and k != "graduation_fail_close"]
    try:
        from app.playbook.live_arm_gate import read_live_probe_waiver
    except Exception as exc:                       # noqa: BLE001
        # 读不到判定源时不得静默当作"无破例"了事 — 记进 errors 供审计
        errors.append(f"lock_waiver_reader: {type(exc).__name__}: {exc}")
        return {"active": False, "reason": "reader_unavailable"}, False, loose
    w = read_live_probe_waiver(root / WAIVER_FILE)
    info = {"active": bool(w.get("active")), "reason": w.get("reason"),
            "expires": w.get("expires"), "authorization_id": w.get("authorization_id"),
            "waiver_id": (w.get("raw") or {}).get("waiver_id"),
            "locks_waived": w.get("locks_waived") or {}, "days_left": w.get("days_left")}
    if not w.get("active"):
        return info, False, loose
    waived = w.get("locks_waived") or {}
    # 精确覆盖: 松动的锁必须在授权清单里, 且实测值 == 授权值。
    # 少一条即不抑制 (授权不得当万能消音器 — 别的锁松了必须响)。
    uncovered = [k for k in loose if not (k in waived and observed.get(k) == waived[k])]
    suppressed = (not uncovered)
    info["covers_all_loose"] = suppressed
    return info, suppressed, uncovered


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


def kr2_freeze(invariant=None, baseline=None, cohort_settled=None, today=None):
    """KR2 (参数冻结) — **2026-09-06 修订判据** (用户授权代裁, 落档 OKR-2026-09 变更记录)。

    判分单位从"8/24 起全窗唯一值数=1"改为"**主 cohort 窗**唯一值数=1":
      · 为什么必须改: 全窗事实上已有 **4 个**退出哈希且**不可逆** (8/24→9/2 每 1-2 天一版,
        全部经 DEB + 用户裁走豁免通道) ⇒ 按原文字面判, KR2 在 9/6 就已永久 BROKEN, 而 9/30
        结算会把"9/3 起硬冻结"能创造出的 **≈215 笔单版本样本**一起判作废 —— 那是自毁项目史上
        第一个统计可用窗口 (OKR-2026-09 KR2 的价值陈述本身)。
      · 为什么不是只改判分不冻未来 (纯 (a)) : cohort 判分若不配"变更预算归零", 继续每 1-2 天
        换版 ⇒ cohort 永远攒不满 ⇒ 判据退化成"永远在攒样"的僵尸尺子 (判据字典 B17 病灶)。
      ⇒ 采 **(a)+(b) 合流**: 判分改 cohort (可达成且可证伪) + 9/6→9/30 **零换版**承诺
        (P0 豁免通道对本维关闭) + 多版本期如实留痕 (只可作跨版本对照, 不得作单版本期望结论)。

    口径纪律 (三条, 全部由钉子钉住):
      1. **不自建 SQL** — cohort 维由日级守卫 `daily_invariant_check._check_config_freeze`
         一处测量 (本模块只读其产物), 避免第四把尺子;
      2. **两个口径并列不得互相冒充** — 守卫 `exit_status='ok'` 是**豁免后**口径 (无未豁免漂移),
         cohort `status` 才是 KR2 判据口径; 原判分式查 `trade_records.config_version` **列**
         (恒 'v2') = 结构性假绿 (OKR-2026-09 KR2 勘误 2026-09-03);
      3. **窗未闭合不提前判 MET** — 9/30 之前只报 ACCUMULATING (当前零换版), 不做乐观自评。
    """
    inv = invariant or {}
    doc, err = inv.get("doc"), inv.get("error")
    cf = (doc or {}).get("config_freeze") or {}
    cohort = cf.get("cohort") or {}
    base = (baseline or {}).get("doc") or {}
    base_err = (baseline or {}).get("error")
    exit_versions = cf.get("exit_versions") or {}
    exempt = set((base.get("exit_versions") or {}).keys())
    unexempted = sorted(set(exit_versions) - exempt)
    c_versions = cohort.get("versions") or {}
    n_unique = cohort.get("n_unique")
    today_s = str(today or "")[:10]
    window_closed = bool(today_s) and today_s >= OKR_SETTLEMENT_DATE
    evidence = {
        "cohort_window_start": cohort.get("window_start") or COHORT_START,
        "cohort_n_unique_versions": n_unique,
        "cohort_versions": c_versions,
        "cohort_status": cohort.get("status"),
        "cohort_hash_missing_rows": cohort.get("hash_missing_rows"),
        "cohort_settled_n": cohort_settled,
        # 口径声明 (军规: 新字段带单位/来源): cohort_settled_n 取 okr_status 既有的
        # "毕业样本 (当前退出参数版本, 30d 已结算)" 聚合, 而 cohort 维数的是 trade_date ≥
        # COHORT_START 的**全部行** ⇒ 两者可差 1-2 笔 (同版本但成交日在 cohort 窗前, 如
        # exit-de49b1299290 的 9/2 行)。两个数都**不是判据**, 只作功效提示, 差值必须可见。
        "cohort_settled_caliber": ("graduation_sample: 当前版本 30d **已结算**笔数; 与 cohort 窗 "
                                   "(trade_date≥%s, 全部行) 口径不同, 差值 = 窗前同版本行"
                                   % COHORT_START),
        "cohort_rows_all": sum(int((v or {}).get("n") or 0) for v in c_versions.values()),
        "cohort_settled_target": COHORT_SETTLED_TARGET,
        "okr_original_estimate_n": 215,
        "change_budget_remaining": KR2_CHANGE_BUDGET,
        "exempt_channel_for_cohort": KR2_EXEMPT_CHANNEL,
        "full_window_n_unique_versions": len(exit_versions),
        "full_window_versions": exit_versions,
        "full_window_exempt": sorted(set(exit_versions) & exempt),
        "full_window_unexempted_drift": unexempted,
        "daily_guard_exit_status": cf.get("exit_status"),
        "baseline_error": base_err,
        "artifact_age_hours": inv.get("age_hours"),
        "window_closed": window_closed,
    }
    unit = "唯一值数 (个) — 退出哈希 metadata_json.config_version, 主 cohort 窗口径"
    source = (f"{OKR_DOC} KR2 (2026-09-06 修订) + {ARTIFACTS['invariant'][0]} "
              f"(config_freeze.cohort 块, 由 daily_invariant_check 一处测量)")
    gate = (f"主 cohort 窗 {COHORT_START}→{OKR_SETTLEMENT_DATE}; 变更预算 {KR2_CHANGE_BUDGET} 次, "
            f"豁免通道 {KR2_EXEMPT_CHANNEL}")
    power = ("" if cohort_settled is None else
             f" 功效提示 (非判据): cohort 已结算 {cohort_settled} 笔 / 目标 ≈{COHORT_SETTLED_TARGET} "
             f"(OKR 自算 215; 原 KR 价值陈述 250; 口径=当前版本 30d 已结算, cohort 窗全部行 "
             f"{sum(int((v or {}).get('n') or 0) for v in c_versions.values())} 笔) ⇒ "
             + ("样本门已达, 可对毛期望=0 做首次有功效的确认/证伪"
                if cohort_settled >= COHORT_SETTLED_TARGET else
                f"尚差 {COHORT_SETTLED_TARGET - cohort_settled} 笔 (n<200 时期望检验功效不足, "
                f"冻结成立≠结论可测)"))
    if err or not cf:
        return _block("KR2", "参数冻结: 主 cohort 窗退出参数唯一值数 = 1", VERDICT_UNMEASURABLE,
                      unit=unit, source=source, evidence=evidence, gate=gate, score=None,
                      note=f"不可测 (fail-close, 不当 0 也不当达成): {err or 'config_freeze 块为空'}")
    if n_unique is None:
        return _block("KR2", "参数冻结: 主 cohort 窗退出参数唯一值数 = 1", VERDICT_UNMEASURABLE,
                      unit=unit, source=source, evidence=evidence, gate=gate, score=None,
                      note="产物里无 `config_freeze.cohort` 块 — 该载体是**旧版守卫**跑的 "
                           "(cohort 维 2026-09-06 才加) ⇒ 重跑 scripts/daily_invariant_check.py "
                           "或等次日 05:05 cron; 不得用全窗维冒充 cohort 判分")
    if n_unique > 1:
        return _block("KR2", "参数冻结: 主 cohort 窗退出参数唯一值数 = 1", VERDICT_NOT_MET,
                      unit=unit, source=source, evidence=evidence, gate=gate, score=0.0,
                      alarm=True,
                      note=f"🚨 **BROKEN** — cohort 窗内出现 {n_unique} 个退出哈希 "
                           f"({sorted(c_versions)}) ⇒ 变更预算 ({KR2_CHANGE_BUDGET}) 已破且豁免通道"
                           f"对本维关闭, 豁免清单**不适用**。后果按预注册回滚线: 9 月单版本样本作废, "
                           f"须 DEB 落档 + 10 月开跑会项目级裁决 (用户主持)。{power}")
    if window_closed:
        return _block("KR2", "参数冻结: 主 cohort 窗退出参数唯一值数 = 1", VERDICT_MET,
                      unit=unit, source=source, evidence=evidence, gate=gate, score=100.0,
                      note=f"窗已闭合 ({today_s} ≥ {OKR_SETTLEMENT_DATE}) 且 cohort 唯一值数 = 1 ⇒ "
                           f"MET。多版本期 ({OKR_WINDOW_START}→{COHORT_START}) 有 "
                           f"{len(exit_versions)} 版如实留痕, 其样本只可作跨版本对照。{power}")
    return _block("KR2", "参数冻结: 主 cohort 窗退出参数唯一值数 = 1", VERDICT_ACCUMULATING,
                  unit=unit, source=source, evidence=evidence, gate=gate, score=None,
                  alarm=bool(unexempted),
                  note=(f"⏳ 窗未闭合 ({today_s} < {OKR_SETTLEMENT_DATE}) — 当前 cohort 唯一值数 = "
                        f"{n_unique} ({sorted(c_versions) or '窗内零成交行'}) ⇒ **零换版, 判据在轨**; "
                        f"9/30 才出 MET/BROKEN, 不提前自评。全窗 (8/24 起) {len(exit_versions)} 版"
                        f"属多版本期 (变更预算已用尽), 其中 {len(set(exit_versions) & exempt)}/"
                        f"{len(exit_versions)} 在豁免清单内"
                        + (f", **未豁免漂移 {unexempted}**" if unexempted else "")
                        + f"。日级守卫 exit_status='{cf.get('exit_status')}' 是**豁免后**口径, "
                          f"不得冒充本判据达成。{power}"))


def kr3_prereg(kr3=None, repo_root=None, prereg_docs=KR3_PREREG_DOCS,
               min_blocked_n=KR3_MIN_BLOCKED_N):
    """KR3 (找 edge): ①只许一个预注册假设 ②RFC-072 §三.5 样本门 ③裁决批是否可开。

    **归因更正 (2026-09-06, 修尺子批④)**: 批③ 把"预注册三要素 (按日聚类 t / MDE / OOS) 在产物
    里全缺"记成尺子未落地, 并说成 KR1 的因果链 —— **不准确**。RFC-072 §六.3 明写:
    「期末裁决为独立批次 (含按日聚类统计脚本), **不在本预注册内预先实现判分代码** — 判分口径在
    裁决批现写现验, 防『先写判分再凑数据』的选择压力」⇒ 三要素缺席是**预注册明令的设计**,
    不是病灶。本批尊重该边界, **不写判分代码**; 三要素只作 evidence 透出 (裁决批的待办清单)。

    可机械判的只有两件 (都不是"判分"):
      · 假设数 = 1 (KR3 原文「只许一个」; >1 ⇒ 多重比较, 任何显著结果都不得当 edge 呈裁);
      · §三.5 样本门: would-block 子集已结算 n ≥ 30 才可裁决 (现产物 groups 里数字段直接求和,
        零新统计量) —— 未达 ⇒ ACCUMULATING (不裁决, 延长观察), 已达 ⇒ READY_TO_ADJUDICATE。
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
    groups = (doc or {}).get("groups") or {}
    blocked_n = sum(int((groups.get(f"{d}|blocked") or {}).get("n_eval") or 0)
                    for d in ("LONG", "SHORT"))
    pass_n = sum(int((groups.get(f"{d}|pass") or {}).get("n_eval") or 0)
                 for d in ("LONG", "SHORT"))
    sample_gate = {"blocked_n": blocked_n, "min_n": min_blocked_n,
                   "met": blocked_n >= min_blocked_n, "pass_n": pass_n,
                   "rule": "RFC-072 §三.5: would-block 子集 n<30 → 不裁决, 延长观察"}
    hypothesis_count = len(present)
    count_ok = hypothesis_count == KR3_HYPOTHESIS_LIMIT
    evidence = {
        "hypothesis_count": hypothesis_count,
        "hypothesis_limit": KR3_HYPOTHESIS_LIMIT,
        "prereg_docs_present": present,
        "prereg_docs_missing": absent,
        "sample_gate": sample_gate,
        "adjudication_elements_pending": missing,
        "adjudication_elements_landed": landed,
        "elements_absent_by_design": ("RFC-072 §六.3 明令判分代码不得预先实现 (裁决批现写现验) "
                                      "⇒ 缺项是设计不是病灶"),
        "n_collected": (doc or {}).get("n_collected"),
        "n_eval": (doc or {}).get("n_eval"),
        "prereg_text": (doc or {}).get("prereg"),
        "verdict_by_direction": (doc or {}).get("verdict_by_direction"),
        "artifact_age_hours": art.get("age_hours"),
        "registered_in_shadow_registry": registry_mentions,
    }
    unit = "假设数 (个) + would-block 已结算样本 n (笔)"
    source = f"{OKR_DOC} KR3 + docs/decisions/RFC-072-* + {ARTIFACTS['kr3_verdict'][0]}"
    gate = "第 2-4 周; 判据预注册冻结 (RFC-072 §三), 期间禁改阈值 (宪章第 6 律)"
    registry_note = ("" if registry_mentions is True else
                     " 另: KR3 影子**未在 `app/playbook/shadow_registry.py` 注册** (注册表纪律"
                     "「新增影子必须在此注册, 否则无人跟踪 = 等于没做」) — 待裁是否补登。")
    if err or not doc:
        return _block("KR3", "找 edge: 预注册入场侧假设, 只许一个", VERDICT_UNMEASURABLE,
                      unit=unit, source=source, evidence=evidence, gate=gate, score=None,
                      note=f"载体不可用 (fail-close): {err or '产物为空'}{registry_note}")
    if not count_ok:
        return _block("KR3", "找 edge: 预注册入场侧假设, 只许一个", VERDICT_NOT_MET,
                      unit=unit, source=source, evidence=evidence, gate=gate, score=0.0,
                      alarm=True,
                      note=f"🚨 预注册假设 **{hypothesis_count} 个 > 上限 {KR3_HYPOTHESIS_LIMIT}** "
                           f"({present}) ⇒ 违反 KR3「只许一个假设进入检验」(防七路并发改动重演)。"
                           f"多假设下的任何显著结果都是**多重比较产物**, 不得当 edge 呈裁 — "
                           f"须先由用户裁撤回哪一个, 再继续攒样。")
    if not sample_gate["met"]:
        return _block("KR3", "找 edge: 预注册入场侧假设, 只许一个", VERDICT_ACCUMULATING,
                      unit=unit, source=source, evidence=evidence, gate=gate, score=None,
                      note=(f"⏳ 样本门未达: would-block 已结算 **n={blocked_n} < {min_blocked_n}** "
                            f"(LONG|blocked + SHORT|blocked) ⇒ 按 RFC-072 §三.5 **不裁决, 延长观察** "
                            f"—— 这与日历无关, 是样本门在挡。判分三要素 {missing} 按 §六.3 留到裁决批"
                            f"现写现验 (**本批不实现判分代码**); 现产物的分组均值/中位/符号检验只是"
                            f"稳健性视角, 不得当预注册判据读。{registry_note}"))
    return _block("KR3", "找 edge: 预注册入场侧假设, 只许一个", VERDICT_READY,
                  unit=unit, source=source, evidence=evidence, gate=gate, score=None,
                  note=(f"✅ 样本门已达 (would-block n={blocked_n} ≥ {min_blocked_n}) ⇒ 可开**独立裁决批**: "
                        f"按 RFC-072 §三 现写现验 ①按日聚类双样本 t>2 ②MDE 覆盖声称效应 "
                        f"③窗口最后 2 周 vs 之前 OOS 方向一致 (三要素当前均未实现, 见 evidence)。"
                        f"确认或**干净证伪**都算 KR3 达成 (产出是证据不是盈利); 证伪后不立即换假设 "
                        f"(防链式 fishing, OKR KR3 明令)。{registry_note}"))


def kr4_hard_gate(locks=None, gate_redefined=False, weekly_checkpoint_present=None,
                  gate_prereg_draft=None):
    """KR4 (实盘硬门): 四锁零松动 + 毕业门禁重定义。四锁任一松 = 硬门破 (alarm)。"""
    lk = locks or {}
    closed = lk.get("closed") or {}
    all_closed = bool(lk.get("all_closed"))
    # RFC-135 破例: alarm 只在"松动且未被有效预注册授权覆盖"时响。
    # all_closed 保持实测真相 (锁松着就是 False) — 授权不改写事实, 只决定要不要吵醒值班人。
    alarm_suppressed = bool(lk.get("alarm_suppressed"))
    waiver = lk.get("waiver") or {}
    loose_locks = lk.get("loose_locks") or []
    alarm = (not all_closed) and (not alarm_suppressed)
    clauses = [
        # ok 保持实测真相: 破例只抑制 alarm (不吵醒值班人), 不改写"锁是否闭合"这个事实,
        # 也不把 clause 记成通过 — 否则下一次真松动会被同一把"已消音"的门吞掉。
        {"id": "four_locks_all_closed", "ok": all_closed,
         "evidence": (f"observed={lk.get('observed')} closed={closed}"
                      + ("" if all_closed else
                         f" | 破例: active={waiver.get('active')} "
                         f"reason={waiver.get('reason')} "
                         f"waiver_id={waiver.get('waiver_id')} "
                         f"authorization_id={waiver.get('authorization_id')} "
                         f"expires={waiver.get('expires')} "
                         f"days_left={waiver.get('days_left')} "
                         f"covers_all_loose={waiver.get('covers_all_loose')} "
                         f"未被覆盖的松动锁={loose_locks or '无'} "
                         f"alarm_suppressed={alarm_suppressed}"))},
        {"id": "graduation_gate_redefined_preregistered", "ok": bool(gate_redefined),
         "evidence": ("新判据已预注册 + 用户批准 ⇒ 生效" if gate_redefined else
                      "旧 confidence IC 判据已 DEPRECATED; 新判据"
                      + (f"**草案已出** ({gate_prereg_draft}) 但**未生效** — OKR-2026-09 KR4 要求"
                         f"「预注册 + 用户批准」, 且「打标前严格不切实盘 = 用户明令」⇒ 批准权不在"
                         f"代理侧; 门禁保持现状 (fail-close = 安全侧)"
                         if gate_prereg_draft else
                         "**尚未**预注册/批准 ⇒ 门禁保持现状 (fail-close = 安全侧)"))},
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
        score=None, alarm=alarm,
        note=("四锁全闭 ✅ (硬门未破; 规则=任一松动即 alarm 呈裁, 不是扣分项)。门禁重定义未完成 "
              "⇒ KR4 记 NOT_MET, 但系统仍在安全侧 (graduated=False ⇒ fail-close)。"
              if all_closed else
              ("🔕 **四锁松动但已被有效预注册破例覆盖** (alarm 抑制) — RFC-135 "
               f"waiver_id={waiver.get('waiver_id')} expires={waiver.get('expires')} "
               f"days_left={waiver.get('days_left')}。破例到期或撤销后告警自动复响; "
               "破例期不代表锁已闭合 (closed 仍记实测真相)。"
               if alarm_suppressed else
               "🚨 **四锁松动** (alarm=True) — 立即呈裁, 不得继续跑任何结算")))


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


def settle(krs, today=None):
    """OKR-2026-09 §结算判据 (2026-09-30): KR1+KR2+KR4 全达成 ∧ KR3 给出干净的确认或证伪。

    本函数输出的是**当日投影**, 不是结算 — 结算只在 9/30 (窗闭合) 出。状态优先级 (取劣):
      NOT_MET (决定性: 硬门破/判据破) > NOT_MEASURABLE (尺子/载体不可用) >
      PENDING_USER_RULING (裁决权在人) > ACCUMULATING (窗未闭合/样本门未达) > MET。
    **不给百分比总分**: 五个二元/待裁/攒样项取算术平均 = 假精度 (旧块 overall_score=60 即此病)。
    """
    required = ("KR1", "KR2", "KR4")
    verdicts = {k: (v or {}).get("verdict") for k, v in krs.items()}
    counts = {}
    for v in verdicts.values():
        counts[v] = counts.get(v, 0) + 1
    today_s = str(today or "")[:10]
    window_closed = bool(today_s) and today_s >= OKR_SETTLEMENT_DATE
    blocking = sorted(k for k in required if verdicts.get(k) != VERDICT_MET)
    not_met = [k for k in (*required, "KR3") if verdicts.get(k) == VERDICT_NOT_MET]
    unmeasurable = [k for k in (*required, "KR3") if verdicts.get(k) == VERDICT_UNMEASURABLE]
    pending = [k for k in required if verdicts.get(k) == VERDICT_PENDING]
    accumulating = [k for k in ("KR2", "KR3")
                    if verdicts.get(k) in (VERDICT_ACCUMULATING, VERDICT_READY)]
    if not_met:
        status, why = VERDICT_NOT_MET, (
            f"决定性未达成 {not_met} — "
            + ("KR2 判据破 ⇒ 按预注册回滚线 9 月单版本样本作废, 10 月开跑会项目级裁决"
               if "KR2" in not_met else
               "KR4 四锁/门禁条款未完成 (硬门项, 不是扣分项)" if "KR4" in not_met else
               "KR3 违反「只许一个假设」⇒ 多重比较, 任何显著结果不得当 edge")
            + (f"; 另窗未闭合项 {accumulating}" if accumulating else ""))
    elif unmeasurable:
        status, why = VERDICT_UNMEASURABLE, (
            f"尺子/载体不可用 {unmeasurable} ⇒ 结算不可判 (不可测 ≠ 0 ≠ 达成)")
    elif pending:
        status, why = VERDICT_PENDING, (
            f"{pending} 挂用户裁决 ⇒ 结算不可自评 (OKR 原文「不得擅自结算」)")
    elif not window_closed or accumulating:
        status, why = VERDICT_ACCUMULATING, (
            f"窗未闭合 (今日 {today_s or '?'} < {OKR_SETTLEMENT_DATE}) 且 {accumulating or required} "
            f"仍在攒样/待裁决批 ⇒ 当日投影不结算; 判据本身在轨 (无 NOT_MET/不可测项)")
    else:
        status, why = VERDICT_MET, "KR1+KR2+KR4 全 MET 且 KR3 给出干净的确认或证伪"
    return {"rule": (f"{OKR_DOC} §结算判据: 达标 = KR1 + KR2 + KR4 全达成, 且 KR3 给出干净的"
                     f"确认或证伪 (两者都算达成); 未达标 = KR2 破 (又换参) 或 KR1 未完成"),
            "settlement_date": OKR_SETTLEMENT_DATE, "as_of": today_s or None,
            "window_closed": window_closed, "status": status, "why": why,
            "verdicts": verdicts, "counts": counts, "blocking": blocking,
            "not_met": not_met, "unmeasurable": unmeasurable, "pending": pending,
            "accumulating": accumulating,
            "no_numeric_total": ("本 OKR 不设盈利目标, 五个二元/待裁/攒样项取算术平均是假精度 "
                                 "(旧块 overall_score 即此病) ⇒ 只出 verdict 计数")}


def collect_and_score(repo_root, ic_playbook=None, graduation=None,
                      graduation_deprecated=None, trade_stats=None, window_days=30,
                      gate_redefined=False, now=None, cohort_settled=None, today=None):
    """读全部载体 → 判五个 KR → 结算状态。只读, 零副作用 (不写任何文件)。"""
    root = Path(repo_root)
    arts = {k: read_artifact(root, k, now=now) for k in ARTIFACTS}
    locks = measure_locks(root, graduation=graduation,
                          graduation_deprecated=graduation_deprecated)
    ts = trade_stats or {}
    krs = {
        "KR1": kr1_ruler(ic_playbook=ic_playbook, exit_scorecard=arts["exit_scorecard"]["doc"],
                         exit_scorecard_error=arts["exit_scorecard"]["error"]),
        "KR2": kr2_freeze(invariant=arts["invariant"], baseline=arts["freeze_baseline"],
                          cohort_settled=cohort_settled,
                          today=today or _now(now).date().isoformat()),
        "KR3": kr3_prereg(kr3=arts["kr3_verdict"], repo_root=root),
        "KR4": kr4_hard_gate(locks=locks, gate_redefined=gate_redefined,
                             weekly_checkpoint_present=(root / WEEKLY_CHECKPOINT).exists(),
                             gate_prereg_draft=(GATE_PREREG_DRAFT
                                                if (root / GATE_PREREG_DRAFT).exists() else None)),
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
        "settlement": settle(krs, today=today or _now(now).date().isoformat()),
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
                                            "NOT_MEASURABLE": "❓",
                                            "ACCUMULATING": "⏳",
                                            "READY_TO_ADJUDICATE": "🎯"}.get(b.get("verdict"), "·")
        lines.append(f"  {flag} {kr} {b.get('title','?'):<28} {b.get('verdict','?')}")
        note = b.get("note")
        if note:
            lines.append(f"      └ {note}")
    lines.append("  ⚠️ 上方 OVERALL % 是 OKR-2026-08 KR 集 (2026-08-23 SUPERSEDED) 的续算值, "
                 "不代表现行判据")
    return lines
