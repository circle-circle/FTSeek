import sqlite3, statistics
from collections import defaultdict

con = sqlite3.connect('file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row

rows = con.execute("""
SELECT symbol, direction, COALESCE(pnl_net,realized_pnl) pnl, realized_pnl gross, exit_reason,
       mfe_pct, entry_time, exit_time, decision_id, market_regime, fees_est
FROM trade_records
WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL AND entry_time>='2026-08-25' AND entry_price>=5
""").fetchall()

def src(r):
    d=str(r['decision_id'] or '')
    return 'DA' if d.startswith('DA-') else ('UE' if d.startswith('universe') else ('reentry' if d.startswith('intraday') else 'other'))

print('── other 源明细 ──')
for r in rows:
    if src(r)=='other':
        p = r['pnl']
        print(f"  {r['symbol']:9s} {str(r['direction']):5s} {str(r['entry_time'])[:10]} pnl={p} did={r['decision_id']} er={r['exit_reason']}")

print('\n── 减法后的账 (仅 DA + UE_SHORT, pnl NULL 计0) ──')
keep=[r for r in rows if src(r)=='DA' or (src(r)=='UE' and r['direction']=='SHORT')]
pnls=[(r['pnl'] or 0) for r in keep]
gross=[(r['gross'] or 0) for r in keep]
fees=[(r['fees_est'] or 0) for r in keep]
days=sorted(set(str(r['entry_time'])[:10] for r in keep))
print(f"  n={len(keep)} 毛=${sum(gross):.2f} 费=${sum(fees):.2f} 净=${sum(pnls):.2f}")
print(f"  单笔净=${statistics.mean(pnls):.2f} 交易日={len(days)} 笔/日={len(keep)/len(days):.1f} 日均净=${sum(pnls)/len(days):.2f}")

wk=defaultdict(list)
for r in keep:
    d=str(r['entry_time'])[:10]
    key=d[:8]+('上旬' if int(d[8:])<=10 else ('中旬' if int(d[8:])<=20 else '下旬'))
    wk[key].append(r['pnl'] or 0)
for w in sorted(wk):
    print(f"    {w}: n={len(wk[w])} 净=${sum(wk[w]):.2f}")

print('\n── 被砍源 (UE_LONG + reentry + other) ──')
cut=[r for r in rows if src(r) in ('reentry','other') or (src(r)=='UE' and r['direction']=='LONG')]
cp=[(r['pnl'] or 0) for r in cut]
print(f"  n={len(cut)} 净=${sum(cp):.2f} 费=${sum((r['fees_est'] or 0) for r in cut):.2f}")

# 若 UE_SHORT 也砍
keep2=[r for r in rows if src(r)=='DA']
p2=[(r['pnl'] or 0) for r in keep2]
print(f"  仅DA: n={len(keep2)} 净=${sum(p2):.2f} 单笔=${statistics.mean(p2):.2f}")
