"""§29.2 SIM侧自验核对 + 攻击点e 标的池重叠检验."""
import sqlite3
from collections import defaultdict

con = sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT symbol, direction, entry_time, exit_time, pnl_net
    FROM trade_records
    WHERE strategy_id='playbook_sim' AND pnl_net IS NOT NULL AND entry_time>='2026-07-01'
""").fetchall()
con.close()


def mins(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16])


def agg(rs, label):
    if not rs:
        print(f'{label}: EMPTY')
        return
    n = len(rs)
    pnl = sum(r['pnl_net'] for r in rs)
    wr = 100 * sum(1 for r in rs if r['pnl_net'] > 0) / n
    print(f'{label}: n={n} WR={wr:.0f}% pnl={pnl:+.0f}')


short_open = [r for r in rows if r['direction'] == 'SHORT' and 570 <= mins(r['entry_time']) < 720]
agg(short_open, 'SIM SHORT open9:30-12 (§29.2 expect n=41 -104 WR39%)')
long_open = [r for r in rows if r['direction'] == 'LONG' and 570 <= mins(r['entry_time']) < 720]
agg(long_open, 'SIM LONG open9:30-12 (§29.2 expect n=202 -1837 WR39%)')
long_open30 = [r for r in rows if r['direction'] == 'LONG' and 570 <= mins(r['entry_time']) < 600]
agg(long_open30, 'SIM LONG open30 (§28.2 expect -910)')

# 攻击点e: SIM open SHORT 标的池 vs 人工 H1 池 {AMD,FIG,BABA,AVGO,MRVL,XOM,TCOM,NVDA}
manual_h1 = {'US.AMD', 'US.FIG', 'US.BABA', 'US.AVGO', 'US.MRVL', 'US.XOM', 'US.TCOM', 'US.NVDA'}
sim_syms = defaultdict(lambda: [0, 0.0])
for r in short_open:
    sim_syms[r['symbol']][0] += 1
    sim_syms[r['symbol']][1] += r['pnl_net']
print('\nSIM open-window SHORT symbols:')
overlap_n = 0
for k, (n, p) in sorted(sim_syms.items(), key=lambda kv: -kv[1][0]):
    mark = ' <-- 人工H1池' if k in manual_h1 else ''
    print(f'  {k}: n={n} {p:+.0f}{mark}')
    if k in manual_h1:
        overlap_n += n
print(f'重叠笔数 {overlap_n}/{len(short_open)}')

# SIM SHORT 全时段 (7/1起) 总账 — 机器臂 regime 背景
all_short = [r for r in rows if r['direction'] == 'SHORT']
agg(all_short, '\nSIM SHORT all-session 7/1起')
# 7/9-7/27 窗口 (机器DA-SHORT链窗口) SIM SHORT
win = [r for r in all_short if '2026-07-09' <= r['entry_time'][:10] <= '2026-07-27']
agg(win, 'SIM SHORT 7/9-7/27')
