#!/usr/bin/env python3
"""财报票方向判据实证 (只读, 零下单): 「财报好坏」能不能决定做多/做空?

2026-09-23 用户提问「对于财报日 如何选择做多还是做空?」⇒ 用**已验证的实盘形态数据**回答。

读数 (118 条超预期财报事件, 2026-08-28..09-22):
  - 超预期幅度 vs 报告日涨跌 **Pearson r = -0.006** (零相关)
  - 超预期票当日上涨占比 **47%** (低于抛硬币 50%), 中位 -0.33%
  - 分档上涨占比 0-5%:50% / 5-20%:**38%** / >=20%:52% (不单调)
  - >=20% 档次日跳空 中位 +1.14% 但 P10 -3.67%/P90 +23.35% ⇒ 离散大, 不可作方向判据
  - **数据集无 miss(不及预期) 票的路径** (通道只喂 beat) ⇒ 空侧财报规则缺的正是这批数据
⇒ 结论: 财报决定"有没有事件", **幅度**决定"做哪一边" (小涨 0.5~2% ⇒ 多 / 大跳空 >=+3% ⇒ 空)。

依赖: scratch/replay_earnings_criterion_final.json (回放产物, 不入库; 见 scripts/rth_fade_replay.py 同款说明)
"""
import json
from pathlib import Path
from statistics import median

SCR = Path("/root/.hermes/cache/scratch")
rows = [r for r in json.load(open(SCR / "replay_earnings_criterion_final.json"))["rows"]
        if r.get("surp_pct") is not None and r.get("d_close") and r.get("ref")]
print(f"样本: {len(rows)} 条财报事件 (有超预期幅度 + 报告日收盘)")

def pct(a, b):
    return (a / b - 1) * 100 if (a and b) else None

for r in rows:
    r["day_react"] = pct(r["d_close"], r["ref"])
    r["next_gap"] = pct(r["d1_open"], r["d_close"]) if r.get("d1_open") else None

buckets = [("miss (<0%)", lambda s: s < 0), ("beat 0-5%", lambda s: 0 <= s < 5),
           ("beat 5-20%", lambda s: 5 <= s < 20), ("beat >=20%", lambda s: s >= 20)]

print("\n① 报告日当日 RTH 反应 (收盘/前收-1) 按超预期分档")
print(f"  {'档':<12} {'n':>4} {'中位':>8} {'均值':>8} {'上涨占比':>8}")
for name, f in buckets:
    g = [r["day_react"] for r in rows if f(r["surp_pct"]) and r["day_react"] is not None]
    if not g:
        continue
    up = sum(1 for x in g if x > 0)
    print(f"  {name:<12} {len(g):>4} {median(g):>+8.2f} {sum(g)/len(g):>+8.2f} {100*up/len(g):>7.0f}%")

print("\n② 次日跳空 (次日开盘/报告日收盘-1) 按超预期分档 — 只含已过次日的行")
sub = [r for r in rows if r["next_gap"] is not None]
for name, f in buckets:
    g = [r["next_gap"] for r in sub if f(r["surp_pct"])]
    if not g:
        continue
    up = sum(1 for x in g if x > 0)
    print(f"  {name:<12} {len(g):>4} {median(g):>+8.2f} {sum(g)/len(g):>+8.2f} {100*up/len(g):>7.0f}%")

print("\n③ 方向命中率 (朴素想法: 好财报做多 / 差财报做空)")
beat = [r for r in rows if r["surp_pct"] >= 0 and r["day_react"] is not None]
miss = [r for r in rows if r["surp_pct"] < 0 and r["day_react"] is not None]
for label, g, want in (("beat(超预期)", beat, "涨"), ("miss(不及预期)", miss, "跌")):
    if not g:
        print(f"  {label}: **样本为空** —— 本数据集只含通过'超预期'判据的票, 没有 miss 的价格路径 "
              f"(若要空侧财报规则, 缺的正是这批数据)")
        continue
    hit = sum(1 for r in g if (r["day_react"] > 0) == (want == "涨"))
    print(f"  {label}: n={len(g)} ⇒ 当日{want}的比例 = {100*hit/len(g):.0f}% "
          f"(中位反应 {median([r['day_react'] for r in g]):+.2f}%)")
_all = [r["day_react"] for r in rows]
print(f"  全体基线: n={len(_all)} ⇒ 当日上涨占比 {100*sum(1 for x in _all if x>0)/len(_all):.0f}% "
      f"(中位 {median(_all):+.2f}%)")

print("\n④ 极端值检查 (beat >=20% 档: 中位 vs 均值 差距大 ⇒ 少数暴涨主导)")
g20 = [r["next_gap"] for r in rows if r["surp_pct"] >= 20 and r["next_gap"] is not None]
if g20:
    g20s = sorted(g20)
    print(f"  n={len(g20)} 中位 {median(g20):+.2f}% 均值 {sum(g20)/len(g20):+.2f}% "
          f"P10 {g20s[len(g20s)//10]:+.2f}% P90 {g20s[9*len(g20s)//10]:+.2f}% "
          f"剔最强一笔后均值 {sum(g20s[:-1])/max(1,len(g20s)-1):+.2f}%")

print("\n⑤ 与按幅度定方向的对比")
print("  幅度: RTH 开盘跳空 >=+3% => 做空  净 +$75.80/笔 胜 65% (n=26, scripts/rth_fade_replay.py)")
print("  幅度: 温和涨 0.5~2%   => 做多  净 +$23.09/笔 (n=19, 延长时段回放)")
print("  财报好坏: 见 ③")

xs = [r["surp_pct"] for r in rows]; ys = [r["day_react"] for r in rows]; n = len(xs)
mx, my = sum(xs)/n, sum(ys)/n
cov = sum((a-mx)*(b-my) for a, b in zip(xs, ys)) / n
sx = (sum((a-mx)**2 for a in xs)/n) ** 0.5
sy = (sum((b-my)**2 for b in ys)/n) ** 0.5
print(f"\n⑥ 超预期幅度 vs 报告日反应的 Pearson r = {cov/(sx*sy):+.3f} (n={n})")
