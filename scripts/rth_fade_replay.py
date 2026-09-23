#!/usr/bin/env python3
"""RTH fade 回放 (D 项, 只读零下单): 「盘前/盘后异动的财报票 → 跳空 → 盘中方向」是否可用.

用户 2026-09-23 "按建议继续" 批的 D 项。回答两个问题:
  ① 延长时段读数能否给**盘中方向**试水 (延续 vs 回归)
  ② 若能, 是现役(纯多头)通道可做, 还是必须做空侧

**预注册口径 (读前写死, 不得事后改)**
  - 样本: 当日财报票 (bmo → 当日 RTH; amc → 次日 RTH), 前收 = 财报前最后收盘
  - **跳空% = RTH开盘 / RTH日的前一交易日收盘 - 1** (前收取**日K**, 不是 artifact 的 `ref`)。
    2026-09-23 口径更正: artifact 的 `ref` = **报告日的前一日**收盘 ⇒ 对 amc 票 (RTH 日 = 报告日+1)
    它比正确基准早一天, 会把报告日当天的 RTH 涨跌也算进"跳空" (实测 118 行里 51 行前收不一致,
    63/64 行 gap 差 >0.05%, 极端如 FEAM −6.31% vs +9.66% 符号相反) ⇒ 本脚本改用日K前收。
  - 日内% = RTH收盘/RTH开盘-1
  - 规则臂: 顺跳空(延续) / 逆跳空(fade), 均在 RTH 开盘进、RTH 收盘出 (基准臂, 无 SL/TP ⇒ 上限读数)
  - **做空收益口径 = (开盘-收盘)/开盘** (分母为卖出价, 不是买入价) —— 早期一版用 (开/收-1) 把读数
    虚增 2 倍 (50 笔 +$5,101 → +$2,400), 已校正。
  - 费用: 往返 FEE_PCT (默认 0.53 = 线上费闸实测), SIZE=5000 ⇒ 净/笔 = SIZE*(收益%-FEE)/100
  - 对照臂 (安慰剂): 同标的、同窗口**非财报日**、同 |跳空| 档 ⇒ 增量 = 财报日 − 安慰剂
  - 市场基准: 全部安慰剂日的日内均值 (若 ≈0 则 fade 收益不是买了 β)

**读数 (2026-08-28..09-22, **口径更正后**: 跳空 = RTH开盘/RTH日前一交易日收盘−1; 26 笔/8 RTH 日)**

  - **fade 做空侧 (跳空≥+3%): 净 +$75.80/笔, 胜 65%, 中位 +4.48% (n=26)** ‖ 安慰剂 −$15.66 ⇒ **增量 +$91.46**
  - fade 做多侧 (跳空跌→做多): **−$55.24/笔** (n=22) ⇒ 现役纯多头通道**不可做**
  - 顺跳空 (延续): −$68.74/笔 (n=48, 胜 40%) ⇒ 追方向是负期望
  - 市场基准 (全安慰剂日日内均值): +0.02% ⇒ 不是买了 β
  - 分日: 8 日 / 26 笔, 正净日 4/8, **留一口 (剔最强日) 净/笔 +$24.05**, 最差日 −$279, 0 个单日 ≤−$400
  - 机制: ETH 方向与跳空同号 92%、|跳空|/|ETH| 中位 1.10 ⇒ 开盘价≈把延长时段幅度全额定价;
    延长时段内 fade (+$47.96) 与 RTH 开盘 fade 同源 ⇒ **不需要延长时段开空成交能力**
    (只要 RTH 空头通道 — 已存在且在跑)
  - 局限: 26 笔/8 日 (样本小、单日敏感 — 留一口从 +$75.8 掉到 +$24.05); 未施加成交额/价差/深度闸;
    跨 09:30-16:00 持仓需接 RTH 侧 SL/TP 纪律 (保护臂见 scripts/rth_fade_protection_arms.py)
  - **口径更正 (2026-09-23)**: 本脚本早期用 artifact 的 `ref` 做前收, 而 `ref` = **报告日的前一日**收盘
    ⇒ 对 amc 票 (RTH 日 = 报告日+1) 比正确基准早一天, 会把报告日当天的 RTH 涨跌也算进"跳空"
    (实测 118 行中 51 行前收不一致, 63/64 行 gap 差 >0.05%; FEAM −6.31% vs +9.66% 符号相反)。
    改用**日K前收**后: 做空侧 n 50→26 (池更干净), 净/笔 +$48.01→**+$75.80**, 尾部反而更轻。输入 (回放产物, 不入库): 
  scratch/replay_earnings_criterion_final.json (财报票 + 前收), series_cache.json (ETH 5min 序列),
  rth_daily_bars.json (日K open/close, 28 标的×窗口; 缺失会自动抓取并缓存)
用法: PYTHONPATH=. venv/bin/python scripts/rth_fade_replay.py
"""
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
SRC, SER, BARS = SCR / "replay_earnings_criterion_final.json", SCR / "series_cache.json", SCR / "rth_daily_bars.json"
SIZE = 5000.0
FEE = float(os.environ.get("FEE_PCT", "0.53"))
GAP_MIN = 3.0


