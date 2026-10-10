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
import gzip
import re
import sqlite3
import subprocess
import sys
import statistics
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

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
ET_ZONE = ZoneInfo("America/New_York")


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _one(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else {}


def _et_naive_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(ET_ZONE).replace(tzinfo=None)


def _bar_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _local_replay_bars(symbol: str, et_date: str) -> list | None:
    path = REPO / "data" / "replay_bars" / f"{symbol.replace('.', '_')}_{et_date}.json"
    if not path.exists():
        return None
    try:
        bars = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return bars if isinstance(bars, list) else None


def _candidate_a_eod_metrics(row: dict, bars: list) -> dict | None:
    entry_time = _et_naive_datetime(row.get("entry_time") or "")
    marked_at = _et_naive_datetime(row.get("marked_at_et") or "")
    if entry_time is None or marked_at is None:
        return None
    eod_cutoff = min(marked_at, datetime.combine(marked_at.date(), time(16, 0)))
    usable_bars = []
    n_bad_bars = 0
    for bar in bars:
        if not isinstance(bar, list) or len(bar) < 5:
            continue
        bar_time = _bar_datetime(bar[0])
        if bar_time is None or bar_time > eod_cutoff:
            continue
        if bar_time.date() == entry_time.date() and bar_time < entry_time:
            continue
        try:
            usable_bars.append((bar_time, float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])))
        except (TypeError, ValueError):
            n_bad_bars += 1  # 坏 bar 计数留痕 (except 棘轮纪律), 不静默
            continue
    if not usable_bars:
        return None
    direction = row.get("direction")
    entry_price = float(row["entry_price"])
    quantity = int(row["quantity"])
    if direction == "LONG":
        excursion_price = max(bar[2] for bar in usable_bars)
        eod_mfe_pct = (excursion_price - entry_price) / entry_price * 100.0
    elif direction == "SHORT":
        excursion_price = min(bar[3] for bar in usable_bars)
        eod_mfe_pct = (entry_price - excursion_price) / entry_price * 100.0
    else:
        return None
    eod_close = usable_bars[-1][4]
    close_direction = 1.0 if direction == "LONG" else -1.0
    eod_close_net = close_direction * (eod_close - entry_price) * quantity - float(row["fees"] or 0.0)
    eod_close_return_pct = close_direction * (eod_close - entry_price) / entry_price * 100.0
    return {
        "eod_mfe_pct": round(eod_mfe_pct, 4),
        "eod_close": round(eod_close, 4),
        "eod_close_net": round(eod_close_net, 2),
        "eod_close_return_pct": round(eod_close_return_pct, 4),
        "underwater_close": eod_close_net < 0,
        "bar_count": len(usable_bars),
        "n_bad_bars": n_bad_bars,
        "bar_last_time": usable_bars[-1][0].strftime("%Y-%m-%d %H:%M:%S"),
        "full_day_bar": usable_bars[-1][0].strftime("%H:%M") >= "15:55",
    }


def _day_cluster_delta(rows: list[dict]) -> dict:
    by_day = {}
    for row in rows:
        day = row["trade_date"]
        by_day.setdefault(day, []).append(row["delta"])
    day_means = [statistics.mean(values) for values in by_day.values()]
    day_count = len(day_means)
    if day_count < 2:
        return {"n_days": day_count, "mean": None, "t": None, "distinguishable": False}
    mean = statistics.mean(day_means)
    stdev = statistics.stdev(day_means)
    t_stat = mean / (stdev / (day_count ** 0.5)) if stdev else None
    return {
        "n_days": day_count,
        "mean": round(mean, 2),
        "t": round(t_stat, 3) if t_stat is not None else None,
        "distinguishable": t_stat is not None and abs(t_stat) >= 2.0,
    }


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


