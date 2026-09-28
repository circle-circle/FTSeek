import sqlite3, statistics
from collections import defaultdict

con = sqlite3.connect('file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row

# UE LONG 分窗复算 — 对齐 DEB-20260923 的 IS/OOS 切点(9/5)
rows = con.execute("""
SELECT symbol, direction, COALESCE(pnl_net,realized_pnl) pnl, realized_pnl gross, fees_est,
       entry_time, exit_reason, mfe_pct, decision_id
FROM trade_records
WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL AND entry_price>=5
  AND decision_id LIKE 'universe%'
ORDER BY entry_time
""").fetchall()

def stat(grp, label):
    if not grp:
        print(f'  {label}: n=0'); return
    p=[r['pnl'] or 0 for r in grp]
    w=sum(1 for x in p if x>0)
    med=statistics.median(p)
    print(f'  {label}: n={len(p):3d} 净=${sum(p):9.2f} 均=${statistics.mean(p):7.2f} 中位=${med:7.2f} WR={w/len(p)*100:3.0f}% 毛=${sum(r["gross"] or 0 for r in grp):8.2f}')

print('═══ UE LONG 分窗 (对齐 DEB-20260923 切点) ═══')
L=[r for r in rows if r['direction']=='LONG']
S=[r for r in rows if r['direction']=='SHORT']
stat([r for r in L if str(r['entry_time'])[:10]<'2026-09-05'], 'LONG <9/5 (IS)')
stat([r for r in L if str(r['entry_time'])[:10]>='2026-09-05'], 'LONG ≥9/5 (OOS)')
stat([r for r in L if str(r['entry_time'])[:10]>='2026-08-25'], 'LONG ≥8/25 (我上轮用的窗)')
stat(L, 'LONG 全窗')
print()
stat([r for r in S if str(r['entry_time'])[:10]<'2026-09-05'], 'SHORT <9/5')
stat([r for r in S if str(r['entry_time'])[:10]>='2026-09-05'], 'SHORT ≥9/5')
stat([r for r in S if str(r['entry_time'])[:10]>='2026-08-25'], 'SHORT ≥8/25')
stat(S, 'SHORT 全窗')

print('\n═══ UE LONG ≥9/5 逐笔 (判尾部主导 or 系统性) ═══')
for r in sorted([r for r in L if str(r['entry_time'])[:10]>='2026-09-05'], key=lambda x: (x['pnl'] or 0)):
    print(f"  {r['symbol']:9s} {str(r['entry_time'])[:10]} ${r['pnl'] or 0:8.2f} mfe={(r['mfe_pct'] or 0):.2f}% {r['exit_reason']}")

print('\n═══ 关键: 若砍 UE LONG, ≥9/5 窗口的边际效应 ═══')
post = [r for r in rows if str(r['entry_time'])[:10]>='2026-09-05']
stat(post, '≥9/5 全 UE')
stat([r for r in post if r['direction']!='LONG'], '≥9/5 留存(非LONG)')
stat([r for r in post if r['direction']=='LONG'], '≥9/5 拦截组(LONG)')
lp=[r['pnl'] or 0 for r in post if r['direction']=='LONG']
rp=[r['pnl'] or 0 for r in post if r['direction']!='LONG']
if lp and rp:
    print(f'  拦−留存笔均 = ${statistics.mean(lp)-statistics.mean(rp):.2f} (G3门要求 ≤ −$8)')
    print(f'  拦截组盈利单占比 = {sum(1 for x in lp if x>0)/len(lp)*100:.0f}% (G5门要求 ≤40%)')
