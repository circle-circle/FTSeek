"""落地前反事实: UE LONG 三笔大亏(QCOM 9/8/UBER 9/11/RIVN 9/21)若过 DA 择时闸会被拦吗?
择时闸语义(pipeline.py:1306-1315 + signal_gate.py:424-430):
  真实动量 trend=='空头排列' → _scanner_direction=bearish
  bearish + BUY → 破位硬买拦截 → SKIP (硬拦, 不降档)
  其他trend(短期偏多/偏空/震荡/多头排列) → 不判冲突
方法: 用各标的入场日**前一交易日**日线MA5/10/20排列复算trend (择时闸用当日盘前快照的昨收MA排列, 与DA一致)。
只读: 本地Futu缓存/K线, 不写任何生产文件。
"""
import sqlite3, json, sys
sys.path.insert(0, '/root/.hermes/skills/deep-stock-analysis/scripts')

# 三笔大亏 + 盈利腿对照 (NOK/FPS/ASML/STX/WDC = TRAIL_LOCK 盈利)
cases = [
    ('US.QCOM','2026-09-08',-227.50,'LONG','SL_INITIAL'),
    ('US.UBER','2026-09-11',-99.37,'LONG','tighten'),
    ('US.RIVN','2026-09-21',-319.55,'LONG','OVERNIGHT_GAP_STOP'),
    ('US.NOK','2026-09-15',132.36,'LONG','SL_TRAIL_LOCK'),
    ('US.FPS','2026-09-15',128.55,'LONG','SL_TRAIL_LOCK'),
    ('US.ASML','2026-09-15',45.93,'LONG','SL_TRAIL_LOCK'),
    ('US.STX','2026-09-09',44.15,'LONG','SL_TRAIL_LOCK'),
    ('US.WDC','2026-09-21',42.35,'LONG','SL_TRAIL_LOCK'),
]

import data_collector
def trend_from_kline(sym, entry_date):
    """复算 DA 择时闸的 trend: 用 entry_date 当日盘前可见的昨收MA排列。
    DA 口径: kline 末根=最近交易日(昨收), 算 MA5/10/20。"""
    kl = data_collector.collect_kline(sym, '1d', 60)
    if not kl:
        return 'no_kline', None
    # 取 entry_date 之前(不含当日)的日线 — 盘前可见
    bars = [b for b in kl if str(b.get('time_key') or b.get('date') or '')[:10] < entry_date]
    if len(bars) < 20:
        return 'insufficient', len(bars)
    closes = [float(b['close']) for b in bars[-20:]]
    ma5 = sum(closes[-5:])/5
    ma10 = sum(closes[-10:])/10
    ma20 = sum(closes[-20:])/20
    # DA 趋势分档(deep_analyzer): 多头排列=ma5>ma10>ma20且价>ma5; 空头排列=ma5<ma10<ma20且价<ma5
    last = closes[-1]
    if ma5 > ma10 > ma20 and last > ma5:
        return '多头排列', (round(ma5,2),round(ma10,2),round(ma20,2),round(last,2))
    if ma5 < ma10 < ma20 and last < ma5:
        return '空头排列', (round(ma5,2),round(ma10,2),round(ma20,2),round(last,2))
    return '其他(不判冲突)', (round(ma5,2),round(ma10,2),round(ma20,2),round(last,2))

print(f"{'标的':8s} {'入场日':11s} {'实际盈亏':>9s} {'退出':18s} {'复算trend':14s} {'MA5/10/20/价':28s} {'择时闸判定'}")
print('-'*130)
block_wins=0; block_losses=0; blocked_n=0; keep_n=0
for sym,d,pnl,dirn,er in cases:
    trend,mas = trend_from_kline(sym,d)
    if trend=='空头排列':
        verdict='🔴SKIP(破位硬买拦截)'; blocked_n+=1
        if pnl>0: block_wins+=1
        else: block_losses+=1
    else:
        verdict='放行'; keep_n+=1
    print(f"{sym.replace('US.',''):8s} {d:11s} {pnl:9.2f} {er:18s} {trend:14s} {str(mas):28s} {verdict}")

print(f"\n=== 反事实汇总 ===")
print(f"会被择时闸拦(空头排列+BUY→SKIP): {blocked_n} 笔")
print(f"  其中亏损笔(拦对): {block_losses}  盈利笔(误杀): {block_wins}")
print(f"放行: {keep_n} 笔")
tail3 = sum(p for _,_,p,_,_ in cases[:3])
print(f"\n三笔大亏合计: {tail3:.2f}")
blocked_tail = sum(p for s,d,p,dr,e in cases[:3] if trend_from_kline(s,d)[0]=='空头排列')
print(f"其中被择时闸拦到的: {blocked_tail:.2f}")
