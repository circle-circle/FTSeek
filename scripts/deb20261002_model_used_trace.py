"""deterministic 通道溯源: model_used 标签真伪拆解 (规则 54.3)

前科: `pipeline.py:3227` = `getattr(inst,'model_used',None) or "deterministic"`
      `db.py:1004`  SKIP 占位行硬编码 model_used='deterministic'
      `db.py:41`   DA 直写被陈旧批覆盖 ⇒ "model_used 同步退化为 deterministic"
⇒ 标签可为三种成因, 必须用 decision_id 前缀 (写入侧硬事实) 交叉验证真实归属。

判据 (读数前写死):
  T1  deterministic 组按 decision_id 前缀拆分: DA-/universe_expansion/intraday_reentry/其他
  T2  真·确定性链 = 前缀与标签一致者 (非 DA-/非 UE-) + 退化行单列
  T3  死单率按「真归属」重算; 若真确定性组 n<30 ⇒ 原 14.0% 读数降级为「不可判定」
  T4  逐月复核 (防单月主导)
"""
import sqlite3
import statistics as st
from collections import defaultdict

# 两表都有 exit_time/entry_time ⇒ 必须逐列加表前缀, 否则 ambiguous column
POP = ("t.entry_time>='2026-06-27' AND t.exit_time IS NOT NULL AND t.pnl_net IS NOT NULL")
c = sqlite3.connect("file:data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
J = ("FROM trade_records t JOIN playbook_instructions i ON i.id = ("
     "  SELECT MIN(i2.id) FROM playbook_instructions i2 WHERE i2.decision_id = t.decision_id) "
     f"WHERE {POP}")

rows = c.execute("SELECT t.id tid, t.symbol, t.direction, t.entry_time, t.pnl_net, "
                 "COALESCE(t.mfe_pct,0) mfe, i.model_used, i.decision_id, i.direction idir, "
                 "i.skip_reason, i.confidence " + J).fetchall()
print(f"总样本 n={len(rows)} (COUNT DISTINCT tid={len({r['tid'] for r in rows})})")

DEAD = lambda r: r["mfe"] < 0.3


def prefix(did):
    if not did:
        return "(空)"
    if did.startswith("DA-"):
        return "DA-"
    if did.startswith("universe_expansion"):
        return "UE"
    if did.startswith("intraday_reentry"):
        return "reentry"
    if did.startswith("ext") or "extended" in did:
        return "EXT"
    return did.split(":")[0].split("-")[0][:14]


def rate(g):
    n = len(g)
    d = sum(1 for r in g if DEAD(r))
    return n, d, (100.0 * d / n if n else None)


# ── T1: deterministic 组按 decision_id 前缀拆 ────────────────
det = [r for r in rows if str(r["model_used"] or "") == "deterministic"]
print(f"\n=== T1: model_used='deterministic' 共 n={len(det)} — 按 decision_id 前缀拆真归属 ===")
by_p = defaultdict(list)
for r in det:
    by_p[prefix(r["decision_id"])].append(r)
for p, g in sorted(by_p.items(), key=lambda x: -len(x[1])):
    n, d, dr = rate(g)
    net = sum(x["pnl_net"] for x in g)
    flag = "🔴 标签与前缀矛盾 = 退化污染" if p in ("DA-", "UE") else "✅ 前缀一致"
    print(f"  {p:<16} n={n:<4} 死={d:<3} 死率={dr if dr is None else round(dr,1)}%  "
          f"净={net:+8.1f}  {flag}")

# ── T2: 真确定性 vs 退化 ─────────────────────────────────────
TRUE_P = {p for p in by_p if p not in ("DA-", "UE")}
true_det = [r for r in det if prefix(r["decision_id"]) in TRUE_P]
degraded = [r for r in det if prefix(r["decision_id"]) in ("DA-", "UE")]
print(f"\n=== T2: 拆分 ===")
print(f"  真·确定性链 (前缀一致)  n={len(true_det)}")
print(f"  退化污染 (DA-/UE- 却标 deterministic) n={len(degraded)}")
for lbl, g in (("真确定性", true_det), ("退化污染", degraded)):
    n, d, dr = rate(g)
    net = sum(x["pnl_net"] for x in g)
    if g:
        mo = sorted({x["entry_time"][:7] for x in g})
        print(f"  {lbl:<8} n={n:<4} 死率={round(dr,1) if dr is not None else None}%  "
              f"净={net:+8.1f}  月份={mo}")

# ── T3: 重算死单率 + 与 LLM 组对照 ───────────────────────────
llm = [r for r in rows if str(r["model_used"] or "") not in ("deterministic", "", "None")
       and "cache" not in str(r["model_used"] or "")]
print(f"\n=== T3: 按真归属重算 (原判据: 真确定性组 n<30 ⇒ 降级不可判定) ===")
for lbl, g in (("真·确定性", true_det), ("退化污染", degraded), ("LLM 通道", llm)):
    n, d, dr = rate(g)
    net = sum(x["pnl_net"] for x in g)
    ok = "✅ n≥30" if n >= 30 else "🔴 n<30 不可判定"
    print(f"  {lbl:<10} n={n:<4} 死率={round(dr,1) if dr is not None else None}%  "
          f"净={net:+9.1f}  {ok}")

# 原读数复核 (标签口径, 即我上一条报的)
n, d, dr = rate(det)
print(f"\n  [原标签口径] deterministic n={n} 死率={round(dr,1)}% ⇒ 我上一条报的 14.0%")
print(f"  ⇒ 该数字含 {len(degraded)} 笔退化污染行"
      f" ({100*len(degraded)/len(det):.0f}% of 组)")

# ── T4: 逐月复核 ─────────────────────────────────────────────
print(f"\n=== T4: 逐月 (真确定性 vs LLM) ===")
for m in ("2026-07", "2026-08", "2026-09", "2026-10"):
    a = [r for r in true_det if r["entry_time"][:7] == m]
    b = [r for r in llm if r["entry_time"][:7] == m]
    na, da_, ra = rate(a)
    nb, db_, rb = rate(b)
    if na and nb:
        print(f"  {m}: 真确定性 n={na:<3} 死率={round(ra,1)}%  |  LLM n={nb:<3} "
              f"死率={round(rb,1)}%  |  Δ={round(ra-rb,1):+}pp")
    else:
        print(f"  {m}: 真确定性 n={na} / LLM n={nb} ⇒ 样本不足")

# ── 附: deterministic 组的 confidence 分布 (验是否真是无LLM链) ──
print(f"\n=== 附: 真确定性组 confidence 分布 (无LLM链应有固定/离散签名) ===")
cf = defaultdict(int)
for r in true_det:
    cf[r["confidence"]] += 1
print(f"  取值分布: {dict(sorted(cf.items(), key=lambda x: -x[1])[:10])}")
print(f"  distinct confidence 值数 = {len(cf)} (真确定性链应极少)")
cf2 = defaultdict(int)
for r in llm:
    cf2[r["confidence"]] += 1
print(f"  对照 LLM 组 distinct = {len(cf2)}")
