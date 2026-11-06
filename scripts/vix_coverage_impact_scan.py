#!/usr/bin/env python3
"""只读影响面扫描: playbook_regime_log 覆盖缺口 + trade_records.entry_vix 假零值 (2026-11-06)。

背景: 复核 P3 时先误判"生产 12 行 vix=0 被继承"，实为 weekly_report 幂等标记 (生产已过滤)。
      但由此查到两个**真实**残留，本脚本只量化、不改任何东西:
        ① playbook_regime_log 的**真实环境行**在 A10 窗口覆盖不足 (消费者按"最近一条"兜底 ⇒ 用到陈旧 regime)
        ② trade_records.entry_vix 缺失值编码不一致 (0.0 vs NULL)，分桶分析会把缺失当"低波动"

用法: venv/bin/python3.11 scripts/vix_coverage_impact_scan.py
"""
from __future__ import annotations

import sqlite3
import statistics as st
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = f"file:{ROOT / 'data' / 'ftseek_us.db'}?mode=ro"
A10_START = "2026-07-01"


def main() -> int:
    con = sqlite3.connect(DB, uri=True)
    con.row_factory = sqlite3.Row

    # ── ① regime_log 覆盖 ────────────────────────────────────────────────
    real = [(r["date"], r["vix"], r["regime"]) for r in con.execute(
        "select date, vix, regime from playbook_regime_log "
        "where regime not like '%weekly%' order by date")]
    markers = [(r["date"], r["regime"]) for r in con.execute(
        "select date, regime from playbook_regime_log where regime like '%weekly%'")]
    trade_days = sorted({r[0] for r in con.execute(
        "select distinct trade_date from trade_records where trade_date >= ?", (A10_START,))})
    have = {d for d, _v, _r in real if d >= A10_START}
    missing = [d for d in trade_days if d not in have]

    print(f"① playbook_regime_log")
    print(f"   真实环境行 {len(real)} (vix<=0 的真实行: "
          f"{[d for d, v, _r in real if (v or 0) <= 0] or '无'})")
    print(f"   周报幂等标记行 {len(markers)} (生产 get_regime_log 已按 regime NOT LIKE '%weekly%' 过滤)")
    print(f"   A10 交易日 {len(trade_days)} 天, 有环境行 {len(have)} 天, 缺 {len(missing)} 天 "
          f"({len(missing)/len(trade_days):.0%})")
    if missing:
        print(f"   缺失区间样例: {missing[:6]} ... {missing[-4:]}")
    # 消费方按"最近一条"兜底时会用多旧的 regime
    gaps = []
    prev = None
    for d in sorted(have):
        if prev:
            gap = (date.fromisoformat(d) - date.fromisoformat(prev)).days
            gaps.append((prev, d, gap))
        prev = d
    if gaps:
        worst = sorted(gaps, key=lambda x: -x[2])[:3]
        print(f"   相邻环境行最大间隔: {[(a, b, g) for a, b, g in worst]} (自然日)")
        stale_days = sum(max(0, g - 1) for _a, _b, g in gaps)
        print(f"   ⇒ 兜底读取时\"用的是陈旧 regime\"的自然日总数 ≈ {stale_days}")

    # ── ② entry_vix 编码 ────────────────────────────────────────────────
    rows = [dict(r) for r in con.execute(
        "select trade_date, symbol, owner_strategy, entry_vix, pnl_net, mfe_pct "
        "from trade_records where trade_date >= ? and data_quality in ('clean','real') "
        "and coalesce(source_type,'') not in ('POSITION_SYNC','MANUAL') and exit_price > 0",
        (A10_START,))]
    n = len(rows)
    zero = [r for r in rows if r["entry_vix"] == 0]
    null = [r for r in rows if r["entry_vix"] is None]
    ok = [r for r in rows if (r["entry_vix"] or 0) > 0]
    print(f"\n② trade_records.entry_vix (A10 n={n})")
    print(f"   有效 {len(ok)} ({len(ok)/n:.1%}) / 假零 0.0 {len(zero)} ({len(zero)/n:.1%}) / "
          f"NULL {len(null)} ({len(null)/n:.1%})")
    print(f"   写入侧默认值: app/storage/trade_store.py:859 `record.get('entry_vix', 0.0) or 0.0` "
          f"⇒ 缺数据落 0.0 而非 NULL")

    def band(v):
        if v is None:
            return "NULL"
        if v == 0:
            return "0.0(假零)"
        return "<15" if v < 15 else "15-20" if v < 20 else "20-30" if v < 30 else ">30"

    for title, data in (("含假零/ NULL 直接分桶 (错误)", rows),
                        ("剔除 0.0 与 NULL (正确)", ok)):
        g = defaultdict(list)
        for r in data:
            g[band(r["entry_vix"])].append(r)
        print(f"   --- {title}")
        for k in ("0.0(假零)", "NULL", "<15", "15-20", "20-30", ">30"):
            v = g.get(k)
            if not v:
                continue
            print(f"     {k:10s} n={len(v):4d} 净额={sum(x['pnl_net'] for x in v):8.0f} "
                  f"每笔净={st.mean([x['pnl_net'] for x in v]):7.2f} "
                  f"胜率={100*sum(1 for x in v if x['pnl_net'] > 0)/len(v):5.1f}%")

    # 量化扭曲: 把假零并入 <15 档会怎样
    if zero:
        low_ok = [r for r in ok if r["entry_vix"] < 15]
        merged = low_ok + zero
        print(f"   ⇒ 若把假零并入 <15 档: 每笔净由 {st.mean([r['pnl_net'] for r in low_ok]):.2f} "
              f"变为 {st.mean([r['pnl_net'] for r in merged]):.2f} "
              f"(n {len(low_ok)}→{len(merged)})")

    # 假零的时间分布 (根因: 那些天 VIX 取数失败)
    print(f"   假零按日 top6: {Counter(r['trade_date'] for r in zero).most_common(6)}")
    print(f"   假零按策略: {Counter(r['owner_strategy'] for r in zero).most_common(5)}")

    # ── ③ 是否有交易决策依赖 entry_vix ─────────────────────────────────
    print("\n③ 决策依赖: app/playbook/playbook_service.py:1284 注释 "
          "'零 VIX 拦截 — entry_vix 仅被记账不被消费'")
    print("   ⇒ 无入场/退出/风控门读 entry_vix；影响面限于研究与归因分桶。")
    print("\n只读扫描，未写入任何文件/库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())