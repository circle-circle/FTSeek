"""DEB-20260815 §29 复核 part2: 口径差异归因 + 攻击点b/d/e/f 补充计算."""
import json
import statistics
from collections import defaultdict

d = json.load(open('data/manual_live_roundtrips_2026-08-15.json'))
trips = [x for x in d['trips'] if x['direction'] in ('LONG', 'SHORT')]
core = [x for x in trips if x['symbol'] not in ('US.MU', 'US.SNDK')]
S = [x for x in core if x['direction'] == 'SHORT']
L = [x for x in core if x['direction'] == 'LONG']

# 月度口径归因: exit month
mon_e = defaultdict(list)
for r in S:
    mon_e[r['exit_time'][:7]].append(r)
print('SHORT monthly by EXIT (找 §29.1 的 4月+225/n=3 口径):')
for m in sorted(mon_e):
    rows = mon_e[m]
    print(f'  {m}: n={len(rows)} {sum(r["gross_pnl"] for r in rows):+.0f}')

# LONG 尾盘口径归因: 仅 15:00-16:00
late15 = [r for r in L if int(r['entry_time'][11:13]) == 15]
print(f'\nLONG entry hour==15: n={len(late15)} pnl={sum(r["gross_pnl"] for r in late15):+.2f} (expect n=4 -2595)')
for r in sorted(late15, key=lambda x: x['gross_pnl'])[:3]:
    print(f'   {r["symbol"]} {r["entry_time"][:16]} {r["gross_pnl"]:+.0f}')

# LONG 持仓分桶: 日内 / 隔夜(<=24h?) / 2-5天 / >5天 — §29.1: LONG 1-5天桶 +8173 WR78-87%
def hold_bucket(r):
    h = r['hold_h']
    if h <= 7:
        return 'intraday(<=7h)'
    if h <= 24:
        return 'overnight(7-24h)'
    if h <= 120:
        return 'swing2-5d(24-120h)'
    return 'gt5d(>120h)'

b = defaultdict(list)
for r in L:
    b[hold_bucket(r)].append(r)
print('\nLONG by hold bucket:')
for k in ('intraday(<=7h)', 'overnight(7-24h)', 'swing2-5d(24-120h)', 'gt5d(>120h)'):
    rows = b.get(k, [])
    if rows:
        wins = sum(1 for r in rows if r['gross_pnl'] > 0)
        print(f'  {k}: n={len(rows)} WR={100*wins/len(rows):.0f}% pnl={sum(r["gross_pnl"] for r in rows):+.0f}')
ov = b.get('overnight(7-24h)', []) + b.get('swing2-5d(24-120h)', [])
print(f'  overnight+2-5d combined: n={len(ov)} pnl={sum(r["gross_pnl"] for r in ov):+.0f} (expect +8173)')

# 攻击点e/f 输入: 人工 SHORT open窗 逐月分布 (vs SIM 自验窗 7/1起)
def sess(r):
    t = int(r['entry_time'][11:13]) * 60 + int(r['entry_time'][14:16])
    return 570 <= t < 720

s_open = [r for r in S if sess(r)]
mo = defaultdict(list)
for r in s_open:
    mo[r['entry_time'][:7]].append(r)
print('\nATTACK-e/f: 人工SHORT open窗 by entry month (SIM自验窗=7月+):')
for m in sorted(mo):
    rows = mo[m]
    print(f'  {m}: n={len(rows)} pnl={sum(r["gross_pnl"] for r in rows):+.0f}')

# 攻击点f: 人工核心盘SHORT 7月是否有样本
jul = [r for r in S if r['entry_time'][:7] == '2026-07']
print(f'人工核心盘SHORT 7月: n={len(jul)} (机器DA-SHORT链=7/9-7/27)')

# 攻击点b 补充: >48h 的 SHORT 都是谁
slow = [r for r in S if r['hold_h'] > 48]
sp = defaultdict(float)
for r in slow:
    sp[r['symbol']] += r['gross_pnl']
print(f'\nATTACK-b: SHORT >48h n={len(slow)} pnl={sum(r["gross_pnl"] for r in slow):+.0f}, by symbol:')
for k, v in sorted(sp.items(), key=lambda kv: -kv[1]):
    print(f'  {k}: {v:+.0f}')

# FIG 盈利率 vs 持仓: FIG 若48h强平会怎样 (用盈亏方向观察: FIG winners hold 分布)
fig = [r for r in S if r['symbol'] == 'US.FIG']
fh = [r['hold_h'] for r in fig]
print(f'\nFIG hold: min={min(fh):.0f}h p25={sorted(fh)[len(fh)//4]:.0f}h median={statistics.median(fh):.0f}h max={max(fh):.0f}h')
fig48 = [r for r in fig if r['hold_h'] <= 48]
print(f'FIG <=48h: n={len(fig48)} pnl={sum(r["gross_pnl"] for r in fig48):+.0f}')
