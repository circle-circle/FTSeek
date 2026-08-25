#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同族(板块)动量一致性 — 前提检验 (2026-08-23, 只读零交易路径)。

═══ 为什么先测前提 ═══
两次资金管理层干预已被否决 (仓位不对称 64 配置全不可分辨 / 持有期延长越长越差),
收敛结论: 资金管理无法制造 edge, 瓶颈在方向性。板块联动是候选的 edge 来源。
但在造任何机制前, 先用最便宜的方式回答一个是非题:

  **交易方向与其同族(板块)动量一致时, 结局是否更好?**

若前提不成立, 后面所有板块机制都不必造 (避免重复"造了没人吃"的 deep_sector_rotation)。

═══ 为什么不用 GICS 板块标签 ═══
标签地基不存在: playbook_instructions.sector 84% 为空; deep_sector_rotation.symbol_sectors
仅 32 条覆盖 11% 交易标的; sector_pulse_history 仅 16 日 (7/31~8/21)。
且利弗莫尔的"姐妹股/领涨组"本质是**共动**而非分类标签 — 直接用收益相关性构造同族,
既绕开缺失的标签地基, 也更贴近原始概念。数据源 replay_bars_daily (182标的×1270日, 100%覆盖)。

═══ 预注册口径 (见数前写定) ═══
人群: PLAYBOOK_SIM 已平仓, trade_date>=2026-07-01, 有日线覆盖。
同族构造 (严格无前视, 全部只用 D-1 及更早收盘):
  1. 相关窗: D-1 往前 60 个交易日的日收益
  2. 同族 = 与本标的相关系数最高的 K 个标的 (K=5, 排除自身; 要求窗内数据齐全)
  3. 同族动量 peer_mom_M = 同族成员 (close[D-1]/close[D-1-M] - 1) 的均值, M ∈ {5, 20}
  4. 方向对齐量 aligned = peer_mom_M × sign(direction)  (>0 = 同族在往我的方向走)
对照变量: 本标的自身动量 own_mom_M × sign(direction) — 用于回答
  "同族动量是否提供了自身动量之外的增量信息" (否则只是动量因子换个说法)。
结局: 实际 pnl_net (实盘记账值)。
统计: 分桶净额/胜率 + aligned>0 vs aligned<=0 两组的按日聚类差 t + MDE (宪章第9律)。

═══ 诚实披露 (见数前声明) ═══
① 同族由相关性自建, 非官方板块分类 → 结论是"共动族"的结论, 不能直接外推到 GICS 板块。
② 相关性窗口 60 日与 K=5 是常识初值, 未扫描 → 若前提成立需做敏感性; 若不成立则无需。
③ peer_mom 用 D-1 收盘 → 无前视, 但也因此不含当日盘中板块信息 (sector_pulse 那一层),
   即本检验是"隔夜可得的同族动量"的下限证据; 当日盘中板块联动需 16 日样本另测。
④ 只做观测, 不改任何生产路径。

