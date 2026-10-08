#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[DEB-20261007 追问] 盘前计划价 vs 盘中 reprice: 幅度真值 + 是否携带结局信息.

阶段1 = 描述与分组 (只读, 零行情拉取).
数据源优先级:
  A. metadata_json.reprice_audit (reprice_from/reprice_to, 精确, n≈156) — 权威
  B. raw_instruction_json.planned_entry vs DB entry_price (n≈645) — 覆盖广但 entry_price 已被回写
  C. anchor_dev_pct 列 (n≈694) — band 反推锚 vs entry_price, 与 B 同族

纪律 (backtest-rule-replay):
  - 不用 LIMIT 截断求和 (DEB 自身教训)
  - NULL 显式处理, 禁 CASE WHEN NULL>0 静默走 ELSE
  - symbol 归一化 US. 前缀
  - 报 mean + median + 日聚类 t (T18/T39)
  - 结局变量 (mfe/pnl) 只做**结果侧**分析, 禁用于构造入场门 (T19)
"""
from pathlib import Path as _P
_ROOT = str(_P(__file__).resolve().parents[1])
import sqlite3, json, math, statistics as st, collections
from datetime import date as _date

c = sqlite3.connect(f"file:{_ROOT}/data/ftseek_us.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row


def _sym(s):
    s = str(s or "").strip()
    return s if s.startswith("US.") else ("US." + s if s else s)


def day_t(groups):
    """日聚类 t: 先按日聚合成日均值, 再对日序列算 t (同日样本共享市场环境, 指令级 iid 不成立)."""
    byd = collections.defaultdict(list)
    for d, v in groups:
        if v is not None:
            byd[d].append(v)
    if len(byd) < 3:
        return None, len(byd), None
    ds = [st.mean(v) for v in byd.values()]
    m = st.mean(ds)
    sd = st.stdev(ds) if len(ds) > 1 else 0.0
    t = m / (sd / math.sqrt(len(ds))) if sd > 1e-12 else None
    return t, len(byd), m


# ── 1) 取全部指令 (禁 LIMIT) ──
rows = c.execute("""
    SELECT id, date, symbol, direction d, entry_price e, entry_low lo, entry_high hi,
           execution_price xp, executed x, triggered t, skip_reason sr,
           raw_instruction_json raw, metadata_json md, anchor_dev_pct ad, atr_pct atr,
           vol_bucket vb, confidence conf, decision_id did, execution_time xt,
           mfe_analysis, exit_reason, pnl_pct
    FROM playbook_instructions ORDER BY id