# ── I. candidate C: 入场锚漂移与弱人群 (P1 只读) ──
def seg_i_candidate_c_entry_anchor(db) -> dict:
    out: dict = {"segment": "I", "title": "candidate C (FADE_SHORT/UE 入场锚) 前提检验"}
    rows = [dict(r) for r in db.execute(f"""
        SELECT t.id, t.trade_date, t.symbol, t.direction, t.entry_price, t.exit_reason,
               t.mfe_pct, ROUND({NET},2) net,
               json_extract(i.raw_instruction_json,'$.planned_entry') planned,
               COALESCE(json_extract(i.raw_instruction_json,'$.source'), 'OTHER') source,
               COALESCE(json_extract(i.raw_instruction_json,'$.entry_archetype'), 'UNKNOWN') archetype
        FROM trade_records t
        JOIN playbook_instructions i
          ON i.id = (SELECT MAX(p.id) FROM playbook_instructions p
                     WHERE p.decision_id = t.decision_id AND t.decision_id <> '')
        WHERE {BASE} AND t.quantity > 0 AND t.entry_price > 0
          AND json_extract(i.raw_instruction_json,'$.planned_entry') IS NOT NULL
    """)]
    for row in rows:
        planned = float(row["planned"])
        if row["direction"] == "SHORT":
            row["adverse_drift_pct"] = round((planned - row["entry_price"]) / planned * 100.0, 3)
        else:
            row["adverse_drift_pct"] = round((row["entry_price"] - planned) / planned * 100.0, 3)

    fan = _one(db, f"""SELECT COUNT(*) rows_join, COUNT(DISTINCT t.id) ids
        FROM trade_records t
        JOIN playbook_instructions i
          ON i.id = (SELECT MAX(p.id) FROM playbook_instructions p
                     WHERE p.decision_id = t.decision_id AND t.decision_id <> '')
        WHERE {BASE} AND t.quantity > 0 AND t.entry_price > 0
          AND json_extract(i.raw_instruction_json,'$.planned_entry') IS NOT NULL""")
    out["join_fanout"] = {"rows_join": fan.get("rows_join"), "distinct_ids": fan.get("ids"),
                          "ok": fan.get("rows_join") == fan.get("ids")}
    out["historical_n"] = len(rows)
    out["historical_net"] = round(sum(r["net"] or 0 for r in rows), 2)

    def summarize(values):
        return {
            "n": len(values),
            "net": round(sum(r["net"] or 0 for r in values), 2),
            "sl_initial_n": sum(r["exit_reason"] == "SL_INITIAL" for r in values),
            "graveyard_n": sum((r["mfe_pct"] or 0) < 0.3 for r in values),
        }

    out["by_source"] = {
        source: summarize([r for r in rows if r["source"] == source])
        for source in ("deep_analysis", "universe_expansion")
    }
    out["by_source"]["other"] = summarize(
        [r for r in rows if r["source"] not in ("deep_analysis", "universe_expansion")])
    out["by_archetype"] = {
        archetype: summarize([r for r in rows if r["archetype"] == archetype])
        for archetype in ("FADE_SHORT", "PULLBACK_LIMIT", "UNKNOWN")
    }
    out["by_adverse_drift"] = {
        label: summarize([r for r in rows if lo <= r["adverse_drift_pct"] < hi])
        for label, lo, hi in (
            ("<=0", -1e9, 0.0), ("0_1", 0.0, 1.0), ("1_2", 1.0, 2.0),
            ("2_3", 2.0, 3.0), (">3", 3.0, 1e9),
        )
    }

    anchor_rows = []
    n_bad_json = 0
    ets = REPO / "data" / "entry_timing_shadow.jsonl"
    if ets.exists():
        for line in ets.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                n_bad_json += 1  # 坏行计数留痕 (except 棘轮纪律), 不静默
                continue
            if row.get("anchor_recorded") and (row.get("dual_anchor") or {}).get("schema") == "rfc123-s0-v1":
                anchor_rows.append(row)
    if n_bad_json:
        print(f"  [warn] entry_timing_shadow.jsonl 坏行 {n_bad_json} 条已跳过", file=sys.stderr)
    grouped = {}
    for row in anchor_rows:
        key = row.get("decision_id")
        if not key:
            continue
        dual = row["dual_anchor"]
        state = grouped.setdefault(key, {"divergences": [], "trigger_divergent": False,
                                         "source": row.get("source")})
        if isinstance(dual.get("divergence_pct"), (int, float)):
            state["divergences"].append(abs(float(dual["divergence_pct"])))
        state["trigger_divergent"] = state["trigger_divergent"] or dual.get("trigger_divergent") is True
    matched = {r["decision_id"]: dict(r) for r in db.execute(
        """SELECT decision_id, COALESCE(pnl_net, realized_pnl) net, exit_reason, mfe_pct
            FROM trade_records WHERE source_type='PLAYBOOK_SIM' AND decision_id <> ''
              AND exit_time IS NOT NULL AND exit_reason <> ''""")}
    matched_states = [state for decision_id, state in grouped.items() if decision_id in matched]
    all_divergences = [v for state in grouped.values() for v in state["divergences"]]
    out["rfc123"] = {
        "anchor_rows": len(anchor_rows),
        "unique_decisions": len(grouped),
        "missing_rate": round(
            sum((row.get("dual_anchor") or {}).get("gen_source") == "missing"
                for row in anchor_rows) / len(anchor_rows) * 100.0, 4
        ) if anchor_rows else None,
        "abs_divergence_median_pct": round(statistics.median(all_divergences), 4) if all_divergences else None,
        "abs_divergence_p90_pct": round(sorted(all_divergences)[int(0.9 * len(all_divergences))], 4)
                                  if all_divergences else None,
        "trigger_divergent_decisions": sum(state["trigger_divergent"] for state in grouped.values()),
        "matched_trade_decisions": len(matched_states),
        "matched_net": round(sum(matched[decision_id]["net"] or 0
                                 for decision_id, state in grouped.items() if decision_id in matched), 2),
        "insufficient_for_gate": len(grouped) < 30 or len(matched_states) < 30,
    }
    out["verdict_note"] = (
        "FADE_SHORT/UE弱人群成立, 但adverse drift分层非单调(2-3%为负、>3%转正), "
        "且RFC-123 matched trade<30; 只能继续攒样, 不得直接立锚漂移门。"
    )
    return out