输出: data/peer_momentum_premise_report.json + 控制台
回滚: 删本脚本 + report
"""
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
DB = REPO / "data" / "ftseek_us.db"
BARS_D = REPO / "data" / "replay_bars_daily"
OUT = REPO / "data" / "peer_momentum_premise_report.json"
SINCE = "2026-07-01"
CORR_WIN = 60
TOP_K = 5
MOMS = [5, 20]


def load_universe():
    """{symbol: {date: close}} + 全局交易日序列。"""
    closes, dates = {}, set()
    for p in sorted(BARS_D.glob("US_*.json")):
        sym = "US." + p.stem.replace("US_", "")
        try:
            rows = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        m = {}
        for r in rows:
            try:
                m[str(r[0])[:10]] = float(r[4])
            except (IndexError, TypeError, ValueError):
                continue
        if len(m) > CORR_WIN + 30:
            closes[sym] = m
            dates |= set(m)
    return closes, sorted(dates)


def rets(series, days):
    """给定日期序列的日收益列表 (None 表示数据缺失)。"""
    out = []
    for i in range(1, len(days)):
        a, b = series.get(days[i - 1]), series.get(days[i])
        out.append((b / a - 1.0) if (a and b and a > 0) else None)
    return out


def pearson(x, y):
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    n = len(pairs)
    if n < 20:
        return None
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a, _ in pairs))
    sy = math.sqrt(sum((b - my) ** 2 for _, b in pairs))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in pairs) / (sx * sy)


def mom(series, days, end_idx, m):
    """(close[end_idx] / close[end_idx-m]) - 1。end_idx 为 D-1 在 days 中的下标。"""
    if end_idx - m < 0:
        return None
    a, b = series.get(days[end_idx - m]), series.get(days[end_idx])
    return (b / a - 1.0) if (a and b and a > 0) else None


def clustered_diff(group_a, group_b):
    """两组按日聚类均值差 + t + MDE。group=[(date, net), ...]"""
    da, db_ = defaultdict(list), defaultdict(list)
    for d, v in group_a:
        da[d].append(v)
    for d, v in group_b:
        db_[d].append(v)
    diffs = []
    for d in sorted(set(da) & set(db_)):
        diffs.append(sum(da[d]) / len(da[d]) - sum(db_[d]) / len(db_[d]))
    n = len(diffs)
    if n < 2:
        return {"n_paired_days": n}
    m = sum(diffs) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in diffs) / (n - 1))
    res = {"n_paired_days": n, "day_clustered_diff_mean": round(m, 2)}
    if sd > 0:
        res["t"] = round(m / (sd / math.sqrt(n)), 3)
        res["mde"] = round(2.8 * sd * math.sqrt(2 / n), 2)
        res["distinguishable"] = bool(abs(m) > res["mde"])
    else:
        res.update({"t": None, "mde": None, "distinguishable": False})
    return res


def main():
    closes, all_days = load_universe()
    print(f"日线宇宙: {len(closes)} 标的, {len(all_days)} 交易日")

    cc = sqlite3.connect(str(DB), timeout=10)
    raw = cc.execute(
        """SELECT symbol, direction, trade_date, pnl_net FROM trade_records
           WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL
             AND trade_date >= ? AND pnl_net IS NOT NULL""", (SINCE,)).fetchall()
    cc.close()

    recs, skipped = [], 0
    corr_cache = {}
    for sym, direction, td, net in raw:
        if sym not in closes or td not in all_days:
            skipped += 1
            continue
        di = all_days.index(td)
        if di < CORR_WIN + max(MOMS) + 2:
            skipped += 1
            continue
        d_prev = di - 1                       # D-1 下标 (无前视上限)
        win = all_days[d_prev - CORR_WIN:d_prev + 1]
        ck = (sym, td)
        if ck not in corr_cache:
            base = rets(closes[sym], win)
            scored = []
            for other, oser in closes.items():
                if other == sym:
                    continue
                c = pearson(base, rets(oser, win))
                if c is not None:
                    scored.append((c, other))
            scored.sort(reverse=True)
            corr_cache[ck] = scored[:TOP_K]
        peers = corr_cache[ck]
        if len(peers) < TOP_K:
            skipped += 1
            continue
        sign = -1.0 if str(direction).upper() == "SHORT" else 1.0
        row = {"symbol": sym, "date": td, "net": float(net),
               "direction": direction,
               "peer_corr_mean": round(sum(c for c, _ in peers) / len(peers), 3),
               "peers": [p for _, p in peers]}
        ok = True
        for M in MOMS:
            pm = [mom(closes[p], all_days, d_prev, M) for _, p in peers]
            pm = [v for v in pm if v is not None]
            om = mom(closes[sym], all_days, d_prev, M)
            if not pm or om is None:
                ok = False
                break
            row[f"peer_aligned_{M}"] = (sum(pm) / len(pm)) * sign
            row[f"own_aligned_{M}"] = om * sign
        if not ok:
            skipped += 1
            continue
        recs.append(row)

    print(f"可评估 {len(recs)} 笔 (跳过 {skipped})  同族平均相关={sum(r['peer_corr_mean'] for r in recs)/max(len(recs),1):.3f}")

    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "pre_registered": "2026-08-23 见数前写定 (见脚本 docstring)",
              "hypothesis": "交易方向与同族动量一致时结局更好",
              "peer_construction": f"D-1 前 {CORR_WIN} 交易日收益相关性 top{TOP_K}, 严格无前视",
              "n_evaluated": len(recs), "n_skipped": skipped,
              "disclosures": [
                  "同族由相关性自建, 非GICS分类 → 结论限于'共动族'",
                  "CORR_WIN=60/K=5 为常识初值未扫描 (前提成立才做敏感性)",
                  "peer_mom 用 D-1 收盘, 无前视但不含当日盘中板块信息 → 下限证据",
                  "只做观测, 零生产路径改动",
              ],
              "results": {}}

    for key_prefix, label in (("peer_aligned", "同族动量"), ("own_aligned", "自身动量")):
        for M in MOMS:
            k = f"{key_prefix}_{M}"
            pos = [(r["date"], r["net"]) for r in recs if r[k] > 0]
            neg = [(r["date"], r["net"]) for r in recs if r[k] <= 0]
            st = clustered_diff(pos, neg)
            res = {
                "n_aligned": len(pos), "n_misaligned": len(neg),
                "aligned_net": round(sum(v for _, v in pos), 1),
                "misaligned_net": round(sum(v for _, v in neg), 1),
                "aligned_mean": round(sum(v for _, v in pos) / max(len(pos), 1), 2),
                "misaligned_mean": round(sum(v for _, v in neg) / max(len(neg), 1), 2),
                "aligned_wr": round(sum(1 for _, v in pos if v > 0) / max(len(pos), 1), 3),
                "misaligned_wr": round(sum(1 for _, v in neg if v > 0) / max(len(neg), 1), 3),
                **st}
            report["results"][k] = res
            print(f"\n── {label} {M}日 (aligned = 同向) ──")
            print(f"   一致 n={res['n_aligned']:<4} 净=${res['aligned_net']:<9.1f} "
                  f"笔均=${res['aligned_mean']:<7.2f} 胜率={res['aligned_wr']*100:.0f}%")
            print(f"   逆向 n={res['n_misaligned']:<4} 净=${res['misaligned_net']:<9.1f} "
                  f"笔均=${res['misaligned_mean']:<7.2f} 胜率={res['misaligned_wr']*100:.0f}%")
            print(f"   按日聚类差={res.get('day_clustered_diff_mean')} t={res.get('t')} "
                  f"MDE={res.get('mde')} 可分辨={res.get('distinguishable')}")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
