"""DEB-20260815 §29 攻击点d/f: 核心盘SHORT的SPY beta剥离 + regime同型检验.

d: 逐回合 alpha = gross_pnl + spy_ret(hold窗口) × 名义 (SHORT的beta成分=-spy_ret×名义).
   日线粒度, entry/exit当日收盘近似.
f: SPY 月度收益表 — 人工SHORT盈利月(1-6月)与机器DA-SHORT链(7/9-7/27)是否同为SPY回撤regime.
只读: roundtrips json + Futu 行情(quote, 无交易权限).
"""
import json
import os
import time
from collections import defaultdict
from pathlib import Path

CACHE = Path('data/replay_bars/daily_US_SPY_2026-01-01_2026-08-15.json')
if CACHE.exists():
    bars = json.loads(CACHE.read_text())
else:
    from futu import OpenQuoteContext, KLType, RET_OK, AuType
    ctx = OpenQuoteContext(host=os.getenv('FUTU_OPEND_HOST', '127.0.0.1'),
                           port=int(os.getenv('FUTU_OPEND_PORT', '11111')))
    bars = []
    try:
        for _ in (1, 2):
            ret, data, _ = ctx.request_history_kline(
                'US.SPY', ktype=KLType.K_DAY, start='2026-01-01', end='2026-08-15',
                max_count=400, autype=AuType.NONE)
            if ret == RET_OK and data is not None and len(data) > 0:
                bars = [[str(r['time_key'])[:10], float(r['open']), float(r['high']),
                         float(r['low']), float(r['close'])] for _, r in data.iterrows()]
                bars.sort(key=lambda b: b[0])
                CACHE.write_text(json.dumps(bars))
                break
            time.sleep(3.0)
    finally:
        ctx.close()

print(f'SPY bars: {len(bars)} {bars[0][0]}~{bars[-1][0]}')
close = {b[0]: b[4] for b in bars}
dates = sorted(close)


def spy_close_on_or_after(d):
    for x in dates:
        if x >= d:
            return close[x]
    return close[dates[-1]]


def spy_close_on_or_before(d):
    prev = close[dates[0]]
    for x in dates:
        if x > d:
            return prev
        prev = close[x]
    return prev


# 月度 SPY 收益 (attack f)
mon = defaultdict(list)
for dt in dates:
    mon[dt[:7]].append(dt)
print('\nSPY monthly returns:')
for m in sorted(mon):
    ds = mon[m]
    r = close[ds[-1]] / close[ds[0]] - 1
    print(f'  {m}: {100*r:+.2f}%  ({ds[0]}~{ds[-1]})')
# 机器链窗口 7/9-7/27
r_mach = spy_close_on_or_before('2026-07-27') / spy_close_on_or_after('2026-07-09') - 1
print(f'SPY 2026-07-09~07-27 (机器DA-SHORT链窗口): {100*r_mach:+.2f}%')

# 攻击点d: 逐回合 beta 剥离
d = json.load(open('data/manual_live_roundtrips_2026-08-15.json'))
S = [x for x in d['trips'] if x['direction'] == 'SHORT' and x['symbol'] not in ('US.MU', 'US.SNDK')]
tot_gross = 0.0
tot_beta = 0.0
mon_a = defaultdict(lambda: [0.0, 0.0])
for r in S:
    notional = r['entry_price'] * r['qty']
    e = spy_close_on_or_after(r['entry_time'][:10])
    x = spy_close_on_or_before(r['exit_time'][:10])
    spy_ret = x / e - 1
    beta_pnl = -spy_ret * notional
    tot_gross += r['gross_pnl']
    tot_beta += beta_pnl
    m = r['exit_time'][:7]
    mon_a[m][0] += r['gross_pnl']
    mon_a[m][1] += r['gross_pnl'] - beta_pnl
print(f'\nATTACK-d SHORT beta剥离: gross={tot_gross:+.0f} beta成分={tot_beta:+.0f} alpha={tot_gross - tot_beta:+.0f}')
print('monthly gross -> alpha (exit月):')
for m in sorted(mon_a):
    g, a = mon_a[m]
    print(f'  {m}: {g:+.0f} -> {a:+.0f}')

# open窗子集同口径 (H1 的 beta 检验)
s_open = [r for r in S if 570 <= int(r['entry_time'][11:13]) * 60 + int(r['entry_time'][14:16]) < 720]
g = b_ = 0.0
for r in s_open:
    notional = r['entry_price'] * r['qty']
    spy_ret = spy_close_on_or_before(r['exit_time'][:10]) / spy_close_on_or_after(r['entry_time'][:10]) - 1
    g += r['gross_pnl']
    b_ += -spy_ret * notional
print(f'\nH1 open窗 (n={len(s_open)}): gross={g:+.0f} beta={b_:+.0f} alpha={g - b_:+.0f}')

# <=48h 子集同口径 (攻击b 合并视角)
fast = [r for r in S if r['hold_h'] <= 48]
g = b_ = 0.0
for r in fast:
    notional = r['entry_price'] * r['qty']
    spy_ret = spy_close_on_or_before(r['exit_time'][:10]) / spy_close_on_or_after(r['entry_time'][:10]) - 1
    g += r['gross_pnl']
    b_ += -spy_ret * notional
print(f'<=48h (n={len(fast)}): gross={g:+.0f} beta={b_:+.0f} alpha={g - b_:+.0f}')
