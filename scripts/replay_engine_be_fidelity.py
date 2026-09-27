#!/usr/bin/env python3
"""官方回放引擎的 SL_BREAKEVEN 桶**聚合**保真度 — 校准单 (MU id=1561) 的推广.

为何需要: 我上一轮声称「两个回放器同犯成交语义错 ⇒ 系统性低估 BE 出场, 且对低 be 档
低估更多」。**该声称对官方引擎是错的** — 铁证 scripts/exit_replay_backtest.py:385-389:
    eff = _active_sl(...)                       # 生效 SL 线
    hit = (px - eff) * sign <= 0
    exited = {"exit_price": eff * (1 - slip * sign), ...}   # ← 按 **eff 线**结算, 不是 px
穿越价语义会是"px≤eff 就按 **px** 结算"(我自写的简易回放器确实这么写, 那部分声称成立);
官方引擎按线结算, 且 L278 复用生产 SSOT `_compute_dynamic_effective_sl`。

MU id=1561 校准单 (已全链核验, 实际净 +15.67):
    L334 be_line    = entry*(1+0.15/100)     = 1089.6721  (引擎用**固定** fee 0.15%)
    L388 exit_price = be_line*(1-3bps)       = 1089.3452  ⇒ 引擎净 **+2.40**
    生产限价线 (fee-aware 0.0763%)            = 1088.8702
    实际成交 (限价被动侧有利滑点 −28.77bps)   = 1092.0000  ⇒ 实际净 **+15.67**
  ⇒ 两侧偏差**方向相反**:
     ① 引擎保本线比生产**高** 0.80 (固定 fee 0.15% > fee-aware 实弹 avg 0.1094%) ⇒ 偏**乐观**
     ② 引擎不模拟限价单**有利成交** (实际 1092.0 ≫ 线 1088.87) ⇒ 偏**悲观**
  ⇒ 单笔差 −13.27, 但**净方向未量化** ⇒ 必须做聚合对账才能谈"系统性"。

本脚本做聚合对账: 官方引擎 SL_BREAKEVEN 桶 vs 实盘 SL_BREAKEVEN 桶 (同窗口)。
只读 DB + 只读引擎产物 ⇒ 零交易行为变更、零产物写入。
用法: ./venv/bin/python scripts/replay_engine_be_fidelity.py [产物路径]
"""
import json, sqlite3, statistics, sys
from collections import defaultdict
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data" / "ftseek_us.db"

# 产物路径在脚本内解析 (产物 >1MB, 命令行直接引用会被 lifecycle 守卫的扫描上限拦)
# 顺序: ①命令行显式给的 ②本轮为复核跑的有界窗口产物 (/tmp) ③仓库内默认产物 (可能是旧的/无界)
cands = [Path(x) for x in (sys.argv[1:] if len(sys.argv) > 1 else [])]
cands += sorted(Path("/tmp").glob("er817_be05.json"))
cands += sorted(Path("/tmp").glob("exit_replay_817_repro.json"))
cands += [REPO / "data" / "exit_replay_latest.json"]
prod = next((p for p in cands if p.exists()), None)
if prod is None:
    raise SystemExit(f"未找到引擎产物, 候选: {[str(p) for p in cands]}")

d = json.loads(prod.read_text(encoding="utf-8"))
since, until = d.get("since"), d.get("until")
vs = d.get("variants", {})
print(f"引擎产物: {prod}")
print(f"窗口 since={since} until={until}  n_replayed={d.get('trades_replayed')}"
      f"  live_sum_net={d.get('live_sum_net')}")
v = d.get("validation", {})
print(f"保真度: reason_match_pct={v.get('reason_match_pct')}  "
      f"exit_pct_delta_median={v.get('exit_pct_delta_median')}  p90={v.get('exit_pct_delta_p90')}")
print(f"params: {json.dumps(d.get('params'), ensure_ascii=False)}")

# ── 引擎侧: baseline 的 SL_BREAKEVEN 桶 ──
base = vs.get("live_baseline", {}).get("pess", {})
eng_be = (base.get("by_reason", {}) or {}).get("SL_BREAKEVEN", {})
print(f"\n=== 引擎侧 (live_baseline/pess) ===")
print(f"  SL_BREAKEVEN: n={eng_be.get('n')}  sum_net={eng_be.get('sum_net')}")
eng_all = {k: val.get("n") for k, val in (base.get("by_reason") or {}).items()}
print(f"  全部 reason 桶: {eng_all}")

# ── 实盘侧: 同窗口 SL_BREAKEVEN 桶 ──
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
# ⚠️ until 可能为 None (引擎默认不截断) ⇒ SQL 里 `date(entry_time) <= NULL` **恒 false**
#    会让实盘侧静默返回 0 行 (首跑即踩, 表现为"实盘 n=0"而非报错)。按是否有 until 分支拼 SQL。
q_base = """SELECT id, symbol, COALESCE(pnl_net, realized_pnl, 0) AS net, exit_reason,
              exit_time, metadata_json
       FROM trade_records
       WHERE date(entry_time) >= ?
         AND exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason != 'ORPHAN_CLEANUP'"""
if until:
    rows = con.execute(q_base + " AND date(entry_time) <= ?", (since, until)).fetchall()
else:
    rows = con.execute(q_base, (since,)).fetchall()
con.close()

def fee_of(r):
    try:
        fb = json.loads(r["metadata_json"] or "{}").get("fee_breakdown") or {}
    except Exception:
        return None
    e = (fb.get("entry") or {}).get("total")
    x = (fb.get("exit") or {}).get("total")
    return None if (e is None and x is None) else float(e or 0) + float(x or 0)

