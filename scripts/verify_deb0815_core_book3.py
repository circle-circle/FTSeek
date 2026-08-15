"""§29 复核 part3: LONG尾盘口径归因 + H1 open窗标的热度 + SIM侧自验(§29.2)核对."""
import json
import sqlite3
from collections import defaultdict

d = json.load(open('data/manual_live_roundtrips_2026-08-15.json'))
core = [x for x in d['trips'] if x['direction'] in ('LONG', 'SHORT') and x['symbol'] not in ('US.MU', 'US.SNDK')]
L = [x for x in core if x['direction'] == 'LONG']
S = [x for x in core if x['direction'] == 'SHORT']

# --- LONG 尾盘 n=4 -2595 口径归因: 试 exit hour / entry hour>=15 / 15:30+ 等变体
variants = {
    'entry hour==14': [r for r in L if int(r['entry_time'][11:13]) == 14],
    'exit 14-16': [r for r in L if 14 <= int(r['exit_time'][11:13]) < 16],
    'exit hour==15': [r for r in L if int(r['exit_time'][11:13]) == 15],
    'entry>=15:30': [r for r in L if int(r['entry_time'][11:13]) * 60 + int(r['entry_time'][14:16]) >= 930],
}
for k, rows in variants.items():
    if rows:
        print(f'{k}: n={len(rows)} pnl={sum(r["gross_pnl"] for r in rows):+.0f}')
# CVNA LONG 亏损笔定位
cvna_l = [r for r in L if r['symbol'] == 'US.CVNA']
print('\nCVNA LONG trips:')
for r in sorted(cvna_l, key=lambda x: x['gross_pnl']):
    print(f"  entry {r['entry_time'][:16]} exit {r['exit_time'][:16]} hold={r['hold_h']:.0f}h pnl={r['gross_pnl']:+.0f}")

# --- H1 open窗 SHORT 标的广度
s_open = [r for r in S if 570 <= int(r['entry_time'][11:13]) * 60 + int(r['entry_time'][14:16]) < 720]
sp = defaultdict(lambda: [0, 0.0])
for r in s_open:
    sp[r['symbol']][0] += 1
    sp[r['symbol']][1] += r['gross_pnl']
print('\nH1 open窗 SHORT symbols:')
for k, (n, p) in sorted(sp.items(), key=lambda kv: -kv[1][1]):
    print(f'  {k}: n={n} {p:+.0f}')

# --- SIM 侧 (§29.2): playbook_sim 7/1起 开盘窗 SHORT / open LONG
con = sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
cols = [c[1] for c in con.execute("PRAGMA table_info(trade_records)").fetchall()]
print('\ntrade_records cols:', [c for c in cols if 'time' in c or 'date' in c or 'dir' in c or 'pnl' in c or 'symbol' in c or 'strategy' in c])
rows = con.execute("""
    SELECT symbol, direction, entry_time, exit_time, pnl_net
    FROM trade_records
    WHERE strategy_id='playbook_sim' AND pnl_net IS NOT NULL AND entry_time>='2026-07-01'
""").fetchall()
con.close()
print(f'SIM 7/1起 closed: n={len(rows)}')


def sim_sess(ts):
    # entry_time 时区待确认 — 先看分布
    return int(ts[11:13])


hours = defaultdict(int)
for r in rows:
    hours[sim_sess(r['entry_time'])] += 1
print('SIM entry hour histogram:', dict(sorted(hours.items())))
