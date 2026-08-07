#!/usr/bin/env python3
"""
E2 shadow: 每日盘后入场动能确认增量回放 (2026-08-07)
=====================================================
背景 (E2 counterfactual 已验证): 历史 129 笔 DA 交易, 15min 窗口动能确认
(window mfe>=0.5%) 拦截 86笔 -963 (WR34%), 放行 43笔 +97 (WR51%);
全参数网格 (窗口5-60min × θ0.3-1.0%) 方向稳健 — 拦截组全亏, 放行组全正。

本脚本 (record-only, 零行为变化): 每日盘后对"当日新平仓的 DA 交易"
计算入场后 N 分钟窗口内 mfe (动能判据), 追加到 data/e2_momentum_shadow.jsonl,
与最终 pnl_net 对照累积样本。用途: 攒满 20-30 笔后验证入场动能确认
是否值得落地为硬门 (不砍当前供给, 与 sim 采集期"数据量优先"兼容)。

用法:
    venv/bin/python scripts/e2_momentum_shadow_daily.py [--window 15] [--date 2026-08-06]
    # 无 --date: 自动选取最近未回放交易日 (按 trade_date 增量)
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.e2_momentum_confirm_replay import parse_entry_et, load_bars  # noqa: E402

DB_TRADES = PROJECT_ROOT / "data" / "ftseek_us.db"
DB_SNAPS = PROJECT_ROOT / "data" / "signal_snapshots.db"
OUT_JSONL = PROJECT_ROOT / "data" / "e2_momentum_shadow.jsonl"


def load_done_dates():
    """已回放过的 trade_date 集合 + 已处理 decision_id 集合."""
    dates, dids = set(), set()
    if OUT_JSONL.exists():
        for line in OUT_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if row.get("trade_date"):
                    dates.add(row["trade_date"])
                if row.get("decision_id"):
                    dids.add(row["decision_id"])
            except Exception:
                continue
    return dates, dids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=15)
    ap.add_argument("--date", type=str, default=None)
    args = ap.parse_args()

    done_dates, done_dids = load_done_dates()

    conn = sqlite3.connect(str(DB_TRADES))
    conn.row_factory = sqlite3.Row
    if args.date:
        date_filter = (args.date,)
        date_sql = "trade_date = ?"
    else:
        # 增量: 最近 10 个交易日中未回放过的
        date_filter = tuple(
            r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM trade_records "
                "WHERE decision_id LIKE 'DA-%' AND trade_date >= date('now','-10 days') "
                "ORDER BY trade_date DESC"
            ).fetchall()
        )
        date_sql = f"trade_date IN ({','.join('?' * len(date_filter))})"
    rows = conn.execute(f"""
        SELECT decision_id, symbol, direction, entry_time, exit_time, exit_reason,
               mfe_pct, mae_pct, pnl_net, trade_date, entry_price
        FROM trade_records
        WHERE decision_id LIKE 'DA-%' AND {date_sql}
          AND mfe_pct IS NOT NULL AND entry_time IS NOT NULL AND entry_price > 0
          AND exit_time IS NOT NULL
    """, date_filter).fetchall()
    conn.close()

    snap = sqlite3.connect(str(DB_SNAPS))
    snap.row_factory = sqlite3.Row
    strength_map = {r["decision_id"]: r["strength"]
                    for r in snap.execute("SELECT decision_id, strength FROM signal_snapshots WHERE strength IS NOT NULL")}
    snap.close()

    new_rows = []
    for r in rows:
        did = r["decision_id"]
        if did in done_dids:
            continue
        entry_dt = parse_entry_et(r["entry_time"])
        if entry_dt is None:
            continue
        bars = load_bars(r["symbol"], r["trade_date"])
        if not bars:
            continue
        window_end = entry_dt + timedelta(minutes=args.window)
        win_high = win_low = None
        for b in bars:
            try:
                ts = datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if ts < entry_dt:
                continue
            if ts > window_end:
                break
            hi, lo = float(b[2]), float(b[3])
            win_high = hi if win_high is None else max(win_high, hi)
            win_low = lo if win_low is None else min(win_low, lo)
        if win_high is None or win_low is None:
            continue
        entry = float(r["entry_price"])
        if r["direction"] == "LONG":
            win_mfe = (win_high - entry) / entry * 100
        else:
            win_mfe = (entry - win_low) / entry * 100
        new_rows.append({
            "ts": datetime.utcnow().isoformat(timespec="seconds"),
            "trade_date": r["trade_date"],
            "decision_id": did,
            "symbol": r["symbol"],
            "direction": r["direction"],
            "strength": strength_map.get(did),
            "entry_time": r["entry_time"],
            "exit_time": r["exit_time"],
            "exit_reason": r["exit_reason"],
            "window_min": args.window,
            "window_mfe_pct": round(win_mfe, 3),
            "full_mfe_pct": r["mfe_pct"],
            "mae_pct": r["mae_pct"],
            "pnl_net": r["pnl_net"],
        })

    if new_rows:
        with open(OUT_JSONL, "a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"新增 {len(new_rows)} 条 → {OUT_JSONL}")
    else:
        print("无新增 (全部已回放或无数据)")

    # 汇总 (含历史累计)
    all_rows = []
    if OUT_JSONL.exists():
        for line in OUT_JSONL.read_text(encoding="utf-8").splitlines():
            try:
                all_rows.append(json.loads(line))
            except Exception:
                continue

    def _stats(grp):
        if not grp:
            return "n=0"
        net = sum(x.get("pnl_net") or 0 for x in grp)
        wr = sum(1 for x in grp if x.get("pnl_net") and x["pnl_net"] > 0) / len(grp)
        return f"n={len(grp)} net={net:+.1f} WR={wr:.0%}"

    theta = 0.5
    blocked = [x for x in all_rows if x["window_mfe_pct"] < theta]
    passed = [x for x in all_rows if x["window_mfe_pct"] >= theta]
    print(f"\n=== E2 shadow 累计 (window={args.window}min θ={theta}%) ===")
    print(f"  拦截 (无动能): {_stats(blocked)}")
    print(f"  放行 (有动能): {_stats(passed)}")


if __name__ == "__main__":
    main()