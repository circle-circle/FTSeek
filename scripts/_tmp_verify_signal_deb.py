"""DEB-20260808-signal-source-strategy 证据复算 (Qoder 核查)."""
import json
import sqlite3
from collections import defaultdict

c = sqlite3.connect('data/ftseek_us.db')
c.row_factory = sqlite3.Row

print("=== 1. 时段分桶复算 (§3.3, 7-8月全部 playbook 成交) ===")
tb = defaultdict(lambda: [0, 0.0])
for r in c.execute("""
    SELECT entry_time, pnl_net FROM trade_records
    WHERE trade_date >= '2026-07-01' AND source_type LIKE 'PLAYBOOK%' AND exit_price > 0
"""):
    hm = str(r['entry_time'])[11:16]
    if '09:30' <= hm < '09:45':
        b = '09:30-09:45'
    elif '09:45' <= hm < '10:00':
        b = '09:45-10:00'
    elif '10:00' <= hm < '10:15':
        b = '10:00-10:15'
    elif '10:15' <= hm < '10:45':
        b = '10:15-10:45'
    else:
        b = '其他时段'
    tb[b][0] += 1
    tb[b][1] += float(r['pnl_net'] or 0)
for k in sorted(tb):
    n, net = tb[k]
    print(f"  {k}: n={n:>3} 净{net:+9.2f} 笔均{net/n:+6.2f}")

print("\n=== 2. score 分档 (§3.1, 8月 DA) ===")
try:
    rows = c.execute("""
        SELECT confidence, pnl FROM playbook_instructions
        WHERE date >= '2026-08-01' AND executed = 1 AND pnl IS NOT NULL
    """).fetchall()
    print(f"  指令表 pnl 字段样本: {len(rows)}")
except Exception as e:
    print(f"  指令表无 pnl 列, 需 join: {e}")

print("\n=== 3. 赢家/失败池 ATR 检验 (§3.6) ===")
c2 = sqlite3.connect('data/ftseek_us.db')
c2.row_factory = sqlite3.Row
pool = defaultdict(float)
for r in c2.execute("""
    SELECT i.symbol, i.atr_pct, t.pnl_net FROM trade_records t
    JOIN playbook_instructions i ON i.date = t.trade_date AND i.symbol = t.symbol
    WHERE t.trade_date >= '2026-08-01' AND t.source_type LIKE 'PLAYBOOK%' AND t.exit_price > 0
      AND i.atr_pct IS NOT NULL
"""):
    pool[(r['symbol'], round(r['atr_pct'], 1))] += float(r['pnl_net'] or 0)
for (sym, atr), net in sorted(pool.items(), key=lambda x: x[1]):
    print(f"  {sym:>10} ATR={atr:>4}%  净{net:+8.2f}")
