# -*- coding: utf-8 -*-
"""R9 收盘位置 + R10 NR7 窄幅日 — 用现有日K OHLC (无需分钟数据), 直接过四关门."""
import json, glob, os, math, random
from collections import defaultdict

ROOT = "/opt/ftseek/FTSeek-US"
bars = {}
for fp in glob.glob(f"{ROOT}/data/replay_bars_daily/US_*.json"):
    sym = "US." + os.path.basename(fp).replace(".json", "").replace("US_", "")
    try:
        rows = json.load(open(fp))
        bars[sym] = [(float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]  # o,h,l,c
    except Exception:
        pass
print(f"日K OHLC 载入: {len(bars)} 只")

def tstat(vals):
    n = len(vals)
    if n < 2: return 0.0, 0.0
    m = sum(vals)/n
    var = sum((v-m)**2 for v in vals)/(n-1)
    sd = math.sqrt(var) if var > 0 else 1e-9
    return m, m/(sd/math.sqrt(n))

def eval_rule(fwd):
    n = len(fwd)
    if n < 30: return None
    m, t = tstat(fwd)
    h = n//2
    im, it = tstat(fwd[:h]); om, ot = tstat(fwd[h:])
    same = (im < 0 and om < 0) or (im > 0 and om > 0)
    return {"n": n, "mean": round(m,3), "t": round(t,2),
            "is_mean": round(im,3), "oos_mean": round(om,3),
            "PASS": abs(t) >= 2.0 and same}

def run_symbol(sym, seq):
    n = len(seq)
    if n < 60: return None
    closes = [x[3] for x in seq]
    ranges = [x[1]-x[2] for x in seq]
    ret1 = [0.0]*n
    for i in range(n-1):
        ret1[i] = (closes[i+1]-closes[i])/closes[i]*100
    events = defaultdict(list)
    for i in range(7, n-1):
        rng = ranges[i]
        if rng <= 0 or closes[i] <= 0: continue
        pos = (closes[i]-seq[i][2])/rng  # 0=最低 1=最高
        if pos >= 0.8:
            events["R9_收盘贴日高(≥80%)"].append(ret1[i])
        elif pos <= 0.2:
            events["R9b_收盘贴日低(≤20%)"].append(ret1[i])
        if rng/closes[i]*100 == min(ranges[j]/closes[j]*100 for j in range(i-6, i+1) if closes[j] > 0):
            events["R10_NR7窄幅日"].append(ret1[i])
    return {r: eval_rule(f) for r, f in events.items() if eval_rule(f)}

results = {sym: run_symbol(sym, seq) for sym, seq in bars.items()}
results = {k: v for k, v in results.items() if v}

# 第四关安慰剂
_rnd = random.Random(20260819)
_sym_rets = {}
for sym, seq in bars.items():
    closes = [x[3] for x in seq]
    if len(closes) >= 60:
        _sym_rets[sym] = [(closes[i+1]-closes[i])/closes[i]*100 for i in range(len(closes)-1)]

def _gate_pass(vals):
    n = len(vals)
    if n < 30: return False
    m = sum(vals)/n
    var = sum((v-m)**2 for v in vals)/(n-1)
    sd = math.sqrt(var) if var > 0 else 1e-9
    t = m/(sd/math.sqrt(n))
    h = n//2
    def _tm(vv):
        nn = len(vv)
        if nn < 2: return 0.0
        mm = sum(vv)/nn
        vv2 = sum((x-mm)**2 for x in vv)/(nn-1)
        ssd = math.sqrt(vv2) if vv2 > 0 else 1e-9
        return mm/(ssd/math.sqrt(nn))
    im, om = _tm(vals[:h]), _tm(vals[h:])
    same = (im < 0 and om < 0) or (im > 0 and om > 0)
    return abs(t) >= 2.0 and same

def placebo(obs_n):
    counts = []
    for _ in range(100):
        passed = 0
        for sym, sz in obs_n.items():
            rets = _sym_rets.get(sym)
            if not rets or sz < 30: continue
            idx = _rnd.sample(range(len(rets)), min(sz, len(rets)))
            vals = [rets[i] for i in idx]
            if _gate_pass(vals): passed += 1
        counts.append(passed)
    m = sum(counts)/len(counts)
    sd = math.sqrt(sum((c-m)**2 for c in counts)/(len(counts)-1))
    return m, sd

pass_by_rule = defaultdict(int)
pass_n = defaultdict(dict)
for sym, rules in results.items():
    for rule, r in rules.items():
        if r["PASS"]:
            pass_by_rule[rule] += 1
            pass_n[rule][sym] = r["n"]

print(f"\n=== R9/R10 四关门 + 安慰剂 ===")
verdict = {}
for rule in sorted(pass_by_rule):
    m_exp, sd_exp = placebo(pass_n[rule])
    thresh = m_exp + 2*sd_exp
    ok = pass_by_rule[rule] > thresh
    verdict[rule] = ok
    print(f"{rule:<22} 观察PASS={pass_by_rule[rule]:<3} 期望={m_exp:.1f} 阈值={thresh:.1f} "
          f"{'✅真信号' if ok else '❌噪声内'}")

WATCH = ["US.TSLA","US.COIN","US.CAT","US.GS","US.CVX","US.NVDA","US.RIVN","US.UBER","US.SNDK",
         "US.ARM","US.MU","US.BWXT","US.LLY","US.AAPL","US.MSFT","US.GOOG","US.AMZN","US.META",
         "US.AVGO","US.TSM","US.BABA","US.PDD","US.NIO","US.NFLX","US.CRM","US.ORCL","US.JPM",
         "US.BA","US.LMT","US.UNH","US.JNJ","US.V","US.ABBV","US.MRK","US.MA","US.HON","US.KO",
         "US.PEP","US.DIS","US.VZ","US.COST","US.WMT","US.SPOT","US.XOM"]
print(f"\n=== Watchlist 逐股 PASS (仅过四关规则) ===")
for sym in WATCH:
    if sym in results:
        ps = [(r, d) for r, d in results[sym].items() if d["PASS"] and verdict.get(r)]
        for rule, d in ps:
            print(f"{sym:<10} {rule:<22} n={d['n']:<4} 次日均{d['mean']:+6.2f}% t={d['t']:+5.2f} "
                  f"IS{d['is_mean']:+6.2f}% OOS{d['oos_mean']:+6.2f}%")
json.dump({"results": {k: v for k, v in results.items()}, "verdict": verdict,
           "generated": __import__("datetime").date.today().isoformat()},
          open("/opt/ftseek/FTSeek-US/data/r9r10_replay_latest.json", "w"), ensure_ascii=False)
print("\n→ data/r9r10_replay_latest.json")
