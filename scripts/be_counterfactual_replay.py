"""MU案反事实: BE cohort (8/1起 SL_BREAKEVEN 出场) — 若无BE规则, 同路径会如何?
复用 repo 官方回放引擎 (exit_replay_backtest.py), 与实盘同一退出函数."""
import sys, json
sys.path.insert(0, '/opt/ftseek/FTSeek-US')
sys.path.insert(0, '/opt/ftseek/FTSeek-US/scripts')

import exit_replay_backtest as ER
from app.playbook.pipeline import _get_factor_exit_config

factor_rules, params = ER._load_configs()
trades = ER.load_trades('2026-08-01', '2026-08-20')
be_trades = [t for t in trades if t['live_reason'] == 'SL_BREAKEVEN']
print(f"BE cohort n={len(be_trades)}")

bars = ER.collect_bars(be_trades, refresh=False, e_days=0)

rows = []
for tr in be_trades:
    day = tr['entry_et'].strftime('%Y-%m-%d')
    b = bars.get((tr['symbol'], day))
    if not b:
        print('MISSING BARS', tr['symbol'], day); continue
    # 与官方 main() 同口径: 跳过入场 bar, 从下一分钟开始
    b = [x for x in b if x['t'] > tr['entry_et'].replace(second=0, microsecond=0)]
    if len(b) < 5:
        print('TOO FEW BARS', tr['symbol'], day); continue
    exit_cfg = _get_factor_exit_config(tr['conditions'], factor_rules, tr['direction'])
    fee = ER._fee_of(tr)
    if fee is None or tr.get('live_net') is None:
        print('SKIP FEE/NET UNKNOWN', tr['symbol'], day)
        continue
    sign = 1 if tr['direction'] == 'LONG' else -1
    out = {}
    for ordering in ('pess', 'opt'):
        live = ER.replay_trade(tr, b, exit_cfg, {'name': 'live_baseline'}, params, ordering, 3.0)
        nobe = ER.replay_trade(tr, b, exit_cfg, {'name': 'noBE', 'be': 99.0}, params, ordering, 3.0)
        be8 = ER.replay_trade(tr, b, exit_cfg, {'name': 'be0.8', 'be': 0.8}, params, ordering, 3.0)
        out[ordering] = dict(
            live=(live['exit_price'], live['reason']),
            nobe=(nobe['exit_price'], nobe['reason']),
            be8=(be8['exit_price'], be8['reason']))
    net = lambda px: tr['qty'] * sign * (px - tr['entry']) - fee
    rows.append(dict(sym=tr['symbol'], day=day, dir=tr['direction'], entry=tr['entry'],
                     live_gross=tr['live_gross'], live_net=tr['live_net'], fee=round(fee, 2),
                     live_px=out['pess']['live'][0], live_rsn=out['pess']['live'][1],
                     nobe_px=out['pess']['nobe'][0], nobe_rsn=out['pess']['nobe'][1],
                     nobe_net=round(net(out['pess']['nobe'][0]), 2),
                     nobe_net_opt=round(net(out['opt']['nobe'][0]), 2),
                     be8_px=out['pess']['be8'][0], be8_rsn=out['pess']['be8'][1],
                     be8_net=round(net(out['pess']['be8'][0]), 2),
                     cfg=f"trail={exit_cfg.get('trailing_enabled')}"))

print(f"\n{'sym':9}{'day':11}{'live_net':>9}{'cf_noBE':>9}{'cf_be0.8':>9}  noBE_exit")
for r in rows:
    print(f"{r['sym']:9}{r['day']:11}{r['live_net']:>9.2f}{r['nobe_net']:>9.2f}{r['be8_net']:>9.2f}  {r['nobe_rsn']} @{r['nobe_px']:.2f} ({r['cfg']})")

n = len(rows)
print(f"\nn={n}")
print(f"live gross sum={sum(r['live_gross'] for r in rows):.2f}; fees={sum(r['fee'] for r in rows):.2f}; "
      f"live net sum={sum(r['live_net'] for r in rows):.2f}")
print(f"cf noBE pess sum ={sum(r['nobe_net'] for r in rows):.2f}   opt sum={sum(r['nobe_net_opt'] for r in rows):.2f}")
print(f"cf be0.8 pess sum={sum(r['be8_net'] for r in rows):.2f}")
better = sum(1 for r in rows if r['nobe_net'] > r['live_net'])
print(f"noBE better in {better}/{n} trades (pess)")
from collections import Counter
print("noBE exit reasons:", Counter(r['nobe_rsn'] for r in rows))
print("be0.8 exit reasons:", Counter(r['be8_rsn'] for r in rows))
