"""2026-08-20 净口径回填: playbook_instructions 的 pnl/outcome/pnl_pct 按净重判.

只动 sim 数据 (trade_mode 由 trade_records 侧过滤), 只回填能 join 到
trade_records.pnl_net 的行 (decision_id 1:1); join 不上的保持毛口径不动.
先 dry-run 打印 diff, --apply 才写入.
"""
import sqlite3, sys

APPLY = "--apply" in sys.argv
db = sqlite3.connect('/opt/ftseek/FTSeek-US/data/ftseek_us.db')
db.row_factory = sqlite3.Row

# instructions ↔ trade_records 经 decision_id 1:1 (DA- 前缀, playbook 执行通道)
q = """
SELECT i.id, i.symbol, i.date, i.outcome, i.pnl, i.pnl_pct,
       t.realized_pnl, t.pnl_net, t.fees_est, t.entry_price, t.quantity
FROM playbook_instructions i
JOIN trade_records t ON t.decision_id = i.decision_id
WHERE i.outcome IN ('win','loss')
  AND i.decision_id IS NOT NULL AND i.decision_id != ''
  AND t.pnl_net IS NOT NULL
"""
rows = db.execute(q).fetchall()
print(f"joinable instructions: {len(rows)}")

flips, diffs = [], 0
for r in rows:
    net = round(float(r['pnl_net']), 2)
    new_outcome = 'win' if net > 0 else 'loss'
    old_outcome = r['outcome']
    # 净 pnl_pct: 毛 ratio - fees/notional
    new_pct = r['pnl_pct']
    try:
        notional = float(r['entry_price'] or 0) * float(r['quantity'] or 0)
        if notional > 0 and r['fees_est'] is not None:
            new_pct = round(float(r['pnl_pct'] or 0) - float(r['fees_est']) / notional, 4)
    except Exception:
        pass
    if new_outcome != old_outcome or abs((r['pnl'] or 0) - net) > 0.011:
        flips.append((r['id'], r['symbol'], r['date'], old_outcome, new_outcome,
                      r['pnl'], net))
        diffs += 1

print(f"需改判/改值: {diffs} 笔")
w2l = sum(1 for f in flips if f[3] == 'win' and f[4] == 'loss')
l2w = sum(1 for f in flips if f[3] == 'loss' and f[4] == 'win')
print(f"  win→loss (假盈利现形): {w2l} | loss→win: {l2w} | 仅pnl值改: {diffs - w2l - l2w}")
for f in flips[:15]:
    print(f"  id={f[0]} {f[1]} {f[2]} {f[3]}→{f[4]} pnl {f[5]}→{f[6]}")
if len(flips) > 15:
    print(f"  ... 还有 {len(flips)-15} 笔")

if not APPLY:
    print("\nDRY-RUN (加 --apply 写入)")
    sys.exit(0)

n = 0
for r in rows:
    net = round(float(r['pnl_net']), 2)
    new_outcome = 'win' if net > 0 else 'loss'
    new_pct = r['pnl_pct']
    try:
        notional = float(r['entry_price'] or 0) * float(r['quantity'] or 0)
        if notional > 0 and r['fees_est'] is not None:
            new_pct = round(float(r['pnl_pct'] or 0) - float(r['fees_est']) / notional, 4)
    except Exception:
        pass
    db.execute("UPDATE playbook_instructions SET outcome=?, pnl=?, pnl_pct=? WHERE id=?",
               (new_outcome, net, new_pct, r['id']))
    n += 1
db.commit()
print(f"\n已写入 {n} 行 (outcome/pnl/pnl_pct 净口径)")
