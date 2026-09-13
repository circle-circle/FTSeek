#!/usr/bin/env python3
"""隔夜 W_WEEK 深度门回放 v2 (2026-09-13) — 对真实 W_WEEK 台账反向回放.

v1 (overnight_depth_gate_replay.py) 样本失效: 8/15 W_WEEK 启用后水下仓不再走
PRE_CLOSE_EXIT, 旧回放样本全是启用前浅水仓 (depth≥-1.25%), 门谱系无区分度.

v2 方法: 取真实被豁免过夜的仓 (metadata_json.overnight_arm, source=w_week),
反事实 = 标记日 (marked_at_et 当日) EOD 收盘平仓 vs 实际持有结局.
delta = 反事实pnl − 实际pnl (正 = 平仓更好/门有价值).
对 depth 门谱系 [1.0, 1.5, 2.0, 2.5, 3.0, 99] 分别算: 门拦下哪些仓, 省多少.

收盘价来源: data/replay_bars/daily_<sym>_*.json (bar[0]=date, bar[4]=close).
已知局限: n=16; 收盘价近似 15:52 平仓价 (误差分钟级).
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "ftseek_us.db"
CACHE = Path(__file__).resolve().parents[1] / "data" / "replay_bars"
GATES = [1.0, 1.5, 2.0, 2.5, 3.0, 99.0]


def load_bars_covering(symbol, date):
    """选包含 date 的最长 bar 文件 (load_bars 只选全局最长, 可能不覆盖 mark_date)."""
    best = []
    for f in CACHE.glob(f"daily_{symbol.replace('.', '_')}_*.json"):
        try:
            b = json.loads(f.read_text())
        except Exception:
            continue
        if any(x[0] == date for x in b) and len(b) > len(best):
            best = b
    return best


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT symbol, direction, entry_time, exit_time, entry_price, exit_price,
               quantity, pnl_net, fees_est, exit_reason, metadata_json
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND exit_time IS NOT NULL
          AND json_extract(metadata_json,'$.overnight_arm') IS NOT NULL
        ORDER BY entry_time
    """).fetchall()
    con.close()

    samples = []
    skipped = defaultdict(int)
    for r in rows:
        arm = json.loads(r["metadata_json"]).get("overnight_arm", {})
        if arm.get("source") != "w_week":
            skipped[f"arm_{arm.get('source')}"] += 1
            continue
        depth = float(arm.get("underwater_pct_net", 0))  # 负=水下
        mark_date = (arm.get("marked_at_et") or "")[:10]
        if not mark_date:
            skipped["no_mark_date"] += 1
            continue
        bars = load_bars_covering(r["symbol"], mark_date)
        if not bars:
            skipped["no_bars"] += 1
            continue
        days = [b[0] for b in bars]
        if mark_date not in days:
            skipped["mark_day_missing"] += 1
            continue
        close_px = bars[days.index(mark_date)][4]
        entry = float(r["entry_price"]); qty = float(r["quantity"] or 1)
        s = 1.0 if r["direction"] == "LONG" else -1.0
        # 反事实: 标记日 EOD 平仓的毛pnl; 实际: pnl_net (已含费). 同 round-trip 费用相同 → 比毛价差即可
        cf_pnl = s * (close_px - entry) * qty
        actual = float(r["pnl_net"] or 0)
        fees = float(r["fees_est"] or 0)
        actual_gross = actual + fees
        delta = cf_pnl - actual_gross  # 正=当时平掉更省
        samples.append({
            "symbol": r["symbol"], "direction": r["direction"],
            "entry_date": r["entry_time"][:10], "mark_date": mark_date,
            "depth_pct": depth, "night_n": arm.get("night_n"),
            "actual_gross": actual_gross, "cf_pnl": cf_pnl, "delta": delta,
            "exit_reason": r["exit_reason"], "actual_pnl_net": actual,
        })

    n = len(samples)
    print(f"═══ 样本: 真实 W_WEEK 豁免仓 n={n} | 跳过: {dict(skipped)} ═══\n")
    if n == 0:
        return

    print("═══ 逐笔 (depth → 实际毛pnl vs 标记日EOD平仓反事实) ═══")
    for x in sorted(samples, key=lambda y: y["depth_pct"]):
        print(f"  {x['mark_date']} {x['symbol']:<10} {x['direction']:<5} n{x['night_n']} "
              f"depth={x['depth_pct']:+.2f}% 实际=${x['actual_gross']:+7.0f} "
              f"EOD平=${x['cf_pnl']:+7.0f} delta=${x['delta']:+7.0f} ({x['exit_reason']})")

    print(f"\n═══ 深度门谱系 (|depth|>gate → 不豁免, 当日EOD平仓) ═══")
    print(f"{'gate':>6} {'held':>5} {'blocked':>7} {'持有实际Σ':>10} {'门省下Σdelta':>12} {'修复率':>7}")
    for gate in GATES:
        held = [x for x in samples if abs(x["depth_pct"]) <= gate]
        blocked = [x for x in samples if abs(x["depth_pct"]) > gate]
        held_sum = sum(x["actual_gross"] for x in held)
        saved = sum(x["delta"] for x in blocked)
        win = sum(1 for x in held if x["actual_gross"] > 0)
        print(f"{gate:>6.1f} {len(held):>5} {len(blocked):>7} {held_sum:>+10.0f} "
              f"{saved:>+12.0f} {win/len(held)*100 if held else 0:>6.0f}%")

    out = Path(__file__).resolve().parents[1] / "data" / "overnight_depth_gate_replay_v2_2026-09-13.json"
    out.write_text(json.dumps(samples, ensure_ascii=False, indent=1))
    print(f"\n[saved] {out}")


if __name__ == "__main__":
    main()