# ── J. candidate B: D2' 2min 延迟确认的功效与载体可行性 ──
def seg_j_candidate_b_d2_delay() -> dict:
    out: dict = {"segment": "J", "title": "candidate B (D2' 2min 延迟确认) 功效与载体检验"}
    path = REPO / "data" / "shadow" / "entry_delay_confirm_latest.json"
    if not path.exists():
        out["error"] = f"{path.name} 不存在"
        return out
    latest = json.loads(path.read_text(encoding="utf-8"))
    out["generated_at"] = latest.get("generated_at")
    out["original_preregistered_criteria"] = latest.get("criteria")
    out["graduation"] = latest.get("graduation")
    out["delays"] = {}
    for delay in ("2", "5"):
        delay_block = latest.get("delays", {}).get(delay, {})
        out["delays"][delay] = {}
        for population in ("ALL", "SL_INITIAL"):
            block = delay_block.get(population, {})
            day = block.get("day_clustered") or {}
            out["delays"][delay][population] = {
                "n": block.get("n"),
                "avoidance_rate": block.get("avoidance_rate"),
                "avoided_value_usd": block.get("avoided_value_usd"),
                "opp_cost_usd": block.get("opp_cost_usd"),
                "net_value_usd": block.get("net_value_usd"),
                "n_miskilled": block.get("n_miskilled"),
                "day_clustered": {
                    "t": day.get("t"), "n_days": day.get("n_days"),
                    "mde_80pct": day.get("mde_80pct"),
                    "distinguishable": day.get("distinguishable"),
                },
            }

    cadence = {}
    for log_name in (
        "ftseek_us.log-20261009.gz",
        "ftseek_us.log-20261008.gz",
        "ftseek_us.log-20261007.gz",
    ):
        log_path = REPO / "logs" / log_name
        starts = []
        if log_path.exists():
            with gzip.open(log_path, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "auto_execute 开始" not in line:
                        continue
                    match = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
                    if match:
                        starts.append(datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
        intervals = [
            (right - left).total_seconds() / 60.0
            for left, right in zip(starts, starts[1:])
            if 0 < (right - left).total_seconds() / 60 < 30
        ]
        cadence[log_name] = {
            "starts": len(starts),
            "intervals": len(intervals),
            "median_min": round(statistics.median(intervals), 2) if intervals else None,
            "p90_min": round(sorted(intervals)[int(0.9 * len(intervals))], 2) if intervals else None,
            "max_min": round(max(intervals), 2) if intervals else None,
        }
    out["host_auto_execute_cadence"] = cadence
    out["ttl_conflict"] = {
        "entry_archetype_ttl_seconds": 300,
        "p95_cadence_max_min": 4.0,
        "two_min_confirm_remaining_ttl_sec": 180,
        "five_min_confirm_exceeds_ttl": True,
    }
    out["carrier_verdict"] = (
        "现有auto_execute宿主中位3.05-3.18min、p90≤3.42min、max≤4.0min；"
        "挂到该循环无法保证2min确认，会变成3-4min未预注册变体。"
        "若要实装2min，须另建专用计时载体并另行裁决；5min确认与300s TTL直接互斥。"
    )
    out["sample_threshold_note"] = (
        "D2'原文预注册门槛为n≥20；当前SL_INITIAL n=29已满足。"
        "§27.5的n≥30是后验治理加严，不得替代原预注册判据；当前阻塞是载体节奏/TTL/适用面，不是样本n。"
    )
    return out


def seg_k_candidate_a_eod_counterfactual(db) -> dict:
    out: dict = {
        "segment": "K",
        "title": "candidate A EOD时点MFE反事实 (short + w_week)",
        "method": (
            "EOD前bar重建时点MFE；blocked=underwater_close且EOD_MFE<0.5%；"
            "blocked笔以EOD close净额替换actual净额，未blocked笔保留actual；"
            "费用使用trade_records.fees_est同口径扣减，不新增费用假设。"
        ),
    }
    closed_filter = (
        "t.source_type='PLAYBOOK_SIM' AND t.exit_time IS NOT NULL "
        "AND t.exit_reason IS NOT NULL AND t.exit_reason<>'' "
        "AND t.decision_id<>''"
    )
    latest_instruction = (
        "JOIN playbook_instructions p ON p.id=(SELECT MAX(p2.id) FROM playbook_instructions p2 "
        "WHERE p2.decision_id=t.decision_id)"
    )
    sql = f"""
        SELECT t.id, t.symbol, t.direction, t.trade_date, t.entry_time, t.entry_price,
               t.quantity, t.exit_reason, t.decision_id,
               ROUND(COALESCE(t.pnl_net, t.realized_pnl), 2) actual_net,
               CASE WHEN t.pnl_net IS NULL AND t.realized_pnl IS NULL THEN 1 ELSE 0 END null_actual_net,
               ROUND(COALESCE(t.fees_est, 0), 2) fees,
               json_extract(t.metadata_json, '$.overnight_arm.source') arm_source,
               json_extract(t.metadata_json, '$.overnight_arm.marked_at_et') marked_at_et,
               json_extract(t.metadata_json, '$.overnight_arm.underwater_pct_net') metadata_underwater_pct_net,
               t.metadata_json
        FROM trade_records t {latest_instruction}
        WHERE {closed_filter} AND p.horizon='short'
          AND json_extract(t.metadata_json, '$.overnight_arm.source')='w_week'
        ORDER BY t.trade_date, t.id
    """
    rows = [dict(row) for row in db.execute(sql)]
    fanout = _one(
        db,
        f"""SELECT COUNT(*) rows_join, COUNT(DISTINCT t.id) distinct_ids
            FROM trade_records t {latest_instruction}
            WHERE {closed_filter} AND p.horizon='short'
              AND json_extract(t.metadata_json, '$.overnight_arm.source')='w_week'""",
    )
    out["join_fanout"] = {
        "rows_join": fanout.get("rows_join"),
        "distinct_ids": fanout.get("distinct_ids"),
        "ok": fanout.get("rows_join") == fanout.get("distinct_ids"),
    }
    out["sample_n"] = len(rows)
    out["local_bar_missing_n"] = 0
    out["unusable_rows"] = []
    evaluated_rows = []
    for row in rows:
        if any(
            row.get(field) is None
            for field in ("direction", "entry_time", "entry_price", "quantity", "marked_at_et")
        ):
            out["unusable_rows"].append({
                "trade_id": row["id"],
                "symbol": row["symbol"],
                "trade_date": row["trade_date"],
                "mark_date": (row.get("marked_at_et") or "")[:10],
                "reason": "missing_required_fields",
            })
            continue
        mark_date = (row.get("marked_at_et") or "")[:10]
        bars = _local_replay_bars(row["symbol"], mark_date)
        if bars is None:
            out["local_bar_missing_n"] += 1
            out["unusable_rows"].append({
                "trade_id": row["id"],
                "symbol": row["symbol"],
                "trade_date": row["trade_date"],
                "mark_date": mark_date,
                "reason": "local_bar_missing_or_invalid",
            })
            continue
        metrics = _candidate_a_eod_metrics(row, bars)
        if metrics is None:
            out["unusable_rows"].append({
                "trade_id": row["id"],
                "symbol": row["symbol"],
                "trade_date": row["trade_date"],
                "mark_date": mark_date,
                "reason": "missing_mark_time_or_no_usable_bar",
            })
            continue
        evaluated = dict(row)
        evaluated.update(metrics)
        evaluated["blocked"] = metrics["underwater_close"] and metrics["eod_mfe_pct"] < 0.5
        if evaluated["blocked"] and row["actual_net"] is not None:
            evaluated["delta"] = round(metrics["eod_close_net"] - row["actual_net"], 2)
        else:
            evaluated["delta"] = None
        evaluated["metadata_underwater"] = row["metadata_underwater_pct_net"] is not None and float(
            row["metadata_underwater_pct_net"]
        ) < 0
        evaluated_rows.append(evaluated)

    out["evaluated_n"] = len(evaluated_rows)
    out["null_actual_net_n"] = sum(row["null_actual_net"] for row in evaluated_rows)
    out["full_day_bar_n"] = sum(row["full_day_bar"] for row in evaluated_rows)
    blocked_rows = [row for row in evaluated_rows if row["blocked"]]
    passed_rows = [row for row in evaluated_rows if not row["blocked"]]

    def summarize(values: list[dict]) -> dict:
        actual = [float(row["actual_net"]) for row in values if row["actual_net"] is not None]
        return {
            "n": len(values),
            "actual_net": round(sum(actual), 2),
            "eod_close_net": round(sum(row["eod_close_net"] for row in values), 2),
            "delta": round(sum(row["delta"] or 0.0 for row in values), 2),
            "false_kill_n": sum(row["actual_net"] is not None and row["actual_net"] > 0 for row in values),
            "gap_n": sum(row["exit_reason"] == "OVERNIGHT_GAP_STOP" for row in values),
        }

    out["blocked"] = summarize(blocked_rows)
    out["passed"] = summarize(passed_rows)
    out["blocked_by_gap"] = {
        "gap": summarize([row for row in blocked_rows if row["exit_reason"] == "OVERNIGHT_GAP_STOP"]),
        "non_gap": summarize([row for row in blocked_rows if row["exit_reason"] != "OVERNIGHT_GAP_STOP"]),
    }
    out["eod_mfe_lt_05_n"] = sum(row["eod_mfe_pct"] < 0.5 for row in evaluated_rows)
    out["underwater_close_n"] = sum(row["underwater_close"] for row in evaluated_rows)
    out["underwater_metadata_n"] = sum(row["metadata_underwater"] for row in evaluated_rows)
    out["underwater_agreement_n"] = sum(
        row["metadata_underwater"] == row["underwater_close"] for row in evaluated_rows
    )
    changed_rows = [row for row in blocked_rows if row["delta"] is not None]
    out["day_cluster_delta"] = _day_cluster_delta(changed_rows)
    out["counterfactual_scope"] = "blocked-only EOD close replacement; non-blocked rows unchanged"
    out["blocked_rows"] = [
        {
            "trade_id": row["id"],
            "symbol": row["symbol"],
            "trade_date": row["trade_date"],
            "mark_date": (row["marked_at_et"] or "")[:10],
            "direction": row["direction"],
            "exit_reason": row["exit_reason"],
            "eod_mfe_pct": row["eod_mfe_pct"],
            "eod_close_return_pct": row["eod_close_return_pct"],
            "metadata_underwater_pct_net": row["metadata_underwater_pct_net"],
            "actual_net": row["actual_net"],
            "eod_close_net": row["eod_close_net"],
            "delta": row["delta"],
        }
        for row in blocked_rows
    ]
    out["verdict"] = "record-only; no gate or exit behavior changed"
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
        seg_i_candidate_c_entry_anchor(db),
        seg_j_candidate_b_d2_delay(),
        seg_k_candidate_a_eod_counterfactual(db),
    ]
    print(json.dumps({"probe": SELF.name, "db": str(DB.relative_to(REPO)),
                      "window_start": WINDOW_START, "net_caliber": "COALESCE(pnl_net, realized_pnl)",
                      "segments": segs}, ensure_ascii=False, indent=1, default=str))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
