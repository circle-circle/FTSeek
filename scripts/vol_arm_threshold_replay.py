"""波动率武装线回放 — 裁决: 按个股 ATR 分布动态化武装阈值能否治"死亡谷"?

变体:
  V0 固定 0.5% (现状基线)
  V1 固定 0.8% (已被 A 节否决, 复现对照)
  V2 max(0.5%, 0.3×ATR%) — 波动率化温和版
  V3 max(0.5%, 0.5×ATR%) — 波动率化激进版
协议: bar 收盘利润≥阈值才武装 → 武装后 bar low 穿净保本线即出 (exit=min(open,be线))
     未武装期间 bar low 穿初始SL → SL出场; 出场日收盘仍未触发 → EOD收盘近似.
"""
import json, sqlite3, datetime, statistics
from pathlib import Path

DB = Path('data/ftseek_us.db')
BARS = Path('data/replay_bars_1m')
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

def load_bars(sym):
    f = BARS / f"{sym.replace('.', '_')}.json"
    if not f.exists():
        return []
    d = json.load(open(f))
    return d if isinstance(d, list) else d.get('bars', d.get('data', []))

def norm_et(s):
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    return dt.astimezone(datetime.timezone(datetime.timedelta(hours=-4)))

rows = con.execute(
    "SELECT id, symbol, direction, entry_price, entry_time, exit_time, exit_reason, "
    "pnl_net, realized_pnl, pnl_pct, fees_est, metadata_json "
    "FROM trade_records WHERE exit_reason='SL_BREAKEVEN' AND exit_time >= '2026-08-10' ORDER BY exit_time"
).fetchall()

VARIANTS = {
    'V0_flat0.5': lambda atr: 0.5,
    'V1_flat0.8': lambda atr: 0.8,
    'V2_vol0.3x': lambda atr: max(0.5, 0.3 * atr),
    'V3_vol0.5x': lambda atr: max(0.5, 0.5 * atr),
}

def simulate(trade, bars, arm_pct, cushion_pct):
    """返回 (net_pct, exit_why, armed)"""
    entry = float(trade['entry_price'])
    direction = trade['direction']
    is_short = direction == 'SHORT'
    sgn = -1 if is_short else 1
    m = json.loads(trade['metadata_json'] or '{}')
    init_sl = float(m.get('stop_loss', 0) or 0)
    ent_dt = norm_et(trade['entry_time'])
    exit_dt = norm_et(trade['exit_time'])
    ent_key = ent_dt.strftime('%Y-%m-%d %H:%M:%S')
    exit_day = exit_dt.strftime('%Y-%m-%d')
    be_line = entry * (1 - cushion_pct / 100) if is_short else entry * (1 + cushion_pct / 100)
    armed = False
    peak = 0.0
    day_bars = []
    for b in bars:
        ts = str(b[0])
        if ts < ent_key or ts > exit_day + ' 23:59':
            continue
        day_bars.append((ts, b))
    if not day_bars:
        return None
    for ts, b in day_bars:
        o, h, l, c = b[1], b[2], b[3], b[4]
        prof_close = (c - entry) / entry * 100 * sgn
        prof_high = (h - entry) / entry * 100 * sgn
        peak = max(peak, prof_high if not is_short else (entry - l) / entry * 100)
        if not is_short:
            peak = max(peak, (h - entry) / entry * 100)
        else:
            peak = max(peak, (entry - l) / entry * 100)
        if not armed and peak >= arm_pct:
            armed = True
        if armed:
            touched = l <= be_line if not is_short else h >= be_line
            if touched:
                px = min(o, be_line) if not is_short else max(o, be_line)
                gross = (px - entry) / entry * 100 * sgn
                return gross - cushion_pct, 'BE', True
        else:
            if init_sl > 0:
                sl_touched = l <= init_sl if not is_short else h >= init_sl
                if sl_touched:
                    px = min(o, init_sl) if not is_short else max(o, init_sl)
                    gross = (px - entry) / entry * 100 * sgn
                    return gross - cushion_pct, 'SL_INITIAL', False
    # EOD 近似 (出场日最后一根bar收盘)
    last = day_bars[-1][1]
    px = last[4]
    gross = (px - entry) / entry * 100 * sgn
    return gross - cushion_pct, 'EOD', armed

results = {k: [] for k in VARIANTS}
details = []
for t in rows:
    sym = t['symbol']
    bars = load_bars(sym)
    if not bars:
        continue
    m = json.loads(t['metadata_json'] or '{}')
    atr = float(m.get('atr', 0) or 0)
    atr_pct = atr / float(t['entry_price']) * 100
    cushion = float(m.get('_be_cushion_fee_aware', 0.08) or 0.08)
    real_net_pct = float(t['pnl_net'] or 0) / (float(t['realized_pnl'] or 1) / float(t['pnl_pct'] or 1)) * 100 if t['pnl_pct'] else None
    row_out = {'id': t['id'], 'sym': sym, 'atr_pct': round(atr_pct, 2), 'real_net': round(real_net_pct, 3) if real_net_pct else None}
    for vname, vfn in VARIANTS.items():
        arm = vfn(atr_pct)
        sim = simulate(t, bars, arm, cushion)
        if sim is None:
            results[vname].append(0)
            row_out[vname] = 'nobars'
            continue
        net, why, armed = sim
        results[vname].append(net)
        row_out[vname] = f"{net:+.3f}({why})"
    details.append(row_out)

print(f"样本: {len(details)} 笔 (SL_BREAKEVEN, 8/10 后有1分K)\n")
print(f"{'变体':<12} {'n':>3} {'均值pp':>8} {'中位':>8} {'胜率':>6} | BE出 | SL出 | EOD | 未武装")
for vname in VARIANTS:
    vals, whys, unarmed = [], {'BE': 0, 'SL_INITIAL': 0, 'EOD': 0}, 0
    for t in rows:
        sym = t['symbol']
        bars = load_bars(sym)
        if not bars:
            continue
        m = json.loads(t['metadata_json'] or '{}')
        atr_pct = float(m.get('atr', 0) or 0) / float(t['entry_price']) * 100
        cushion = float(m.get('_be_cushion_fee_aware', 0.08) or 0.08)
        sim = simulate(t, bars, VARIANTS[vname](atr_pct), cushion)
        if sim:
            vals.append(sim[0])
            whys[sim[1]] += 1
            if not sim[2]:
                unarmed += 1
    wins = sum(1 for v in vals if v > 0)
    print(f"{vname:<12} {len(vals):>3} {statistics.mean(vals):8.3f} {statistics.median(vals):8.3f} "
          f"{wins/len(vals)*100:5.0f}% | {whys['BE']:4d} | {whys['SL_INITIAL']:4d} | {whys['EOD']:3d} | {unarmed}")

print("\n=== 逐笔明细 (real=实际净%, 其余=反事实净%) ===")
for r in details:
    print(f"  #{r['id']} {r['sym']:9} ATR={r['atr_pct']}% real={r['real_net']} | "
          + " | ".join(f"{k}={r[k]}" for k in VARIANTS))
