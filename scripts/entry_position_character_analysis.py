#!/usr/bin/env python3
"""追强 vs 回踩 标的性格测试 (复现工具, DEB-20260813 §15 入场特征零预测力验证的延伸)

背景: 另一agent提出"标的性格"规则(SNDK追强WR68%>回踩57%, MU追空亏钱),
本脚本用自有 trade_records 复现检验: 追强/平盘/回踩 入场位置是否影响 WR/pnl。

特征定义:
  市场级 chase_vs_open = (fill价/当日开盘 - 1)%, 用 metadata_json.entry_gap (7/22起落库)
  ATR归一 u = chase_vs_open% × entry_price / atr   (>0.1=追高, <-0.1=回踩, else 平盘)
  带内级 band_pos = (fill - entry_low)/(entry_high - entry_low) (join playbook_instructions)
死单口径: mfe_pct < 0.2% (DEB-20260813 §14)
纪律: 分桶 n<30 只出方向不下结论; 参数级结论须过双窗口同号 (本脚本含中点切半检验)。

用法: venv/bin/python scripts/entry_position_character_analysis.py [--symbol US.CRM]
输出: stdout 报告 + data/analysis/chase_pullback_features.json (特征落盘)
"""
import sys, json, math, sqlite3
from collections import defaultdict

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
OUT = "/opt/ftseek/FTSeek-US/data/analysis/chase_pullback_features.json"

