# -*- coding: utf-8 -*-
"""全窗量化 (193 笔可配对): re-price 是否把落库 SL 拉过 DA 自己的失效线.

数据源: raw_instruction_json.invalidation_triggers (planned 价位线) +
        raw_instruction_json.planned_stop_loss / 落库 stop_loss.
只统计"价格型"失效触发器 (metric 含 收盘/close/价格 或 value 与 planned_entry 同量级).
"""
import json
import sqlite3

db = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

rows = db.execute(
    """SELECT i.id, i.date, i.symbol, i.direction, i.entry_price, i.stop_loss,
              i.take_profit, i.decision_id, i.raw_instruction_json,
              t.pnl_net, t.exit_reason, t.mfe_pct
       FROM playbook_instructions i
       LEFT JOIN trade_records t ON t.decision_id = i.decision_id
            AND t.source_type = 'PLAYBOOK_SIM'
       WHERE i.date >= '2026-08-15' AND i.direction IN ('BUY','SELL_SHORT')
         AND i.entry_price IS NOT NULL AND i.entry_price > 0""").fetchall()

n = 0
has_inv = 0
crossed = 0
crossed_rows = []
for r in rows:
    try:
        raw = json.loads(r["raw_instruction_json"] or "{}")
    except Exception:
        continue
    pe, ps = raw.get("planned_entry"), raw.get("planned_stop_loss")
    db_e, db_s = r["entry_price"], r["stop_loss"]
    invs = raw.get("invalidation_triggers") or []
    if not all(isinstance(x, (int, float)) and x > 0 for x in (pe, ps, db_e, db_s)):
        continue
    n += 1
    moved = abs(db_e / pe - 1) > 0.002
    short = r["direction"] == "SELL_SHORT"
    # 价格型失效线: value 与 planned_entry 同量级 (0.3x ~ 5x), 排除百分比型 (如 5日涨跌>5.0)
    price_invs = []
    for t in invs:
        v = t.get("value")
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        if 0.3 * pe <= v <= 5.0 * pe:
            price_invs.append((t.get("metric"), t.get("operator"), v))
    if not price_invs:
        continue
    has_inv += 1
    for metric, op, iv in price_invs:
        # SHORT: DA SL 在失效线之上(先给空间), 落库 SL 被拉到失效线之下 = 论点未失效先止损
        if short and op == ">" and ps >= iv > db_s:
            crossed += 1
            crossed_rows.append((r["date"][5:], r["symbol"].split(".")[-1], "SHORT",
                                 f"DA_SL={ps} inv({metric}{op})={iv} 落库SL={db_s}",
                                 f"re-priced={moved}", f"pnl={r['pnl_net']}", r["exit_reason"]))
            break
        if (not short) and op == "<" and ps <= iv < db_s:
            crossed += 1
            crossed_rows.append((r["date"][5:], r["symbol"].split(".")[-1], "LONG",
                                 f"DA_SL={ps} inv({metric}{op})={iv} 落库SL={db_s}",
                                 f"re-priced={moved}", f"pnl={r['pnl_net']}", r["exit_reason"]))
            break

print(f"可配对指令 {n} | 带价格型失效线 {has_inv} | 落库SL 跨过 DA 失效线 {crossed}"
      f" ({crossed/max(has_inv,1)*100:.0f}%)")
for x in crossed_rows[:25]:
    print("  ", " | ".join(str(v) for v in x))
