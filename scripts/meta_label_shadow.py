#!/usr/bin/env python3
"""元标签影子 (meta-labeling shadow, 2026-10-31) —— 「这条已有信号该给多大仓位」。

为什么建 (用户 2026-10-31 裁「都做」): 现役入场门是**二值硬门** (进/不进), 所以必然
拿交易量换质量 —— 实测 entry_confirm 门通过率 51.2%, 弃掉 81 笔/15 日, 净经济价值 ≈ 0。
成熟做法 (López de Prado 2018, 第 3 章 meta-labeling) 把它拆成两层:
   一层 (已存在) 决定方向 —— DA 评级 → bridge → admission;
   二层 (本脚本) 只判断「这条一层信号, 事后是否值得重仓」→ 输出概率 → **调仓位, 不砍单**。
⇒ 交易量 100% 保留, 只把差单压小。这就是「兼顾量」的机制形态。

人群/标签 (预注册, 读数前写死):
  人群: trade_records 已平仓 + A10 口径 (trade_date >= 2026-07-01) + 非 research_momentum。
  标签: y = 1 iff `pnl_net > 0` (**扣费后**净额, 故费用线已内含, 不再另设 ±bp 阈值)。
  特征 (6 个, 全部**事前可得**, 禁 exit_reason / 禁任何事后字段 —— T19 红线):
    is_short / session_min (开盘后分钟) / atr_pct / entry_vix / log(entry_price) / n_conditions
  缺失值一律用**训练折内中位数**填补 (禁全样本中位数, 会泄漏)。

验证 (两套, 都只看 OOS):
  A. purged 日块 5 折: 按交易日排序切 5 个连续块, 每折两侧各 purge 1 个交易日 (同日多笔高度相关)。
  B. 时间切分: 训练 ≤ 2026-09-15 / 测试 ≥ 2026-09-16 (与 A 互为交叉验证)。

政策模拟 (阈值**事先**定为 0.40/0.60, 不看读数后调 —— 防阈值购物):
  P0 全量全仓 (现状基线) / P1 三档 (p<0.40 弃, 0.40-0.60 半仓, >0.60 全仓) / P2 两档 (p<0.50 半仓)。

判据 (三项全过 = PROMOTE_CANDIDATE, 呈用户裁; 任一不过 = KEEP_SHADOW):
  G1 排序力: 顶三分之一 vs 底三分之一的 pnl_net 差 ≥ $5.00/笔 ∧ OOS AUC ≥ 0.55
  G2 保留量: P1 保留笔数 ≥ 80% 全量 (这是"兼顾交易量"的量化门)
  G3 净额: P1 净额 ≥ P0 净额 × 0.95 (允许略降, 换更小回撤)
  即便三项全过, 本脚本也只出读数: 接线 = 改仓位算法 = 生产变更, 须另立 DEB + 用户裁。

只读零副作用: 不写 trade_records / 不改仓位 / 不推送。产物 = data/shadow/meta_label_latest.json。
用法: PYTHONPATH=. venv/bin/python scripts/meta_label_shadow.py
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "shadow" / "meta_label_latest.json"

SINCE = "2026-07-01"
TRAIN_END = "2026-09-15"
L2 = 1.0
ITERS = 4000
LR = 0.15
P_SKIP, P_FULL = 0.40, 0.60
G1_MIN_GAP_USD = 5.00
G1_MIN_AUC = 0.55
G2_MIN_KEEP = 0.80
G3_MIN_NET_RATIO = 0.95


def load():
    conn = sqlite3.connect(str(DB), timeout=20)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT trade_date, symbol, direction, entry_time, entry_price, pnl_net,
               signal_strength, entry_vix, metadata_json, factor_detail
        FROM trade_records
        WHERE COALESCE(exit_price,0) > 0
          AND trade_date >= ?
          AND COALESCE(json_extract(metadata_json,'$.gate_mode'),'') != 'research_momentum'
          AND COALESCE(pnl_net, realized_pnl) IS NOT NULL
        ORDER BY trade_date, entry_time
    """, (SINCE,)).fetchall()
    out = []
    for r in rows:
        md = {}
        try:
            md = json.loads(r["metadata_json"] or "{}")
        except Exception:                                         # noqa: BLE001
            md = {}
        try:
            fd = json.loads(r["factor_detail"] or "{}")
        except Exception:                                         # noqa: BLE001
            fd = {}
        conds = fd.get("conditions") if isinstance(fd, dict) else fd
        conds = conds or []
        # session_min: entry_time 自带 -04:00/-05:00 偏移 ⇒ 字符串里的时分就是 ET
        try:
            t = datetime.fromisoformat(str(r["entry_time"]))
            smin = t.hour * 60 + t.minute - (9 * 60 + 30)
        except Exception:                                         # noqa: BLE001
            smin = None
        atr = md.get("atr")
        try:
            atr_pct = (float(atr) / float(r["entry_price"]) * 100.0) if atr else None
        except (TypeError, ValueError, ZeroDivisionError):
            atr_pct = None
        out.append({
            "date": r["trade_date"], "symbol": r["symbol"],
            "is_short": 1.0 if r["direction"] == "SELL_SHORT" else 0.0,
            "session_min": smin,
            "atr_pct": atr_pct,
            "vix": float(r["entry_vix"]) if r["entry_vix"] is not None else None,
            "log_price": float(np.log(max(float(r["entry_price"] or 1), 1e-6))),
            "n_conditions": float(len(conds)),
            "pnl_net": float(r["pnl_net"] if r["pnl_net"] is not None else r["realized_pnl"]),
            "y": 1.0 if float(r["pnl_net"] if r["pnl_net"] is not None else r["realized_pnl"]) > 0 else 0.0,
        })
    return out


