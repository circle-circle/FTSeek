#!/usr/bin/env python3
"""交易日归因尺子 (只读, 零副作用) — 盘后复查用。

为什么有这个脚本
----------------
盘后复查最容易出的两类错:
  ① **口径混用** —— 「当日收益」可以按开仓日/出场日/生成时点快照算, 三个数不同且都不是错;
     复盘 md 是**生成时点快照**(之后出场的笔不在内), 日报 md 是较晚生成的出场日口径 ⇒ 同一天两个数。
  ② **只看标签不看分层** —— exit_reason='SL_BREAKEVEN' 里混着"真保本/被打穿/滑点"三种;
     仓内已有 `metadata.be_exit_class = OK/GAP/SLIP` 做分层, 聚合层不用它就会得出假结论。

本脚本把一个交易日的账一次拆干净, 每个口径都显式命名, 避免下次再混。

口径声明
--------
· trade_records.pnl_net = realized_pnl − fee (仓内 SSOT, scripts/ticket_size_breakeven.py 头部)
· 未平仓的浮盈在 `playbook_instructions.pnl` (DA 指令行) / `open_positions` (全策略)
· 盘前跳空 = data/da_checkpoint/<date>/<SYM>.raw.json → collected.snapshot.pre_change_rate
· 开盘跳空 = trade_records.metadata_json → entry_gap.aligned_gap_pct (open vs prev_close)
· be_exit_class = trade_records.metadata_json → be_exit_class (OK / GAP / SLIP)
· 闸门拦截审计 = data/shadow/exec_gate_audit.jsonl (只读统计, 不判代价)

只读保证
--------
DB 一律 `file:...?mode=ro`; 不写任何文件 (含 data/shadow); 零网络; 零 cron; 确定性输出。

用法:
  venv/bin/python scripts/session_attribution.py --date 2026-11-09
  venv/bin/python scripts/session_attribution.py --date 2026-11-09 --json
  venv/bin/python scripts/session_attribution.py --date 2026-11-09 --gap-threshold 1.0
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "ftseek_us.db"
CKPT_DIR = ROOT / "data" / "da_checkpoint"
GATE_AUDIT = ROOT / "data" / "shadow" / "exec_gate_audit.jsonl"


def _rows(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return out


def _json_field(raw: Optional[str], *path: str) -> Any:
    try:
        cur: Any = json.loads(raw or "{}")
        for p in path:
            cur = (cur or {}).get(p)
        return cur
    except (TypeError, ValueError, AttributeError):
        return None


def premarket_gap(day: str, symbol: str) -> Optional[float]:
    p = CKPT_DIR / day / (symbol + ".raw.json")
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text())
        return float(((j.get("collected") or {}).get("snapshot") or {}).get("pre_change_rate"))
    except (TypeError, ValueError, AttributeError, OSError):
        return None


def segment_a(day: str) -> Dict[str, Any]:
    by_open = _rows("""SELECT * FROM trade_records WHERE trade_date=? ORDER BY entry_time""", (day,))
    by_close = _rows("""SELECT * FROM trade_records WHERE exit_time LIKE ?||'%' ORDER BY exit_time""", (day,))
    closed_open = [r for r in by_open if r["pnl_net"] is not None]
    open_pos = _rows("""SELECT * FROM open_positions ORDER BY entry_time""")
    instr_pnl = {(r["symbol"]): r["pnl"] for r in _rows(
        """SELECT symbol, pnl FROM playbook_instructions WHERE date=? AND executed=1""", (day,))}
    return {
        "n_by_open": len(by_open),
        "n_closed_by_open": len(closed_open),
        "net_by_open": sum(r["pnl_net"] for r in closed_open),
        "n_by_close": len([r for r in by_close if r["pnl_net"] is not None]),
        "net_by_close": sum(r["pnl_net"] for r in by_close if r["pnl_net"] is not None),
        "open_rows": [
            {"symbol": r["symbol"], "direction": r["direction"], "strategy": r["strategy_id"],
             "entry": r["entry_price"], "entry_time": (r["entry_time"] or "")[:19],
             "instr_pnl": instr_pnl.get(r["symbol"])}
            for r in by_open if r["pnl_net"] is None],
        "open_positions_table": [
            {"symbol": r["symbol"], "direction": r["direction"], "size": r["size"],
             "entry": r["avg_entry_price"], "sl": r["current_sl"], "tp": r["initial_tp"],
             "strategy": r["strategy_id"], "sync": r["sync_status"],
             "entry_time": (r["entry_time"] or "")[:19]}
            for r in open_pos],
    }


def segment_b(day: str) -> Dict[str, Any]:
    rows = _rows("""SELECT strategy_id, COUNT(*) n,
        SUM(CASE WHEN pnl_net IS NOT NULL THEN 1 ELSE 0 END) closed,
        COALESCE(SUM(pnl_net),0) net FROM trade_records WHERE trade_date=? GROUP BY strategy_id""", (day,))
    carry = _rows("""SELECT symbol, trade_date, entry_time, exit_time, pnl_net, strategy_id, exit_reason
        FROM trade_records WHERE exit_time LIKE ?||'%' AND trade_date < ? ORDER BY entry_time""", (day, day))
    return {"by_strategy": rows, "carry_over": carry}


def segment_c() -> Dict[str, Any]:
    rows = _rows("""SELECT exit_reason, pnl_net FROM trade_records
        WHERE realized_pnl IS NOT NULL AND pnl_net IS NOT NULL AND exit_reason IS NOT NULL""")
    g = defaultdict(list)
    for r in rows:
        g[r["exit_reason"]].append(r["pnl_net"])
    out = []
    for reason, v in g.items():
        out.append({"reason": reason, "n": len(v), "losers": sum(1 for x in v if x < 0),
                    "net": round(sum(v), 2), "per": round(statistics.mean(v), 2),
                    "worst": round(min(v), 2)})
    out.sort(key=lambda x: -x["n"])
    be = _rows("""SELECT json_extract(metadata_json,'$.be_exit_class') cls, pnl_net
        FROM trade_records WHERE pnl_net IS NOT NULL AND json_extract(metadata_json,'$.be_exit_class') IS NOT NULL""")
    bg = defaultdict(list)
    for r in be:
        bg[r["cls"]].append(r["pnl_net"])
    return {"by_reason": out, "be_exit_class": [
        {"cls": k, "n": len(v), "net": round(sum(v), 2), "per": round(statistics.mean(v), 2),
         "losers": sum(1 for x in v if x < 0)} for k, v in sorted(bg.items())]}


def segment_d(day: str) -> Dict[str, Any]:
    instr = _rows("""SELECT symbol, direction, skip_reason, executed, triggered
        FROM playbook_instructions WHERE date=? AND row_kind='instruction'""", (day,))
    skipped = [r for r in instr if not r["executed"]]
    buckets: Dict[str, List[str]] = defaultdict(list)
    for r in skipped:
        reason = (r["skip_reason"] or "").split("(")[0].strip() or "未分类"
        buckets[reason].append(r["symbol"])
    audit = []
    bad_audit_rows = 0
    if GATE_AUDIT.exists():
        for line in GATE_AUDIT.read_text().splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except ValueError:
                bad_audit_rows += 1
                continue
            if j.get("date") == day:
                audit.append(j)
    return {"n_instructions": len(instr), "n_executed": len(instr) - len(skipped),
            "skip_buckets": {k: v for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))},
            "gate_audit_rows": len(audit), "gate_audit_bad_rows": bad_audit_rows,
            "gate_audit_by_action": dict(Counter(str(j.get("action") or "?") for j in audit))}


def segment_e(day: str) -> Dict[str, Any]:
    """跨臂方向冲突: 同标的同时被 DA 与 EXT 相反方向持有/开过。"""
    da = _rows("""SELECT symbol, direction, executed, execution_time, skip_reason FROM playbook_instructions
        WHERE date=? AND row_kind='instruction' AND decision_id LIKE 'DA-%'""", (day,))
    ext = _rows("""SELECT symbol, direction, entry_time FROM trade_records
        WHERE trade_date=? AND strategy_id='extended_hours_entry'""", (day,))
    def _norm(direction: Optional[str]) -> str:
        d = str(direction or "").upper()
        if "SHORT" in d or d == "SELL":
            return "SHORT"
        if "BUY" in d or d == "LONG":
            return "LONG"
        return d

    dmap = {r["symbol"]: r for r in da}
    conflicts = []
    for e in ext:
        d = dmap.get(e["symbol"])
        if d and _norm(d["direction"]) != _norm(e["direction"]):
            conflicts.append({"symbol": e["symbol"], "da_direction": d["direction"],
                               "da_executed": bool(d["executed"]),
                               "da_skip": (d.get("skip_reason") or "")[:60],
                               "ext_direction": e["direction"], "ext_entry": (e["entry_time"] or "")[:19]})
    return {"da_executed": {r["symbol"]: r["direction"] for r in da if r["executed"]},
            "da_all": {r["symbol"]: r["direction"] for r in da},
            "ext_entries": [{"symbol": r["symbol"], "direction": r["direction"]} for r in ext],
            "conflicts": conflicts}


def segment_f(day: str, thr: float) -> Dict[str, Any]:
    """盘前跳空阈值分组 (RFC-192 C2 口径候选 a) —— 只对 playbook_sim 的 DA 单。"""
    rows = _rows("""SELECT symbol, direction, pnl_net, exit_price FROM trade_records
        WHERE trade_date=? AND strategy_id='playbook_sim'""", (day,))
    keep, drop = [], []
    for r in rows:
        g = premarket_gap(day, r["symbol"])
        r["pm_gap"] = g
        (keep if (g is not None and abs(g) < thr) else drop).append(r)
    def _s(sub):
        return {"n": len(sub), "net": round(sum(x["pnl_net"] or 0 for x in sub), 2),
                "symbols": [(x["symbol"], x["pm_gap"], x["pnl_net"]) for x in sub]}
    return {"threshold_pct": thr, "keep": _s(keep), "drop": _s(drop),
            "gap_coverage": sum(1 for r in rows if r["pm_gap"] is not None),
            "n_da_trades": len(rows)}


def segment_g(day: str) -> Dict[str, Any]:
    """只读体检三件套 (DEB-20261109 Batch 0.6):
       ① 无 TP 跨夜仓   ② 同标的平后立即重入   ③ 跨臂相反暴露(= 段 E)
    """
    # ① 无 TP 的现存仓 (EXT 只写 SL 不写 TP ⇒ 止盈完全依赖出场引擎)
    no_tp = _rows("""SELECT symbol, direction, size, avg_entry_price, initial_sl, current_sl, initial_tp,
        strategy_id, sync_status, entry_time, metadata_json FROM open_positions
        WHERE size > 0 AND (initial_tp IS NULL OR initial_tp = 0)""")
    # ② 同标的平后立即重入: 当日平掉 + 当日更晚再开
    closed_today = _rows("""SELECT symbol, exit_time, pnl_net, exit_reason, strategy_id
        FROM trade_records WHERE exit_time LIKE ?||'%' AND pnl_net IS NOT NULL""", (day,))
    opened_today = _rows("""SELECT symbol, entry_time, pnl_net, strategy_id, decision_id
        FROM trade_records WHERE trade_date=?""", (day,))
    reentry = []
    bad_reentry_time_rows = 0
    for c in closed_today:
        for o in opened_today:
            if o["symbol"] != c["symbol"]:
                continue
            if not o["entry_time"] or not c["exit_time"]:
                continue
            try:
                gap_min = (int(o["entry_time"][11:13]) * 60 + int(o["entry_time"][14:16])
                           - (int(c["exit_time"][11:13]) * 60 + int(c["exit_time"][14:16])))
            except ValueError:
                bad_reentry_time_rows += 1
                continue
            if gap_min <= 0:
                continue
            reentry.append({"symbol": c["symbol"], "gap_min": gap_min,
                            "closed_pnl": c["pnl_net"], "closed_reason": c["exit_reason"],
                            "closed_strategy": c["strategy_id"],
                            "reopened_strategy": o["strategy_id"],
                            "reopened_pnl": o["pnl_net"],
                            "reopened_state": "未平" if o["pnl_net"] is None else "已平"})
    reentry.sort(key=lambda x: x["gap_min"])
    no_tp_rows = []
    for position in no_tp:
        entry = float(position["avg_entry_price"] or 0)
        initial_stop = float(position["initial_sl"] or 0)
        size = float(position["size"] or 0)
        cert_lock = _json_field(position.get("metadata_json"), "cert_lock_pct")
        try:
            cert_lock_value = float(cert_lock) if cert_lock is not None else None
        except (TypeError, ValueError):
            cert_lock_value = None
        no_tp_rows.append({
            "symbol": position["symbol"], "direction": position["direction"], "size": position["size"],
            "entry": position["avg_entry_price"], "initial_sl": position["initial_sl"],
            "sl": position["current_sl"], "tp": position["initial_tp"],
            "strategy": position["strategy_id"], "sync": position["sync_status"],
            "entry_time": (position["entry_time"] or "")[:19],
            "entry_grade": _json_field(position.get("metadata_json"), "entry_grade"),
            "gate_mode": _json_field(position.get("metadata_json"), "gate_mode"),
            "cert_lock_pct": cert_lock_value,
            "initial_risk_usd": round(abs(entry - initial_stop) * size, 2) if entry and initial_stop else None,
            "profit_protection": (f"cert_lock {cert_lock_value}% (触发阈值)"
                                  if cert_lock_value and cert_lock_value > 0 else "BE/trail/RTH (条件触发)"),
        })
    return {"no_tp_positions": no_tp_rows, "same_day_reentry": reentry,
            "bad_reentry_time_rows": bad_reentry_time_rows}


def segment_h(day: str) -> Dict[str, Any]:
    """EXT 入场风险影子 (DEB-20261109 Batch 1.2/1.3) 的读数: 覆盖率 + 冲突组 vs 非冲突组结果。"""
    path = ROOT / "data" / "shadow" / "ext_entry_risk_shadow.jsonl"
    rows: List[Dict[str, Any]] = []
    bad_shadow_rows = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
            except ValueError:
                bad_shadow_rows += 1
                continue
            if str(j.get("date") or "")[:10] == day:
                rows.append(j)
    trades = _rows("""SELECT symbol, direction, pnl_net, exit_price, exit_reason, exit_time
        FROM trade_records WHERE trade_date=? AND strategy_id='extended_hours_entry'""", (day,))
    tmap = {t["symbol"]: t for t in trades}
    for r in rows:
        t = tmap.get(r.get("symbol"))
        r["outcome_pnl"] = t["pnl_net"] if t else None
        r["outcome_reason"] = t["exit_reason"] if t else None
        r["still_open"] = bool(t) and t["pnl_net"] is None
    conflict = [r for r in rows if r.get("direction_conflict")]
    clean = [r for r in rows if not r.get("direction_conflict")]

    def _agg(sub):
        vals = [r["outcome_pnl"] for r in sub if r.get("outcome_pnl") is not None]
        return {"n": len(sub), "closed": len(vals),
                "net": round(sum(vals), 2) if vals else None,
                "per": round(sum(vals) / len(vals), 2) if vals else None}
    return {"n_shadow_rows": len(rows), "bad_shadow_rows": bad_shadow_rows,
            "da_available_rate": (sum(1 for r in rows if r.get("da_available")) / len(rows)) if rows else None,
            "conflict_n": len(conflict), "conflict": _agg(conflict),
            "clean": _agg(clean),
            "rows": [{"symbol": r.get("symbol"), "session": r.get("session"),
                      "da_available": r.get("da_available"), "da_direction": r.get("da_direction"),
                      "conflict": r.get("direction_conflict"),
                      "max_loss_usd": r.get("max_loss_usd"), "outcome_pnl": r.get("outcome_pnl")} for r in rows]}


def entry_session_bucket(entry_time: Optional[str]) -> str:
    try:
        dt = datetime.fromisoformat(str(entry_time or ""))
    except ValueError:
        return "unknown"
    minute = dt.hour * 60 + dt.minute + dt.second / 60.0 - 570
    if minute < 0:
        return "pre_open"
    if minute < 15:
        return "open_0_15"
    if minute < 60:
        return "open_15_60"
    return "after_60"


def pnl_structure(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    nets = [float(r["pnl_net"] if r["pnl_net"] is not None
                  else r["realized_pnl"] - (r["fees_est"] or 0)) for r in rows]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    gross = sum(float(r["realized_pnl"] or 0) for r in rows)
    fees = sum(float(r["fees_est"] or 0) for r in rows)
    notional = sum(float(r["entry_price"] or 0) * float(r["quantity"] or 0) for r in rows)
    avg_win = statistics.mean(wins) if wins else None
    avg_loss = statistics.mean(losses) if losses else None
    breakeven = None
    if avg_win is not None and avg_loss not in (None, 0):
        breakeven = -avg_loss / (avg_win - avg_loss) * 100.0
    return {
        "n": len(rows),
        "win_n": len(wins),
        "loss_n": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(rows), 2) if rows else None,
        "gross": round(gross, 2),
        "fees": round(fees, 2),
        "net": round(sum(nets), 2),
        "net_bps": round(sum(nets) / notional * 10000.0, 2) if notional else None,
        "expectancy": round(statistics.mean(nets), 2) if nets else None,
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
        "payoff": round(avg_win / -avg_loss, 3) if avg_win is not None and avg_loss not in (None, 0) else None,
        "breakeven_win_rate_pct": round(breakeven, 2) if breakeven is not None else None,
        "fee_to_gross_pct": round(fees / gross * 100.0, 2) if gross > 0 else None,
        "sl_initial_n": sum(1 for r in rows if r.get("exit_reason") == "SL_INITIAL"),
    }


def segment_i(day: str) -> Dict[str, Any]:
    """月初至 `--date` 的入场 cohort；只读展示，不进入任何交易判定。"""
    month_start = day[:8] + "01"
    rows = _rows("""SELECT id,symbol,trade_date,strategy_id,entry_time,entry_price,quantity,
        realized_pnl,pnl_net,fees_est,fees_source,exit_reason,mfe_pct,metadata_json
        FROM trade_records
        WHERE source_type='PLAYBOOK_SIM' AND trade_date>=? AND trade_date<=?
          AND exit_price>0 AND COALESCE(pnl_net,realized_pnl) IS NOT NULL
          AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''
          AND data_quality IN ('clean','real')
        ORDER BY trade_date,id""", (month_start, day))
    da = [r for r in rows if r.get("strategy_id") == "playbook_sim"]
    ext = [r for r in rows if r.get("strategy_id") == "extended_hours_entry"]
    bucket_order = ("pre_open", "open_0_15", "open_15_60", "after_60", "unknown")
    buckets = []
    for bucket in bucket_order:
        subset = [r for r in da if entry_session_bucket(r.get("entry_time")) == bucket]
        if subset:
            buckets.append({"bucket": bucket, **pnl_structure(subset)})
    sl_initial = [r for r in da if r.get("exit_reason") == "SL_INITIAL"]
    stop_families = {}
    for family, subset in (
        ("raw_initial", [r for r in sl_initial if not _json_field(r.get("metadata_json"), "_tight_sl_killed")]),
        ("early_tight", [r for r in sl_initial if _json_field(r.get("metadata_json"), "_tight_sl_killed")]),
    ):
        stop_families[family] = {
            **pnl_structure(subset),
            "open_0_15_n": sum(1 for r in subset if entry_session_bucket(r.get("entry_time")) == "open_0_15"),
            "symbols": [r.get("symbol") for r in subset],
        }
    return {
        "window": {"start": month_start, "end": day, "basis": "entry_cohort_closed_A9_A10"},
        "overall": {"playbook_sim": pnl_structure(da), "extended_hours_entry": pnl_structure(ext)},
        "da_entry_buckets": buckets,
        "da_sl_initial": stop_families,
        "fees_source": sorted({str(r.get("fees_source") or "unknown") for r in rows}),
        "decision_use": "display_only_not_a_trading_gate",
    }


def print_segment_i(i: Dict[str, Any]) -> None:
    def fmt(value: Any, suffix: str = "") -> str:
        return "—" if value is None else f"{value}{suffix}"

    print("\n── I. 月初至今“赢小输大”结构 (%s→%s, 入场cohort/A9/A10) ──" % (
        i["window"]["start"], i["window"]["end"]))
    for strategy, label in (("playbook_sim", "DA"), ("extended_hours_entry", "EXT")):
        r = i["overall"][strategy]
        print("  %-3s n=%d WR=%s avgW=%s avgL=%s 打平WR=%s 净=%s bps=%s 费/毛=%s" % (
            label, r["n"], fmt(r["win_rate_pct"], "%"), fmt(r["avg_win"]),
            fmt(r["avg_loss"]), fmt(r["breakeven_win_rate_pct"], "%"),
            fmt(r["net"]), fmt(r["net_bps"]), fmt(r["fee_to_gross_pct"], "%")))
    print("  DA 入场时段:")
    for r in i["da_entry_buckets"]:
        print("    %-12s n=%3d WR=%6s SL_INIT=%d 净=%9s avgW=%8s avgL=%9s" % (
            r["bucket"], r["n"], fmt(r["win_rate_pct"], "%"), r["sl_initial_n"],
            fmt(r["net"]), fmt(r["avg_win"]), fmt(r["avg_loss"])))
    print("  DA SL_INITIAL 分型 (标签相同, 机制不同):")
    for family, r in i["da_sl_initial"].items():
        print("    %-12s n=%d open0-15=%d 净=%s 标的=%s" % (
            family, r["n"], r["open_0_15_n"], fmt(r["net"]), ",".join(r["symbols"]) or "—"))
    print("  口径: fees_source=%s; 未平仓不进本段; 仅展示, 不触发交易门。" % i["fees_source"])


def segment_j(day: str) -> Dict[str, Any]:
    """DA planned/effective/fill 三层价格账；成交数只认严格 decision_id。"""
    from app.playbook.price_ledger import build_price_ledger

    instructions = _rows("""SELECT * FROM playbook_instructions
        WHERE date=? AND row_kind='instruction' AND decision_id LIKE 'DA-%' ORDER BY id""", (day,))
    trades = _rows("""SELECT decision_id, entry_price, quantity, metadata_json FROM trade_records
        WHERE trade_date=? AND decision_id LIKE 'DA-%'""", (day,))
    trade_map = {trade["decision_id"]: trade for trade in trades}
    rows = []
    for inst in instructions:
        trade = trade_map.get(inst.get("decision_id"))
        enriched = dict(inst)
        if trade:
            enriched["trade_fill_price"] = trade.get("entry_price")
            enriched["trade_fill_quantity"] = trade.get("quantity")
        ledger = build_price_ledger(enriched)
        reprice = _json_field(inst.get("metadata_json"), "reprice_audit") or {}
        execution = _json_field(trade.get("metadata_json") if trade else None, "execution") or {}
        planned, effective, fill = ledger["planned"], ledger["effective"], ledger["fill"]
        rows.append({
            "symbol": inst.get("symbol"),
            "decision_id": inst.get("decision_id"),
            "direction": inst.get("direction"),
            "executed_latch": bool(inst.get("executed")),
            "strict_fill": trade is not None,
            "planned": planned,
            "effective": effective,
            "fill": fill,
            "reprice_rounds": reprice.get("rounds"),
            "fill_slippage_bps": execution.get("slippage_bps"),
        })
    return {
        "n_instructions": len(instructions),
        "n_strict_fills": len(trades),
        "n_executed_latch": sum(1 for inst in instructions if inst.get("executed")),
        "rows": rows,
        "decision_use": "display_only_not_a_trading_gate",
    }


def _is_asof_key(key: str) -> bool:
    normalized = str(key).lower()
    if normalized == "time_range":
        return False
    return normalized in {"time", "date"} or any(
        token in normalized for token in ("as_of", "publish_time", "update_time", "timestamp", "snapshot_time")
    )


def _has_top_level_asof(value: Any) -> bool:
    if isinstance(value, dict):
        if "_error" in value:
            return False
        return any(_is_asof_key(key) for key in value)
    if isinstance(value, list):
        first = value[0] if value and isinstance(value[0], dict) else {}
        return any(_is_asof_key(key) for key in first)
    return False


def segment_k(day: str) -> Dict[str, Any]:
    """DA raw 输入新鲜度审计：覆盖≠实时；只读披露 as-of 缺口，不接交易门。"""
    root = CKPT_DIR / day
    files = sorted(root.glob("*.raw.json")) if root.exists() else []
    quality_counts: Counter = Counter()
    vix_basis_counts: Counter = Counter()
    snapshot_sessions: Counter = Counter()
    source_status_keys: Counter = Counter()
    missing_asof: Counter = Counter()
    dimension_totals: Counter = Counter()
    bad_raw_files = 0
    for path in files:
        try:
            collected = (json.loads(path.read_text(encoding="utf-8", errors="ignore")).get("collected") or {})
        except (OSError, ValueError):
            bad_raw_files += 1
            continue
        quality = collected.get("_quality") or {}
        quality_counts[f"{quality.get('ok')}/{quality.get('total')}"] += 1
        vix = ((collected.get("market_context") or {}).get("vix") or {})
        vix_basis_counts[str(vix.get("as_of_basis") or vix.get("caliber") or "missing")] += 1
        snapshot = collected.get("snapshot") or {}
        snapshot_sessions[str(snapshot.get("session") or "missing")] += 1
        for key in (collected.get("_source_status") or {}):
            source_status_keys[str(key)] += 1
        for dimension, value in collected.items():
            if dimension.startswith("_") or dimension in {"symbol", "sector_context", "ue_candidate_context"}:
                continue
            dimension_totals[dimension] += 1
            if not _has_top_level_asof(value):
                missing_asof[dimension] += 1
    return {
        "raw_count": len(files),
        "bad_raw_files": bad_raw_files,
        "quality_counts": dict(quality_counts),
        "vix_as_of_basis": dict(vix_basis_counts),
        "snapshot_sessions": dict(snapshot_sessions),
        "source_status_keys": dict(source_status_keys),
        "missing_top_level_asof": [
            {"dimension": dimension, "missing_n": count, "total_n": dimension_totals[dimension]}
            for dimension, count in sorted(missing_asof.items(), key=lambda item: (-item[1], item[0]))
        ],
        "decision_use": "display_only_not_a_trading_gate",
    }


def print_segment_j(j: Dict[str, Any]) -> None:
    def fmt(value: Any) -> str:
        return "—" if value is None else str(value)

    print("\n── J. DA planned/effective/fill 三层价格账 (严格 decision_id) ──")
    print("  指令 %d | 严格成交 %d | executed 闩 %d" % (
        j["n_instructions"], j["n_strict_fills"], j["n_executed_latch"]))
    for r in j["rows"]:
        print("    %-10s %-12s planE=%-9s effE=%-9s fillE=%-9s planRR=%-7s effRR=%-7s fillRR=%-7s rounds=%s" % (
            r["symbol"], r["direction"], fmt(r["planned"]["entry_price"]),
            fmt(r["effective"]["entry_price"]), fmt(r["fill"]["entry_price"]),
            fmt(r["planned"]["risk_reward"]), fmt(r["effective"]["risk_reward"]),
            fmt(r["fill"]["risk_reward"]), fmt(r["reprice_rounds"])))
    print("  口径: executed 闩≠成交; SL/TP/quantity/slippage 见 --json J.rows。")


def segment_l(day: str) -> Dict[str, Any]:
    """数据准确度覆盖：TCA 字段 / stop_loss_price 契约 / legacy 卫生。只读。"""
    month_start = day[:8] + "01"
    rows = _rows("""SELECT strategy_id,
        json_extract(metadata_json,'$.execution.slippage_bps') AS exec_slip,
        json_extract(metadata_json,'$.slippage_bps') AS top_slip,
        json_extract(metadata_json,'$.entry_price_source') AS entry_price_source
        FROM trade_records
        WHERE trade_date>=? AND trade_date<=? AND exit_time IS NOT NULL AND exit_price>0
          AND source_type='PLAYBOOK_SIM' AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
          AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''
          AND data_quality IN ('clean','real')""", (month_start, day))
    by_strategy: Dict[str, Dict[str, int]] = {}
    slippage_present = 0
    entry_source_present = 0
    for row in rows:
        strategy = str(row.get("strategy_id") or "unknown")
        bucket = by_strategy.setdefault(strategy, {"n": 0, "slippage_present": 0, "entry_price_source_present": 0})
        has_slippage = row.get("exec_slip") is not None or row.get("top_slip") is not None
        has_source = row.get("entry_price_source") is not None
        bucket["n"] += 1
        bucket["slippage_present"] += int(has_slippage)
        bucket["entry_price_source_present"] += int(has_source)
        slippage_present += int(has_slippage)
        entry_source_present += int(has_source)
    stop_null = _rows("""SELECT symbol, initial_sl, current_sl FROM open_positions
        WHERE size>0 AND (stop_loss_price IS NULL OR stop_loss_price=0) ORDER BY symbol""")
    legacy_missing = _rows("""SELECT COUNT(*) AS n FROM trade_records
        WHERE exit_time IS NOT NULL AND (pnl_net IS NULL OR fees_est IS NULL)""")[0]["n"]
    decision_missing = _rows("""SELECT COUNT(*) AS n FROM trade_records
        WHERE decision_id IS NULL OR decision_id=''""")[0]["n"]
    decision_duplicates = _rows("""SELECT COUNT(*) AS n FROM (
        SELECT decision_id FROM trade_records WHERE decision_id IS NOT NULL AND decision_id<>''
        GROUP BY decision_id HAVING COUNT(*)>1)""")[0]["n"]
    return {
        "window": {"start": month_start, "end": day, "basis": "A9_A10_closed"},
        "tca": {
            "n": len(rows),
            "slippage_present": slippage_present,
            "entry_price_source_present": entry_source_present,
            "by_strategy": by_strategy,
        },
        "open_positions_stop_loss_price_null": stop_null,
        "legacy_hygiene": {
            "closed_missing_pnl_or_fees": legacy_missing,
            "decision_id_missing": decision_missing,
            "duplicate_decision_id_groups": decision_duplicates,
        },
        "decision_use": "display_only_not_a_trading_gate",
    }


def print_segment_l(accuracy: Dict[str, Any]) -> None:
    tca = accuracy["tca"]
    print("\n── L. 数据准确度覆盖 (只读) ──")
    print("  TCA A9/A10 n=%d | slippage=%d/%d | entry_price_source=%d/%d" % (
        tca["n"], tca["slippage_present"], tca["n"],
        tca["entry_price_source_present"], tca["n"]))
    for strategy, bucket in sorted(tca["by_strategy"].items()):
        print("    %-24s n=%3d slip=%3d source=%3d" % (
            strategy, bucket["n"], bucket["slippage_present"], bucket["entry_price_source_present"]))
    print("  open_positions stop_loss_price 缺值: %s" % (
        [row["symbol"] for row in accuracy["open_positions_stop_loss_price_null"]] or "无"))
    legacy = accuracy["legacy_hygiene"]
    print("  legacy: 已平缺pnl/fees=%d | decision_id缺=%d | 重复decision_id组=%d" % (
        legacy["closed_missing_pnl_or_fees"], legacy["decision_id_missing"],
        legacy["duplicate_decision_id_groups"]))
    print("  口径: 只读披露，不接交易门；历史 legacy 不混入当前 DA/EXT 严格 cohort。")


def print_segment_k(k: Dict[str, Any]) -> None:
    print("\n── K. DA 输入新鲜度审计 (覆盖≠实时) ──")
    print("  raw=%d | quality=%s | VIX as_of=%s | snapshot session=%s" % (
        k["raw_count"], k["quality_counts"], k["vix_as_of_basis"], k["snapshot_sessions"]))
    print("  _source_status 覆盖维度: %s" % sorted(k["source_status_keys"]))
    print("  顶层 as-of 缺失维度 (missing/total):")
    for r in k["missing_top_level_asof"][:12]:
        print("    %-24s %d/%d" % (r["dimension"], r["missing_n"], r["total_n"]))
    print("  口径: 只读披露，不接交易门；VIXY prev_close 不得当实时 VIX。")


def print_report(day: str, a: Dict[str, Any], b: Dict[str, Any], c: Dict[str, Any],
                 d: Dict[str, Any], e: Dict[str, Any], f: Dict[str, Any],
                 g: Optional[Dict[str, Any]] = None,
                 h: Optional[Dict[str, Any]] = None,
                 i: Optional[Dict[str, Any]] = None,
                 price_ledger: Optional[Dict[str, Any]] = None,
                 freshness: Optional[Dict[str, Any]] = None,
                 accuracy: Optional[Dict[str, Any]] = None) -> None:
    print("=" * 78)
    print("交易日归因 (只读) — %s" % day)
    print("=" * 78)
    print("\n── A. 口径对照 (三个数都可能是对的, 但不是同一个数) ──")
    print("  按开仓日=当日, 已平: n=%d 净 $%.2f" % (a["n_closed_by_open"], a["net_by_open"]))
    print("  按出场日=当日, 已平: n=%d 净 $%.2f   ← 盘后日报口径" % (a["n_by_close"], a["net_by_close"]))
    print("  当日开仓但未平: %d 笔" % len(a["open_rows"]))
    for r in a["open_rows"]:
        print("    %-10s %-6s %-22s 入场 %s  指令浮盈 %s" % (
            r["symbol"], r["direction"], r["strategy"], r["entry_time"][:16], r["instr_pnl"]))
    print("  open_positions 现存仓 (跨日):")
    for r in a["open_positions_table"]:
        print("    %-10s %-6s size=%-6s entry=%-9s SL=%-9s TP=%-8s sync=%-13s %s" % (
            r["symbol"], r["direction"], r["size"], r["entry"], r["sl"], r["tp"], r["sync"],
            r["entry_time"][:16]))

    print("\n── B. 来源拆分 ──")
    for r in b["by_strategy"]:
        print("  %-24s n=%2d 已平 %2d 净 $%8.2f" % (r["strategy_id"], r["n"], r["closed"], r["net"]))
    print("  跨日平掉的遗留仓 (当日平、但开仓日更早): %d 笔" % len(b["carry_over"]))
    for r in b["carry_over"]:
        print("    %-10s 开%s %s→%s %-8s %-22s %s" % (
            r["symbol"], r["trade_date"], (r["entry_time"] or "")[11:16], (r["exit_time"] or "")[11:16],
            r["pnl_net"], r["strategy_id"], r["exit_reason"]))

    print("\n── C. 出场归因: exit_reason × be_exit_class ──")
    print("  %-22s %5s %7s %11s %9s %10s" % ("exit_reason", "n", "亏笔", "净额", "每笔", "最差"))
    for r in c["by_reason"][:12]:
        print("  %-22s %5d %6.1f%% $%10.2f $%8.2f $%9.2f" % (
            r["reason"], r["n"], 100.0 * r["losers"] / r["n"], r["net"], r["per"], r["worst"]))
    print("  be_exit_class 分层 (BE 出场内部):")
    for r in c["be_exit_class"]:
        print("    %-6s n=%3d 净 $%9.2f 每笔 $%7.2f 亏笔 %.0f%%" % (
            r["cls"], r["n"], r["net"], r["per"], 100.0 * r["losers"] / r["n"]))

    print("\n── D. 闸门拦截 (当日 DA 指令) ──")
    print("  指令 %d 条, 执行 %d 条, 未执行 %d 条" % (d["n_instructions"], d["n_executed"],
                                                     d["n_instructions"] - d["n_executed"]))
    for k, v in d["skip_buckets"].items():
        print("    %-26s %d 笔  %s" % (k, len(v), ",".join(v)))
    print("  exec_gate_audit 当日行: %d" % d["gate_audit_rows"])
    for k, v in sorted(d["gate_audit_by_action"].items(), key=lambda kv: -kv[1])[:10]:
        print("    %-30s %d" % (k, v))

    print("\n── E. 跨臂方向冲突 ──")
    print("  DA 执行方向: %s" % e["da_executed"])
    print("  DA 全部方向(含被拦): %s" % e["da_all"])
    print("  EXT 成交: %s" % [(r["symbol"], r["direction"]) for r in e["ext_entries"]])
    print("  冲突: %s" % (e["conflicts"] or "无"))

    print("\n── F. 盘前跳空阈值反事实 (RFC-192 C2 口径候选 a, 阈值 |gap|<%.1f%%) ──" % f["threshold_pct"])
    print("  DA 成交 %d 笔, 盘前 gap 可算 %d 笔" % (f["n_da_trades"], f["gap_coverage"]))
    print("  保留: n=%d 净 $%.2f  %s" % (f["keep"]["n"], f["keep"]["net"], f["keep"]["symbols"]))
    print("  挡掉: n=%d 净 $%.2f  %s" % (f["drop"]["n"], f["drop"]["net"], f["drop"]["symbols"]))

    if g is not None:
        print("\n── G. 只读体检三件套 (Batch 0.6) ──")
        print("  ① 无 TP 的现存仓 (止盈只能靠出场引擎):")
        if not g["no_tp_positions"]:
            print("     (无)")
        for r in g["no_tp_positions"]:
            print("     %-10s %-6s size=%-6s entry=%-9s SL=%-9s TP=%-8s sync=%-13s %s" % (
                r["symbol"], r["direction"], r["size"], r["entry"], r["sl"], r["tp"],
                r["sync"], r["entry_time"][:16]))
            print("       grade=%s gate=%s cert_lock=%s 初始风险=$%s 利润保护=%s" % (
                r.get("entry_grade"), r.get("gate_mode"), r.get("cert_lock_pct"),
                r.get("initial_risk_usd"), r.get("profit_protection")))
        print("  ② 同标的平后立即重入:")
        if not g["same_day_reentry"]:
            print("     (无)")
        for r in g["same_day_reentry"]:
            print("     %-10s 间隔 %3d 分钟 | 平 %s (%s, %s) → 再开 %s %s" % (
                r["symbol"], r["gap_min"], r["closed_pnl"], r["closed_reason"],
                r["closed_strategy"][:12], r["reopened_strategy"][:12], r["reopened_state"]))
        print("  ③ 跨臂相反暴露: 见段 E")

    if h is not None:
        print("\n── H. EXT 入场风险影子 (Batch 1.2/1.3) ──")
        print("  影子行数 %d | DA 卡当时可得的比例 %s" % (
            h["n_shadow_rows"],
            ("%.0f%%" % (100 * h["da_available_rate"])) if h["da_available_rate"] is not None else "—"))
        print("  方向冲突组: %s" % h["conflict"])
        print("  非冲突组  : %s" % h["clean"])
        for r in h["rows"]:
            print("     %-10s %-10s da=%-7s conflict=%-5s max_loss=%-8s 结果=%s" % (
                r["symbol"], r["session"], r["da_direction"], r["conflict"],
                r["max_loss_usd"], r["outcome_pnl"]))

    if i is not None:
        print_segment_i(i)
    if price_ledger is not None:
        print_segment_j(price_ledger)
    if freshness is not None:
        print_segment_k(freshness)
    if accuracy is not None:
        print_segment_l(accuracy)


def main() -> int:
    ap = argparse.ArgumentParser(description="交易日归因尺子 (只读)")
    ap.add_argument("--date", required=True, help="交易日 (YYYY-MM-DD)")
    ap.add_argument("--gap-threshold", type=float, default=1.0, help="盘前跳空阈值 %% (默认 1.0)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    seg_a = segment_a(a.date)
    seg_b = segment_b(a.date)
    seg_c = segment_c()
    seg_d = segment_d(a.date)
    seg_e = segment_e(a.date)
    seg_f = segment_f(a.date, a.gap_threshold)
    seg_g = segment_g(a.date)
    seg_h = segment_h(a.date)
    seg_i = segment_i(a.date)
    seg_j = segment_j(a.date)
    seg_k = segment_k(a.date)
    seg_l = segment_l(a.date)

    if a.json:
        print(json.dumps({"A": seg_a, "B": seg_b, "C": seg_c, "D": seg_d, "E": seg_e,
                          "F": seg_f, "G": seg_g, "H": seg_h, "I": seg_i,
                          "J": seg_j, "K": seg_k, "L": seg_l},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print_report(a.date, seg_a, seg_b, seg_c, seg_d, seg_e, seg_f, seg_g, seg_h, seg_i, seg_j, seg_k, seg_l)
    return 0


if __name__ == "__main__":
    sys.exit(main())
