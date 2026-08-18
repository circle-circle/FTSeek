#!/usr/bin/env python3
"""DEB-20260816-no-trigger 辩论点2复核: 未触发BUY按前日个股涨跌分桶 (一次性分析脚本)."""
import json, sqlite3, statistics as st, glob

conn = sqlite3.connect('data/ftseek_us.db')
conn.row_factory = sqlite3.Row
rows = conn.execute("""
SELECT symbol, substr(created_at,1,10) d, executed, fwd_return_t1_open r
FROM playbook_instructions
WHERE direction='BUY' AND fwd_return_t1_open IS NOT NULL
  AND (executed=1 OR skip_reason LIKE '未触发%')
""").fetchall()
conn.close()

bars_cache = {}
def load_sym(sym):
    sym_f = sym.replace('.', '_')
    if sym_f in bars_cache:
        return bars_cache[sym_f]
    data = {}
    for f in glob.glob(f'data/replay_bars/daily_{sym_f}_*.json'):
        for b in json.load(open(f)):
            data[b[0]] = {'close': b[4]}
    bars_cache[sym_f] = data
    return data

def prev_day_ret(sym, d):
    data = load_sym(sym)
    days = sorted(data)
    if d not in days:
        return None
    i = days.index(d)
    if i < 2:
        return None
    c1, c0 = data[days[i-1]]['close'], data[days[i-2]]['close']
    return c1 / c0 - 1 if c0 > 0 else None

def tstat(x):
    n = len(x)
    if n < 2:
        return (0, 0, n)
    m = st.mean(x)
    s = st.stdev(x)
    return (m, m / (s / n ** 0.5), n)

for label, ex_flag in [('未触发BUY', 0), ('已执行BUY(对照)', 1)]:
    buckets = {'前日跌(<-0.5%)': [], '前日平': [], '前日涨(>+0.5%)': []}
    miss = 0
    for r in rows:
        if r['executed'] != ex_flag:
            continue
        pr = prev_day_ret(r['symbol'], r['d'])
        if pr is None:
            miss += 1
            continue
        k = '前日跌(<-0.5%)' if pr < -0.005 else ('前日涨(>+0.5%)' if pr > 0.005 else '前日平')
        buckets[k].append(r['r'])
    print(f'=== {label} 按前日个股涨跌分桶 (open基准) ===')
    for k, v in buckets.items():
        m, t, n = tstat(v)
        pos = sum(1 for x in v if x > 0) / n if n else 0
        print(f'  {k}: n={n} mean={m*100:+.2f}% t={t:.2f} 正向率={pos*100:.0f}%')
    print(f'  (缺数据: {miss})')
