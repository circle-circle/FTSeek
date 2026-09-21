#!/usr/bin/env python3
"""汇总: EOD 豁免规则全样本 Δ (两臂: 标记日收盘 / 入场日收盘) + 归一化 t + 尾部."""
import json
import math
from collections import defaultdict

D = json.loads(open("/tmp/eod_exempt_prices.json").read())
S = [x for x in D["samples"] if x["closed"]]


def stats(rows, key):
    v = [(x, x[key]) for x in rows if x.get(key) is not None]
    if not v:
        return None
    d = [y for _, y in v]
    notional = [x["entry"] * x["qty"] for x, _ in v]
    bps = [y / n * 1e4 for y, n in zip(d, notional) if n > 0]
    n = len(d)
    mean = sum(d) / n
    sd = math.sqrt(sum((y - mean) ** 2 for y in d) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 and n > 1 else 0.0
    mb = sum(bps) / len(bps) if bps else 0.0
    sdb = math.sqrt(sum((y - mb) ** 2 for y in bps) / (len(bps) - 1)) if len(bps) > 1 else 0.0
    tb = mb / (sdb / math.sqrt(len(bps))) if sdb > 0 and len(bps) > 1 else 0.0
    act = sum(float(x["pnl_net"] or 0) for x, _ in v)
    help_ = [y for _, y in v if y < -5]
    hurt = [y for _, y in v if y > 5]
    return {"n": n, "delta": sum(d), "mean": mean, "t": t, "bps": mb, "t_bps": tb,
            "actual_net": act, "n_help": len(help_), "sum_help": sum(help_),
            "n_hurt": len(hurt), "sum_hurt": sum(hurt),
            "worst_hurt": max(d) if d else 0, "best_help": min(d) if d else 0}


def show(tag, r):
    if not r:
        print(f"  {tag:<24} n=0")
        return
    print(f"  {tag:<24} n={r['n']:>2} ΔΣ${r['delta']:>+9.1f} 均${r['mean']:>+8.1f} t={r['t']:>+5.2f} "
          f"| bps均值{r['bps']:>+7.1f} t={r['t_bps']:>+5.2f} | 实际净Σ${r['actual_net']:>+9.1f} "
          f"| 修复{r['n_help']}({r['sum_help']:>+8.1f}) 烧钱{r['n_hurt']}({r['sum_hurt']:>+7.1f})")


for key, lab in [("d_md", "A臂=标记日EOD平"), ("d_ed", "B臂=入场日EOD平 (规则关闭)")]:
    print(f"\n═══ {lab} (Δ>0 = 当时平掉更省 = 豁免烧钱) ═══")
    show("全部豁免仓", stats(S, key))
    print("  -- 按臂 --")
    for tr in sorted({x["track"] for x in S}):
        show(tr, stats([x for x in S if x["track"] == tr], key))
    print("  -- 按决策时刻净水深 --")
    for nm, lo, hi in [("浅水 ≤1%", 0, 1), ("中水 1–2.5%", 1, 2.5), ("深水 >2.5%", 2.5, 999)]:
        show(nm, stats([x for x in S if lo < abs(x["depth"]) <= hi], key))
    print("  -- 按夜数 --")
    show("1夜", stats([x for x in S if (x["night_n"] or 1) == 1], key))
    show("多夜", stats([x for x in S if (x["night_n"] or 1) > 1], key))
    print("  -- 按出场原因 --")
    for rs in sorted({x["exit_reason"] for x in S}):
        show(str(rs), stats([x for x in S if x["exit_reason"] == rs], key))

print("\n═══ 尾部形态 (Δ 排序: 烧钱最多 → 修复最多) ═══")
for x in sorted(S, key=lambda y: -(y.get("d_md") or 0)):
    n = x["entry"] * x["qty"]
    print(f"  {x['mark_date']} {x['symbol']:<9} 名义${n:>8.0f} depth{x['depth']:>+6.2f}% "
          f"Δ_收盘${x.get('d_md') or 0:>+8.1f} ({((x.get('d_md') or 0)/n*1e4):>+7.0f}bps) "
          f"Δ_入场日${x.get('d_ed') or 0:>+8.1f} 实际净${x['pnl_net'] or 0:>+8.1f} {x['exit_reason']}")

print("\n═══ 极端子集: |Δ|>费用地板($5) 的分布 ═══")
h = [x for x in S if (x.get("d_md") or 0) > 5]
g = [x for x in S if (x.get("d_md") or 0) < -5]
print(f"  烧钱笔 n={len(h)} Σ${sum(x['d_md'] for x in h):+.1f} 中位${sorted(x['d_md'] for x in h)[len(h)//2]:+.1f} "
      f"最大${max(x['d_md'] for x in h):+.1f}")
print(f"  修复笔 n={len(g)} Σ${sum(x['d_md'] for x in g):+.1f} 中位${sorted(x['d_md'] for x in g)[len(g)//2]:+.1f} "
      f"最小${min(x['d_md'] for x in g):+.1f}")

print("\n═══ 与影子/官方口径对撞 ═══")
off = json.loads(open("/opt/ftseek/FTSeek-US/data/shadow/overnight_arm_report_latest.json").read())
print(f"  官方 w_week 报表(9/21 07:34): closed={off['w_week']['closed']} "
      f"修复率(结局pnl>0)={off['w_week']['repair_rate_pct']}% 净PnL=${off['w_week']['net_pnl']} "
      f"verdict={off['verdict']}")
ww = [x for x in S if x["track"] == "w_week"]
print(f"  我的 w_week 已平 n={len(ww)} 实际净Σ=${sum(x['pnl_net'] or 0 for x in ww):+.1f} "
      f"修复率(实际净>0)={sum(1 for x in ww if (x['pnl_net'] or 0) > 0)/len(ww)*100:.0f}%")
print(f"  我的 Δ 口径: A臂 Σ${sum(x['d_md'] for x in S if x.get('d_md') is not None):+.1f} "
      f"B臂 Σ${sum(x['d_ed'] for x in S if x.get('d_ed') is not None):+.1f}")