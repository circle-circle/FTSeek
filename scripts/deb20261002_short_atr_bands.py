"""第十三轮 (决定性): SHORT×ATR<3% 与 low_atr_gate 的关系

若该腿 21 笔都在门上线日 (2026-07-24, DEB-20260723/24) 之后 ⇒ 门没拦住 ⇒
   口径不同 (metadata.atr 盘前采集 vs inst.atr_pct 止损口径含VIX缩放/三层底限)
   ⇒ 该腿**现在活着**, 可落地
若都在 7/24 之前 ⇒ 门上线后该腿已死 ⇒ 样本饿死 (T10-9), 不可落地
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

rows = c.execute(
    "SELECT symbol, direction, entry_time, entry_price, quantity, decision_id, "
    " metadata_json, mfe_pct, pnl_net, realized_pnl, fees_est "
    "FROM trade_records WHERE entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND exit_time IS NOT NULL "
    " AND pnl_net IS NOT NULL").fetchall()

recs = []
for r in rows:
    try:
        md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
    except Exception:
        md = {}
    gap = md.get("entry_gap") or {}
    recs.append({
        "sym": r["symbol"], "dir": r["direction"], "t": r["entry_time"] or "",
        "price": r["entry_price"] or 0, "atr": md.get("atr"),
        "gap": gap.get("aligned_gap_pct", gap.get("gap_pct")),
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0,
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
    })

GATE_LIVE = "2026-07-24"
sh_lo = [x for x in recs if x["dir"] == "SHORT" and x["atr"] not in (None, 0) and x["atr"] < 3]
print(f"=== SHORT × ATR<3% (百分数口径) n={len(sh_lo)} ===")
pre = [x for x in sh_lo if x["t"][:10] < GATE_LIVE]
post = [x for x in sh_lo if x["t"][:10] >= GATE_LIVE]
print(f"  门上线 ({GATE_LIVE}) 前: {len(pre)} 笔 净={sum(x['net'] for x in pre):+.1f}")
print(f"  门上线后:            {len(post)} 笔 净={sum(x['net'] for x in post):+.1f}")
print("\n  逐笔 (门上线后):")
for x in sorted(post, key=lambda z: z["t"]):
    print(f"   {x['t'][:16]} {x['sym']:10s} atr={x['atr']:6.2f}% gap={x['gap']} "
          f"价={x['price']:8.2f} 净={x['net']:+8.1f} 死={x['dead']}")
print("\n  逐笔 (门上线前):")
for x in sorted(pre, key=lambda z: z["t"]):
    print(f"   {x['t'][:16]} {x['sym']:10s} atr={x['atr']:6.2f}% 净={x['net']:+8.1f}")

print("\n=== 门上线后 SHORT 全量按 ATR 档 (看门是否真在拦低波空单) ===")
post_sh = [x for x in recs if x["dir"] == "SHORT" and x["t"][:10] >= GATE_LIVE]
print(f"  门后 SHORT n={len(post_sh)} (atr 有效 {sum(1 for x in post_sh if x['atr'] not in (None,0))})")
for lo, hi, lab in ((0, 3, "ATR<3%"), (3, 5, "3-5%"), (5, 8, "5-8%"), (8, 1e9, ">=8%")):
    s2 = [x for x in post_sh if x["atr"] not in (None, 0) and lo <= x["atr"] < hi]
    if not s2:
        print(f"   {lab:10s} n=0")
        continue
    net = sum(x["net"] for x in s2)
    print(f"   {lab:10s} n={len(s2):3d} 净={net:+8.1f} 均={net/len(s2):+7.2f} "
          f"死率={sum(x['dead'] for x in s2)/len(s2)*100:3.0f}% "
          f"日期={min(x['t'][:10] for x in s2)}→{max(x['t'][:10] for x in s2)}")

print("\n=== 门上线后 LONG × ATR<3% (门的原始设计对象) ===")
post_lg = [x for x in recs if x["dir"] == "LONG" and x["t"][:10] >= GATE_LIVE
           and x["atr"] not in (None, 0) and x["atr"] < 3]
if post_lg:
    net = sum(x["net"] for x in post_lg)
    print(f"  n={len(post_lg)} 净={net:+.1f} 均={net/len(post_lg):+.2f} "
          f"死率={sum(x['dead'] for x in post_lg)/len(post_lg)*100:.0f}%")
    for x in sorted(post_lg, key=lambda z: z["net"])[:6]:
        print(f"   {x['t'][:16]} {x['sym']:10s} atr={x['atr']:5.2f}% 净={x['net']:+8.1f} 死={x['dead']}")
else:
    print("  n=0 ⇒ 门对 LONG 生效 (或该档无 LONG 候选)")

print("\n=== 结论判定 ===")
if post and len(post) >= 5:
    net_post = sum(x["net"] for x in post)
    print(f"  ✅ 门上线后该腿仍有 {len(post)} 笔成交, 净={net_post:+.1f}")
    print("  ⇒ 门**没有**拦住它 ⇒ metadata.atr (盘前采集, 百分数) 与 inst.atr_pct")
    print("    (止损口径: 含VIX缩放/三层底限/分钟兜底, 比例) 不是同一个值")
    print("  ⇒ 该腿现在活着, 不是样本饿死")
elif not post:
    print("  🔴 门上线后该腿 0 笔 ⇒ 样本饿死 (T10-9), 历史读数不可外推")
else:
    print(f"  ⚠️ 门后仅 {len(post)} 笔 ⇒ n 不足, 只能登记")
c.close()