def wilson(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k/n; d = 1+z*z/n; c = p+z*z/(2*n); m = z*math.sqrt(max(p*(1-p)/n+z*z/(4*n*n),0))
    return ((c-m)/d, (c+m)/d)

def stats(grp, label):
    n = len(grp)
    if n == 0:
        print(f"{label:26s} n=  0"); return
    w = sum(1 for x in grp if x['pnl_net'] > 0)
    lo, hi = wilson(w, n)
    tot = sum(x['pnl_net'] for x in grp)
    dead = sum(1 for x in grp if (x['mfe_pct'] or 0) < 0.2)
    print(f"{label:26s} n={n:3d}  WR={w/n*100:5.1f}%  CI95=[{lo*100:4.1f},{hi*100:4.1f}]  pnl总={tot:+8.1f}  均={tot/n:+6.2f}  死单率={dead/n*100:4.1f}%")

def load():
    db = sqlite3.connect(DB); db.row_factory = sqlite3.Row
    rows = db.execute("""
    SELECT t.id, t.symbol, t.direction, t.entry_time, t.entry_price, t.pnl_net, t.mfe_pct,
           t.exit_reason, t.metadata_json, i.entry_low, i.entry_high
    FROM trade_records t
    LEFT JOIN playbook_instructions i ON i.date = date(t.entry_time) AND i.symbol = t.symbol
    WHERE t.exit_time IS NOT NULL AND t.symbol NOT LIKE '%TEST%'
    """).fetchall()
    recs = []
    for r in rows:
        j = {}
        if r['metadata_json']:
            try: j = json.loads(r['metadata_json'])
            except Exception: pass
        o = {'id': r['id'], 'symbol': r['symbol'], 'direction': r['direction'],
             'entry_price': r['entry_price'], 'entry_time': r['entry_time'],
             'date': r['entry_time'][:10], 'pnl_net': r['pnl_net'],
             'mfe_pct': r['mfe_pct'], 'exit_reason': r['exit_reason']}
        eg = j.get('entry_gap')
        if isinstance(eg, dict) and eg.get('prev_close') and eg.get('today_open') and r['entry_price']:
            pc, op, fill = float(eg['prev_close']), float(eg['today_open']), float(r['entry_price'])
            o['gap_vs_pc'] = (fill/pc-1)*100
            o['chase_vs_open'] = (fill/op-1)*100
            o['open_gap'] = (op/pc-1)*100
        if j.get('atr'): o['atr'] = float(j['atr'])
        if r['entry_low'] and r['entry_high'] and r['entry_high'] > r['entry_low'] and r['entry_price']:
            o['band_pos'] = (float(r['entry_price'])-r['entry_low'])/(r['entry_high']-r['entry_low'])
        recs.append(o)
    import os; os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(recs, open(OUT, 'w'))
    return recs

def atru(x):
    if not x.get('atr') or not x.get('entry_price'): return None
    return x['chase_vs_open']/100*x['entry_price']/x['atr']

def main():
    only_symbol = sys.argv[sys.argv.index('--symbol')+1] if '--symbol' in sys.argv else None
    recs = load()
    if only_symbol:
        recs = [x for x in recs if x['symbol'] == only_symbol]
    G = [x for x in recs if 'chase_vs_open' in x]
    print(f"闭合样本={len(recs)}  市场级特征={len(G)} (窗口 {min(x['date'] for x in G)}~{max(x['date'] for x in G)})")

    L = [x for x in G if x['direction']=='LONG']; S = [x for x in G if x['direction']=='SHORT']
    print(f"\n== LONG 相对开盘 n={len(L)} ==")
    stats([x for x in L if x['chase_vs_open'] > 0.5], "追高 (>开盘+0.5%)")
    stats([x for x in L if -0.5 <= x['chase_vs_open'] <= 0.5], "平盘 (±0.5%)")
    stats([x for x in L if x['chase_vs_open'] < -0.5], "回踩 (<开盘-0.5%)")
    LA = [x for x in L if atru(x) is not None]
    print(f"-- ATR归一 n={len(LA)} --")
    stats([x for x in LA if atru(x) > 0.1], "追高 (>0.1 ATR)")
    stats([x for x in LA if -0.1 <= atru(x) <= 0.1], "平盘 (±0.1 ATR)")
    stats([x for x in LA if atru(x) < -0.1], "回踩 (<-0.1 ATR)")
    print(f"\n== SHORT 相对开盘 n={len(S)} ==")
    stats([x for x in S if x['chase_vs_open'] < -0.5], "追空 (<开盘-0.5%)")
    stats([x for x in S if -0.5 <= x['chase_vs_open'] <= 0.5], "平盘 (±0.5%)")
    stats([x for x in S if x['chase_vs_open'] > 0.5], "等反弹 (>开盘+0.5%)")

    print(f"\n== 开盘跳空 × LONG ==")
    stats([x for x in L if x['open_gap'] > 1], "高开>1% 追多")
    stats([x for x in L if -1 <= x['open_gap'] <= 1], "平开 做多")
    stats([x for x in L if x['open_gap'] < -1], "低开<-1% 接多")

    B = [x for x in recs if x.get('band_pos') is not None]
    BL = [x for x in B if x['direction']=='LONG']
    print(f"\n== LONG 带内位置 n={len(BL)} (0=回踩侧 1=追入侧) ==")
    stats([x for x in BL if x['band_pos'] >= 0.67], "上1/3 (追入侧)")
    stats([x for x in BL if 0.33 < x['band_pos'] < 0.67], "中1/3")
    stats([x for x in BL if x['band_pos'] <= 0.33], "下1/3 (回踩侧)")

    dates = sorted(set(x['date'] for x in G))
    if len(dates) >= 4:
        mid = dates[len(dates)//2]
        print(f"\n== 双窗口 OOS (分割 {mid}) ==")
        for half in ("前半", "后半"):
            sel = [x for x in L if (x['date'] < mid) == (half == "前半")]
            ch = [x for x in sel if atru(x) is not None and atru(x) > 0.1]
            pu = [x for x in sel if atru(x) is not None and atru(x) < -0.1]
            wc = sum(1 for x in ch if x['pnl_net']>0); wp = sum(1 for x in pu if x['pnl_net']>0)
            print(f"  {half}: 追高n={len(ch)} WR={wc/len(ch)*100 if ch else 0:.0f}% pnl={sum(x['pnl_net'] for x in ch):+.0f}"
                  f" | 回踩n={len(pu)} WR={wp/len(pu)*100 if pu else 0:.0f}% pnl={sum(x['pnl_net'] for x in pu):+.0f}")

if __name__ == '__main__':
    main()
