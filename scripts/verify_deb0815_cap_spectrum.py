"""DEB §31.3/§32/§33 复核: 标的计数争议 + 单日开仓帽反事实谱系 + 8/14实锤.

口径 (按 §32.1 描述): playbook_sim 已平仓交易, 按 entry_time(ET) 逐日排序,
每日保留前 N 笔 (cap), 比较保留集 pnl_net 合计 vs 全量.
"""
import json
import sqlite3
from collections import defaultdict

con = sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT symbol, direction, entry_time, pnl_net
    FROM trade_records
    WHERE strategy_id='playbook_sim' AND pnl_net IS NOT NULL
      AND entry_time>='2026-06-01' AND entry_time<'2026-09-01'
    ORDER BY entry_time
""").fetchall()
con.close()
print(f'6-8月 playbook_sim closed: n={len(rows)} (expect 342)')

by_day = defaultdict(list)
for r in rows:
    by_day[r['entry_time'][:10]].append(r)
print(f'交易天数: {len(by_day)}')

for label, lo, hi in (('7月', '2026-07-01', '2026-08-01'), ('8月', '2026-08-01', '2026-09-01')):
    sub = [r for r in rows if lo <= r['entry_time'][:10] < hi]
    print(f'{label}: n={len(sub)} pnl={sum(r["pnl_net"] for r in sub):+.0f}')


def capped(cap):
    kept, cut = [], []
    for dt, rs in by_day.items():
        kept.extend(rs[:cap])
        cut.extend(rs[cap:])
    return kept, cut


full = sum(r['pnl_net'] for r in rows)
print(f'\nuncapped: n={len(rows)} pnl={full:+.0f} (expect -118)')
for cap, expect in ((5, '+1442 n=183'), (8, '+4177'), (10, '+4028')):
    kept, cut = capped(cap)
    kp = sum(r['pnl_net'] for r in kept)
    cp = sum(r['pnl_net'] for r in cut)
    print(f'cap={cap}: kept n={len(kept)} pnl={kp:+.0f} | cut n={len(cut)} avg={cp/len(cut) if cut else 0:+.1f} (expect {expect})')

# cap=5 分窗
for label, lo, hi, exp in (('7月', '2026-07-01', '2026-08-01', '-245'), ('8月', '2026-08-01', '2026-09-01', '-594')):
    kept, _ = capped(5)
    sub = [r for r in kept if lo <= r['entry_time'][:10] < hi]
    print(f'cap=5 {label}: n={len(sub)} pnl={sum(r["pnl_net"] for r in sub):+.0f} (expect {exp})')

# 保留率: §33.1 cap=5 54% / cap=8 77%
k5, _ = capped(5)
k8, _ = capped(8)
print(f'\n保留率: cap5={100*len(k5)/len(rows):.0f}% (expect 54%) cap8={100*len(k8)/len(rows):.0f}% (expect 77%)')

# 8/14 实锤: 单日14笔, 10:16-10:17 十笔LONG, 净-794
d0814 = by_day.get('2026-08-14', [])
print(f'\n8/14: n={len(d0814)} (expect 14) pnl={sum(r["pnl_net"] for r in d0814):+.0f} (expect -794)')
burst = [r for r in d0814 if '2026-08-14T10:16' <= r['entry_time'][:16] <= '2026-08-14T10:17']
print(f'8/14 10:16-10:17: n={len(burst)} (expect 10) LONG={sum(1 for r in burst if r["direction"]=="LONG")}')
if burst:
    ts = sorted(r['entry_time'] for r in burst)
    print(f'  span: {ts[0][11:19]} ~ {ts[-1][11:19]}')

# §31.3 计数争议: 核心盘 SHORT ≤48h 剔FIG+AMD 的 distinct 标的数 (我说10, Hermes说8, n=29 +2994)
d = json.load(open('data/manual_live_roundtrips_2026-08-15.json'))
S = [x for x in d['trips'] if x['direction'] == 'SHORT' and x['symbol'] not in ('US.MU', 'US.SNDK')]
sub = [r for r in S if r['hold_h'] <= 48 and r['symbol'] not in ('US.FIG', 'US.AMD')]
syms = sorted({r['symbol'] for r in sub})
print(f'\n§31.3: ≤48h ex-FIG+AMD n={len(sub)} pnl={sum(r["gross_pnl"] for r in sub):+.0f} symbols={len(syms)} {syms}')
