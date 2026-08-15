#!/usr/bin/env python3
"""DEB-20260815 §38 前置回放: 水下仓多夜持有上限谱系 (裁决 max_hold_days 最优值).

用户问: "隔夜5日是我拍脑袋想的, 有没有更合理的日期, 或不需要限制时长?"
方法: 复用 overnight_loser_multiday_replay.py 的样本/SL/simulate_multiday
(W_WEEK 语义: EOD转正落袋 + SL触碰 + 到期强平), 对 max_days 跑谱系
[1,2,3,5,8,10,15,99] — 99 即"无限制"(在 bars 范围内).

用法: venv/bin/python scripts/overnight_holdcap_spectrum.py
"""
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from overnight_loser_multiday_replay import (  # noqa: E402
    DB, MIN, atr14_pct, compute_sl_pct, load_bars, simulate_multiday,
)

CAPS = [1, 2, 3, 5, 8, 10, 15, 99]


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
    samples = []  # (baseline_close, bars, i, direction, entry_price, sl_price, qty)
    for r in rows:
        sym = r["symbol"]
        if sym not in _bc:
            _bc[sym] = load_bars(sym)
        bars = _bc[sym]
        if not bars:
            continue
        days = [b[0] for b in bars]
        ex_date = r["exit_time"][:10]
        if ex_date not in days:
            continue
        i = days.index(ex_date)
        if i + 1 >= len(bars):
            continue
        entry_price = float(r["entry_price"])
        qty = float(r["quantity"] or 1)
        direction = r["direction"]
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
            continue
        samples.append((float(r["exit_price"]), bars, i, direction,
                        entry_price, sl_price, qty, sym, ex_date))

    n = len(samples)
    print(f"═══ 样本: EOD水下仓可回放 n={n} ({MIN}起) ═══\n")
    if n == 0:
        print("样本不足")
        return

    print(f"{'上限':>4} | {'Σdelta':>9} | {'均笔':>8} | {'修复率':>6} | "
          f"{'SL出局':>6} | {'到期强平':>7} | {'平均持有':>7}")
    print("-" * 72)
    results = {}
    for cap in CAPS:
        deltas, tags = [], []
        holds = []
        for (base, bars, i, d, ep, sl, qty, sym, dt) in samples:
            ex, off, tag = simulate_multiday(bars, i, d, ep, sl, qty, cap)
            s = 1.0 if d == "LONG" else -1.0
            deltas.append(s * (ex - base) * qty)
            tags.append(tag)
            holds.append(off)
        wins = sum(1 for x in deltas if x > 0)
        sl_out = sum(1 for t in tags if t.startswith("SL"))
        forced = sum(1 for t in tags if t in ("max_hold", "bars_exhausted"))
        tot = sum(deltas)
        results[cap] = tot
        cap_lab = "无限制" if cap == 99 else f"{cap}日"
        print(f"{cap_lab:>4} | ${tot:>+8,.0f} | ${tot/n:>+7.2f} | "
              f"{wins/n*100:>5.1f}% | {sl_out:>5} | {forced:>6} | "
              f"{sum(holds)/n:>6.1f}天")

    best = max(results, key=results.get)
    print(f"\n谱系最优: {best}日 (${results[best]:+,.0f})")
    out = Path(__file__).resolve().parents[1] / "data" / "overnight_holdcap_spectrum_2026-08-16.json"
    out.write_text(json.dumps({"n": n, "results": {str(k): v for k, v in results.items()}},
                              indent=1, ensure_ascii=False))
    print(f"落盘: {out}")


if __name__ == "__main__":
    main()
