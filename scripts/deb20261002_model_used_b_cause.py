"""B 组 (n=122) 定性收口: SKIP 占位 vs 真指令行; 为什么同期 140 行写了真模型名

关键分岔:
  · 若 B 组几乎全是 direction=SKIP ⇒ 服务侧写 SKIP 行时不写 model_used = 设计,
    但 db.py:1004 那类占位行是另一批 (skip_reason='非当前通道候选'), 需区分
  · 若含真指令行 (BUY/SELL_SHORT) ⇒ 活的遥测缺口, scorecard 按模型分层失真
对照: 9月起 DA- 且 model_used='qwen3.7-max' 的 n=140 — 与 B 组差在哪 (direction? 写入路径? 时段?)
"""
import json
import sqlite3
from app.storage.db_pool import get_db_path
from collections import defaultdict

c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

# ── B 组 direction × skip_reason 分布 ────────────────────────
B = c.execute("""
  SELECT id, decision_id, date, symbol, direction, model_used, confidence,
         skip_reason, created_at, entry_price, atr_pct
  FROM playbook_instructions
  WHERE model_used='deterministic' AND decision_id LIKE 'DA-%'
    AND substr(decision_id,4,8) = replace(substr(date,1,10),'-','')
    AND date <= '2026-10-01'
""").fetchall()
print(f"B 组 (did日期==date列) n={len(B)}")
by_dir = defaultdict(list)
for r in B:
    by_dir[r["direction"]].append(r)
print(f"\n=== direction 分布 ===")
for d, g in sorted(by_dir.items(), key=lambda x: -len(x[1])):
    sr = defaultdict(int)
    for r in g:
        sr[(r["skip_reason"] or "(空)")[:26]] += 1
    print(f"  {d:<12} n={len(g):<4} skip_reason: {dict(sorted(sr.items(), key=lambda x:-x[1])[:4])}")

# ── 对照: 同期 DA- 写了真模型名的行 ─────────────────────────
Q = c.execute("""
  SELECT id, decision_id, date, symbol, direction, model_used, confidence, skip_reason
  FROM playbook_instructions
  WHERE decision_id LIKE 'DA-%' AND date>='2026-08-01' AND date < '2026-10-02 00:00:00' AND date<='2026-10-01'
    AND model_used IS NOT NULL AND model_used!='deterministic'
""").fetchall()
print(f"\n=== 对照 Q 组 (DA- 且写真模型名) n={len(Q)} ===")
qd = defaultdict(int)
for r in Q:
    qd[r["direction"]] += 1
print(f"  direction 分布: {dict(sorted(qd.items(), key=lambda x:-x[1]))}")
print(f"  model_used 取值: {dict(defaultdict(int, {r['model_used']: 0 for r in Q})) or {r['model_used'] for r in Q}}")

# ── 同日同标的: B 组与 Q 组是否共存 (⇒ 两条写入路径) ─────────
bkey = {(r["date"][:10], r["symbol"]) for r in B}
qkey = {(r["date"][:10], r["symbol"]) for r in Q}
both = bkey & qkey
print(f"\n=== 同日同标的共存检查 ===")
print(f"  B 组键 n={len(bkey)}  Q 组键 n={len(qkey)}  交集 n={len(both)}")
if both:
    s = sorted(both)[:5]
    print(f"  交集样例 (同日同标的既有 deterministic 行又有真模型名行 ⇒ 两条写入路径):")
    for d, sym in s:
        br = [r for r in B if r["date"][:10] == d and r["symbol"] == sym]
        qr = [r for r in Q if r["date"][:10] == d and r["symbol"] == sym]
        print(f"    {d} {sym:<10} B: dir={br[0]['direction']:<10} conf={br[0]['confidence']} "
              f"created={str(br[0]['created_at'])[:19]}")
        print(f"    {'':<21} Q: dir={qr[0]['direction']:<10} conf={qr[0]['confidence']} "
              f"model={qr[0]['model_used']}")

# ── B 组真指令行 (非 SKIP) 的成交与结局 ─────────────────────
real_B = [r for r in B if r["direction"] != "SKIP"]
print(f"\n=== B 组中真指令行 (direction != SKIP) n={len(real_B)} ===")
ids = [r["id"] for r in real_B]
if ids:
    ph = ",".join("?" * len(ids))
    tr = c.execute(f"""
      SELECT t.id, t.symbol, t.direction, t.entry_time, t.pnl_net, COALESCE(t.mfe_pct,0) mfe,
             t.decision_id
      FROM trade_records t
      WHERE t.decision_id IN (SELECT decision_id FROM playbook_instructions WHERE id IN ({ph}))
        AND t.entry_time>='2026-06-27' AND t.entry_time < '2026-10-02 00:00:00' AND t.exit_time IS NOT NULL AND t.pnl_net IS NOT NULL
    """, ids).fetchall()
    n = len(tr)
    d = sum(1 for r in tr if r["mfe"] < 0.3)
    net = sum(r["pnl_net"] for r in tr)
    print(f"  已成交结算: n={n} 死率={round(100*d/n,1) if n else None}% 净={net:+.1f}")
    print(f"  ⇒ 这 {n} 笔是真·DA(LLM) 产出被错标 deterministic")
else:
    print("  B 组无真指令行 ⇒ 全部是 SKIP 行")

# ── SKIP 行的 entry_price / atr_pct 是否有值 (判是否占位) ────
sk = [r for r in B if r["direction"] == "SKIP"]
if sk:
    ep = sum(1 for r in sk if r["entry_price"] is not None)
    at = sum(1 for r in sk if r["atr_pct"] is not None)
    cf = defaultdict(int)
    for r in sk:
        cf[r["confidence"]] += 1
    print(f"\n=== B 组 SKIP 行 n={len(sk)} 特征 ===")
    print(f"  有 entry_price: {ep}/{len(sk)}   有 atr_pct: {at}/{len(sk)}")
    print(f"  confidence 分布: {dict(sorted(cf.items(), key=lambda x:-x[1])[:8])}")
    print(f"  ⇒ 若 conf 有真值 (70/50…) 且有 entry_price, 说明是【DA 真实分析后被门拦下】的行,")
    print(f"     不是 db.py:1004 那类反事实占位行 (占位行无 entry_price/永不结算)")
