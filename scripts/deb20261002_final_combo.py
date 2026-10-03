"""DEB 证据补测 3 (最终三刀 K1+K2+K3, K4 已撤): 完整读数 + 两个落地前提核验
① K1+K2+K3 组合的 IS/OOS/逐月/剔尾/日聚类 (E表期望值)
② halved/flip 标记词表是否真实存在 (防 0/138 是假设值自证)
③ low_atr skip 的 8 条历史行方向 (C1 是否零行为变更)
"""
import json
import sqlite3
import sys
from app.storage.db_pool import get_db_path
import statistics as st

BASE = "/opt/ftseek/FTSeek-US"
c = sqlite3.connect(f"file:{get_db_path('ftseek_research')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

print("=== ② halved/flip 词表核验: 全库 metadata 里有无任何降仓标记键 ===")
rows_all = c.execute("SELECT metadata_json FROM trade_records "
                     "WHERE entry_time>='2026-06-27' AND entry_time < '2026-10-02 00:00:00' AND metadata_json IS NOT NULL").fetchall()
keys = {}
for r in rows_all:
    try:
        md = json.loads(r["metadata_json"])
    except Exception as exc:
        print(f"skip bad metadata_json row: {exc}", file=sys.stderr)
        continue
    for k in md:
        keys[k] = keys.get(k, 0) + 1
hits = {k: v for k, v in keys.items()
        if any(w in k.lower() for w in ("halv", "flip", "revers", "size", "reduce", "trim"))}
print(f"  metadata 顶层键共 {len(keys)} 个; 含 halv/flip/revers/size/reduce/trim 的键: {hits or '无'}")
print("  ⇒ 若无任何此类键: '0/138 带降仓标记' 不是'无降仓单'的证据, 只是词表不存在 (自证陷阱)")
print("     该声称降级为: '无法从 metadata 识别降仓单' — RFC-071 幸存带用户数不可测")

print("\n=== ③ low_atr skip 历史行的方向 (C1 LONG-only 是否零行为变更) ===")
sk = c.execute("SELECT date, symbol, direction, skip_reason FROM playbook_instructions "
               "WHERE skip_reason LIKE '%low_atr%'").fetchall()
print(f"  low_atr skip 共 {len(sk)} 行:")
for r in sk:
    print(f"   {r['date']} {r['symbol']:10s} dir={r['direction']!r}")
dirs = {r["direction"] for r in sk}
print(f"  方向集合: {dirs}")
print(f"  ⇒ C1 (门加 LONG-only) {'零行为变更 (历史无 SHORT 被拦)' if 'SHORT' not in dirs and 'SELL_SHORT' not in dirs else '🔴 行为变更: 历史有 SHORT 被拦过'}")

print("\n=== ① K1+K2+K3 最终组合完整读数 ===")
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
        "day": (r["entry_time"] or "")[:10], "month": (r["entry_time"] or "")[:7],
        "price": r["entry_price"] or 0,
        "notional": (r["entry_price"] or 0) * (r["quantity"] or 0),
        "gap": gap.get("aligned_gap_pct", gap.get("gap_pct")), "atr": md.get("atr"),
        "dead": 1 if (r["mfe_pct"] or 0) < 0.3 else 0,
        "net": r["pnl_net"] or 0, "gross": r["realized_pnl"] or 0, "fees": r["fees_est"] or 0,
    })


def A(g):
    n = len(g)
    if not n:
        return None
    net = sum(x["net"] for x in g)
    gross = sum(x["gross"] for x in g)
    fees = sum(x["fees"] for x in g)
    nt = sum(x["notional"] for x in g)
    return dict(n=n, net=net, gross=gross, fees=fees, avg=net / n,
                wr=sum(1 for x in g if x["net"] > 0) / n * 100,
                dead=sum(x["dead"] for x in g) / n * 100,
                gb=gross / nt * 1e4 if nt else 0, fb=fees / nt * 1e4 if nt else 0)


def rob(g):
    syms = {}
    for x in g:
        syms[x["sym"]] = syms.get(x["sym"], 0) + x["net"]
    rk = sorted(syms.items(), key=lambda z: z[1])
    tot = sum(x["net"] for x in g)
    return tot - sum(v for _, v in rk[:3]), tot - sum(v for _, v in rk[-3:])


fee_ok = lambda x: x["price"] >= 26 and x["notional"] >= 4200
atr_ok = lambda x: x["atr"] not in (None, 0)
FINAL3 = lambda x: (fee_ok(x)
                    and not (x["dir"] == "SHORT" and x["gap"] is not None and abs(x["gap"]) >= 8)
                    and not (x["dir"] == "SHORT" and atr_ok(x) and x["atr"] >= 8))
