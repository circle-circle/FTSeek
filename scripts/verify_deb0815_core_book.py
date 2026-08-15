"""DEB-20260815 §29 复核: 核心盘 (剔MU/SNDK) 全部中间量独立重算.

只读 data/manual_live_roundtrips_2026-08-15.json, 不写库不联网.
口径: 剔除 direction 含 RESIDUAL 的行, 再剔 symbol∈{US.MU, US.SNDK}.
"""
import json
import statistics
from collections import defaultdict

d = json.load(open('data/manual_live_roundtrips_2026-08-15.json'))
trips = [x for x in d['trips'] if x['direction'] in ('LONG', 'SHORT')]
core = [x for x in trips if x['symbol'] not in ('US.MU', 'US.SNDK')]
print('core n =', len(core), '(expect 186)')


def agg(rows, label):
    n = len(rows)
    if not n:
        print(f'{label}: EMPTY')
        return rows
    pnl = sum(r['gross_pnl'] for r in rows)
    wins = [r for r in rows if r['gross_pnl'] > 0]
    losses = [r for r in rows if r['gross_pnl'] <= 0]
    wr = 100 * len(wins) / n
    aw = sum(r['gross_pnl'] for r in wins) / len(wins) if wins else 0
    al = sum(r['gross_pnl'] for r in losses) / len(losses) if losses else 0
    print(f'{label}: n={n} WR={wr:.1f}% pnl={pnl:+.2f} avgW={aw:+.0f} avgL={al:+.0f}')
    return rows


L = agg([x for x in core if x['direction'] == 'LONG'], 'LONG  (expect n=113 WR67.3% +301)')
S = agg([x for x in core if x['direction'] == 'SHORT'], 'SHORT (expect n=73 WR76.7% +10852)')

sym_pnl = defaultdict(lambda: [0, 0.0])
for r in S:
    sym_pnl[r['symbol']][0] += 1
    sym_pnl[r['symbol']][1] += r['gross_pnl']
top = sorted(sym_pnl.items(), key=lambda kv: -kv[1][1])
print('\nSHORT by symbol:')
for k, (n, p) in top:
    print(f'  {k}: n={n} {p:+.0f}')
tot_s = sum(p for _, p in sym_pnl.values())
top2 = sum(v[1] for _, v in top[:2])
print(f'Top2 share = {100 * top2 / tot_s:.1f}% (expect 54%, AMD+3007/CVNA+2806)')

for excl, label in [({'US.FIG'}, 'ex-FIG (expect n=49 +9191 WR80%)'),
                    ({'US.FIG', 'US.AMD'}, 'ex-FIG+AMD (expect n=37 +6184 WR76%)')]:
    agg([r for r in S if r['symbol'] not in excl], label)

w1 = [r for r in S if r['entry_time'][:10] <= '2026-02-26']
w2 = [r for r in S if r['entry_time'][:10] > '2026-02-26']
agg(w1, 'w1 entry<=2/26 (expect n=37 +4898)')
agg(w2, 'w2 (expect n=36 +5954)')

mon = defaultdict(list)
for r in S:
    mon[r['entry_time'][:7]].append(r)
print('\nSHORT monthly (expect 1:+2940 2:+1647 3:+1425 4:+225(n=3) 5:+1271 6:+3229 8:+116(n=1), 7月缺席):')
for m in sorted(mon):
    rows = mon[m]
    print(f'  {m}: n={len(rows)} {sum(r["gross_pnl"] for r in rows):+.0f}')

fig = [r for r in S if r['symbol'] == 'US.FIG']
fig_dates = sorted(r['entry_time'][:10] for r in fig)
print(f'\nFIG: n={len(fig)} (expect 24), window {fig_dates[0]}~{fig_dates[-1]} (expect 2/12~3/9)')
print(f'  max single win = {max(r["gross_pnl"] for r in fig):+.0f} (expect +1350), '
      f'median hold = {statistics.median(r["hold_h"] for r in fig):.1f}h (expect 226h)')

sw = [r['hold_h'] for r in S if r['gross_pnl'] > 0]
sl = [r['hold_h'] for r in S if r['gross_pnl'] <= 0]
print(f'\nSHORT hold: winners median {statistics.median(sw):.1f}h (expect 28.2) '
      f'vs losers {statistics.median(sl):.1f}h (expect 169.2)')


def sess(r):
    h = int(r['entry_time'][11:13])
    mnt = int(r['entry_time'][14:16])
    t = h * 60 + mnt
    if 570 <= t < 720:
        return 'open(9:30-12)'
    if 720 <= t < 840:
        return 'mid(12-14)'
    return 'late(14-16)'


for dirn, rows in (('SHORT', S), ('LONG', L)):
    b = defaultdict(list)
    for r in rows:
        b[sess(r)].append(r)
    print(f'\n{dirn} by session:')
    for k in ('open(9:30-12)', 'mid(12-14)', 'late(14-16)'):
        rs = b.get(k, [])
        if rs:
            agg(rs, f'  {k}')
s_open = [r for r in S if sess(r) == 'open(9:30-12)']
agg([r for r in s_open if r['symbol'] != 'US.FIG'], 'SHORT open ex-FIG (expect n=21 +4775 WR90%)')

# --- 攻击点b: 核心盘 + 持仓<=48h 帽
cap48 = [r for r in S if r['hold_h'] <= 48]
agg(cap48, '\nATTACK-b SHORT hold<=48h')
cap48x = [r for r in cap48 if r['symbol'] != 'US.FIG']
agg(cap48x, 'ATTACK-b ex-FIG hold<=48h')

# --- 攻击点c: 借券费 5%/年 扣减 (名义=entry_price*qty)
fee_tot = 0.0
for r in S:
    notional = r['entry_price'] * r['qty']
    fee_tot += notional * 0.05 * r['hold_h'] / 24 / 365
net_after = sum(r['gross_pnl'] for r in S) - fee_tot
print(f'\nATTACK-c borrow fee @5%: total fee=${fee_tot:.0f}, SHORT net {sum(r["gross_pnl"] for r in S):+.0f} -> {net_after:+.0f}')

# 逐月借券费后净额
print('monthly net-after-borrow:')
for m in sorted(mon):
    rows = mon[m]
    gp = sum(r['gross_pnl'] for r in rows)
    f = sum(r['entry_price'] * r['qty'] * 0.05 * r['hold_h'] / 24 / 365 for r in rows)
    print(f'  {m}: gross {gp:+.0f} fee {f:.0f} net {gp - f:+.0f}')
