"""波动率武装阈值全样本回放 — 裁决"按个股波动率分布治源头".
全量平仓仓 (7/14 be feature 后), 4变体: V0固定0.5 / V1固定0.8 / V2 max(0.5,0.3ATR) / V3 max(0.5,0.5ATR).
每变体反事实路径 vs 该仓真实净结局配对. 若波动率方案治本, 全样本均值应显著优于 V0.
"""
import json, sqlite3, datetime, statistics
from pathlib import Path

con = sqlite3.connect('data/ftseek_us.db')
con.row_factory = sqlite3.Row

def load_bars(sym):
    f = Path('data/replay_bars_1m') / f"{sym.replace('.', '_')}.json"
    if not f.exists():
        return []
    d = json.load(open(f))
    return d if isinstance(d, list) else d.get('bars', d.get('data', []))

def norm_et(s):
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return dt.astimezone(datetime.timezone(datetime.timedelta(hours=-4)))

rows = con.execute(
    "SELECT id, symbol, direction, entry_price, entry_time, exit_time, exit_reason, "
    "pnl_net, realized_pnl, pnl_pct, fees_est, metadata_json "
    "FROM trade_records WHERE exit_reason IS NOT NULL AND entry_time >= '2026-07-14' "
    "ORDER BY exit_time"
).fetchall()

VAR = {
    'V0_flat0.5': lambda a: 0.5,
    'V1_flat0.8': lambda a: 0.8,
    'V2_vol0.3x': lambda a: max(0.5, 0.3 * a),
    'V3_vol0.5x': lambda a: max(0.5, 0.5 * a),
}

def simulate(trade, bars, arm_pct, cushion):
    entry = float(trade['entry_price'])
    short = trade['direction'] == 'SHORT'
    sgn = -1 if short else 1
    m = json.loads(trade['metadata_json'] or '{}')
    init_sl = float(m.get('stop_loss', 0) or 0)
    ent_dt = norm_et(trade['entry_time'])
    exit_dt = norm_et(trade['exit_time'])
    ent_key = ent_dt.strftime('%Y-%m-%d %H:%M:%S')
    exit_day = exit_dt.strftime('%Y-%m-%d')
    be_line = entry * (1 - cushion / 100) if short else entry * (1 + cushion / 100)
    armed, peak = False, 0.0
    day = [(str(b[0]), b) for b in bars if ent_key <= str(b[0]) <= exit_day + ' 23:59']
    if not day:
        return None
    for ts, b in day:
        o, h, l, c = b[1], b[2], b[3], b[4]
        peak = max(peak, (h - entry) / entry * 100 * sgn)
        if not armed and peak >= arm_pct:
            armed = True
        if armed:
            if (l <= be_line) if not short else (h >= be_line):
                px = min(o, be_line) if not short else max(o, be_line)
                return (px - entry) / entry * 100 * sgn - cushion, 'BE', True
        elif init_sl > 0 and ((l <= init_sl) if not short else (h >= init_sl)):
            px = min(o, init_sl) if not short else max(o, init_sl)
            return (px - entry) / entry * 100 * sgn - cushion, 'SL', False
    last = day[-1][1]
    return (last[4] - entry) / entry * 100 * sgn - cushion, 'EOD', armed

out = {k: [] for k in VAR}
n_sim = 0
for t in rows:
    bars = load_bars(t['symbol'])
    if not bars:
        continue
    m = json.loads(t['metadata_json'] or '{}')
    atr = float(m.get('atr', 0) or 0)
    if atr <= 0:
        continue
    atr_pct = atr / float(t['entry_price']) * 100
    cushion = float(m.get('_be_cushion_fee_aware') or 0.08)
    pct = float(t['pnl_pct'] or 0)
    notional = float(t['realized_pnl'] or 0) / pct if pct else 0
    if notional <= 0:
        continue
    real_net = float(t['pnl_net'] or 0) / notional * 100
    ok = False
    for vn, fn in VAR.items():
        sim = simulate(t, bars, fn(atr_pct), cushion)
        if sim:
            out[vn].append(sim[0] - real_net)
            ok = True
    if ok:
        n_sim += 1

print(f"全样本可回放仓: {n_sim}\n")
print(f"{'变体':<12} {'n':>4} {'均值diff_pp':>10} {'中位':>8} {'胜率':>6}")
for vn in VAR:
    v = out[vn]
    w = sum(1 for x in v if x > 0)
    print(f"{vn:<12} {len(v):>4} {statistics.mean(v):10.3f} {statistics.median(v):8.3f} {w/len(v)*100:5.0f}%")
print("\n判读: diff_pp = 变体反事实净% − 真实净%. 波动率治本 ⇔ V2/V3 均值显著优于 V0.")
