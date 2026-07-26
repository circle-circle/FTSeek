#!/usr/bin/env python3
"""从 trade_records 全量重建 strategy_memory.db (幂等/可复现)。

背景: feedback_loop.py 按 trade_date 逐日增量 update_from_trade(), 无去重/无幂等。
历史多次 backfill 重跑 + 过滤条件中途变更 → symbol_memory 次数虚高约3倍、
部分亏损单漏记 (如 GOOG -$6015 未入库反显示 100% 胜)。

本脚本以 trade_records (realized_pnl 已核对准确) 为唯一真源, 用与 feedback_loop
_get_closed_trades() 完全一致的过滤条件, 全量重算 symbol_memory 与 pattern_memory。
可反复运行, 结果幂等。默认 dry-run, --apply 才写库 (写前自动备份)。

用法:
    ./venv/bin/python scripts/rebuild_strategy_memory.py            # 预览差异
    ./venv/bin/python scripts/rebuild_strategy_memory.py --apply    # 备份并重建
"""
import argparse
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

# 与 DA skill 同源的路径推导 (config.FTSEEK_DB 同目录)
FTSEEK_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "ftseek_us.db")
MEM_DB = os.path.join(os.path.dirname(FTSEEK_DB), "strategy_memory.db")

# feedback_loop._get_closed_trades 的过滤 (SSOT, 必须与之保持一致)
# MAIN_PIPELINE: 旧管线已禁用 (TRAILING_STOP/STRUCTURE_EXIT/SL_FIXED_PCT 等退出逻辑
#   与统一管线完全不同), 其历史规律会带偏现行策略 → 排除, 画像只学统一管线 PLAYBOOK_SIM。
_EXCLUDE_STRATEGY = ("manual", "manual_sync", "reconciler", "futu_sync", "broker_reconcile")
_EXCLUDE_SOURCE = ("SCALPING", "ORB_ENGINE", "PLAYBOOK_SHADOW", "MAIN_PIPELINE")


def _strength_bucket(strength) -> str:
    try:
        s = int(strength)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 80:
        return "80+"
    elif s >= 70:
        return "70-80"
    elif s >= 60:
        return "60-70"
    elif s >= 50:
        return "50-60"
    else:
        return "<50"


