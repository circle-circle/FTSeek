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
    for i in range(3, n - 1):
        # R1/R2 连涨/连跌 — 2026-08-19 循环bug修复 (DEB-20260819 §9.0, Qoder 发现):
        #   原条件 up[i] 把结果日 i 包进了事件条件 → R1 结果 100% 为正 (代数必然,
        #   非动量)。修正: 条件只用两根已完结K线 (i-1, i-2), 结果仍是 ret1[i]。
        if up[i - 1] and up[i - 2]:
            events["R1_连涨2+"].append(ret1[i])
        if dn[i - 1] and dn[i - 2]:
            events["R2_连跌2+"].append(ret1[i])
        if up[i - 1] and up[i - 2] and up[i - 3]:
            events["R1b_连涨3+"].append(ret1[i])
        if dn[i - 1] and dn[i - 2] and dn[i - 3]:
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
        r["PASS"] = (r["n"] >= 30 and abs(r["t"]) >= 2.0 and r["oos_same_sign"])
        out[rule] = r
    return out

results = {}
for sym, seq in bars.items():
    r = run_symbol(sym, seq)
    if r:
        results[sym] = r
print(f"回放完成: {len(results)} 只\n")

# ── 第四关: 规则级安慰剂校准 (DEB-20260819 §9.0 发现二, Qoder 提出) ──
#   三关门 (n>=30 / |t|>1.5 / IS-OOS同号) 在肥尾+牛市漂移下假阳率高达 14-24%,
#   观察 PASS 数与随机选日不可区分。第四关: 对每条规则, 按实际事件规模随机重抽样
#   过同样三门 100 次 → 得零假设期望假PASS 数 (mean_exp) 与标准差 (sd_exp);
#   观察 PASS 数须 > mean_exp + 2*sd_exp 才判"真信号"。
import random as _rnd
_rnd = _rnd.Random(20260819)

def _gate_pass(vals):
    n = len(vals)
    if n < 30:
        return False
    m = sum(vals) / n
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 1e-9
    t = m / (sd / math.sqrt(n))
    h = n // 2
    def _tm(vv):
        nn = len(vv)
        if nn < 2: return 0.0
        mm = sum(vv) / nn
        vv2 = sum((x - mm) ** 2 for x in vv) / (nn - 1)
        ssd = math.sqrt(vv2) if vv2 > 0 else 1e-9
        return mm / (ssd / math.sqrt(nn))
    im, om = _tm(vals[:h]), _tm(vals[h:])
    same = (im < 0 and om < 0) or (im > 0 and om > 0)
    return abs(t) >= 2.0 and same

# 收集每票的日收益序列 (供安慰剂抽样)
_sym_rets = {}
for sym, seq in bars.items():
    closes = [x[2] for x in seq]
    if len(closes) >= 60:
        _sym_rets[sym] = [(closes[i + 1] - closes[i]) / closes[i] * 100 for i in range(len(closes) - 1)]

def _placebo_rule(obs_n_per_sym, iters=100):
    """obs_n_per_sym: {sym: 该规则观察到的事件数}. 返回 (mean_exp, sd_exp) 假PASS计数."""
    counts = []
    for _ in range(iters):
        passed = 0
        for sym, sz in obs_n_per_sym.items():
            rets = _sym_rets.get(sym)
            if not rets or sz < 30:
                continue
            idx = _rnd.sample(range(len(rets)), min(sz, len(rets)))
            vals = [rets[i] for i in idx]
            if _gate_pass(vals):
                passed += 1
        counts.append(passed)
    m = sum(counts) / len(counts)
    sd = math.sqrt(sum((c - m) ** 2 for c in counts) / (len(counts) - 1))
    return m, sd

# 汇总每条规则的观察 PASS 数
pass_by_rule = defaultdict(int)
pass_syms_by_rule = defaultdict(list)
for sym in results:
    for rule, r in results[sym].items():
        if r["PASS"]:
            pass_by_rule[rule] += 1
            pass_syms_by_rule[rule].append(sym)

