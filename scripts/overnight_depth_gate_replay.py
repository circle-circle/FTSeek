#!/usr/bin/env python3
"""隔夜 W_WEEK 深度门回放裁决 (2026-09-13, 用户批准"同意继续").

问题: loser_overnight_max_depth_pct=99 (无深度门) — 任何水下仓都豁免过夜.
实测 (overnight_arm 台账, 16笔 W_WEEK):
  浅水(>-2%): n=12 +$337 胜83% | 深水(≤-3%): n=4 -$961 胜25% (全是隔夜跳空/SL打死)
本回放: 复用 overnight_loser_multiday_replay 样本+simulate_multiday,
对 EOD 水下深度跑门谱系 GATE=[1.0, 1.5, 2.0, 2.5, 3.0, 99] —
depth > gate → 不豁免, 当日平仓 (delta=0); depth ≤ gate → W_WEEK 多日持有.

用法: venv/bin/python scripts/overnight_depth_gate_replay.py
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overnight_loser_multiday_replay import (  # noqa: E402
    DB, MIN, MAX_HOLD_DAYS, atr14_pct, compute_sl_pct, load_bars, simulate_multiday,
)

GATES = [1.0, 1.5, 2.0, 2.5, 3.0, 99.0]


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT DISTINCT t.id, t.symbol, t.direction, t.entry_time, t.exit_time,
               t.entry_price, t.exit_price, t.quantity, t.pnl_net, t.metadata_json
        FROM trade_records t
        WHERE t.exit_reason = 'PRE_CLOSE_EXIT' AND t.pnl_net IS NOT NULL
          AND t.pnl_net <= 0 AND t.exit_time >= ?
        ORDER BY t.exit_time
    """, (MIN,)).fetchall()
    con.close()

    _bc = {}
    samples = []
    skipped = defaultdict(int)
    for r in rows:
        sym = r["symbol"]
        if sym not in _bc:
            _bc[sym] = load_bars(sym)
        bars = _bc[sym]
        if not bars:
            skipped["no_bars"] += 1
            continue
        days = [b[0] for b in bars]
        ex_date = r["exit_time"][:10]
        if ex_date not in days:
            skipped["exit_day_missing"] += 1
            continue
        i = days.index(ex_date)
        if i + 1 >= len(bars):
            skipped["no_next_day"] += 1
            continue
        entry_price = float(r["entry_price"])
        qty = float(r["quantity"] or 1)
        direction = r["direction"]
        baseline_close = float(r["exit_price"])
        sl_price = None
        try:
            md = json.loads(r["metadata_json"] or "{}")
            sl_price = float(md.get("stop_loss") or 0) or None
        except Exception:
            pass
        if not sl_price:
            ap = atr14_pct(bars, i)
            if ap:
                sl_pct = compute_sl_pct(ap, direction)
                if sl_pct:
                    sl_price = entry_price * (1 - sl_pct / 100) if direction == "LONG" \
                        else entry_price * (1 + sl_pct / 100)
        if not sl_price:
            skipped["no_sl"] += 1
            continue
        # EOD 水下深度 % (符号化: 负=水下)
        s = 1.0 if direction == "LONG" else -1.0
        depth_pct = s * (baseline_close - entry_price) / entry_price * 100
        exit_w, off_w, tag_w = simulate_multiday(
            bars, i, direction, entry_price, sl_price, qty, MAX_HOLD_DAYS)
        d_wk = s * (exit_w - baseline_close) * qty
        samples.append({
            "date": ex_date, "symbol": sym, "direction": direction,
            "depth_pct": depth_pct, "day_pnl": float(r["pnl_net"]),
            "w_week_delta": d_wk, "w_week_tag": tag_w, "w_week_days": off_w,
        })

    n = len(samples)
    print(f"═══ 样本: EOD水下仓可回放 n={n} ({MIN}起) | 跳过: {dict(skipped)} ═══\n")
    if n == 0:
        return

    print(f"═══ 深度门谱系 (depth > gate → 当日平仓不豁免; W_WEEK 上限{MAX_HOLD_DAYS}日) ═══")
    print(f"{'gate':>6} {'held':>5} {'closed':>6} {'Σdelta':>9} {'均笔':>8} "
          f"{'修复率':>7} {'拦掉的delta':>10}")
    results = {}
    for gate in GATES:
        held = [x for x in samples if x["depth_pct"] >= -gate]
        closed = [x for x in samples if x["depth_pct"] < -gate]
        deltas = [x["w_week_delta"] for x in held]
        wins = sum(1 for d in deltas if d > 0)
        sigma = sum(deltas)
        blocked = sum(x["w_week_delta"] for x in closed)
        results[gate] = {"n_held": len(held), "sigma": sigma,
                         "avg": sigma / len(held) if held else 0.0,
                         "win": wins / len(held) * 100 if held else 0.0,
                         "blocked_delta": blocked}
        print(f"{gate:>6.1f} {len(held):>5} {len(closed):>6} {sigma:>+9.0f} "
              f"{results[gate]['avg']:>+8.2f} {results[gate]['win']:>6.1f}% {blocked:>+10.0f}")

    # 被门拦掉的深水仓明细 (gate=2.0 为例)
    print("\n═══ gate=2.0 被拦明细 (深水仓 W_WEEK 反事实结局) ═══")
    for x in sorted([s for s in samples if s["depth_pct"] < -2.0], key=lambda y: y["depth_pct"]):
        print(f"  {x['date']} {x['symbol']:<10} {x['direction']:<5} "
              f"depth={x['depth_pct']:+.2f}% → delta=${x['w_week_delta']:+8.0f} "
              f"({x['w_week_tag']}, {x['w_week_days']}d)")

    # 深水 vs 浅水分布 (无门基线)
    deep = [x for x in samples if x["depth_pct"] < -3.0]
    mid = [x for x in samples if -3.0 <= x["depth_pct"] < -2.0]
    shallow = [x for x in samples if x["depth_pct"] >= -2.0]
    print("\n═══ 无门基线分层 ═══")
    for name, sub in (("浅水(>-2%)", shallow), ("中水(-2~-3%)", mid), ("深水(≤-3%)", deep)):
        if sub:
            sig = sum(x["w_week_delta"] for x in sub)
            w = sum(1 for x in sub if x["w_week_delta"] > 0)
            print(f"  {name:<12} n={len(sub):>2} Σdelta=${sig:+8.0f} "
                  f"均笔${sig/len(sub):+7.2f} 修复率={w/len(sub)*100:.0f}%")

    out = Path(__file__).resolve().parents[1] / "data" / "overnight_depth_gate_replay_2026-09-13.json"
    out.write_text(json.dumps({"samples": samples, "results": {str(k): v for k, v in results.items()}},
                              ensure_ascii=False, indent=1))
    print(f"\n[saved] {out}")


if __name__ == "__main__":
    main()
