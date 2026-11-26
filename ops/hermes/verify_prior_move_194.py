"""RFC-194 一次性验收: prior_move 审计字段是否真的落到成交记录里 (只读)。

判定口径:
  - 只看**改后**成交 (datetime(entry_time) >= 2026-11-09 14:10 UTC = 22:10 CST 提交时刻);
  - prior_move 非空 = 记上了; as_of 必须 == 上一交易日 (stale=False);
  - 覆盖 < 100% 或出现 stale ⇒ 明确报出来, 不用"大概正常"糊过去。
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
CHANGE_UTC = "2026-11-09 14:10:00"

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
try:
    rows = conn.execute(
        """select symbol, entry_time, exit_time, realized_pnl, metadata_json
           from trade_records
           where owner_strategy='extended_hours_entry'
             and datetime(entry_time) >= ?
           order by datetime(entry_time)""", (CHANGE_UTC,)).fetchall()
finally:
    conn.close()

if not rows:
    print("【RFC-194 验收】改后暂无 EXT 成交 —— 字段链路未取到样本, 不作\"正常\"结论。")
    raise SystemExit(0)

ok, stale, missing = 0, 0, 0
lines = []
for sym, etime, xtime, pnl, meta in rows:
    try:
        pm = (json.loads(meta or "{}") or {}).get("prior_move")
    except Exception:
        pm = None
    if not pm:
        missing += 1
        lines.append(f"- {sym} {etime[:16]} → prior_move **缺失**")
        continue
    if pm.get("stale"):
        stale += 1
    else:
        ok += 1
    lines.append(
        f"- {sym} {etime[:16]} as_of={pm.get('as_of')} src={pm.get('source')} "
        f"stale={pm.get('stale')} t1={pm.get('t1_ret_pct')}% t5={pm.get('t5_ret_pct')}% "
        f"rv_rank={pm.get('spy_rv20_rank')} pnl={pnl if pnl is not None else '未平'}")

verdict = "✅ 记录正常" if (missing == 0 and stale == 0) else "⚠️ 有问题"
print(f"【RFC-194 验收】{verdict} —— 改后成交 {len(rows)} 笔: "
      f"有效 {ok} / stale {stale} / 缺失 {missing}")
print("\n".join(lines))