def net(xs):
    if not xs:
        return None
    p = sorted(xs)
    m = sum(p) / len(p)
    return {"n": len(p), "mean": m, "median": p[len(p) // 2],
            "win": sum(1 for v in p if v > 0) / len(p), "net": SIZE * (m - FEE) / 100}


def show(tag, a, b=None):
    if not a:
        print(f"  {tag:<32} n=0")
        return
    s = (f"  {tag:<32} n={a['n']:<3} 均值={a['mean']:+6.2f}% 中位={a['median']:+6.2f}% "
         f"胜={100*a['win']:>3.0f}% 净/笔=${a['net']:+7.2f} 合计${a['net']*a['n']:+8.0f}")
    if b:
        s += f"  ‖ 安慰剂 净/笔=${b['net']:+7.2f}(n={b['n']}) 增量 ${a['net']-b['net']:+7.2f}"
    print(s)


def fetch_bars(rows):
    from futu import OpenQuoteContext, KLType
    from scripts.replay_ext_entry_sources import _kline_cached
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    out = {}
    try:
        for r in rows:
            if r["symbol"] in out:
                continue
            bars = _kline_cached(ctx, r["symbol"], r["date"], "2026-09-30", KLType.K_DAY) or []
            out[r["symbol"]] = {str(b["t"])[:10]: [float(b["o"]), float(b["c"])]
                                for b in bars if b.get("o") and b.get("c")}
    finally:
        ctx.close()
    return out


def build(rows, bars):
    rep, pla = [], []
    for r in rows:
        bs, d0, hour = bars.get(r["symbol"]) or {}, r["date"], r["hour"]
        if not bs:
            continue
        days = sorted(bs)
        rth = d0 if (hour == "bmo" and d0 in bs) else next((d for d in days if d > d0), None)
        if not rth:
            continue
        o, c = bs[rth]
        prev = next((bs[d][1] for d in reversed(days) if d < rth), None)     # RTH 日前一交易日收盘
        if not prev:
            continue
        rep.append({"sym": r["symbol"], "rth": rth, "gap": (o / prev - 1) * 100,
                    "intra": (1 - c / o) * 100})          # 做空口径基准; 多侧取负
        for d in days:                                     # 安慰剂 = 同标非财报日
            i = days.index(d)
            if d <= d0 or d == rth or i == 0:
                continue
            o2, c2, prev = *bs[d], bs[days[i - 1]][1]
            pla.append({"sym": r["symbol"], "rth": d, "gap": (o2 / prev - 1) * 100,
                        "intra": (1 - c2 / o2) * 100})
    return rep, pla


def main():
    rows = [r for r in json.load(open(SRC))["rows"] if r.get("ref")]
    bars = json.load(open(BARS)) if BARS.exists() else fetch_bars(rows)
    if not BARS.exists():
        BARS.write_text(json.dumps(bars))
    rep, pla = build(rows, bars)
    print(f"财报日 {len(rep)} 笔 / 安慰剂 {len(pla)} 笔 | 费 {FEE}% | 单笔 ${SIZE:.0f} | "
          f"进出 = RTH 开/收 (基准臂)\n")

    for lo, hi in ((0, 1), (1, 2), (2, 3), (3, 99)):
        R = [s for s in rep if lo <= abs(s["gap"]) < hi]
        P = [s for s in pla if lo <= abs(s["gap"]) < hi]
        if not R:
            continue
        print(f"===== |跳空| ∈ [{lo},{hi}%) =====")
        show("fade 做多侧 (gap<0 ⇒ 做多)", net([-s["intra"] for s in R if s["gap"] < 0]),
             net([-s["intra"] for s in P if s["gap"] < 0]))
        show("fade 做空侧 (gap>0 ⇒ 做空)", net([s["intra"] for s in R if s["gap"] > 0]),
             net([s["intra"] for s in P if s["gap"] > 0]))
        show("顺跳空 (延续)", net([-s["intra"] if s["gap"] > 0 else s["intra"] for s in R]),
             net([-s["intra"] if s["gap"] > 0 else s["intra"] for s in P]))
        print()
    show("市场基准: 全安慰剂日日内均值(多侧)", net([-s["intra"] for s in pla]))

    byday = defaultdict(list)
    for s in rep:
        if s["gap"] >= GAP_MIN:
            byday[s["rth"]].append(s["intra"])
    print(f"\n做空侧 (跳空 ≥+{GAP_MIN}%) 分日 / 留一口:")
    tot = 0.0
    for d in sorted(byday):
        xs = byday[d]
        n = SIZE * (sum(xs) / len(xs) - FEE) / 100
        tot += n * len(xs)
        print(f"  {d}  n={len(xs):<3} 均值={sum(xs)/len(xs):+6.2f}% 净/笔=${n:+7.2f} 日合计=${n*len(xs):+8.0f}")
    n_all = sum(len(v) for v in byday.values())
    pos = sum(1 for v in byday.values() if SIZE * (sum(v) / len(v) - FEE) / 100 > 0)
    print(f"  合计 {len(byday)} 日 / {n_all} 笔 | 日净合计 ${tot:+.0f} | 正净日 {pos}/{len(byday)}")
    best = max(byday, key=lambda d: len(byday[d]) * (sum(byday[d]) / len(byday[d]) - FEE))
    rest = [x for d, v in byday.items() if d != best for x in v]
    print(f"  留一口 (剔除最强日 {best} {len(byday[best])} 笔) ⇒ 净/笔 "
          f"${SIZE*(sum(rest)/len(rest)-FEE)/100:+.2f} (n={len(rest)})")


if __name__ == "__main__":
    main()