print(f"=== 全池 {len(results)} 只: 四关门(n>=30,|t|>=2.0,IS/OOS同号,安慰剂)通过 + 第四关安慰剂校准 ===")
print(f"{'规则':<16}{'观察PASS':>8}{'安慰剂期望':>10}{'阈值(+2σ)':>10}{'第四关':>8}")
rule_verdict = {}
for rule in sorted(pass_by_rule):
    obs_n = {sym: results[sym][rule]["n"] for sym in pass_syms_by_rule[rule]}
    # 安慰剂也要在未PASS的票上抽样以得全池期望——这里简化为对全池按规则事件数抽样
    all_obs_n = {}
    for sym in results:
        if rule in results[sym]:
            all_obs_n[sym] = results[sym][rule]["n"]
    mean_exp, sd_exp = _placebo_rule(all_obs_n)
    thresh = mean_exp + 2 * sd_exp
    ok = pass_by_rule[rule] > thresh
    rule_verdict[rule] = ok
    flag = "✅真信号" if ok else "❌噪声内"
    print(f"{rule:<16}{pass_by_rule[rule]:>8}{mean_exp:>10.1f}{thresh:>10.1f}{flag:>8}")

# 2026-08-19 (DEB §14.2-1): verdict+明细落盘 data/ — build_symbol_rule_cards.py 的
#   R5 名单从本文件派生, 废除 /tmp 依赖与硬编码名单 (cron 化地基).
import pathlib as _pb
_pb.Path("/opt/ftseek/FTSeek-US/data").mkdir(exist_ok=True)
json.dump({"results": results, "rule_verdict": rule_verdict,
           "generated": __import__("datetime").date.today().isoformat()},
          open("/opt/ftseek/FTSeek-US/data/r18_replay_latest.json", "w"), ensure_ascii=False)

print("\n注: 第四关衡量'该规则的PASS票数是否显著超出随机选日期望';")
print("    未过第四关的规则其个股PASS可能是多重检验假阳, 不入股性卡。")

print(f"\n=== Watchlist 44 只逐股 PASS 规则 (仅展示过第四关的规则) ===")
WATCH = ["US.TSLA","US.COIN","US.CAT","US.GS","US.CVX","US.NVDA","US.RIVN","US.UBER","US.SNDK",
         "US.ARM","US.MU","US.BWXT","US.LLY","US.AAPL","US.MSFT","US.GOOG","US.AMZN","US.META",
         "US.AVGO","US.TSM","US.BABA","US.PDD","US.NIO","US.NFLX","US.CRM","US.ORCL","US.JPM",
         "US.BA","US.LMT","US.UNH","US.JNJ","US.V","US.ABBV","US.MRK","US.MA","US.HON","US.KO",
         "US.PEP","US.DIS","US.VZ","US.COST","US.WMT","US.SPOT","US.XOM"]
n_syms_with_pass = 0
for sym in WATCH:
    if sym not in results:
        continue
    passes = [(rule, r) for rule, r in results[sym].items()
              if r["PASS"] and rule_verdict.get(rule)]
    if passes:
        n_syms_with_pass += 1
        print(f"\n{sym} (bars={len(bars[sym])}):")
        for rule, r in sorted(passes, key=lambda x: -abs(x[1]['t'])):
            sign = "续势↑" if r["mean"] > 0 else "回归↓"
            print(f"  {rule:<16} n={r['n']:<3} 次日均{r['mean']:+6.2f}% t={r['t']:+5.2f} | "
                  f"IS{r['is_mean']:+6.2f}% OOS{r['oos_mean']:+6.2f}% {sign}")
print(f"\n有可信规则(过四关)的 watchlist 标的: {n_syms_with_pass}/44")

json.dump(results, open("/tmp/symbol_rule_replay.json", "w"), ensure_ascii=False)
print("→ /tmp/symbol_rule_replay.json")
