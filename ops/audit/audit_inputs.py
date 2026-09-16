# -*- coding: utf-8 -*-
"""补验: ①DA 生成时点与所见价格 vs 当日真实价格区间 ②re-price 机械公式来源."""
import json
import sqlite3
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))
db = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

print("=" * 78)
print("① DA 输入时点 & 所见价格 (9/15 checkpoint, 有 bar 的标的)")
print("=" * 78)
import os
BAR = "/opt/ftseek/FTSeek-US/data/replay_bars"
for f in sorted(os.listdir("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-09-15")):
    if not f.endswith(".json") or ".raw." in f:
        continue
    sym = f[:-5].replace("_", ".")
    p = f"/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-09-15/{f}"
    try:
        d = json.load(open(p))
    except Exception:
        continue
    lo = d.get("_llm_output") or {}
    ep = lo.get("execution_plan") or {}
    ts = d.get("timestamp")
    snap = lo.get("_snapshot") or d.get("snapshot") or {}
    seen = snap.get("price") if isinstance(snap, dict) else None
    when = datetime.fromtimestamp(ts, ET).strftime("%H:%M ET") if ts else "?"
    # 当日 1min bar 真实区间
    bp = f"{BAR}/{sym.replace('.','_')}_2026-09-15.json"
    rng = ""
    pe = ep.get("entry_price")
    reach = ""
    if os.path.exists(bp):
        bars = json.load(open(bp))
        hi = max(float(b[2]) for b in bars)
        lo_ = min(float(b[3]) for b in bars)
        op = float(bars[0][1])
        rng = f"日内 开{op:.2f} 高{hi:.2f} 低{lo_:.2f}"
        if pe and isinstance(pe, (int, float)):
            hit = "✓可成交" if (lo_ <= pe <= hi) else "✗全天未触及"
            reach = f"planned_entry={pe} → {hit}"
    print(f"{sym:11s} DA生成{when} 所见价={seen} | {rng} | {reach}")

print()
print("=" * 78)
print("② re-price 机械公式 (vol_bucket → sl_pct / tp_mult)")
print("=" * 78)
rows = db.execute("""SELECT metadata_json FROM playbook_instructions
                     WHERE date>='2026-09-13' AND metadata_json LIKE '%reprice_audit%'""").fetchall()
import collections
c = collections.Counter()
for r in rows:
    ra = (json.loads(r["metadata_json"]) or {}).get("reprice_audit") or {}
    c[(ra.get("vol_bucket"), ra.get("reprice_sl_pct"), ra.get("reprice_tp_mult"))] += 1
for k, v in sorted(c.items(), key=lambda x: -x[1]):
    print(f"  vol_bucket={k[0]:8s} sl_pct={k[1]}% tp_mult={k[2]} → {v} 笔")
print("  ⇒ SL 距离 = 固定 ATR%(bucket), TP = SL距×1.5, 与 DA 结构位/RR 无关")

print()
print("=" * 78)
print("③ TAKE_PROFIT 触发史 (DA 结构 TP 是否曾被用到)")
print("=" * 78)
r = db.execute("""SELECT COUNT(*) c, MIN(entry_time) mn FROM trade_records
                  WHERE source_type='PLAYBOOK_SIM' AND exit_reason='TAKE_PROFIT'""").fetchone()
print(f"  TAKE_PROFIT 全史: {r['c']} 笔 (最早 {r['mn']})")
r2 = db.execute("""SELECT COUNT(*) c FROM trade_records WHERE source_type='PLAYBOOK_SIM'
                   AND exit_time IS NOT NULL""").fetchone()
print(f"  已平仓总数: {r2['c']} ⇒ TP 触发率 {r['c']/max(r2['c'],1)*100:.2f}%")
r3 = db.execute("""SELECT COUNT(*) c FROM trade_records WHERE source_type='PLAYBOOK_SIM'
                   AND exit_reason='SL_TRAIL_LOCK'""").fetchone()
print(f"  对照 SL_TRAIL_LOCK: {r3['c']} 笔 ({r3['c']/max(r2['c'],1)*100:.0f}%) ⇒ 锁利远早于 TP 线")
