import sqlite3, json, statistics
from collections import defaultdict
from datetime import date, timedelta

con = sqlite3.connect('file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row

# ── 拉全部 W_WEEK 豁免族 (含未平) ──
rows = con.execute("""
SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price, quantity,
       COALESCE(pnl_net, realized_pnl) pnl, exit_reason, mfe_pct, metadata_json
FROM trade_records
WHERE source_type='PLAYBOOK_SIM' AND entry_time>='2026-08-16'
ORDER BY entry_time
""").fetchall()

ww = []
for r in rows:
    try:
        mj = json.loads(r['metadata_json']) if r['metadata_json'] else {}
    except Exception:
        continue
    oa = mj.get('overnight_arm') or {}
    if oa.get('source') == 'w_week':
        ww.append((r, oa, mj))
print(f'W_WEEK 豁免族 n={len(ww)} (含未平)')

# 美股假日 2026 (FTSeek SSOT 离线表口径)
HOL = {'2026-01-01','2026-01-19','2026-02-16','2026-04-03','2026-05-25','2026-06-19',
       '2026-07-03','2026-09-07','2026-11-26','2026-12-25'}

def next_trading_day(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in HOL:
        d += timedelta(days=1)
    return d

print('\n=== 逐笔: 夜属性分类 + EOD平仓反事实 ===')
print(f"{'sym':9s} {'dir':5s} {'入日':10s} {'标记夜':10s} {'夜型':22s} {'实际净$':>9s} {'EOD平反事实$':>12s} {'Δ(省)$':>9s} {'出场原因'}")
cat_stat = defaultdict(lambda: {'n':0,'actual':0.0,'cf':0.0,'rows':[]})
detail = []
for r, oa, mj in ww:
    marked = (oa.get('marked_at_et') or '')[:10]
    if not marked:
        marked = str(r['entry_time'])[:10]
    md = date.fromisoformat(marked)
    ntd = next_trading_day(md)
    span = (ntd - md).days
    underwater = oa.get('underwater_pct_net')
    qty = r['quantity'] or (mj.get('_qty') or 0)
    ep = r['entry_price'] or 0
    actual = r['pnl'] if r['pnl'] is not None else 0.0
    # 反事实: 若 EOD 不豁免, 以标记时刻水深平掉
    if underwater is not None and qty and ep:
        cf = ep * qty * (underwater/100.0)
        cf -= 4.2  # 单边出场费 (入场费两边都付, 抵消)
        has_cf = True
    else:
        cf = None; has_cf = False

    if span >= 3:
        cat = f'周末/假日夜({span}天不可止损)'
    elif r['exit_time'] is None:
        cat = '在仓(未结算)'
    else:
        cat = '普通夜'
    # 财报夜标记 (需外部日历, 这里先只按已知案例手工标)
    st = cat_stat[cat]
    st['n'] += 1; st['actual'] += actual
    if has_cf: st['cf'] += cf
    delta = (actual - cf) if has_cf else None
    print(f"{r['symbol']:9s} {r['direction']:5s} {str(r['entry_time'])[:10]:10s} {marked:10s} {cat:22s} {actual:9.2f} {(f'{cf:12.2f}' if has_cf else '     无水深数据'):>12s} {(f'{delta:+9.2f}' if delta is not None else '        -'):>9s} {r['exit_reason'] or 'OPEN'}")
    detail.append(dict(sym=r['symbol'], dir=r['direction'], entry_d=str(r['entry_time'])[:10],
                       marked=marked, cat=cat, actual=actual, cf=cf, delta=delta,
                       er=r['exit_reason'], uw=underwater, span=span, open=r['exit_time'] is None))

print('\n=== 分类汇总 ===')
for cat, st in cat_stat.items():
    print(f"  {cat}: n={st['n']} 实际净=${st['actual']:.2f} 反事实净=${st['cf']:.2f} → 省=${st['actual']-st['cf']:+.2f}")

# 按夜型算 carve-out 精确效应
weekend = [d for d in detail if d['cat'].startswith('周末')]
normal = [d for d in detail if d['cat']=='普通夜']
for name, grp in (('周末/假日夜 carve-out', weekend), ('普通夜 (carve-out 不动)', normal)):
    act = sum(d['actual'] for d in grp)
    cfs = [d for d in grp if d['cf'] is not None]
    cf = sum(d['cf'] for d in cfs); act_c = sum(d['actual'] for d in cfs)
    print(f"\n  [{name}] n={len(grp)} 实际=${act:.2f}")
    if cfs:
        print(f"     可反事实 n={len(cfs)}: 实际=${act_c:.2f} → EOD平=${cf:.2f} Δ=${act_c-cf:+.2f} (正=carve-out省钱)")
        for d in sorted(cfs, key=lambda x: x['delta'] or 0):
            print(f"       {d['sym']:9s} {d['dir']:5s} {d['marked']} 水深{d['uw']}% 实际${d['actual']:8.2f} cf${d['cf']:8.2f} Δ{d['delta']:+8.2f} {d['er']}")
    else:
        print('     (无可反事实样本 — 缺 underwater_pct_net)')