def _fetch_truth_trades():
    conn = sqlite3.connect(FTSEEK_DB)
    conn.row_factory = sqlite3.Row
    ph_strat = ",".join("?" * len(_EXCLUDE_STRATEGY))
    ph_src = ",".join("?" * len(_EXCLUDE_SOURCE))
    rows = conn.execute(f"""
        SELECT trade_date, symbol, direction, realized_pnl, holding_hours,
               signal_strength, exit_reason
        FROM trade_records
        WHERE realized_pnl IS NOT NULL
          AND strategy_id NOT IN ({ph_strat})
          AND source_type NOT IN ({ph_src})
        ORDER BY trade_date ASC, id ASC
    """, (*_EXCLUDE_STRATEGY, *_EXCLUDE_SOURCE)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _aggregate(trades):
    """返回 (symbol_agg, pattern_agg), 各为 dict[key] -> 统计。"""
    sym = defaultdict(lambda: {
        "total_trades": 0, "wins": 0, "total_pnl": 0.0,
        "best_win": None, "worst_loss": None, "last_trade_date": "",
    })
    pat = defaultdict(lambda: {
        "total_trades": 0, "wins": 0, "total_pnl": 0.0,
        "hold_sum": 0.0, "last_trade_date": "",
    })
    for t in trades:
        pnl = float(t.get("realized_pnl") or 0)
        hold = float(t.get("holding_hours") or 0)
        is_win = pnl > 0
        td = t.get("trade_date") or ""

        sk = (t.get("symbol", ""), t.get("direction", ""))
        s = sym[sk]
        s["total_trades"] += 1
        s["wins"] += 1 if is_win else 0
        s["total_pnl"] += pnl
        if is_win:
            s["best_win"] = pnl if s["best_win"] is None else max(s["best_win"], pnl)
        else:
            s["worst_loss"] = pnl if s["worst_loss"] is None else min(s["worst_loss"], pnl)
        if td > s["last_trade_date"]:
            s["last_trade_date"] = td

        pk = (t.get("direction", ""), t.get("exit_reason", "unknown"),
              _strength_bucket(t.get("signal_strength", 0)))
        p = pat[pk]
        p["total_trades"] += 1
        p["wins"] += 1 if is_win else 0
        p["total_pnl"] += pnl
        p["hold_sum"] += hold
        if td > p["last_trade_date"]:
            p["last_trade_date"] = td
    return sym, pat


def _current_symbol_counts():
    if not os.path.exists(MEM_DB):
        return {}
    c = sqlite3.connect(MEM_DB)
    c.row_factory = sqlite3.Row
    try:
        return {(r["symbol"], r["direction"]): (r["total_trades"], r["wins"])
                for r in c.execute("SELECT symbol,direction,total_trades,wins FROM symbol_memory")}
    except sqlite3.OperationalError:
        return {}
    finally:
        c.close()


def _apply(sym, pat):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(MEM_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("DELETE FROM symbol_memory")
    conn.execute("DELETE FROM pattern_memory")
    for (symbol, direction), s in sym.items():
        tt = s["total_trades"]
        conn.execute("""
            INSERT INTO symbol_memory (symbol, direction, total_trades, wins, total_pnl,
                                       avg_pnl, win_rate, best_win, worst_loss,
                                       last_trade_date, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (symbol, direction, tt, s["wins"], round(s["total_pnl"], 4),
              round(s["total_pnl"] / tt, 4) if tt else 0,
              round(s["wins"] / tt, 4) if tt else 0,
              s["best_win"] if s["best_win"] is not None else 0,
              s["worst_loss"] if s["worst_loss"] is not None else 0,
              s["last_trade_date"], now))
    for (direction, exit_reason, bucket), p in pat.items():
        tt = p["total_trades"]
        conn.execute("""
            INSERT INTO pattern_memory (direction, exit_reason, strength_bucket,
                                        total_trades, wins, total_pnl, avg_hold_hours,
                                        avg_pnl, win_rate, last_updated, last_trade_date)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (direction, exit_reason, bucket, tt, p["wins"], round(p["total_pnl"], 4),
              round(p["hold_sum"] / tt, 4) if tt else 0,
              round(p["total_pnl"] / tt, 4) if tt else 0,
              round(p["wins"] / tt, 4) if tt else 0,
              now, p["last_trade_date"]))
    conn.commit()
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库 (默认 dry-run)")
    args = ap.parse_args()

    if not os.path.exists(FTSEEK_DB):
        print(f"[错误] 找不到真源 DB: {FTSEEK_DB}", file=sys.stderr)
        return 1

    trades = _fetch_truth_trades()
    sym, pat = _aggregate(trades)
    print(f"真源 trade_records (过滤后已平仓): {len(trades)} 笔")
    print(f"重算结果: symbol_memory {len(sym)} 键, pattern_memory {len(pat)} 键")

    old = _current_symbol_counts()
    old_total = sum(v[0] for v in old.values())
    new_total = sum(s["total_trades"] for s in sym.values())
    print(f"\n次数对比: 现库累计 {old_total} 次 → 重算 {new_total} 次 "
          f"(差 {old_total - new_total:+d})")

    # top 差异
    print("\n=== 差异最大 top15 (symbol×direction) ===")
    diffs = []
    keys = set(old) | set(sym)
    for k in keys:
        ot, ow = old.get(k, (0, 0))
        s = sym.get(k)
        nt, nw = (s["total_trades"], s["wins"]) if s else (0, 0)
        diffs.append((abs(ot - nt), k, ot, ow, nt, nw))
    for _, k, ot, ow, nt, nw in sorted(diffs, reverse=True)[:15]:
        tag = "" if (ot == nt and ow == nw) else "  <<<"
        print(f"  {k[0]:9}{k[1]:6} 旧={ot:2}/{ow:2}胜 → 新={nt:2}/{nw:2}胜{tag}")

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply 执行 (会先备份)。")
        return 0

    if os.path.exists(MEM_DB):
        bak = f"{MEM_DB}.bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(MEM_DB, bak)
        print(f"\n[备份] {bak}")
    _apply(sym, pat)
    print("[完成] strategy_memory.db 已从 trade_records 全量重建。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