FEATS = ("is_short", "session_min", "atr_pct", "vix", "log_price", "n_conditions")


def matrix(data, medians=None, mu=None, sd=None):
    med = dict(medians or {})
    for f in FEATS:
        if f not in med:
            vals = [d[f] for d in data if d[f] is not None]
            med[f] = float(np.median(vals)) if vals else 0.0
    X = np.array([[med[f] if d[f] is None else d[f] for f in FEATS] for d in data], dtype=float)
    if mu is None:
        mu = X.mean(axis=0)
        sd = X.std(axis=0)
        sd[sd == 0] = 1.0
    return (X - mu) / sd, med, mu, sd


def fit(X, y, l2=L2, iters=ITERS, lr=LR):
    n, k = X.shape
    w = np.zeros(k)
    b = 0.0
    pos = max(y.sum(), 1.0)
    neg = max(n - y.sum(), 1.0)
    sw = np.where(y > 0.5, 0.5 * n / pos, 0.5 * n / neg)   # 类别加权 (61/39 不平衡不算重, 但先稳)
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-(X @ w + b)))
        g = (p - y) * sw
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, b


def predict(X, w, b):
    return 1.0 / (1.0 + np.exp(-(X @ w + b)))


def auc(y, p):
    """Mann-Whitney AUC, **同分取平均秩** (2026-10-31 自捕: 逐序排秩会把并列当有序,
    全同分时给出 1.0 而不是 0.5 ⇒ 假「有排序力」)。"""
    order = np.argsort(p, kind="mergesort")
    sp = p[order]
    ranks = np.empty(len(p))
    i = 0
    while i < len(sp):
        j = i
        while j + 1 < len(sp) and sp[j + 1] == sp[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0     # 并列取平均秩
        i = j + 1
    pos = y > 0.5
    n1, n0 = pos.sum(), (~pos).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def day_blocks(dates, k=5, purge_days=1):
    ud = sorted(set(dates))
    size = max(1, len(ud) // k)
    out = []
    for i in range(k):
        lo = i * size
        hi = (i + 1) * size if i < k - 1 else len(ud)
        test_days = set(ud[lo:hi])
        purged = set()
        if lo - purge_days >= 0:
            purged |= set(ud[max(0, lo - purge_days):lo])
        if hi + purge_days <= len(ud):
            purged |= set(ud[hi:hi + purge_days])
        out.append((test_days, purged))
    return out


def policy(pnl, prob):
    sizes = np.where(prob < P_SKIP, 0.0, np.where(prob > P_FULL, 1.0, 0.5))
    return {
        "P0_all_full": {"n": int(len(pnl)), "net": round(float(pnl.sum()), 2),
                        "net_per_trade": round(float(pnl.mean()), 2)},
        "P1_three_tier": {"n": int((sizes > 0).sum()),
                          "net": round(float((pnl * sizes).sum()), 2),
                          "net_per_trade": round(float((pnl * sizes).sum() / max((sizes > 0).sum(), 1)), 2),
                          "avg_size": round(float(sizes.mean()), 3),
                          "dropped_n": int((sizes == 0).sum())},
        "P2_half_below_50": {"n": int(len(pnl)),
                             "net": round(float((pnl * np.where(prob < 0.5, 0.5, 1.0)).sum()), 2),
                             "net_per_trade": round(float(pnl.sum() / len(pnl)), 2)},
    }


def buckets(pnl, prob):
    order = np.argsort(prob)
    thirds = np.array_split(order, 3)
    names = ["bottom", "middle", "top"]
    out = {}
    for nm, idx in zip(names, thirds):
        if len(idx):
            out[nm] = {"n": int(len(idx)), "net_per_trade": round(float(pnl[idx].mean()), 2),
                       "win_rate": round(float((pnl[idx] > 0).mean() * 100), 1),
                       "prob_range": [round(float(prob[idx].min()), 3), round(float(prob[idx].max()), 3)]}
    return out


def main() -> int:
    data = load()
    y = np.array([d["y"] for d in data])
    pnl = np.array([d["pnl_net"] for d in data])
    dates = [d["date"] for d in data]
    n = len(data)
    print("=" * 78)
    print(f"元标签影子: n={n} 笔已平仓 (A10 口径, {SINCE} 起) | 胜率 {y.mean() * 100:.1f}% "
          f"| 基线净额 {pnl.sum():+.2f} USD / 笔均 {pnl.mean():+.2f}")
    print("=" * 78)

    # A. purged 日块 5 折 → 汇出 OOS 概率
    oos_p = np.full(n, np.nan)
    for test_days, purged in day_blocks(dates, 5, 1):
        tr = [i for i, d in enumerate(dates) if d not in test_days and d not in purged]
        te = [i for i, d in enumerate(dates) if d in test_days]
        if not tr or not te:
            continue
        Xtr, med, mu, sd = matrix([data[i] for i in tr])
        Xte, _, _, _ = matrix([data[i] for i in te], med, mu, sd)
        w, b = fit(Xtr, y[tr])
        oos_p[te] = predict(Xte, w, b)
    ok = ~np.isnan(oos_p)
    cv_auc = auc(y[ok], oos_p[ok])
    print(f"[A] purged 日块 5 折: 覆盖 {ok.sum()}/{n} 笔, OOS AUC = {cv_auc:.3f}")

    # B. 时间切分
    tr = [i for i, d in enumerate(dates) if d <= TRAIN_END]
    te = [i for i, d in enumerate(dates) if d > TRAIN_END]
    ts_auc = float("nan")
    if len(tr) > 30 and len(te) > 20:
        Xtr, med, mu, sd = matrix([data[i] for i in tr])
        Xte, _, _, _ = matrix([data[i] for i in te], med, mu, sd)
        w, b = fit(Xtr, y[tr])
        pt = predict(Xte, w, b)
        ts_auc = auc(y[te], pt)
        print(f"[B] 时间切分 (训 {len(tr)} / 测 {len(te)}): OOS AUC = {ts_auc:.3f}")

    # 用 A 的 OOS 概率做政策模拟与分桶 (OOS = 唯一可信面)
    pol = policy(pnl[ok], oos_p[ok])
    bkt = buckets(pnl[ok], oos_p[ok])
    print("\n── 分三等份 (按 OOS 概率) ──")
    for k in ("bottom", "middle", "top"):
        v = bkt.get(k)
        if v:
            print(f"  {k:7s} n={v['n']:3d} 笔均 {v['net_per_trade']:+7.2f} 胜率 {v['win_rate']:5.1f}% "
                  f"p∈{v['prob_range']}")
    gap = (bkt.get("top", {}).get("net_per_trade", 0) - bkt.get("bottom", {}).get("net_per_trade", 0))
    print(f"  ⇒ 顶-底 差 = {gap:+.2f} USD/笔 (G1 门: ≥ {G1_MIN_GAP_USD:.2f})")

    print("\n── 政策模拟 (OOS) ──")
    for k, v in pol.items():
        print(f"  {k:18s} n={v['n']:3d} 净额 {v['net']:+9.2f} 笔均 {v['net_per_trade']:+7.2f}"
              + (f" 平均仓位 {v['avg_size']:.2f} 弃 {v['dropped_n']} 笔" if "avg_size" in v else ""))

    base = pol["P0_all_full"]
    keep = pol["P1_three_tier"]["n"] / max(base["n"], 1)
    net_ratio = pol["P1_three_tier"]["net"] / base["net"] if base["net"] else 0.0
    g1 = gap >= G1_MIN_GAP_USD and (cv_auc >= G1_MIN_AUC or (ts_auc == ts_auc and ts_auc >= G1_MIN_AUC))
    g2 = keep >= G2_MIN_KEEP
    # ⚠️ 基线为负时的方向: 用绝对比较, 不用比值 —— 比值在负数区间会反转符号
    #    (2026-10-31 自捕: 基线 −1315、政策 −1071 (更好), 比值 0.815 < 0.95 却被判"劣化" = 假红)
    if base["net"] <= 0:
        g3 = pol["P1_three_tier"]["net"] >= base["net"]
        g3_txt = f"{pol['P1_three_tier']['net']:+.2f} ≥ 基线 {base['net']:+.2f} (负基线用绝对比较)"
    else:
        g3 = net_ratio >= G3_MIN_NET_RATIO
        g3_txt = f"{net_ratio:.3f} (门 {G3_MIN_NET_RATIO:.2f})"
    print("\n── 判据 ──")
    print(f"  G1 排序力  {'✅' if g1 else '❌'}  gap {gap:+.2f}/笔, AUC_CV {cv_auc:.3f}, AUC_TS {ts_auc:.3f}")
    print(f"  G2 保留量  {'✅' if g2 else '❌'}  {keep * 100:.1f}% (门 {G2_MIN_KEEP:.0%})")
    print(f"  G3 净额比  {'✅' if g3 else '❌'}  {g3_txt}")
    verdict = "PROMOTE_CANDIDATE" if (g1 and g2 and g3) else "KEEP_SHADOW"
    print(f"  ⇒ verdict = {verdict}")
    print("\n⚠️ 即便 PROMOTE_CANDIDATE 也只是读数: 接线=改仓位算法=生产变更, 须另立 DEB + 用户裁。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n": n, "since": SINCE, "features": list(FEATS),
        "label": "y = 1 iff pnl_net > 0 (扣费后)", "l2": L2,
        "auc_cv": round(cv_auc, 4), "auc_timesplit": None if ts_auc != ts_auc else round(ts_auc, 4),
        "buckets": bkt, "gap_usd": round(gap, 2), "policies": pol,
        "gates": {"G1_rank": bool(g1), "G2_keep": bool(g2), "G3_net": bool(g3),
                  "G1_min_gap_usd": G1_MIN_GAP_USD, "G1_min_auc": G1_MIN_AUC,
                  "G2_min_keep": G2_MIN_KEEP, "G3_min_net_ratio": G3_MIN_NET_RATIO},
        "verdict": verdict,
        "caliber": "record-only 影子; 阈值 0.40/0.60 事先定; 接线须另立 DEB",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"结果落盘: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())