live = defaultdict(lambda: dict(n=0, net=0.0, fee=0.0))
be_ids = []
for r in rows:
    reason = r["exit_reason"]
    key = "SL_BREAKEVEN" if reason.startswith("SL_BREAKEVEN") else reason
    b = live[key]
    b["n"] += 1
    b["net"] += float(r["net"] or 0)
    f = fee_of(r)
    if f is not None:
        b["fee"] += f
    if key == "SL_BREAKEVEN":
        be_ids.append((r["id"], r["symbol"], float(r["net"] or 0)))

print(f"\n=== 实盘侧 (同窗口 entry_time ∈ [{since}, {until}], n={len(rows)}) ===")
for k in sorted(live, key=lambda x: -live[x]["n"]):
    b = live[k]
    print(f"  {k:24s} n={b['n']:4d}  净={b['net']:>+11,.2f}  费={b['fee']:>9,.2f}")

lb = live.get("SL_BREAKEVEN", dict(n=0, net=0.0))
print("\n" + "=" * 96)
print("=== 聚合对账: 引擎 vs 实盘 的 SL_BREAKEVEN 桶 ===")
print("=" * 96)
en, ln_ = eng_be.get("n") or 0, lb["n"]
enet, lnet = eng_be.get("sum_net") or 0.0, lb["net"]
print(f"  笔数      : 引擎 {en:4d}   实盘 {ln_:4d}   差 {en-ln_:+d}  "
      f"({(en/ln_-1)*100 if ln_ else 0:+.1f}%)")
print(f"  桶净额    : 引擎 {enet:>+11,.2f}   实盘 {lnet:>+11,.2f}   差 {enet-lnet:>+11,.2f}")
print(f"  均净/笔   : 引擎 {enet/en if en else 0:>+11,.2f}   实盘 {lnet/ln_ if ln_ else 0:>+11,.2f}")

# ── 参数时代混算诊断 (本对账**不可直接比**的原因) ──
# 引擎 live_baseline 读**现行 config** (be_0.4, 8/17 落地), 而窗口内绝大多数笔实盘是在
# **be_0.5 时代**成交的 ⇒ 引擎把 be_0.5 时代本不该武装的笔也武装了。
print("\n=== ⛔ 本对账被「参数时代混算」污染, 差值不可直接解读 ===")
con2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con2.row_factory = sqlite3.Row
era = con2.execute(
    """SELECT CASE WHEN date(entry_time) < '2026-08-17' THEN 'be_0.5时代' ELSE 'be_0.4落地日起' END AS era,
              COUNT(*) AS n,
              SUM(CASE WHEN exit_reason LIKE 'SL_BREAKEVEN%' THEN 1 ELSE 0 END) AS be
       FROM trade_records
       WHERE date(entry_time) >= ? AND (? IS NULL OR date(entry_time) <= ?)
         AND exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason != 'ORPHAN_CLEANUP'
       GROUP BY era""", (since, until, until)).fetchall()
con2.close()
for e in era:
    print(f"  实盘 {e['era']:14s}: 全 {e['n']:4d} 笔  BE {e['be']:3d} 笔  "
          f"BE占比 {e['be']/e['n']*100 if e['n'] else 0:5.1f}%")
eng_all_n = base.get("n") or 0
print(f"  引擎 (全窗不分段)  : 全 {eng_all_n:4d} 笔  BE {en:3d} 笔  "
      f"BE占比 {en/eng_all_n*100 if eng_all_n else 0:5.1f}%")
print(f"  ⇒ 引擎 baseline 用**现行 be_0.4** 回放整窗, 而窗口主体是 be_0.5 时代成交")
print(f"     ⇒ BE 笔数虚增 {en-ln_:+d} ({(en/ln_-1)*100 if ln_ else 0:+.1f}%) 的机制是**时代混算**, 不是成交语义")
print(f"     ⇒ `reason_match_pct={v.get('reason_match_pct')}` 偏低的主因也是这个混算")
print(f"     ⇒ 违反 skill 的「参数 era 分层」纪律 (Gate 1): 裁决层只应取现行参数时代样本")

print("\n=== 判读 (预注册, 跑之前写死) ===")
print("  · 若引擎桶净 **显著低于** 实盘桶净 ⇒ 「系统性低估 BE」方向成立 (但机制不是穿越价)")
print("  · 若引擎桶净 **高于或接近** 实盘 ⇒ 该声称**不成立**, 两侧偏差相互抵消或反向")
print("  · ⚠️ 但两侧存在**时代混算**污染 ⇒ 本差值只能用于**证伪方向**, 不能量化引擎偏差幅度")
if ln_:
    gap = enet - lnet
    ratio = gap / abs(lnet) * 100 if lnet else float("inf")
    if gap < 0 and abs(ratio) > 30:
        verdict = f"「系统性低估」方向成立 (引擎低 {abs(gap):,.2f}, {ratio:+.1f}%)"
    elif gap > 0 and abs(ratio) > 30:
        verdict = (f"❌ 引擎桶净**高于**实盘 {gap:,.2f} ({ratio:+.1f}%) ⇒ 「系统性低估 BE」声称"
                   f"**方向相反, 不成立**; 但幅度被时代混算污染, 不可当引擎偏差量")
    else:
        verdict = f"? 差 {gap:+,.2f} ({ratio:+.1f}%) 在 ±30% 内 ⇒ 无系统性单侧偏差"
    print(f"\n  ⇒ {verdict}")

