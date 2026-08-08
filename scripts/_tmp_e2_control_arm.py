"""E2 网格缺失对照臂 + MAE 数据有效性检验 (Qoder 独立复核 DEB-20260808 §十二)."""
import sqlite3

c = sqlite3.connect('data/ftseek_us.db')
c.row_factory = sqlite3.Row

trades = [dict(r) for r in c.execute("""
  SELECT trade_date, symbol, quantity, entry_price, pnl_net, pnl_pct,
         mae_pct, mfe_pct, exit_reason
  FROM trade_records
  WHERE source_type LIKE 'PLAYBOOK%' AND exit_price > 0
    AND trade_date BETWEEN '2026-07-13' AND '2026-08-07'
    AND mae_pct IS NOT NULL AND quantity > 0 AND entry_price > 0
""")]

# 指令 SL% (缺失用中位数 5%)
sl_map = {}
for r in c.execute("""SELECT date, symbol, entry_price, stop_loss FROM playbook_instructions
                      WHERE stop_loss > 0 AND entry_price > 0"""):
    sl_map[(r['date'], r['symbol'])] = abs(float(r['entry_price']) - float(r['stop_loss'])) / float(r['entry_price']) * 100

# ── 检验1: MAE 有效性 — 亏损单的 MAE 应 >= |实际亏损%| ──
bad = 0
checked = 0
for t in trades:
    p = float(t['pnl_pct'] or 0) * 100  # 小数口径 -> %
    if p < 0:
        checked += 1
        if float(t['mae_pct']) < abs(p) - 0.05:   # 容 0.05pp 噪声
            bad += 1
print(f"[检验1] MAE 有效性: 亏损单 {checked} 笔, 其中 MAE < |亏损%| 的 {bad} 笔 "
      f"({bad/checked*100:.0f}% 不自洽 → MAE 低估)")

# ── 检验2: α=infinity 对照臂 (全部单施紧SL, 不看动能) ──
BETA = 0.3
base = sum(float(t['pnl_net'] or 0) for t in trades)
model = 0.0
killed = kept = 0
kept_winner_pnl = 0.0
for t in trades:
    notional = float(t['quantity']) * float(t['entry_price'])
    sl_pct = sl_map.get((t['trade_date'], t['symbol']), 5.0)
    tight = max(0.5, BETA * sl_pct)
    actual = float(t['pnl_net'] or 0)
    if float(t['mae_pct']) > tight:
        model += -(tight + 0.1) / 100 * notional
        killed += 1
    else:
        model += actual
        kept += 1
        if actual > 0:
            kept_winner_pnl += actual
print(f"[检验2] α=inf 对照臂 (beta={BETA}, 全部 {len(trades)} 笔施紧SL, 零动能判别):")
print(f"  基线净 {base:+.2f} -> 模拟净 {model:+.2f} | 改善 {model - base:+.2f}")
print(f"  被紧SL打掉 {killed} 笔 / 保留实际 {kept} 笔 (其中赢家保留 {kept_winner_pnl:+.2f})")
print(f"  注: 样本 {len(trades)} 笔 (DEB 网格用 136 笔 replay 子集, 口径不同, 仅看方向)")
