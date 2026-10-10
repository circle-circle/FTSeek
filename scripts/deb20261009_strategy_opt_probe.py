#!/usr/bin/env python3
"""DEB-20261009-event §25/§26「策略优化逻辑」独立复查探针 (只读, 零写入)。

用途: 复算 §25 四个 candidate (A EOD MFE 门 / B D2' 延迟确认 / C 入场锚 / D trailing+分批)
      的立项前提是否成立, 并核 OKR-2026-10 KR1 冻结窗状态。

纪律:
  - 只读 (sqlite mode=ro, git show, 文件读), 不写任何 DB/文件/config。
  - 净口径 COALESCE(pnl_net, realized_pnl) (DEB-20260730 D1)。
  - 窗口用 ET `trade_date` 列对齐, 不用 ISO 字符串比 UTC 时刻 (§26.2-1 建议)。
  - JOIN playbook_instructions 一律 COUNT(DISTINCT t.id) 核扇出。
  - 单位: trade_records.pnl_pct 小数 / mfe_pct·mae_pct 百分数 (同库两列不同, §26.2-3)。

跑法: PYTHONPATH=. ./venv/bin/python scripts/deb20261009_strategy_opt_probe.py
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
INV = REPO / "data" / "invariant_check_latest.json"
WINDOW_START = "2026-09-10"          # 近 30 日 (§25.1 同窗)
OCT_COHORT_START = "2026-09-17"      # scripts/daily_invariant_check.py:148
NET = "COALESCE(t.pnl_net, t.realized_pnl)"
# 已平仓真条件: exit_reason 非 NULL **且** 非空串, 且 exit_time 已落。
# ⚠️ 只用 `exit_reason IS NOT NULL` 挡不住 `''` — 实测 10-09 有 2 笔未平仓
# (id=1647 SNDK / 1652 AMD, exit_time IS NULL, exit_reason='') 会虚增样本。
BASE = (
    "t.source_type='PLAYBOOK_SIM' AND t.exit_reason IS NOT NULL AND t.exit_reason<>'' "
    "AND t.exit_time IS NOT NULL "
    f"AND date(t.trade_date) >= date('{WINDOW_START}')"
)
SELF = Path(__file__).resolve()


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else {}


# ── A. OKR-2026-10 KR1 冻结窗状态 (§25/§26 均未提, 但 candidate A/D 全触雷) ──
def seg_a_freeze_window() -> dict:
    out: dict = {"segment": "A", "title": "KR1 冻结窗 (oct_cohort) 状态"}
    if not INV.exists():
        out["error"] = f"{INV.name} 不存在"
        return out
    j = json.loads(INV.read_text(encoding="utf-8"))
    oc = ((j.get("config_freeze") or {}).get("oct_cohort") or {})
    out.update(
        status=oc.get("status"),
        n_unique=oc.get("n_unique"),
        versions={k: v.get("n") for k, v in (oc.get("versions") or {}).items()},
        n_closed=oc.get("n_closed"),
        n_target=oc.get("n_target"),
        hash_missing_rows=oc.get("hash_missing_rows"),
    )
    # meta_audit P0 逾期
    log = REPO / "logs" / "meta_audit.log"
    out["p0_open_overdue"] = []
    if log.exists():
        for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "P0 OPEN" in ln:
                out["p0_open_overdue"].append(ln.split("[meta_audit]")[-1].strip()[:120])
    return out


# ── B. 哈希切换根因: 410297295「配置显式化」自称零行为变更 ──
def seg_b_hash_root_cause() -> dict:
    out: dict = {"segment": "B", "title": "exit hash 切换根因 (git 复算)"}
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        out["error"] = "pyyaml 不可用"
        return out
    commit = "410297295"
    blobs = {}
    for ref, label in ((f"{commit}^", "before"), (commit, "after")):
        txt = subprocess.run(
            ["git", "show", f"{ref}:config/playbook_horizon.yaml"],
            capture_output=True, text=True, cwd=REPO,
        ).stdout
        if not txt:
            out["error"] = f"git show {ref} 失败"
            return out
        hc = yaml.safe_load(txt)
        b = json.dumps(hc, sort_keys=True, ensure_ascii=False, default=str)
        blobs[label] = {"sha1_12": hashlib.sha1(b.encode()).hexdigest()[:12], "len": len(b), "raw": hc}
    out["before_sha1_12"] = blobs["before"]["sha1_12"]
    out["after_sha1_12"] = blobs["after"]["sha1_12"]
    out["blob_changed"] = blobs["before"]["sha1_12"] != blobs["after"]["sha1_12"]

    def flat(d, p=""):
        o = {}
        for k, v in (d.items() if isinstance(d, dict) else []):
            if isinstance(v, dict):
                o.update(flat(v, f"{p}{k}."))
            else:
                o[f"{p}{k}"] = v
        return o

    fb, fa = flat(blobs["before"]["raw"]), flat(blobs["after"]["raw"])
    out["changed_keys"] = {
        k: {"before": fb.get(k, "<无>"), "after": fa.get(k, "<无>")}
        for k in sorted(set(fb) | set(fa)) if fb.get(k, "<M>") != fa.get(k, "<M>")
    }
    msg = subprocess.run(["git", "log", "-1", "--format=%s%n%b", commit],
                         capture_output=True, text=True, cwd=REPO).stdout
    out["commit_claim"] = [ln.strip() for ln in msg.splitlines()
                           if "零行为变更" in ln or "显式化" in ln][:2]
    # horizon_config 是否整体进 exit hash
    cfg = (REPO / "app" / "playbook" / "config.py").read_text(encoding="utf-8", errors="replace")
    out["horizon_config_in_hash"] = '"horizon_config"' in cfg
    return out


# ── C. §25.2 因果检验: 两族是否真「被 W_WEEK 隔夜豁免拖成多夜」 ──
def seg_c_left_tail_causality(db) -> dict:
    out: dict = {"segment": "C", "title": "§25.2 左尾因果检验 (持仓时长/跨日/MFE)"}
    out["families"] = [
        _one(db, f"""SELECT t.exit_reason, COUNT(DISTINCT t.id) n,
             ROUND(SUM({NET}),2) net, ROUND(AVG({NET}),2) avg_net,
             ROUND(AVG(t.holding_hours),2) avg_hold_h, ROUND(MIN(t.holding_hours),2) min_h,
             ROUND(MAX(t.holding_hours),2) max_h,
             SUM(CASE WHEN date(t.exit_time)>date(t.entry_time) THEN 1 ELSE 0 END) n_cross_day,
             ROUND(AVG(t.mfe_pct),3) avg_mfe,
             SUM(CASE WHEN t.mfe_pct<0.3 THEN 1 ELSE 0 END) n_grave
             FROM trade_records t WHERE {BASE} AND t.exit_reason=? GROUP BY 1""", (er,))
        for er in ("OVERNIGHT_GAP_STOP", "SL_INITIAL", "tighten", "breakeven_lock", "lock_in")
    ]
    out["families"] = [f for f in out["families"] if f.get("n")]
    reported = sum(f["net"] for f in out["families"]
                   if f["exit_reason"] in ("SL_INITIAL", "OVERNIGHT_GAP_STOP"))
    total = sum(f["net"] for f in out["families"])
    out["s25_2_reported_net"] = round(reported, 2)
    out["left_tail_total_net"] = round(total, 2)
    out["s25_2_coverage_pct"] = round(100 * reported / total, 1) if total else None
    # 最长持仓笔 (真「多夜」证据在哪一族)
    out["longest_hold"] = [
        dict(r) for r in db.execute(
            f"""SELECT t.symbol, t.exit_reason, ROUND(t.holding_hours,1) h, t.mfe_pct, t.mae_pct,
                ROUND({NET},2) net, t.trade_date FROM trade_records t
                WHERE {BASE} AND {NET}<0 ORDER BY t.holding_hours DESC LIMIT 5""")
    ]
    return out


# ── D. 仙股费用污染: 剔除高 bps 单后画像是否翻转 ──
def seg_d_penny_fee_contamination(db) -> dict:
    out: dict = {"segment": "D", "title": "费用 bps 异常单 (仙股) 污染面"}
    rows = [dict(r) for r in db.execute(
        f"""SELECT t.id, t.symbol, t.quantity, t.entry_price,
            ROUND(t.quantity*t.entry_price,2) notional, ROUND(COALESCE(t.fees_est,0),2) fees,
 ROUND(COALESCE({NET},0),2) net, ROUND(COALESCE(t.realized_pnl,0),2) gross,
 COALESCE(t.mfe_pct,0) mfe_pct, t.exit_reason, t.trade_date,
 CASE WHEN t.pnl_net IS NULL AND t.realized_pnl IS NULL THEN 1 ELSE 0 END null_pnl
 FROM trade_records t WHERE {BASE} AND t.quantity>0 AND t.entry_price>0""")]
    for r in rows:
        r["fee_bps"] = round(10000 * r["fees"] / r["notional"], 1) if r["notional"] else None
    hi = [r for r in rows if r["fee_bps"] and r["fee_bps"] > 100]
    keep = [r for r in rows if not (r["fee_bps"] and r["fee_bps"] > 100)]
    out["n_total"] = len(rows)
    out["n_fee_est_zero"] = sum(1 for r in rows if r["fees"] == 0)
    out["n_null_pnl"] = sum(1 for r in rows if r.get("null_pnl"))
    out["all_fees"] = round(sum(r["fees"] for r in rows), 2)
    out["all_net"] = round(sum(r["net"] for r in rows), 2)
    out["high_fee_rows"] = [
        {k: r[k] for k in ("symbol", "trade_date", "quantity", "entry_price",
                           "notional", "fees", "fee_bps", "gross", "net",
                           "mfe_pct", "exit_reason")}
        for r in sorted(hi, key=lambda x: -x["fee_bps"])
    ]
    out["high_fee_n"] = len(hi)
    out["high_fee_fees"] = round(sum(r["fees"] for r in hi), 2)
    out["high_fee_share_of_all_fees_pct"] = (
        round(100 * out["high_fee_fees"] / out["all_fees"], 1) if out["all_fees"] else None)
    out["after_exclude_n"] = len(keep)
    out["after_exclude_net"] = round(sum(r["net"] or 0 for r in keep), 2)
    out["after_exclude_win_rate_pct"] = (
        round(100 * sum(1 for r in keep if r["net"] > 0) / len(keep), 1) if keep else None)
    out["net_sign_flips"] = (out["all_net"] < 0) != (out["after_exclude_net"] < 0)
    # 左尾族剔除前后
    out["families_after_exclude"] = {}
    for er in ("OVERNIGHT_GAP_STOP", "SL_INITIAL", "tighten", "breakeven_lock"):
        k = [r for r in keep if r["exit_reason"] == er]
        h = [r for r in hi if r["exit_reason"] == er]
        out["families_after_exclude"][er] = {
            "kept_n": len(k), "kept_net": round(sum(r["net"] for r in k), 2),
            "excluded_n": len(h), "excluded_net": round(sum(r["net"] for r in h), 2),
        }
    return out


# ── E. 墓地分解 (skill 标准动作, §25 未做) ──
def seg_e_graveyard(db) -> dict:
    out: dict = {"segment": "E", "title": "全样本 MFE 墓地分解 (病灶在入场还是退出)"}
    out["buckets"] = [
        dict(r) for r in db.execute(
            f"""SELECT CASE WHEN t.mfe_pct<0.3 THEN '1_MFE<0.3%(墓地)'
                 WHEN t.mfe_pct<0.5 THEN '2_0.3-0.5%' WHEN t.mfe_pct<1.0 THEN '3_0.5-1%'
                 ELSE '4_>=1%' END b, COUNT(DISTINCT t.id) n, ROUND(SUM({NET}),2) net,
                 ROUND(100.0*SUM(CASE WHEN {NET}>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr,
                 ROUND(AVG(t.fees_est),2) avg_fee
                 FROM trade_records t WHERE {BASE} GROUP BY 1 ORDER BY 1""")]
    tot = _one(db, f"SELECT ROUND(SUM({NET}),2) net FROM trade_records t WHERE {BASE}")["net"]
    grv = _one(db, f"SELECT ROUND(SUM({NET}),2) net FROM trade_records t WHERE {BASE} AND t.mfe_pct<0.3")["net"]
    out["sample_net"] = tot
    out["graveyard_net"] = grv
    out["graveyard_share_of_net_pct"] = round(100 * grv / tot, 1) if tot else None
    # 费用 vs 毛利 (BE 族「给券商打工」检验)
    out["fee_vs_gross"] = _one(db, f"""SELECT ROUND(SUM(t.realized_pnl),2) gross,
        ROUND(SUM({NET}),2) net, ROUND(SUM(t.fees_est),2) fees, COUNT(DISTINCT t.id) n,
        ROUND(AVG(t.fees_est),2) avg_fee FROM trade_records t WHERE {BASE}""")
    g = out["fee_vs_gross"]["gross"]
    out["fee_to_gross_ratio"] = round(out["fee_vs_gross"]["fees"] / abs(g), 2) if g else None
    out["by_exit_reason"] = [
        dict(r) for r in db.execute(
            f"""SELECT t.exit_reason, COUNT(DISTINCT t.id) n, ROUND(SUM({NET}),2) net,
                ROUND(AVG({NET}),2) avg_net, ROUND(AVG(t.mfe_pct),3) avg_mfe,
                ROUND(SUM(t.fees_est),2) fees, ROUND(AVG(t.fees_est),2) avg_fee
                FROM trade_records t WHERE {BASE} GROUP BY 1 ORDER BY net""")]
    return out


# ── F. candidate A 可拦截额 / 误杀 / 事前不可观测性 ──
def seg_f_candidate_a(db) -> dict:
    out: dict = {"segment": "F", "title": "candidate A (short-horizon EOD MFE<0.5% 门) 前提检验"}
    rows = [dict(r) for r in db.execute(
        f"""SELECT t.id, t.symbol, t.mfe_pct, t.mae_pct, ROUND({NET},2) net, t.exit_reason,
            ROUND(t.holding_hours,1) h, t.direction,
            CASE WHEN date(t.exit_time)>date(t.entry_time) THEN 1 ELSE 0 END cross_day
            FROM trade_records t JOIN playbook_instructions p ON p.decision_id=t.decision_id
            WHERE {BASE} AND p.horizon='short'
              AND date(t.exit_time)>date(t.entry_time)""")]
    # 扇出核验
    fan = _one(db, f"""SELECT COUNT(*) rows_join, COUNT(DISTINCT t.id) ids
        FROM trade_records t JOIN playbook_instructions p ON p.decision_id=t.decision_id
        WHERE {BASE} AND p.horizon='short' AND date(t.exit_time)>date(t.entry_time)""")
    out["join_fanout"] = {"rows_join": fan.get("rows_join"), "distinct_ids": fan.get("ids"),
                          "ok": fan.get("rows_join") == fan.get("ids")}
    blocked = [r for r in rows if r["mfe_pct"] < 0.5]
    passed = [r for r in rows if r["mfe_pct"] >= 0.5]
    out["blocked_n"] = len(blocked)
    out["blocked_net"] = round(sum(r["net"] for r in blocked), 2)
    out["blocked_gap_n"] = sum(1 for r in blocked if r["exit_reason"] == "OVERNIGHT_GAP_STOP")
    out["blocked_gap_net"] = round(
        sum(r["net"] for r in blocked if r["exit_reason"] == "OVERNIGHT_GAP_STOP"), 2)
    out["blocked_nongap_n"] = len(blocked) - out["blocked_gap_n"]
    out["blocked_nongap_net"] = round(out["blocked_net"] - out["blocked_gap_net"], 2)
    out["blocked_winners_false_kill_candidates"] = sum(1 for r in blocked if r["net"] > 0)
    out["passed_n"] = len(passed)
    out["passed_net"] = round(sum(r["net"] for r in passed), 2)
    out["passed_gap_n"] = sum(1 for r in passed if r["exit_reason"] == "OVERNIGHT_GAP_STOP")
    # 事前不可观测性: 落库 mfe_pct 是全生命周期峰值 (含次日), 门要在 EOD 判
    out["ex_ante_observable"] = False
    out["ex_ante_note"] = (
        "落库 mfe_pct 为全生命周期峰值(含次日), 门在 EOD 判定时事前只有『EOD 时刻浮盈』; "
        "用 mfe_pct 分层 = 事后口径 ⇒ 误杀率被系统性低估。须从 metadata_json/1min bars 重算 EOD 时点峰值。")
    out["upper_bound_if_zeroed"] = out["blocked_net"]
    out["upper_bound_caveat"] = "『拦下即归零』乐观上限; 真实值须逐笔 EOD 平仓反事实 + 多付一腿费"
    return out


# ── G. candidate D 第 3 条是否重提已否决轴 ──
def seg_g_candidate_d_prior_verdict() -> dict:
    out: dict = {"segment": "G", "title": "candidate D『+0.8% 部分止盈 1/3』vs 既有裁决"}
    deb = REPO / "docs" / "debates" / "DEB-20261008-profit-ladder-scaleout-feasibility.md"
    out["deb_20261008_exists"] = deb.exists()
    if deb.exists():
        txt = deb.read_text(encoding="utf-8", errors="replace")
        out["verdict_lines"] = [ln.strip()[:150] for ln in txt.splitlines()
                                if ("否决实装" in ln or ln.strip().startswith("| **B1**")
                                    or ln.strip().startswith("| **B2**"))][:4]
        out["mentions_partial_take_profit"] = "分批止盈" in txt
    pol = REPO / "app" / "risk" / "exit_policy.yaml"
    out["scale_out_dead_rule"] = None
    if pol.exists():
        for i, ln in enumerate(pol.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "DEAD RULE" in ln and "scale_out" in ln.lower():
                out["scale_out_dead_rule"] = f"{pol.name}:{i} {ln.strip()[:110]}"
                break
    prof = REPO / "config" / "exit_profiles.yaml"
    if prof.exists():
        head = prof.read_text(encoding="utf-8", errors="replace").splitlines()[:8]
        out["exit_profiles_header"] = [ln.strip()[:110] for ln in head if ln.strip().startswith("#")][:4]
    return out


# ── H. 非标准 exit_reason 谱系 (脏标签窗口) ──
def seg_h_exit_reason_lineage(db) -> dict:
    out: dict = {"segment": "H", "title": "非标准 exit_reason 谱系 (标签窗口/来源)"}
    out["nonstandard"] = [
        dict(r) for r in db.execute(
            """SELECT exit_reason, COUNT(*) n, MIN(trade_date) first_d, MAX(trade_date) last_d,
               GROUP_CONCAT(DISTINCT source_type) srcs, GROUP_CONCAT(DISTINCT mode) modes
               FROM trade_records
               WHERE exit_reason IN ('tighten','breakeven_lock','lock_in','P3_DUP_CLEANUP','')
                  OR exit_reason IS NULL
               GROUP BY 1 ORDER BY n DESC""")]
    out["open_not_closed"] = [
        dict(r) for r in db.execute(
            """SELECT id, symbol, trade_date, entry_time, exit_reason, config_version
               FROM trade_records
               WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NULL
                 AND date(trade_date) >= date('2026-09-10')""")]
    out["production_enum_recent"] = [
        r[0] for r in db.execute(
            f"""SELECT DISTINCT t.exit_reason FROM trade_records t WHERE {BASE}
                AND t.exit_reason NOT IN ('tighten','breakeven_lock','lock_in','P3_DUP_CLEANUP','')
                ORDER BY 1""")]
    return out


def main() -> int:
    if not DB.exists():
        print(f"[FATAL] DB 不存在: {DB}", file=sys.stderr)
        return 2
    db = _db()
    segs = [
        seg_a_freeze_window(),
        seg_b_hash_root_cause(),
        seg_c_left_tail_causality(db),
        seg_d_penny_fee_contamination(db),
        seg_e_graveyard(db),
        seg_f_candidate_a(db),
        seg_g_candidate_d_prior_verdict(),
        seg_h_exit_reason_lineage(db),
    ]
    print(json.dumps({"probe": SELF.name, "db": str(DB.relative_to(REPO)),
                      "window_start": WINDOW_START, "net_caliber": "COALESCE(pnl_net, realized_pnl)",
                      "segments": segs}, ensure_ascii=False, indent=1, default=str))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
