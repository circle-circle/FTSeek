#!/usr/bin/env python3
"""Jev 两个 DEB 的冲突复核探针 (只读, 零写入)。

背景: 同轴两份 DEB 给出相反的可落地判断 —
  - DEB-20260924 (727 行, 已提交 ab8bff1cc): n=200 实弹 AUC 0.4030 反向 + 八场景普查
    「明确可用 = 0」+ §5.1 Q3(告警分诊) **明确作废**(真值面已死)。
  - DEB-20261011 (150 行, untracked): 把「Ops 分诊 error_alert_carrier」列为
    **唯一当前可影子**候选①, §2.2 撞车普查**未引用 20260924**, §1.2 称
    「未做本地接入验证」。

本探针只回答一件事: **20260924 判死的真值面/体量, 到 20261011 是否复活了?**
若仍死 ⇒ 候选① 立项在死面上; 若复活 ⇒ 20260924 的结论需更新。

纪律: 只读 (sqlite mode=ro / 文件读 / git show), 零写入、零外发、零 LLM 调用。
跑法: PYTHONPATH=. ./venv/bin/python scripts/deb20261011_jev_conflict_probe.py
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _cols(db, table):
    try:
        return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return []


# ── A. 告警分诊真值面 (20260924 §4.5.3 场景1 判死; 20261011 候选① 立项于此) ──
def seg_a_alert_triage(db) -> dict:
    out: dict = {"segment": "A", "title": "告警/Ops 分诊真值面是否复活 (决定候选①生死)"}
    for tbl in ("failures", "events", "alerts", "issue_registry"):
        cols = _cols(db, tbl)
        if not cols:
            out[tbl] = "<表不存在>"
            continue
        n = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        dcol = next((c for c in ("created_at", "timestamp", "ts", "occurred_at", "date")
                     if c in cols), None)
        last30 = None
        if dcol and n:
            last30 = db.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE date({dcol}) >= date('now','-30 day')"
            ).fetchone()[0]
        info = {"n_total": n, "date_col": dcol, "n_last_30d": last30}
        if dcol and n:
            r = db.execute(f"SELECT MIN(date({dcol})) a, MAX(date({dcol})) b FROM {tbl}").fetchone()
            info["span"] = [r["a"], r["b"]]
        out[tbl] = info
    # recovered 字段填充率 (20260924 实测全 0 = 无标签)
    if "recovered" in _cols(db, "failures"):
        r = db.execute("SELECT COUNT(*) n, SUM(CASE WHEN recovered IS NOT NULL AND recovered<>0 "
                       "THEN 1 ELSE 0 END) nz FROM failures").fetchone()
        out["failures_recovered_fill"] = {"n": r["n"], "nonzero": r["nz"]}
    # failure_type 分布 (20260924 实测 763 行全 exec_rejected)
    fc = _cols(db, "failures")
    tcol = next((c for c in ("failure_type", "type", "kind", "category") if c in fc), None)
    if tcol:
        out["failures_type_dist"] = [
            dict(r) for r in db.execute(
                f"SELECT {tcol} t, COUNT(*) n FROM failures GROUP BY 1 ORDER BY n DESC LIMIT 6")]
    # error_alert_carrier 的冷却态 (20260924 实测 .alert_state.cooldown 仅 3 条)
    st = REPO / ".alert_state.cooldown"
    out["alert_state_cooldown"] = (
        {"exists": True, "lines": len(st.read_text(encoding="utf-8", errors="replace").splitlines())}
        if st.exists() else {"exists": False})
    for cand in ("data/.alert_state.cooldown", "data/alert_state.cooldown"):
        p = REPO / cand
        if p.exists():
            out["alert_state_cooldown"] = {"path": cand, "exists": True,
                                           "lines": len(p.read_text(errors="replace").splitlines())}
    # 日志 ERROR 近 2 天 (20260924 实测 1 条)
    # 日志体量 (候选① 的输入面): 级别用 error_alert.py 的字面量契约 "] ERROR:" / "] CRITICAL:"
    out["log_volume"] = {}
    for rel in ("logs/ftseek_us.log", "logs/error_alert.log"):
        f = REPO / rel
        if not f.exists():
            out["log_volume"][rel] = "<不存在>"
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        out["log_volume"][rel] = {
            "lines": txt.count("\n"),
            "ERROR": txt.count("] ERROR:"), "CRITICAL": txt.count("] CRITICAL:"),
            "loose_ERROR_substr": txt.count("ERROR"),
        }
    # ⚠️ error_alert_carrier.py 是 RFC-094 的 **Hermes no_agent 载体包装器**, 不是分诊器:
    # 其 docstring 明写「只换载体, 不换判据」「阈值/基线/冷却键/推送路径逐字未改」「正常时零输出」。
    # 真判据在 scripts/error_alert.py ⇒ 有没有「严重度分级 / 升级 vs 吸收」的决策空间要查它。
    ea = REPO / "scripts" / "error_alert.py"
    if ea.exists():
        t = ea.read_text(encoding="utf-8", errors="replace")
        out["error_alert_py"] = {
            "exists": True,
            "MIN_THRESHOLD": next((ln.split("=")[1].split("#")[0].strip()
                                   for ln in t.splitlines() if ln.startswith("MIN_THRESHOLD")), None),
            "THRESHOLD_MULT": next((ln.split("=")[1].split("#")[0].strip()
                                    for ln in t.splitlines() if ln.startswith("THRESHOLD_MULT")), None),
            "level_markers_line": next((ln.strip()[:90] for ln in t.splitlines()
                                        if ln.startswith("LEVEL_MARKERS")), None),
            "self_reported_volume": next((ln.strip()[:130] for ln in t.splitlines()
                                          if "0-5 条/日" in ln or "0-5条/日" in ln), None),
        }
    car = REPO / "ops" / "hermes" / "error_alert_carrier.py"
    if car.exists():
        t = car.read_text(encoding="utf-8", errors="replace")
        out["carrier_role"] = {
            "is_wrapper_not_triager": ("只换载体, 不换判据" in t or "只换载体,不换判据" in t),
            "delegates_to": "scripts/error_alert.py" if "error_alert.py" in t else None,
            "zero_output_when_normal": ("零输出" in t),
            "has_severity_grading_logic": any(k in t for k in ("severity", "escalate", "升级", "分级")),
        }
    return out


# ── B. 复盘归因表 (20260924 场景2 判死: 0/223 + 停更 6-26) ──
def seg_b_attribution(db) -> dict:
    out: dict = {"segment": "B", "title": "trade_attribution 真值面 (20260924 判死: 停更3个月)"}
    cols = _cols(db, "trade_attribution")
    if not cols:
        out["status"] = "<表不存在>"
        return out
    n = db.execute("SELECT COUNT(*) FROM trade_attribution").fetchone()[0]
    out["n_total"] = n
    for c in ("root_cause_category", "llm_model", "human_feedback", "key_decision_factor"):
        if c in cols:
            r = db.execute(f"SELECT SUM(CASE WHEN {c} IS NOT NULL AND {c}<>'' THEN 1 ELSE 0 END) "
                           f"FROM trade_attribution").fetchone()[0]
            out[f"{c}_nonzero"] = r
    dcol = next((c for c in ("created_at", "trade_date", "timestamp") if c in cols), None)
    if dcol and n:
        r = db.execute(f"SELECT MAX(date({dcol})) FROM trade_attribution").fetchone()[0]
        out["last_update"] = r
        out["days_stale"] = db.execute(
            f"SELECT CAST(julianday('now') - julianday(MAX(date({dcol}))) AS INT) "
            "FROM trade_attribution").fetchone()[0]
    return out


# ── C. LLM 成本 (20260924 §11⑥ 删除「成本理由」: 7天全部花销 $0.8675) ──
def seg_c_llm_cost(db) -> dict:
    out: dict = {"segment": "C", "title": "LLM 花销 (决定候选②『省 call 数』的价值上限)"}
    ldb = REPO / "data" / "llm_audit.db"
    if not ldb.exists():
        out["status"] = "<llm_audit.db 不存在>"
        return out
    c = sqlite3.connect(f"file:{ldb}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    cols = [r[1] for r in c.execute("PRAGMA table_info(llm_calls)")]
    out["cols"] = cols[:18]
    tcol = next((x for x in ("timestamp", "created_at", "ts") if x in cols), None)
    costcol = next((x for x in ("cost_usd", "total_cost", "cost") if x in cols), None)
    # DEB-20260924 §3② 实测: llm_calls.timestamp 是 **epoch 浮点**, 不是文本
    # ⇒ 必须用 time.time() 比, 不能用 date()/datetime() 字符串比较。
    if tcol:
        import time as _t
        now = _t.time()
        is_epoch = True
        r0 = c.execute(f"SELECT {tcol} FROM llm_calls WHERE {tcol} IS NOT NULL LIMIT 1").fetchone()
        if r0 is not None:
            v = r0[0]
            is_epoch = isinstance(v, (int, float)) or (
                isinstance(v, str) and v.replace(".", "", 1).isdigit())
        out["ts_is_epoch"] = is_epoch
        out["ts_sample"] = r0[0] if r0 is not None else None
        for win in (7, 30):
            cut = now - win * 86400
            if is_epoch:
                r = c.execute(
                    f"SELECT COUNT(*) n, ROUND(SUM(COALESCE({costcol},0)),4) cost "
                    f"FROM llm_calls WHERE CAST({tcol} AS REAL) >= ?", (cut,)).fetchone()
            else:
                r = c.execute(
                    f"SELECT COUNT(*) n, ROUND(SUM(COALESCE({costcol},0)),4) cost "
                    f"FROM llm_calls WHERE date({tcol}) >= date('now', ?)",
                    (f"-{win} day",)).fetchone()
            out[f"last_{win}d"] = {"n_calls": r["n"], "cost_usd": r["cost"]}
    if costcol:
        out["all_time_cost_usd"] = c.execute(
            f"SELECT ROUND(SUM(COALESCE({costcol},0)),4) FROM llm_calls").fetchone()[0]
        # 按模型分 (20261011 候选② 的目标是省 LLM Validator / deep_research 的调用)
        mcol = next((x for x in ("model", "model_name", "llm_model") if x in cols), None)
        if mcol:
            out["by_model_30d"] = [
                dict(x) for x in c.execute(
                    f"SELECT {mcol} model, COUNT(*) n, ROUND(SUM(COALESCE({costcol},0)),4) cost "
                    f"FROM llm_calls WHERE CAST({tcol} AS REAL) >= ? GROUP BY 1 ORDER BY cost DESC LIMIT 8",
                    (now - 30 * 86400,))] if is_epoch else []
    c.close()
    return out


# ── D. 20261011 §1.3 六个代码锚点是否真实 ──
def seg_d_code_anchors() -> dict:
    out: dict = {"segment": "D", "title": "20261011 §1.3 代码锚点核验 (逐行读码, 不信描述)"}
    anchors = [
        ("app/plan/llm_validator.py", 2, "Stage 2"),
        ("app/plan/deep_research_pipeline.py", 9, "9 次 LLM 调用"),
        ("app/web_dashboard/order/gates.py", 8, "只读聚合"),
        ("app/plan/position_intelligence.py", 1, "纯函数式"),
        ("app/web_dashboard/routes/chain.py", 6, "decision_id"),
    ]
    out["anchors"] = []
    for rel, line, claim in anchors:
        p = REPO / rel
        e = {"file": rel, "exists": p.exists(), "claimed_line": line, "claim": claim}
        if p.exists():
            ls = p.read_text(encoding="utf-8", errors="replace").splitlines()
            e["total_lines"] = len(ls)
            e["line_content"] = ls[line - 1].strip()[:110] if 0 < line <= len(ls) else "<越界>"
            e["line_in_range"] = 0 < line <= len(ls)
        out["anchors"].append(e)
    # ops 载体是否存在
    out["ops_carriers"] = {}
    for rel in ("ops/hermes/futu_channel_adjudication_run.py",
                "ops/hermes/error_alert_carrier.py",
                "ops/audit/audit_da_pricing_quality.py"):
        out["ops_carriers"][rel] = (REPO / rel).exists()
    return out


# ── E. 唯一活口: 穿透判别 (20260924 场景5/F14) 体量与真值是否仍在 ──
def seg_e_penetration() -> dict:
    out: dict = {"segment": "E", "title": "穿透模式判别 (20260924 判为唯一活口 F14) 现状"}
    # ⚠️ 首版找 data/penetration_shadow.jsonl (不存在) 后回退抓到
    # penetration_analysis_latest.json (是 JSON 非 jsonl) ⇒ 全 0 假读数。真实路径在 shadow/。
    p = REPO / "data" / "shadow" / "penetration_shadow.jsonl"
    if not p.exists():
        cands = sorted((REPO / "data" / "shadow").glob("*penetration*"))
        out["status"] = f"<{p.relative_to(REPO)} 不存在>; 候选={[c.name for c in cands][:5]}"
        return out
    lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    out["path"] = str(p.relative_to(REPO))
    out["n_lines"] = len(lines)
    import collections as _c
    objs = []
    n_bad = 0
    for ln in lines:
        try:
            objs.append(json.loads(ln))
        except json.JSONDecodeError:
            n_bad += 1  # 坏行计数留痕 (R5): "文件损坏" 与 "查了没查出" 必须可区分
    out["n_bad_json"] = n_bad
    # 行分两类: type=penetration(触发) / type=outcome(结算); labeled 在 **status** 字段
    out["type_dist"] = dict(_c.Counter(o.get("type") for o in objs).most_common())
    out["status_dist"] = {str(k): v for k, v in _c.Counter(o.get("status") for o in objs).most_common()}
    out["n_labeled"] = sum(1 for o in objs if o.get("status") == "labeled")
    truth = [o for o in objs
             if any(k in o for k in ("recovered_1m", "recovered_3m", "recovered_5m", "recovered_10m"))]
    out["n_with_truth_fields"] = len(truth)
    # 日期键 = et_date (首版用 ts/timestamp/date ⇒ distinct 天数 0 是探针错)
    days = _c.Counter(o["et_date"] for o in objs if o.get("et_date"))
    out["n_distinct_days"] = len(days)
    out["per_day"] = round(len(objs) / len(days), 2) if days else None
    out["date_span"] = [min(days), max(days)] if days else None
    out["truth_by_month"] = dict(_c.Counter((o.get("et_date") or "?")[:7] for o in truth).most_common())
    # 少数类失衡 (20260924 记 92:2; 判据须按少数类召回而非 AUC)
    out["class_imbalance"] = {}
    for k in ("recovered_1m", "recovered_3m", "recovered_5m", "recovered_10m"):
        sub = [o for o in truth if k in o]
        if not sub:
            continue
        t = sum(1 for o in sub if o[k])
        f = len(sub) - t
        out["class_imbalance"][k] = {
            "n": len(sub), "true": t, "false": f,
            "minority_n": min(t, f),
            "ratio": f"{round(100*max(t,f)/len(sub),1)}:{round(100*min(t,f)/len(sub),1)}",
        }
    if objs:
        out["sample_keys"] = sorted(objs[-1].keys())
    return out


# ── F. KR1 冻结窗状态 (20261011 §1.4 称「须恒为1」; 实测?) ──
def seg_f_freeze_window() -> dict:
    out: dict = {"segment": "F", "title": "OKR-2026-10 KR1 冻结窗 (20261011 D5 的前提)"}
    inv = REPO / "data" / "invariant_check_latest.json"
    if not inv.exists():
        out["status"] = "<invariant_check_latest.json 不存在>"
        return out
    j = json.loads(inv.read_text(encoding="utf-8"))
    oc = ((j.get("config_freeze") or {}).get("oct_cohort") or {})
    out.update({k: oc.get(k) for k in
                ("status", "n_unique", "n_closed", "n_target", "hash_missing_rows")})
    out["versions"] = {k: v.get("n") for k, v in (oc.get("versions") or {}).items()}
    out["claim_in_20261011"] = "KR1 变更预算 = 0 次（oct cohort 退出哈希唯一值须恒为 1）"
    out["claim_holds"] = (oc.get("n_unique") == 1 and oc.get("status") != "broken")
    return out


# ── G. 两个 DEB 的交叉引用关系 (撞车普查是否漏查) ──
def seg_g_cross_reference() -> dict:
    out: dict = {"segment": "G", "title": "两 DEB 交叉引用 (20261011 是否引用 20260924)"}
    new = REPO / "docs" / "debates" / "DEB-20261011-jev-structured-decision-automation-discipline.md"
    old = REPO / "docs" / "debates" / "DEB-20260924-jev-bounded-judgment-layer-feasibility.md"
    for tag, p in (("new_20261011", new), ("old_20260924", old)):
        if not p.exists():
            out[tag] = "<不存在>"
            continue
        t = p.read_text(encoding="utf-8", errors="replace")
        # ⚠️ 自指排除 (第三形态): 本探针的复查节 §8 **引用了前案** ⇒ 若统计全文,
        # 「新 DEB 未提及 20260924」这条核心证据会被我自己的复查节推翻
        # (实测 mentions_20260924 False→True、lines 151→315)。
        # 判据对象是**作者的原文**, 故一律截断到复查节之前。
        for marker in ("## 8. 第二 Agent 独立复查", "## 13. 第二 Agent 独立复查",
                       "## 27. 第三 Agent 独立复查"):
            if marker in t:
                t = t.split(marker)[0]
                out[f"{tag}_truncated_at"] = marker
                break
        out[tag] = {
            "lines": t.count("\n") + 1,
            "git_tracked": subprocess.run(["git", "ls-files", "--error-unmatch", str(p)],
                                          capture_output=True, cwd=REPO).returncode == 0,
            "mentions_20260924": "20260924" in t,
            "mentions_20261011": "20261011" in t,
            "mentions_AUC_0403": ("0.403" in t or "0.4030" in t),
            "mentions_Q3_voided": ("Q3" in t and ("作废" in t)),
            "mentions_scene_census_zero": ("明确可用" in t and "= 0" in t),
            "mentions_serialization_sensitivity": ("序列化" in t),
            "mentions_bailian_ws": ("llm-nbhpc6wqt1x6i8bw" in t or "decision-model-preview" in t),
            "claims_no_local_validation": ("未做本地接入验证" in t),
        }
    return out


def main() -> int:
    if not DB.exists():
        print(f"[FATAL] DB 不存在: {DB}")
        return 2
    db = _db()
    segs = [seg_a_alert_triage(db), seg_b_attribution(db), seg_c_llm_cost(db),
            seg_d_code_anchors(), seg_e_penetration(), seg_f_freeze_window(),
            seg_g_cross_reference()]
    print(json.dumps({"probe": Path(__file__).name, "segments": segs},
                     ensure_ascii=False, indent=1, default=str))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
