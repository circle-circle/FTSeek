#!/usr/bin/env python3
"""审计: EOD 豁免水下仓持夜 (W_WEEK / reentry 首夜) 的样本表现。
数据源 = 权威台账 trade_records.metadata_json.overnight_arm (RFC-067, 每个被豁免的仓一条)。
只读, 零副作用。"""
import json
import sqlite3
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
conn = sqlite3.connect(f"file:{ROOT}/data/ftseek_us.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute("""
    SELECT id, symbol, direction, entry_time, exit_time, exit_reason,
           entry_price, exit_price, quantity, pnl_net, realized_pnl, fees_est,
           source_type, strategy_id, metadata_json
    FROM trade_records
""").fetchall()

out = []
for r in rows:
    try:
        m = json.loads(r["metadata_json"] or "{}")
    except Exception:
        m = {}
    oa = m.get("overnight_arm")
    if not oa:
        continue
    out.append({
        "id": r["id"], "symbol": r["symbol"], "dir": r["direction"],
        "entry_time": r["entry_time"], "exit_time": r["exit_time"],
        "exit_reason": r["exit_reason"], "entry": r["entry_price"],
        "exit": r["exit_price"], "qty": r["quantity"],
        "pnl_net": r["pnl_net"], "src": r["source_type"],
        "arm": oa,
    })

print(f"overnight_arm 台账行数 = {len(out)}")
print(f"{'id':>5} {'symbol':>9} {'dir':>5} {'arm_src':>18} {'night':>5} {'uw_net%':>8} "
      f"{'marked_at':>22} {'exit_time':>22} {'exit_reason':>18} {'pnl_net':>9}")
for o in sorted(out, key=lambda x: str(x["arm"].get("marked_at_et") or "")):
    a = o["arm"]
    print(f"{o['id']:>5} {str(o['symbol']):>9} {str(o['dir']):>5} "
          f"{str(a.get('source')):>18} {str(a.get('night_n')):>5} "
          f"{str(a.get('underwater_pct_net')):>8} {str(a.get('marked_at_et')):>22} "
          f"{str(o['exit_time'])[:22]:>22} {str(o['exit_reason'])[:18]:>18} "
          f"{o['pnl_net'] if o['pnl_net'] is not None else '-':>9}")

Path("/tmp/overnight_arm_rows.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
print("\n落盘 /tmp/overnight_arm_rows.json")