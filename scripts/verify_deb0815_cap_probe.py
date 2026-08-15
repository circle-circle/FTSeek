"""§32.2 口径探针: 找 Hermes 的 342笔/-118 → cap5 +1442(183笔) 口径."""
import sqlite3
from collections import defaultdict

con = sqlite3.connect('file:data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
cols = ['trade_date', 'entry_time', 'exit_time', 'pnl_net', 'realized_pnl']
rows = con.execute("""
    SELECT trade_date, entry_time, exit_time, pnl_net, realized_pnl
    FROM trade_records
    WHERE strategy_id='playbook_sim' AND pnl_net IS NOT NULL
    ORDER BY entry_time
""").fetchall()
con.close()
print(f'全量 playbook_sim closed: n={len(rows)}')
print(f'entry月分布:')
mon = defaultdict(lambda: [0, 0.0])
for r in rows:
    mon[r['entry_time'][:7]][0] += 1
    mon[r['entry_time'][:7]][1] += r['pnl_net']
for m in sorted(mon):
    print(f'  {m}: n={mon[m][0]} pnl_net={mon[m][1]:+.0f}')

# 变体A: 窗口 5/1起
for lo in ('2026-05-01', '2026-05-15', '2026-06-01'):
    sub = [r for r in rows if lo <= r['entry_time'][:10] < '2026-09-01']
    print(f'entry>={lo}: n={len(sub)} pnl_net={sum(r["pnl_net"] for r in sub):+.0f} realized={sum(r["realized_pnl"] or 0 for r in sub):+.0f}')

# 变体B: 按 trade_date 6-8月
sub = [r for r in rows if '2026-06-01' <= (r['trade_date'] or '') < '2026-09-01']
print(f'trade_date 6-8月: n={len(sub)} pnl_net={sum(r["pnl_net"] for r in sub):+.0f}')
# 变体C: 按 exit_time 6-8月
sub = [r for r in rows if '2026-06-01' <= (r['exit_time'] or '') < '2026-09-01']
print(f'exit_time 6-8月: n={len(sub)} pnl_net={sum(r["pnl_net"] for r in sub):+.0f}')

# 变体D: entry 6-8月 + realized_pnl 字段求和
sub = [r for r in rows if '2026-06-01' <= r['entry_time'][:10] < '2026-09-01']
print(f'entry 6-8月 realized_pnl: n={len(sub)} sum={sum(r["realized_pnl"] or 0 for r in sub):+.0f}')

# 变体E: entry 6-8月, 但剔除 8/14 之后? (§32 写于8/15, 8/15当天可能有新交易)
sub = [r for r in rows if '2026-06-01' <= r['entry_time'][:10] <= '2026-08-15']
print(f'entry 6/1-8/15: n={len(sub)} pnl_net={sum(r["pnl_net"] for r in sub):+.0f}')

# 变体F: 不含6月? 7-8月
sub = [r for r in rows if '2026-07-01' <= r['entry_time'][:10] < '2026-09-01']
print(f'entry 7-8月: n={len(sub)} pnl_net={sum(r["pnl_net"] for r in sub):+.0f}')
