#!/usr/bin/env python3
"""隔夜持仓×新指令冲突影子打标 (2026-08-16, record-only, 零执行路径改动).

背景 (8/16 策略评价): W_WEEK(水下仓最多5交易日隔夜)上线后, 隔夜持仓股次日可能
被DA/UE再次选中。执行段已有方向感知检查(pipeline L3244-3311: 同向拦截/反向放行),
但零观测 — 不知道冲突频率、反向翻转结局、DA候选浪费率。

本脚本盘后回溯:
  1. 每日隔夜持仓 (entry_time < 当日 AND exit_time >= 当日 OR 未平仓)
  2. 当日DA/UE指令 (playbook_instructions)
  3. 重叠标的 → 打冲突标签:
     - same_direction_blocked: 同向持仓+同向新指令 (执行段拦截)
     - opposite_flip: 反向持仓+反向新指令 (F1设计允许翻转)
     - candidate_waste: DA候选含持仓股但未产指令 (分析资源浪费)
  4. 结局回填: 翻转单的pnl vs 被拦单的反事实 (当日该股涨跌幅)

幂等: 按 (date, symbol, inst_id) 去重。
挂载: AFTER_HOURS (scheduler_core), 配置键 conflict_shadow_enabled。
回滚: 配置键 false + 重启。
"""
import json, os, sqlite3, sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "conflict_shadow.jsonl"
MIN_TRADE_DATE = "2026-06-01"


def _load_existing_keys():
    """已落盘的 (date, symbol, inst_id) 集合 — 幂等。"""
    keys = set()
    if OUT.exists():
        for ln in OUT.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
                keys.add((r.get("date", ""), r.get("symbol", ""), r.get("inst_id", "")))
            except Exception:
                pass
    return keys


def run_check(scheduler=None):
    """调度器入口: 读取配置开关后执行打标。"""
    try:
        from app.core.config_container import ConfigContainer
        if not bool(ConfigContainer.get_instance().get_dotted(
                "playbook.conflict_shadow_enabled", True)):
            return
    except Exception:
        pass
    main()


def main():
    conn = sqlite3.connect(str(DB), timeout=15)
    conn.row_factory = sqlite3.Row

    # ── 交易日历: 从 trade_records 提取有交易的日期 ──
    trade_dates = sorted({
        r[0] for r in conn.execute(
            "SELECT DISTINCT substr(entry_time,1,10) FROM trade_records "
            "WHERE entry_time >= ? AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')",
            (MIN_TRADE_DATE,))
    })
    inst_dates = sorted({
        r[0] for r in conn.execute(
            "SELECT DISTINCT substr(created_at,1,10) FROM playbook_instructions "
            "WHERE created_at >= ?", (MIN_TRADE_DATE,))
    })
    all_dates = sorted(set(trade_dates) | set(inst_dates))

    existing = _load_existing_keys()
    added = 0
    rows_out = []

    for d in all_dates:
        # ── 1. 当日隔夜持仓: entry < d AND (exit >= d OR 未平仓) ──
        held = conn.execute(
            "SELECT symbol, direction, entry_time, entry_price, "
            "COALESCE(exit_time,'') exit_time, COALESCE(pnl_net, realized_pnl, 0) pnl "
            "FROM trade_records "
            "WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
            "AND substr(entry_time,1,10) < ? "
            "AND (exit_time IS NULL OR exit_time = '' OR substr(exit_time,1,10) >= ?)",
            (d, d)).fetchall()
        held_map = {}
        for h in held:
            sym = h["symbol"]
            held_map.setdefault(sym, []).append(dict(h))
        if not held_map:
            continue

        # ── 2. 当日指令 ──
        insts = conn.execute(
            "SELECT id, symbol, direction, created_at, entry_price, "
            "executed, triggered, skip_reason, confidence "
            "FROM playbook_instructions "
            "WHERE substr(created_at,1,10) = ? AND direction != 'SKIP'",
            (d,)).fetchall()

        # ── 3. 冲突匹配 ──
        for inst in insts:
            sym = inst["symbol"]
            if sym not in held_map:
                continue
            key = (d, sym, str(inst["id"]))
            if key in existing:
                continue

            inst_dir = (inst["direction"] or "").upper()
            for h in held_map[sym]:
                held_dir = (h["direction"] or "").upper()
                same = (inst_dir == "BUY" and held_dir == "LONG") or \
                       (inst_dir == "SELL_SHORT" and held_dir == "SHORT")
                conflict_type = "same_direction_blocked" if same else "opposite_flip"

                rec = {
                    "date": d,
                    "symbol": sym,
                    "inst_id": str(inst["id"]),
                    "conflict_type": conflict_type,
                    "held_direction": held_dir,
                    "held_entry_date": (h["entry_time"] or "")[:10],
                    "held_pnl": round(float(h["pnl"] or 0), 2),
                    "new_direction": inst_dir,
                    "new_inst_executed": bool(inst["executed"]),
                    "new_inst_triggered": bool(inst["triggered"]),
                    "new_inst_skip_reason": (inst["skip_reason"] or "")[:60],
                    "new_inst_conf": inst["confidence"],
                }
                rows_out.append(rec)
                existing.add(key)
                added += 1

        # ── 4. 候选浪费: 持仓股出现在当日扫描候选但无指令 ──
        scan_file = ROOT / "data" / f"opportunity_scan_{d}.json"
        if scan_file.exists():
            try:
                scan = json.loads(scan_file.read_text())
                items = scan if isinstance(scan, list) else scan.get("candidates", [])
                scan_syms = {it.get("symbol", "").replace("US.", "") for it in items}
                inst_syms = {i["symbol"].replace("US.", "") for i in insts}
                for sym in held_map:
                    bare = sym.replace("US.", "")
                    if bare in scan_syms and bare not in inst_syms:
                        key = (d, sym, "candidate_waste")
                        if key not in existing:
                            rows_out.append({
                                "date": d, "symbol": sym, "inst_id": "candidate_waste",
                                "conflict_type": "candidate_waste",
                                "held_direction": held_map[sym][0]["direction"],
                                "held_entry_date": (held_map[sym][0]["entry_time"] or "")[:10],
                                "held_pnl": round(float(held_map[sym][0]["pnl"] or 0), 2),
                                "new_direction": None,
                                "new_inst_executed": None,
                                "new_inst_triggered": None,
                                "new_inst_skip_reason": None,
                                "new_inst_conf": None,
                            })
                            existing.add(key)
                            added += 1
            except Exception:
                pass

    # ── 落盘 ──
    if rows_out:
        with OUT.open("a", encoding="utf-8") as f:
            for r in rows_out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ── 摘要 ──
    from collections import Counter
    types = Counter(r["conflict_type"] for r in rows_out)
    print(f"[conflict_shadow] 新增 {added} 行 → {OUT}")
    for t, n in types.most_common():
        print(f"  {t}: {n}")
    if not rows_out:
        print("  (无新增)")

    conn.close()


if __name__ == "__main__":
    main()
