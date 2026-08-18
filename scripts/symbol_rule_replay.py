# -*- coding: utf-8 -*-
"""Step 1: 逐股股性规则回放裁决器 (只读).

对每只票回放候选规则, 输出统计显著性表:
  R1 连涨N后续势   R2 连跌N后续跌   R3 站上MA20   R4 跌破MA20
  R5 跳空高开>2%   R6 跳空低开<-2%  R7 大涨>4%次日  R8 大跌<-4%次日
门槛: n>=30 + |t|>1.5 + IS/OOS同号. 全部过 → 该票"可信股性规则".
"""
import json, glob, os, math
from collections import defaultdict

ROOT = "/opt/ftseek/FTSeek-US"

# 加载日K
bars = {}
for fp in glob.glob(f"{ROOT}/data/replay_bars_daily/US_*.json"):
    sym = "US." + os.path.basename(fp).replace(".json", "").replace("US_", "")
    try:
        rows = json.load(open(fp))
        # [date, open, high, low, close]
        bars[sym] = [(r[0], float(r[1]), float(r[4])) for r in rows]  # date, open, close
    except Exception:
        pass
print(f"日K载入: {len(bars)} 只")

def tstat(vals):
    n = len(vals)
    if n < 2:
        return 0.0, 0.0
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 1e-9
    return m, m / (sd / math.sqrt(n))

def split_is_oos(vals):
    h = len(vals) // 2
    return vals[:h], vals[h:]

def eval_rule(fwd_list):
    """fwd_list: 按时间顺序的 1日前瞻收益%"""
    n = len(fwd_list)
    m, t = tstat(fwd_list)
    isv, oosv = split_is_oos(fwd_list)
    im, it = tstat(isv)
    om, ot = tstat(oosv)
    same_sign = (im < 0 and om < 0) or (im > 0 and om > 0)
    return {"n": n, "mean": round(m, 3), "t": round(t, 2),
            "is_mean": round(im, 3), "is_t": round(it, 2),
            "oos_mean": round(om, 3), "oos_t": round(ot, 2),
            "oos_same_sign": same_sign}

def run_symbol(sym, seq):
    """seq: [(date, open, close)]"""
    if len(seq) < 60:
        return None
    dates = [x[0] for x in seq]
    opens = [x[1] for x in seq]
    closes = [x[2] for x in seq]
    n = len(seq)
    ret1 = [0.0] * n
    for i in range(n - 1):
        ret1[i] = (closes[i + 1] - closes[i]) / closes[i] * 100
    up = [ret1[i] > 0 for i in range(n)]
    dn = [ret1[i] < 0 for i in range(n)]
    ma20 = [None] * n
    for i in range(19, n):
        ma20[i] = sum(closes[i - 19:i + 1]) / 20

    events = defaultdict(list)  # rule -> [fwd1%]
    for i in range(2, n - 1):
        # R1/R2 连涨连跌 (N=2 起算: i-1,i 两天同向 → 看 i+1)
        if up[i] and up[i - 1]:
            events["R1_连涨2+"].append(ret1[i])
        if dn[i] and dn[i - 1]:
            events["R2_连跌2+"].append(ret1[i])
        if i >= 3 and up[i] and up[i - 1] and up[i - 2]:
            events["R1b_连涨3+"].append(ret1[i])
        if i >= 3 and dn[i] and dn[i - 1] and dn[i - 2]:
            events["R2b_连跌3+"].append(ret1[i])
        # R3/R4 MA20 位置
        if ma20[i]:
            if closes[i] > ma20[i]:
                events["R3_站上MA20"].append(ret1[i])
            else:
                events["R4_跌破MA20"].append(ret1[i])
        # R5/R6 跳空
        gap = (opens[i] - closes[i - 1]) / closes[i - 1] * 100
        if gap > 2:
            events["R5_跳空高开>2%"].append(ret1[i])
        elif gap < -2:
            events["R6_跳空低开<-2%"].append(ret1[i])
        # R7/R8 大波动次日
        if ret1[i - 1] > 4:
            events["R7_大涨>4%次日"].append(ret1[i])
        elif ret1[i - 1] < -4:
            events["R8_大跌<-4%次日"].append(ret1[i])

    out = {}
    for rule, fwd in events.items():
        r = eval_rule(fwd)
        r["PASS"] = (r["n"] >= 30 and abs(r["t"]) > 1.5 and r["oos_same_sign"])
        out[rule] = r
    return out

results = {}
for sym, seq in bars.items():
    r = run_symbol(sym, seq)
    if r:
        results[sym] = r
print(f"回放完成: {len(results)} 只\n")

# ── 汇总 ──
WATCH = ["US.TSLA","US.COIN","US.CAT","US.GS","US.CVX","US.NVDA","US.RIVN","US.UBER","US.SNDK",
         "US.ARM","US.MU","US.BWXT","US.LLY","US.AAPL","US.MSFT","US.GOOG","US.AMZN","US.META",
         "US.AVGO","US.TSM","US.BABA","US.PDD","US.NIO","US.NFLX","US.CRM","US.ORCL","US.JPM",
         "US.BA","US.LMT","US.UNH","US.JNJ","US.V","US.ABBV","US.MRK","US.MA","US.HON","US.KO",
         "US.PEP","US.DIS","US.VZ","US.COST","US.WMT","US.SPOT","US.XOM"]

total_pass = 0
pass_by_rule = defaultdict(int)
for sym in results:
    for rule, r in results[sym].items():
        if r["PASS"]:
            total_pass += 1
            pass_by_rule[rule] += 1

print(f"=== 全池 {len(results)} 只: 通过三关(n>=30,|t|>1.5,IS/OOS同号)的规则 {total_pass} 条 ===")
for rule, cnt in sorted(pass_by_rule.items(), key=lambda x: -x[1]):
    print(f"  {rule:<16} {cnt} 只")

print(f"\n=== Watchlist 44 只逐股 PASS 规则 ===")
n_syms_with_pass = 0
for sym in WATCH:
    if sym not in results:
        continue
    passes = [(rule, r) for rule, r in results[sym].items() if r["PASS"]]
    if passes:
        n_syms_with_pass += 1
        print(f"\n{sym} (bars={len(bars[sym])}):")
        for rule, r in sorted(passes, key=lambda x: -abs(x[1]['t'])):
            sign = "续势↑" if r["mean"] > 0 else "回归↓"
            print(f"  {rule:<16} n={r['n']:<3} 次日均{r['mean']:+6.2f}% t={r['t']:+5.2f} | "
                  f"IS{r['is_mean']:+6.2f}% OOS{r['oos_mean']:+6.2f}% {sign}")
print(f"\n有可信规则的 watchlist 标的: {n_syms_with_pass}/44")

json.dump(results, open("/tmp/symbol_rule_replay.json", "w"), ensure_ascii=False)
print("→ /tmp/symbol_rule_replay.json")
