# -*- coding: utf-8 -*-
"""分钟级规则族回放 (M1-M5) — 用户"价格范围盘感"核心层.

事件口径: 日级事件(分钟数据构造), 结果=次日收益% (与日K规则族对齐, 可直接入股性卡).
  M1  开盘30min方向 (涨>0.3% / 跌<-0.3%) → 次日
  M2  开盘30min区间突破 (当日高点>30min高 / 低点<30min低) → 次日
  M3  开盘5min 惯性 (±0.3%) → 次日
  M4  收盘在日内区间位置 (≥85% / ≤15%, 分钟级精确版) → 次日
  M5  开盘30min 振幅相对近20日 (放大≥1.5x) → 次日绝对波动 (方向无关的波动预告)
门槛: 四关门 (n>=30, |t|>=2.0, IS/OOS同号, 规则级安慰剂>期望+2σ).
"""
import json, glob, os, math, random
from collections import defaultdict
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
DIR = ROOT / "data" / "replay_bars_1m"

def tstat(vals):
    n = len(vals)
    if n < 2: return 0.0, 0.0
    m = sum(vals)/n
    var = sum((v-m)**2 for v in vals)/(n-1)
    sd = math.sqrt(var) if var > 0 else 1e-9
    return m, m/(sd/math.sqrt(n))

def gate(vals, tmin=2.0):
    n = len(vals)
    if n < 30: return None
    m, t = tstat(vals)
    h = n//2
    im, it = tstat(vals[:h]); om, ot = tstat(vals[h:])
    same = (im < 0 and om < 0) or (im > 0 and om > 0)
    return {"n": n, "mean": round(m,3), "t": round(t,2),
            "is_mean": round(im,3), "oos_mean": round(om,3),
            "PASS": abs(t) >= tmin and same}

# ── 每票: 分钟 bars → 日摘要序列 ──
daily = {}  # sym -> [{date, o, h, l, c, o30h, o30l, o30r, o5r, rng}]
for fp in sorted(glob.glob(str(DIR / "US_*.json"))):
    sym = "US." + os.path.basename(fp).replace(".json", "").replace("US_", "")
    bars = json.load(open(fp))
    days = defaultdict(list)
    for b in bars:
        days[b[0][:10]].append(b)
    seq = []
    for d in sorted(days):
        db = days[d]
        if len(db) < 100:  # 半天剔除
            continue
        o = db[0][1]; c = db[-1][4]
        h = max(x[2] for x in db); l = min(x[3] for x in db)
        o30 = db[:30]   # 09:31-10:00
        o5 = db[:5]
        seq.append({
            "date": d, "o": o, "h": h, "l": l, "c": c,
            "o30h": max(x[2] for x in o30), "o30l": min(x[3] for x in o30),
            "o30r": (o30[-1][4] - o) / o * 100,
            "o5r": (o5[-1][4] - o) / o * 100,
            "rng": (h - l) / o * 100,
        })
    if len(seq) >= 60:
        daily[sym] = seq
print(f"日摘要构建: {len(daily)} 只")