""").fetchall()
print(f"全量指令 n={len(rows)}  (禁 LIMIT, 与 DEB E 表口径一致)")

A = []   # reprice_audit 人群 (权威)
B = []   # planned_entry 人群
n_bad_ra = 0
for r in rows:
    try:
        md = json.loads(r["md"]) if r["md"] else {}
    except Exception:
        md = {}
    if not isinstance(md, dict):
        md = {}
    ra = md.get("reprice_audit") or {}
    if isinstance(ra, dict) and ra.get("reprice_from") and ra.get("reprice_to"):
        try:
            f_, t_ = float(ra["reprice_from"]), float(ra["reprice_to"])
        except Exception:
            n_bad_ra += 1  # 2026-10-08 棘轮清剿: reprice_audit 解析失败计数 (原为静默 continue)
            continue
        if f_ > 0 and t_ > 0:
            A.append(dict(r=r, f=f_, t=t_, dev=(t_ - f_) / f_ * 100,
                          did=str(r["did"] or ""), date=r["date"],
                          short=str(r["d"] or "").upper().startswith("S")))
    try:
        raw = json.loads(r["raw"]) if r["raw"] else {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    pe = raw.get("planned_entry")
    if isinstance(pe, (int, float)) and float(pe) > 0 and r["e"] and float(r["e"]) > 0:
        B.append(dict(r=r, pe=float(pe), e=float(r["e"]),
                      dev=(float(r["e"]) - float(pe)) / float(pe) * 100,
                      did=str(r["did"] or ""), date=r["date"]))

print(f"A) reprice_audit 人群 n={len(A)}   B) planned_entry 人群 n={len(B)}")
if n_bad_ra:
    print(f"   [warn] reprice_audit 解析失败跳过 {n_bad_ra} 条")
print(f"   A 与 B 重叠 (同 id) n={len({a['r']['id'] for a in A} & {b['r']['id'] for b in B})}")


def dist(vals, label):
    if not vals:
        print(f"  {label}: 空");  return
    a = sorted(vals); n = len(a)
    q = lambda p: a[min(int(n * p), n - 1)]
    print(f"  {label}: n={n} 中位={st.median(a):+.4f}% p10={q(.10):+.4f}% p90={q(.90):+.4f}% "
          f"mean={st.mean(a):+.4f}% |x|中位={st.median([abs(x) for x in a]):.4f}% "
          f"|x|p90={sorted(abs(x) for x in a)[int(n*.9)]:.4f}%")


print("\n" + "=" * 96)
print("① 真 reprice 幅度 (reprice_audit.reprice_from → reprice_to, 权威源)")
print("=" * 96)
dist([a["dev"] for a in A], "全部 (带符号, LONG 口径: 正=追价上调)")
lo = [a["dev"] for a in A if not a["short"]]; sh = [a["dev"] for a in A if a["short"]]
dist(lo, "LONG (BUY)")
dist(sh, "SHORT (SELL_SHORT)")
print(f"  方向: 上调 {sum(1 for a in A if a['dev']>0)}/{len(A)} = {100*sum(1 for a in A if a['dev']>0)/len(A):.1f}%  "
      f"下调 {sum(1 for a in A if a['dev']<0)}/{len(A)}  不变 {sum(1 for a in A if abs(a['dev'])<1e-9)}")

# ATR 归一 (T13-3: 固定%阈值歧视高波动票)
print("\n  --- 按 ATR% 归一 (reprice 幅度 ÷ atr_pct, 单位=ATR) ---")
norm = []
for a in A:
    atr = a["r"]["atr"]
    if atr and float(atr) > 0:
        norm.append(abs(a["dev"]) / (float(atr) * 100))
if norm:
    a2 = sorted(norm)
    print(f"  |dev|/ATR%: n={len(a2)} 中位={st.median(a2):.3f} p90={a2[int(len(a2)*.9)]:.3f} max={a2[-1]:.3f}")

print("\n" + "=" * 96)
print("② planned_entry vs DB entry_price (覆盖广, 但 entry_price 已被 reprice 回写 ⇒ 同族读数)")
print("=" * 96)
dist([b["dev"] for b in B], "全部 (entry − planned)/planned")
print(f"  与 A 人群的中位是否同量级: A={st.median([a['dev'] for a in A]) if A else None:+.4f}%  "
      f"B={st.median([b['dev'] for b in B]) if B else None:+.4f}%")
print("  ⚠️ B 的 entry_price 是回写后值 ⇒ B 测的正是「计划价 → 执行价」的全程位移, 与 A 同源不同覆盖")

print("\n" + "=" * 96)
print("③ anchor_dev_pct 列 (band 反推锚 vs entry_price)")
print("=" * 96)
dist([float(r["ad"]) for r in rows if r["ad"] is not None], "全部")

# ── 4) 结局 join ──
print("\n" + "=" * 96)
print("④ reprice 方向 × 结局 (join trade_records by decision_id, symbol 归一化)")
print("=" * 96)
tr = {}
for t in c.execute("""SELECT decision_id, symbol, trade_date, entry_price, exit_price, pnl_net,
                             mfe_pct, mae_pct, exit_reason, realized_pnl
                      FROM trade_records WHERE source_type='PLAYBOOK_SIM'"""):
    k = str(t["decision_id"] or "")
    if k:
        tr.setdefault(k, []).append(t)
print(f"trade_records(PLAYBOOK_SIM) 有 decision_id 的键 n={len(tr)}  行数={sum(len(v) for v in tr.values())}")

# 按 reprice 方向分桶, 看已成交笔的 mfe/pnl_net
buckets = collections.defaultdict(list)
for a in A:
    did = a["did"]
    if not did or did not in tr:
        continue
    d = a["dev"]
    key = ("up" if d > 0.05 else ("down" if d < -0.05 else "flat"))
    for t in tr[did]:
        buckets[key].append((a, t))

print(f"\n  成交笔可按 reprice 方向分组: {sum(len(v) for v in buckets.values())} 笔 "
      f"(up={len(buckets['up'])} flat={len(buckets['flat'])} down={len(buckets['down'])})")
for k in ("down", "flat", "up"):
    g = buckets[k]
    if not g:
        print(f"  {k:5s}: n=0"); continue
    mf = [float(t["mfe_pct"]) for _, t in g if t["mfe_pct"] is not None]
    pn = [float(t["pnl_net"]) for _, t in g if t["pnl_net"] is not None]
    dv = [a["dev"] for a, _ in g]
    tm, nd, mm = day_t([(t["trade_date"], float(t["pnl_net"])) for _, t in g if t["pnl_net"] is not None])
    print(f"  {k:5s}: n={len(g):3d} |reprice|中位={st.median([abs(x) for x in dv]):+.3f}% "
          f"mfe_pct中位={st.median(mf) if mf else None} 均值={round(st.mean(mf),4) if mf else None} | "
          f"pnl_net 均值={round(st.mean(pn),2) if pn else None} 中位={round(st.median(pn),2) if pn else None} "
          f"WR={100*sum(1 for x in pn if x>0)/len(pn):.0f}% 日聚类t={round(tm,2) if tm else None}(日数{nd})")

# ── 5) 关键反事实的第一刀: reprice 后是否「更贵」而结局更差 ──
print("\n" + "=" * 96)
print("⑤ 反事实前置: 被上调(追价)的单, 其 MFE 空间是否被吃掉?")
print("=" * 96)
print("   逻辑: 若成交价比计划价高 X%, 而价格后续最高只到 H, 则按计划价买的 MFE 会多 X%")
print("   ⇒ 直接算 ΔMFE_potential = (exec − planned)/planned, 但**只有价格曾回到 planned 才可达**")
reach = 0; unreach = 0; reach_rows = []
for a in A:
    r = a["r"]
    did = a["did"]
    if did not in tr:
        continue
    for t in tr[did]:
        # 可达性: 计划价是否落在当日 band 内 (entry_low/entry_high = 当日允许区间)
        lo_, hi_ = r["lo"], r["hi"]
        f_ = a["f"]   # reprice_from = 计划价
        if lo_ and hi_ and float(lo_) > 0 and float(hi_) > 0:
            if float(lo_) <= f_ <= float(hi_):
                reach += 1; reach_rows.append((a, t))
            else:
                unreach += 1
print(f"  计划价(reprice_from)落在指令 band 内 = {reach} 笔 / band 外 = {unreach} 笔")
print("  ⚠️ band 内≠当日真实触达, 需要 1m/5m bar 才能判「价格是否真回到过计划价」")
print("     ⇒ 这是阶段2 (需拉 bar) 的入口, 阶段1 不下可达性结论")
if reach_rows:
    dv = [a["dev"] for a, _ in reach_rows]
    mf = [float(t["mfe_pct"]) for _, t in reach_rows if t["mfe_pct"] is not None]
    print(f"  band内子集: n={len(reach_rows)} reprice中位={st.median(dv):+.4f}% mfe中位={st.median(mf) if mf else None}")