base = A(recs)
fin = [x for x in recs if FINAL3(x)]
a = A(fin)
d3w, d3b = rob(fin)
print(f"  基线: n={base['n']} 毛={base['gross']:+.1f} 费={base['fees']:.1f} 净={base['net']:+.1f} "
      f"均={base['avg']:+.2f} WR={base['wr']:.0f}% 死率={base['dead']:.1f}% 费bps={base['fb']:.1f}")
print(f"  K1+K2+K3: n={a['n']} ({a['n']/base['n']*100:.0f}%) 毛={a['gross']:+.1f} 费={a['fees']:.1f} "
      f"净={a['net']:+.1f} 均={a['avg']:+.2f} WR={a['wr']:.0f}% 死率={a['dead']:.1f}% "
      f"毛bps={a['gb']:.1f} 费bps={a['fb']:.1f}")
print(f"  Δ净={a['net']-base['net']:+.1f} Δ死率={a['dead']-base['dead']:+.1f}pp "
      f"剔3亏={d3w:+.1f}{'✅' if d3w>0 else '🔴'} 剔3赚={d3b:+.1f}{'✅' if d3b>0 else '🔴'}")
for d in ("LONG", "SHORT"):
    dd = [x for x in fin if x["dir"] == d]
    if dd:
        aa = A(dd)
        print(f"   {d}: n={aa['n']:3d} 毛={aa['gross']:+8.1f} 净={aa['net']:+8.1f} "
              f"均={aa['avg']:+6.2f} WR={aa['wr']:3.0f}% 死率={aa['dead']:3.0f}%")
for lab, p in (("IS <8/20", lambda x: x["t"] < "2026-08-20"),
               ("OOS >=8/20", lambda x: x["t"] >= "2026-08-20")):
    s3, b3 = [x for x in fin if p(x)], [x for x in recs if p(x)]
    aa, ab = A(s3), A(b3)
    print(f"   {lab:10s} n={aa['n']:3d} 净={aa['net']:+8.1f} 均={aa['avg']:+6.2f} "
          f"死率={aa['dead']:3.0f}% | 基线均={ab['avg']:+6.2f} ⇒ Δ均={aa['avg']-ab['avg']:+6.2f} "
          f"{'✅' if aa['avg'] > ab['avg'] else '🔴'}")
ms, better = [], 0
for m in sorted({x["month"] for x in fin}):
    mm = [x for x in fin if x["month"] == m]
    bb = [x for x in recs if x["month"] == m]
    if len(mm) < 3:
        ms.append(f"{m[5:]}:n{len(mm)}")
        continue
    aa, ab = A(mm), A(bb)
    ms.append(f"{m[5:]}:{aa['net']:+.0f}(基{ab['net']:+.0f})")
    better += aa["net"] > ab["net"]
print(f"   逐月: {' '.join(ms)} ⇒ 改善 {better}/5 月")
days = {}
for x in fin:
    days.setdefault(x["day"], []).append(x)
daily = [sum(y["net"] for y in v) for v in days.values()]
dg = [sum(y["gross"] for y in v) for v in days.values()]
tn = st.mean(daily) / (st.pstdev(daily) / len(daily) ** .5)
tg = st.mean(dg) / (st.pstdev(dg) / len(dg) ** .5)
print(f"   日聚类: 交易日={len(daily)} 日净均={st.mean(daily):+.2f} t净={tn:+.2f} t毛={tg:+.2f}")
cut = [x for x in recs if not FINAL3(x)]
ac = A(cut)
win = [x for x in cut if x["net"] > 0]
print(f"   砍掉 n={ac['n']} 净={ac['net']:+.1f} | 赢单 {len(win)} 笔 +{sum(x['net'] for x in win):.0f}")

print("\n=== 补充: 仅 K1 的当前时代读数 (9/1起, 落地影响面) ===")
cur = [x for x in recs if x["t"] >= "2026-09-01"]
cf = [x for x in cur if not fee_ok(x)]
print(f"  9/1起 n={len(cur)}; 不过费用线 {len(cf)} ({len(cf)/len(cur)*100:.0f}%) "
      f"净={sum(x['net'] for x in cf):+.1f}")
ext_penny = [x for x in cur if x["price"] < 5]
print(f"  其中 价<$5 (admission 已有闸但 EXT 通道绕过): n={len(ext_penny)} "
      f"净={sum(x['net'] for x in ext_penny):+.1f} "
      f"标的={sorted({x['sym'] for x in ext_penny})}")
c.close()