# ── 事件收集 ──
events = defaultdict(lambda: defaultdict(list))  # rule -> sym -> [next_ret%]
for sym, seq in daily.items():
    n = len(seq)
    for i in range(20, n - 1):
        d, nxt = seq[i], seq[i+1]
        prev_c = seq[i-1]["c"]
        nxt_ret = (nxt["c"] - d["c"]) / d["c"] * 100
        nxt_abs = abs(nxt_ret)
        # M1 开盘30min方向
        if d["o30r"] > 0.3:
            events["M1_开盘30min涨>0.3%"][sym].append(nxt_ret)
        elif d["o30r"] < -0.3:
            events["M1b_开盘30min跌<-0.3%"][sym].append(nxt_ret)
        # M2 开盘区间突破
        if d["h"] > d["o30h"] * 1.001:
            events["M2_上破开盘30min区间"][sym].append(nxt_ret)
        if d["l"] < d["o30l"] * 0.999:
            events["M2b_下破开盘30min区间"][sym].append(nxt_ret)
        # M3 开盘5min惯性
        if d["o5r"] > 0.3:
            events["M3_开盘5min涨>0.3%"][sym].append(nxt_ret)
        elif d["o5r"] < -0.3:
            events["M3b_开盘5min跌<-0.3%"][sym].append(nxt_ret)
        # M4 收盘位置 (分钟精确版)
        if d["rng"] > 0.05:
            pos = (d["c"] - d["l"]) / (d["h"] - d["l"])
            if pos >= 0.85:
                events["M4_收盘贴日高≥85%"][sym].append(nxt_ret)
            elif pos <= 0.15:
                events["M4b_收盘贴日低≤15%"][sym].append(nxt_ret)
        # M5 开盘30min放大波动 → 次日绝对波动
        hist_rng = [seq[j]["rng"] for j in range(i-20, i)]
        avg_rng = sum(hist_rng)/len(hist_rng)
        if avg_rng > 0 and d["rng"] >= 1.5 * avg_rng:
            events["M5_当日振幅≥1.5x均幅"][sym].append(nxt_abs)

# ── 四关门 ──
def eval_rule(sym_map):
    out = {}
    for sym, vals in sym_map.items():
        r = gate(vals)
        if r: out[sym] = r
    return out

results = {rule: eval_rule(sm) for rule, sm in events.items()}

pass_by_rule = defaultdict(int)
pass_n = defaultdict(dict)
for rule, syms in results.items():
    for sym, r in syms.items():
        if r["PASS"]:
            pass_by_rule[rule] += 1
            pass_n[rule][sym] = r["n"]

# ── 第四关安慰剂 ──
_rnd = random.Random(20260819)
_sym_rets = {}
for sym, seq in daily.items():
    _sym_rets[sym] = [(seq[i+1]["c"] - seq[i]["c"])/seq[i]["c"]*100 for i in range(len(seq)-1)]

def placebo(obs_n):
    counts = []
    for _ in range(100):
        passed = 0
        for sym, sz in obs_n.items():
            rets = _sym_rets.get(sym)
            if not rets or sz < 30: continue
            idx = _rnd.sample(range(len(rets)), min(sz, len(rets)))
            vals = [rets[i] for i in idx]
            g = gate(vals)
            if g and g["PASS"]: passed += 1
        counts.append(passed)
    m = sum(counts)/len(counts)
    sd = math.sqrt(sum((c-m)**2 for c in counts)/(len(counts)-1)) if len(counts) > 1 else 0
    return m, sd

print(f"\n=== 分钟级规则族 四关门 ===")
print(f"{'规则':<24}{'观察PASS':>8}{'安慰剂期望':>10}{'阈值+2σ':>9}  第四关")
verdict = {}
for rule in sorted(pass_by_rule):
    m_exp, sd_exp = placebo(pass_n[rule])
    thresh = m_exp + 2*sd_exp
    ok = pass_by_rule[rule] > thresh
    verdict[rule] = ok
    print(f"{rule:<24}{pass_by_rule[rule]:>8}{m_exp:>10.1f}{thresh:>9.1f}  {'✅真信号' if ok else '❌噪声内'}")

print(f"\n=== 个股 PASS 明细 (仅过四关规则) ===")
for rule in sorted(results):
    if not verdict.get(rule): continue
    hits = [(sym, r) for sym, r in results[rule].items() if r["PASS"]]
    for sym, r in sorted(hits, key=lambda x: -abs(x[1]["t"])):
        print(f"{sym:<10} {rule:<24} n={r['n']:<4} 次日均{r['mean']:+6.2f}% t={r['t']:+5.2f} "
              f"IS{r['is_mean']:+6.2f}% OOS{r['oos_mean']:+6.2f}%")

json.dump({"results": results, "verdict": verdict,
           "generated": __import__("datetime").date.today().isoformat()},
          open("/opt/ftseek/FTSeek-US/data/minute_rules_replay_latest.json", "w"), ensure_ascii=False)
print("\n→ data/minute_rules_replay_latest.json")
