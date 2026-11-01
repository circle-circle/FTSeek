#!/usr/bin/env python3
"""延长持有 (hold-to-D+n close) 的日线反事实 —— 出场层最后一个未测方案。

问题: 现行出场链 (0.4% 保本 + 0.8% 追踪 + 6h 时间退出) 把赢家切得很短 (捕获率 44%,
MFE 中位 0.98%)。若**只保留初始止损**、把单子拿到 D+n 收盘, 是否更好?

模型 (日线级, 逐日检查止损):
  for d in [entry_day .. entry_day+n]:
      if 当日 low 穿止损 (LONG) → 按 min(open, stop) 出 (跳空承认)
      若 d == target → 按当日 close 出
费用: 往返 3.98 + 0.42bps×名义 (线上口径)
⚠️ 这是**替换整套出场链**, 不是"只延长持有"; 且日线无法复现盘中先触保本还是先触止损, 故模型偏保守。

预注册判据:
  H1 该 n 的净额 > 现状
  H2 逐笔一致性 ≥55%
  H3 改善 > $150
  任一 n 同时满足 H1∧H2∧H3 ⇒ CANDIDATE (须另立 DEB); 否则 FALSIFIED
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict

REPO = '/opt/ftseek/FTSeek-US'
DB = f'{REPO}/data/ftseek_us.db'
CACHE = f'{REPO}/data/shadow/bars_cache_qfq.json'
OUT = f'{REPO}/data/shadow/hold_n_latest.json'
SINCE = '2026-07-01'
HOLD_NS = (0, 1, 2, 3)


def load_trades():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT entry_time, symbol, direction, entry_price, quantity, pnl_net,
               exit_reason, mfe_pct, mae_pct, metadata_json
        FROM trade_records
        WHERE COALESCE(exit_price,0)>0 AND trade_date>=?
          AND COALESCE(json_extract(metadata_json,'$.gate_mode'),'')!='research_momentum'
          AND entry_time IS NOT NULL
    """, (SINCE,)).fetchall()
    out = []
    for r in rows:
        md = json.loads(r['metadata_json'] or '{}')
        sl = md.get('stop_loss')
        ep = float(r['entry_price'] or 0)
        qty = float(r['quantity'] or 0)
        if not sl or ep <= 0 or qty <= 0:
            continue
        out.append(dict(sym=r['symbol'], d=str(r['entry_time'])[:10],
                        long=str(r['direction']).upper().startswith(('BUY', 'LONG')),
                        ep=ep, qty=qty, sl=float(sl), actual=r['pnl_net'] or 0,
                        why=r['exit_reason'], mfe=r['mfe_pct'], mae=r['mae_pct']))
    return out


def get_bars(symbols):
    try:
        cache = json.load(open(CACHE))
    except Exception:
        cache = {}
    need = [s for s in symbols if s not in cache]
    if need:
        from futu import AuType, KLType, OpenQuoteContext
        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        print(f'拉日线 {len(need)} 只 (缓存命中 {len(symbols)-len(need)})…')
        try:
            for i, sym in enumerate(sorted(need), 1):
                try:
                    res = ctx.request_history_kline(sym, start=SINCE, end='2026-11-02',
                                                    ktype=KLType.K_DAY, autype=AuType.QFQ, max_count=250)
                    ret, df = res[0], res[1]
                    if ret == 0 and hasattr(df, 'columns'):
                        cache[sym] = [[str(t)[:10], float(o), float(h), float(l), float(c)]
                                      for t, o, h, l, c in zip(df['time_key'], df['open'],
                                                               df['high'], df['low'], df['close'])]
                except Exception as exc:  # noqa: BLE001
                    print(f'  [skip] {sym}: {type(exc).__name__}', file=sys.stderr)
                if i % 25 == 0:
                    print(f'  {i}/{len(need)}')
                time.sleep(1.15)
        finally:
            ctx.close()
        try:
            json.dump(cache, open(CACHE, 'w'))
        except OSError as exc:
            print(f'[warn] 缓存写失败: {exc}', file=sys.stderr)
    return cache


def simulate(bars, t, n):
    """返回 (exit_price, reason) —— reason ∈ {day0..dayN, stop}"""
    row = [b for b in bars if b[0] >= t['d']]
    if not row:
        return None, 'nodata'
    stop = t['sl']
    for i, (d, o, h, l, c) in enumerate(row[: n + 1]):
        if t['long']:
            if l <= stop:
                return (min(o, stop) if o < stop else stop), 'stop'
        else:
            if h >= stop:
                return (max(o, stop) if o > stop else stop), 'stop'
        if i == n:
            return c, f'D+{n}close'
    return None, 'nodata'


def sanity(t, bars):
    """拆分/复权/脏数据防线: 入场日 close 与成交价偏离 >50% ⇒ 该笔不参与 (记为可疑)。"""
    row = [b for b in bars if b[0] >= t['d']]
    if not row:
        return False
    return abs(row[0][4] / t['ep'] - 1) <= 0.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    T = load_trades()
    bars = get_bars({t['sym'] for t in T})
    print(f'样本 {len(T)} 笔 | 有日线 {len({t["sym"] for t in T} & set(bars))} 个标的')
    base = sum(t['actual'] for t in T)

    res = {}
    T = [t for t in T if sanity(t, bars.get(t['sym'], []))]
    print(f'通过脏数据防线 {len(T)} 笔 (剔除拆分/复权/价差>50% 的)')
    for n in HOLD_NS:
        net, imp, wor, stops, miss = 0.0, 0, 0, 0, 0
        for t in T:
            px, why = simulate(bars.get(t['sym'], []), t, n)
            if px is None:
                net += t['actual']; miss += 1; continue
            nom = px * t['qty']
            fee = 3.98 + nom * 0.000042
            pnl = (px - t['ep']) * t['qty'] if t['long'] else (t['ep'] - px) * t['qty']
            pnl -= fee
            net += pnl
            if why == 'stop':
                stops += 1
            if pnl > t['actual'] + 0.01:
                imp += 1
            elif pnl < t['actual'] - 0.01:
                wor += 1
        cons = imp / max(imp + wor, 1)
        h1, h2, h3 = net > base, cons >= 0.55, (net - base) > 150
        res[n] = dict(net=net, delta=net - base, improved=imp, worsened=wor, consistency=cons,
                      stopped=stops, missing=miss,
                      checks=dict(H1_net_up=h1, H2_consistency=h2, H3_material=h3),
                      state='CANDIDATE' if (h1 and h2 and h3) else 'FALSIFIED')
        print(f'  D+{n} 收盘: 净 {net:9,.0f} (Δ{net-base:+8,.0f}) 改善{imp:4d}/变差{wor:4d} '
              f'一致性{cons*100:5.1f}% 止损{stops:4d} 缺数据{miss:3d} ⇒ {res[n]["state"]}')

    out = dict(baseline=base, n_trades=len(T), schemes=res)
    try:
        json.dump(out, open(OUT, 'w'), ensure_ascii=False, indent=2)
        print(f'产物 {OUT}')
    except OSError as exc:
        print(f'[warn] {exc}', file=sys.stderr)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())