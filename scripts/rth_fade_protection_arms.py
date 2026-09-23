#!/usr/bin/env python3
"""RTH fade 保护臂复算 (只读, 零下单) —— 回答"加保护能不能救尾部" (用户令「打磨优化」).

## 读数 (2026-08-28..09-22 · **修正口径后**: 跳空 = RTH开盘 / RTH日前一交易日收盘 - 1; 26 笔/8 日)

| 臂 | n | 净/笔 | 胜率 | 合计 | 日胜 | 留一口 净/笔 | 单日≤−$400 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A 无保护 (基准) | 26 | **+$75.80** | 65% | +$1,971 | 4/8 | +$24.05 | 0 |
| B 止损 −1.5% | 26 | +$50.49 | 35% | +$1,313 | 5/8 | +$31.97 | 0 |
| B 止损 −2.5% | 26 | +$75.90 | 50% | +$1,973 | 5/8 | +$65.45 | 0 |
| B 止损 −3.5% | 26 | **+$101.65** | 58% | +$2,643 | 5/8 | +$50.62 | 0 |
| C 止盈 +3% | 26 | **−$40.05** | 77% | −$1,041 | 4/8 | −$61.39 | **2** |
| C 止盈 +5% | 26 | +$16.73 | 73% | +$435 | 3/8 | −$10.24 | 1 |
| D SL−2.5%+TP+3% [保守] | 26 | −$14.00 | 50% | −$364 | 3/8 | −$29.28 | 0 |
| E 剔极端票(跳空≥10%) | 10 | +$63.18 | 70% | +$632 | 3/6 | +$4.53 | 0 |
| F 只在 SPY ≤+0.5% 做空 | 10 | +$63.93 | 50% | +$639 | 3/6 | +$7.50 | 0 |
| G 日内熔断 (当日亏≥$150 停手) | 11 | **−$199.23** | 36% | −$2,192 | 2/8 | −$347.29 | 2 |
| H D+E+F 三条同时 | 4 | −$14.00 | 50% | −$56 | 2/4 | −$59.83 | 0 |

**结论 (可操作)**: ① **不要止盈** —— 提前锁盈把大赢家砍掉 (C/D 臂翻负, 坏日集中);
② 若要挂保险, 止损要**宽** (−3.5% 最佳; −1.5% 把胜率打到 35% = 被噪音打掉);
③ **不要日内熔断** —— 该规则的盈利日常常"先亏后赚", 熔断会把后面的赢家一起砍掉 (G 臂最差);
④ E/F 把样本砍到 10 笔, 留一口几乎归零 ⇒ 样本不足, 不做结论。
尾部 (修正口径): 最差日 −$279, **0 个**单日 ≤−$400 —— 早期"3 天 −$300~−420"是错口径读数。

## 预注册臂 (先写死再跑; 读数出来后不得改本段)

基准: 财报票 + RTH 开盘跳空 ≥ +3.0% ⇒ 开盘做空, 当日 RTH 收盘平 (单笔 $5,000, 费 0.53%)。
本脚本在**同一批票、同一天**上比较 (进场价都是当日开盘, 无未来信息):

  A 无保护 (基准):                    开→收
  B 止损 −1.5% / −2.5% / −3.5%:      盘中 high ≥ 开盘*(1+sl) ⇒ 按止损价平; 否则收盘平
  C 止盈 +3% / +5%:                   盘中 low ≤ 开盘*(1−tp) ⇒ 按止盈价平; 否则收盘平
  D 组合 SL−2.5% + TP+3%:             **保守假设: 同日同时触及 ⇒ 记止损** (最坏情况)
  E 极端票剔除:                       跳空 ≥ 10% 的票不做 (它们多是强化趋势票)
  F 市场过滤:                         SPY 当日涨跌 ≤ +0.50% 的日子才做 (其余不做空)
  G 日内熔断:                         当日该票首笔亏 ≥ $150 即当日停手 (只影响当日后续笔)

读数口径: 净/笔 = SIZE*(收益% − FEE)/100; 收益% 均按做空方向 (开盘卖 → 平仓买)。
  稳健性必看: 留一口 (剔除最强单日) 净/笔 / 最差单日 / 单日 ≤ −$400 的日数 / 日胜率。
  记号: `[保守]` = 同时触及止损止盈时按止损记; 未做分笔内路径还原 (日K high/low 足够粗, 更细需 1-5min 数据)。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env.us", override=True)

SCR = Path(os.environ.get("RTH_REPLAY_SCRATCH", "/root/.hermes/cache/scratch"))
SRC, SER, BARS = SCR / "replay_earnings_criterion_final.json", SCR / "series_cache.json", SCR / "rth_daily_hl.json"
SIZE, FEE, GAP_MIN = 5000.0, float(os.environ.get("FEE_PCT", "0.53")), 3.0


def fetch_bars(rows):
    from futu import OpenQuoteContext, KLType
    from scripts.replay_ext_entry_sources import _kline_cached
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    out = {}
    try:
        for i, r in enumerate(rows, 1):
            if r["symbol"] in out:
                continue
            bars = _kline_cached(ctx, r["symbol"], r["date"], "2026-09-30", KLType.K_DAY) or []
            out[r["symbol"]] = {str(b["t"])[:10]: [float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])]
                                for b in bars if b.get("o") and b.get("c")}
            if i % 25 == 0:
                print(f"   {i}/{len(rows)}", flush=True)
    finally:
        ctx.close()
    return out


def stat(xs):
    if not xs:
        return "n=0"
    p = sorted(xs)
    m = sum(p) / len(p)
    return (f"n={len(p):<3} 净/笔=${SIZE*(m-FEE)/100:+7.2f} 中位=${SIZE*(p[len(p)//2]-FEE)/100:+7.2f} "
            f"胜={100*sum(1 for v in p if v>0)/len(p):>3.0f}% 合计=${SIZE*sum(v-FEE for v in p)/100:+8.0f}")


def day_stats(recs, key="net"):
    """按 RTH 日聚合。注意: 日净用**美元**, 留一口用**收益率%** (早期版本把美元当% ⇒ 读数虚高, 已修)。"""
    by_net, by_ret = defaultdict(list), defaultdict(list)
    for r in recs:
        by_net[r["rth"]].append(r["net"])
        by_ret[r["rth"]].append(r["ret"])
    if not by_net:
        return ""
    tot = {d: sum(v) for d, v in by_net.items()}
    pos = sum(1 for v in tot.values() if v > 0)
    worst_d, worst = min(tot.items(), key=lambda kv: kv[1])
    best_d = max(tot, key=lambda d: tot[d])
    rest = [x for d, v in by_ret.items() if d != best_d for x in v]
    leave = (SIZE * (sum(rest) / len(rest) - FEE) / 100) if rest else 0.0
    bad = sum(1 for v in tot.values() if v <= -400)
    return (f"日胜 {pos}/{len(by_net)} | 日净 {min(tot.values()):+.0f}~{max(tot.values()):+.0f} | "
            f"最差日 {worst_d} ${worst:+.0f} | 留一口 净/笔=${leave:+.2f} (n={len(rest)}) | 单日≤−$400 的日数 {bad}")


def main():
    rows = [r for r in json.load(open(SRC))["rows"] if r.get("ref")]
    ser = json.load(open(SER))
    if BARS.exists():
        bars = json.load(open(BARS))
        print(f"[bars] 命中缓存 {len(bars)} 只")
    else:
        print("[bars] 抓取中 (含 high/low)…", flush=True)
        bars = fetch_bars(rows)
        BARS.write_text(json.dumps(bars))
        print(f"[bars] 已缓存 {len(bars)} 只")

    spy = bars.get("US.SPY") or fetch_spy()
    base = []
    for r in rows:
        bs, d0, hour = bars.get(r["symbol"]) or {}, r["date"], r["hour"]
        if not bs:
            continue
        days = sorted(bs)
        rth = d0 if (hour == "bmo" and d0 in bs) else next((d for d in days if d > d0), None)
        if not rth:
            continue
        o, h, l, c = bs[rth]
        pc = next((bs[d][3] for d in reversed(days) if d < rth), None)
        if not pc or o <= 0:
            continue
        gap = (o / pc - 1) * 100
        if gap < GAP_MIN:
            continue
        spy_chg = None
        if spy and rth in spy and (p := next((d for d in sorted(spy) if d < rth), None)):
            spy_chg = (spy[rth][3] / spy[p][3] - 1) * 100
        base.append({"sym": r["symbol"], "rth": rth, "o": o, "h": h, "l": l, "c": c,
                     "gap": gap, "spy": spy_chg})

    def settle(rec, sl=None, tp=None):
        """返回做空收益% (开盘卖 → 平仓买), 保守假设同日双触按止损。"""
        o, h, l, c = rec["o"], rec["h"], rec["l"], rec["c"]
        if sl is not None and h >= o * (1 + sl / 100):
            return -sl
        if tp is not None and l <= o * (1 - tp / 100):
            return tp
        return (o - c) / o * 100

    def run(name, sl=None, tp=None, filt=None, fuse=False):
        recs = []
        for r in base:
            if filt and not filt(r):
                continue
            ret = settle(r, sl, tp)
            rec = dict(r, ret=ret, net=round(SIZE * (ret - FEE) / 100, 2))
            if fuse:
                day_loss = sum(x["net"] for x in recs if x["rth"] == r["rth"])
                if day_loss <= -150:
                    continue
            recs.append(rec)
        print(f"\n{name}\n   {stat([r['ret'] for r in recs])}\n   {day_stats(recs)}")
        return recs

    print(f"\n样本: 跳空 ≥+{GAP_MIN}% 的财报票 {len(base)} 笔 / {len({r['rth'] for r in base})} 个 RTH 日 | 费 {FEE}%\n"
          f"  SPY 市场基准覆盖: {sum(1 for r in base if r['spy'] is not None)}/{len(base)} 笔")
    print("\n" + "=" * 100)
    run("A 无保护 (基准)")
    for sl in (1.5, 2.5, 3.5):
        run(f"B 止损 −{sl}%", sl=sl)
    for tp in (3.0, 5.0):
        run(f"C 止盈 +{tp}%", tp=tp)
    run("D 组合 SL−2.5% + TP+3% [保守]", sl=2.5, tp=3.0)
    run("E 剔除 跳空 ≥10% 的极端票", filt=lambda r: r["gap"] < 10)
    run("F 只在 SPY ≤+0.5% 的日子做空", filt=lambda r: (r["spy"] is None) or (r["spy"] <= 0.5))
    run("G 日内熔断 (当日已亏 ≥$150 则停手)", fuse=True)
    run("H D+E+F 三条同时", sl=2.5, tp=3.0, filt=lambda r: r["gap"] < 10 and ((r["spy"] is None) or r["spy"] <= 0.5))


def fetch_spy():
    from futu import OpenQuoteContext, KLType
    from scripts.replay_ext_entry_sources import _kline_cached
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    try:
        bars = _kline_cached(ctx, "US.SPY", "2026-08-20", "2026-09-30", KLType.K_DAY) or []
        return {str(b["t"])[:10]: [float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])]
                for b in bars if b.get("o") and b.get("c")}
    finally:
        ctx.close()


if __name__ == "__main__":
    main()