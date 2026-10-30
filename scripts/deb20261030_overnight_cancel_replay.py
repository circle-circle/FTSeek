#!/usr/bin/env python3
"""C17 判据复现: 若「整体不做隔夜」(跨夜仓一律在入场日收盘前平掉) 会怎样。

只读承诺: sqlite `mode=ro`, 只读仓内日K/1m 缓存, 不写库、不下单、不改任何文件。

人群/口径
---------
A10: `trade_date >= 2026-07-01` ∧ `data_quality ∈ (clean, real)`
     ∧ `source_type ∉ (POSITION_SYNC, MANUAL)` ∧ 已平 (exit_price > 0 ∧ pnl_net 非空) ⇒ n=721。
跨夜 = `substr(entry_time,1,10) < substr(exit_time,1,10)` ⇒ 79 笔。

反事实 = 把出场价换成**入场日收盘价**, 数量/方向/费用不变:
    cf_net = (entry_day_close − entry_price) × qty × sign − fees_est
Δ = cf_net − pnl_net, **正 = 不做隔夜更好**。
为什么不用线性缩放: 缩放隐含"仍走同一条出场路径"; 这里动作是换出场时点, 必须在那个时点的
真实价格上兑现 (与 C14 同一条纪律)。

稳健性臂: 用 1m 缓存里入场日 15:52 ET 的收盘价 (与生产 EOD 决策点同刻) 只跑覆盖到的子集,
验符号一致性 —— 两臂量级可差, 方向必须同号。

判据见 docs/ADJUDICATED-CRITERIA.md C17。
"""
import glob
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"

A10 = """
SELECT symbol, direction, quantity, entry_price, entry_time, exit_time, exit_reason,
       pnl_net, fees_est, metadata_json
  FROM trade_records
 WHERE exit_price > 0 AND pnl_net IS NOT NULL
   AND trade_date >= '2026-07-01'
   AND COALESCE(data_quality, '') IN ('clean', 'real')
   AND COALESCE(source_type, '') NOT IN ('POSITION_SYNC', 'MANUAL')
 ORDER BY entry_time
"""


def _load(dirname: str, idx: int) -> dict:
    """缓存文件名 US_XXX.json → {symbol: {date[:时间]: close}}。"""
    out = {}
    for f in glob.glob(str(ROOT / dirname / "*.json")):
        sym = os.path.basename(f)[:-5].replace("_", ".", 1)
        out[sym] = {row[0]: float(row[idx]) for row in json.load(open(f))}
    return out


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    trades = [dict(r) for r in conn.execute(A10)]
    overnight = [t for t in trades
                 if str(t["entry_time"])[:10] < str(t["exit_time"])[:10]]
    daily = _load("data/replay_bars_daily", 4)      # [date, o, h, l, c]
    m1 = _load("data/replay_bars_1m", 4)            # [ts, o, h, l, c, vol]

    def cf(r, px):
        sign = 1 if str(r["direction"]).upper() == "LONG" else -1
        return (px - float(r["entry_price"])) * float(r["quantity"]) * sign - float(r["fees_est"] or 0)

    rows, nodata = [], []
    for r in overnight:
        d = str(r["entry_time"])[:10]
        px = daily.get(r["symbol"], {}).get(d)
        if px is None:
            nodata.append((r["symbol"], d, r["exit_reason"], round(r["pnl_net"], 1)))
            continue
        r["cf"] = cf(r, px)
        r["p52"] = next((m1[r["symbol"]][f"{d} {t}:00"] for t in ("15:52", "15:57", "16:00")
                         if r["symbol"] in m1 and f"{d} {t}:00" in m1[r["symbol"]]), None)
        rows.append(r)

    tot_a = sum(r["pnl_net"] for r in rows)
    tot_cf = sum(r["cf"] for r in rows)
    delta = tot_cf - tot_a
    print(f"A10 已平 {len(trades)} 笔 / 跨夜 {len(overnight)} 笔 / 有入场日收盘价 {len(rows)} 笔"
          f" (缺 {len(nodata)}: {nodata})")
    print(f"\n实际(持夜) Σ净 = {tot_a:+.2f}   当日收盘平 Σ净 = {tot_cf:+.2f}"
          f"   Δ(不持夜−实际) = {delta:+.2f}")

    win = [r for r in rows if r["pnl_net"] > 0]
    los = [r for r in rows if r["pnl_net"] <= 0]
    print(f"  错过赢家 {len(win)} 笔: 实际 {sum(r['pnl_net'] for r in win):+.2f} / 当日平 "
          f"{sum(r['cf'] for r in win):+.2f} ⇒ 放弃 {sum(r['pnl_net'] - r['cf'] for r in win):+.2f}")
    print(f"  避开亏损 {len(los)} 笔: 实际 {sum(r['pnl_net'] for r in los):+.2f} / 当日平 "
          f"{sum(r['cf'] for r in los):+.2f} ⇒ 省下 {sum(r['cf'] - r['pnl_net'] for r in los):+.2f}")

    ordered = sorted(rows, key=lambda r: r["cf"] - r["pnl_net"])
    print(f"  剔最有利 1 笔后 Δ={delta - (ordered[-1]['cf'] - ordered[-1]['pnl_net']):+.2f}"
          f"; 剔最不利 1 笔后 Δ={delta - (ordered[0]['cf'] - ordered[0]['pnl_net']):+.2f}")

    agg = defaultdict(list)
    for r in rows:
        agg[r["exit_reason"]].append(r)
    print(f"\n  {'exit_reason':26s}{'n':>4s}{'实际Σ':>10s}{'当日平Σ':>10s}{'Δ':>10s}")
    for k, g in sorted(agg.items(), key=lambda kv: sum(x["cf"] - x["pnl_net"] for x in kv[1])):
        a = sum(x["pnl_net"] for x in g)
        f = sum(x["cf"] for x in g)
        print(f"  {k:26s}{len(g):4d}{a:10.1f}{f:10.1f}{f - a:10.1f}")

    print("\n  按月 Δ (符号是否稳定):")
    for m in sorted({str(r["entry_time"])[:7] for r in rows}):
        g = [r for r in rows if str(r["entry_time"])[:7] == m]
        print(f"   {m} n={len(g):2d} 实际{sum(x['pnl_net'] for x in g):+9.1f}"
              f" 当日平{sum(x['cf'] for x in g):+9.1f}"
              f" Δ{sum(x['cf'] - x['pnl_net'] for x in g):+9.1f}")

    print("\n  最不该取消的 5 笔 (Δ 最正 = 持夜赚最多):")
    for r in ordered[-5:]:
        print(f"    {r['symbol']:9s} {str(r['entry_time'])[:10]} {r['exit_reason']:22s}"
              f" 实际{r['pnl_net']:+8.1f} 当日平{r['cf']:+8.1f} Δ{r['cf'] - r['pnl_net']:+8.1f}")

    sub = [r for r in rows if r["p52"]]
    if sub:
        a = sum(r["pnl_net"] for r in sub)
        f = sum(cf(r, r["p52"]) for r in sub)
        print(f"\n【稳健性臂 · 15:52 1m 价, n={len(sub)}】实际 {a:+.2f} / 当日 15:52 平 {f:+.2f}"
              f" / Δ={f - a:+.2f}  (与日K 收盘臂同号与否: "
              f"{'同号' if (f - a) * delta > 0 else '异号'})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())