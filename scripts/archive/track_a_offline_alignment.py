#!/usr/bin/env python3
"""轨道A 阶段1 — 离线对齐验证报告 (只读, 零风险)。

目的: 用历史 playbook_instructions 数据坐实职责分离的前提:
  分析1  LLM 计划 entry_price vs 实际成交 execution_price 的偏差分布
         → 量化 "LLM 编的 entry 到底离真实成交多远"(若大, 印证 LLM 数字被覆盖、应交给代码)。
  分析2  用单一真源 compute_entry_band 回溯: 实际成交价是否落在 band [entry_low, entry_high] 内
         → 验证扁平 band 模型的贴合度; 落在外的比例为 Q2 波动率化提供依据。

数据约束 (诚实记录):
  - 无 atr_at_generation 等历史技术快照 → 无法离线重算 SL/TP/真实关键位, 该部分延后
    (次交易日 DA 产出 entry_low/high/invalidation 后, 或补存技术快照后再做)。
  - entry_low/high/invalidation 当前 0 非空 (DA 新契约未生效), 故 band 用 compute_entry_band 现算。

运行: ./venv/bin/python scripts/track_a_offline_alignment.py
"""
from __future__ import annotations

import statistics
from typing import List, Tuple

from app.storage.db_pool import get_db
from app.playbook.entry_band import compute_entry_band


def _pct_stats(xs: List[float]) -> str:
    if not xs:
        return "n=0"
    xs_sorted = sorted(xs)
    n = len(xs_sorted)
    p = lambda q: xs_sorted[min(n - 1, int(q * n))]  # noqa: E731
    return (
        f"n={n} mean={statistics.mean(xs):.2f}% median={statistics.median(xs):.2f}% "
        f"p90={p(0.9):.2f}% max={max(xs):.2f}%"
    )


def _is_short(direction: str) -> bool:
    return any(k in (direction or "").upper() for k in ("SHORT", "SELL"))


def load_rows() -> List[Tuple]:
    with get_db().connect("ftseek_us") as conn:
        rows = conn.execute(
            "SELECT symbol, direction, entry_price, execution_price, stop_loss, take_profit "
            "FROM playbook_instructions "
            "WHERE execution_price IS NOT NULL AND execution_price != 0 "
            "  AND entry_price IS NOT NULL AND entry_price != 0"
        ).fetchall()
    return rows


def fill_selfcheck() -> None:
    """新契约/归因字段落列自检 (post-cron 复跑时确认 DA 新输出已生效)。

    近 50 条任一非空 = ✅(新契约已生效); 全 0 = ⏳(DA cron 未产出新数据)。
    """
    text_fields = [
        "invalidation", "market_regime", "vix_at_generation",
        "spy_trend_at_generation", "sector", "entry_low", "entry_high",
    ]
    print("── 分析3: 新契约/归因字段落列自检 (近50条 / 全部) ──")
    with get_db().connect("ftseek_us") as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(playbook_instructions)")}
        total = conn.execute("SELECT COUNT(*) FROM playbook_instructions").fetchone()[0]
        for f in text_fields:
            if f not in cols:
                print(f"  ⚠ {f}: 列不存在")
                continue
            recent = conn.execute(
                f"SELECT COUNT(*) FROM (SELECT {f} FROM playbook_instructions "
                f"ORDER BY id DESC LIMIT 50) WHERE {f} IS NOT NULL AND {f}!='' AND {f}!=0"
            ).fetchone()[0]
            allc = conn.execute(
                f"SELECT COUNT(*) FROM playbook_instructions "
                f"WHERE {f} IS NOT NULL AND {f}!='' AND {f}!=0"
            ).fetchone()[0]
            flag = "✅" if recent > 0 else "⏳"
            print(f"  {flag} {f}: 近50 {recent}/50, 全部 {allc}/{total}")
        # planned_* 快照在 raw_instruction_json blob (Fix③)
        blob_rows = conn.execute(
            "SELECT raw_instruction_json FROM playbook_instructions "
            "WHERE raw_instruction_json IS NOT NULL ORDER BY id DESC LIMIT 50"
        ).fetchall()
        planned = sum(1 for (b,) in blob_rows if b and "planned_entry" in b)
        flag = "✅" if planned > 0 else "⏳"
        print(f"  {flag} planned_* (raw_instruction blob): 近50 {planned}/{len(blob_rows)}")
    print()


def main() -> None:
    rows = load_rows()
    print("=" * 66)
    print("轨道A 阶段1 — 离线对齐验证报告 (只读)")
    print("=" * 66)
    print(f"已成交且有 LLM entry 的样本: {len(rows)}\n")
    if not rows:
        print("无样本, 结束。")
        return

    # ── 分析1: LLM entry vs 实际成交价偏差 ──
    dev_all: List[float] = []
    dev_long: List[float] = []
    dev_short: List[float] = []
    for _sym, direction, entry, exe, _sl, _tp in rows:
        try:
            entry = float(entry); exe = float(exe)
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        dev = abs(exe - entry) / entry * 100.0
        dev_all.append(dev)
        (dev_short if _is_short(direction) else dev_long).append(dev)

    print("── 分析1: LLM 计划 entry_price vs 实际成交 execution_price 偏差 ──")
    print(f"  全部 : {_pct_stats(dev_all)}")
    print(f"  LONG : {_pct_stats(dev_long)}")
    print(f"  SHORT: {_pct_stats(dev_short)}")
    big = sum(1 for d in dev_all if d > 2.0)
    print(f"  偏差 >2% 的样本: {big}/{len(dev_all)} ({big / len(dev_all) * 100:.0f}%) "
          f"→ 这些 LLM entry 与实际成交显著不同, 印证数字被执行端覆盖\n")

    # ── 分析2: 实际成交价是否落在单一真源 band 内 ──
    contained = 0
    outside = 0
    below = 0
    above = 0
    total = 0
    for _sym, direction, entry, exe, _sl, _tp in rows:
        try:
            entry = float(entry); exe = float(exe)
        except (TypeError, ValueError):
            continue
        low, high = compute_entry_band(entry, _is_short(direction))
        if low is None or high is None:
            continue
        total += 1
        if low <= exe <= high:
            contained += 1
        else:
            outside += 1
            if exe < low:
                below += 1
            else:
                above += 1

    print("── 分析2: 实际成交价 vs compute_entry_band 区间 (扁平 3%/5%) ──")
    if total:
        print(f"  落在 band 内: {contained}/{total} ({contained / total * 100:.0f}%)")
        print(f"  落在 band 外: {outside}/{total} ({outside / total * 100:.0f}%) "
              f"[低于下限 {below}, 高于上限 {above}]")
        print(f"  → band 外比例即当前扁平带宽的漏接/失真面; 为 Q2 波动率化提供依据\n")

    fill_selfcheck()

    print("── 延后 (数据不足, 诚实记录) ──")
    print("  · SL/TP 代码重算: 需 atr_at_generation 等历史技术快照 (当前未存)")
    print("  · entry_low/high/invalidation 对齐: 待次交易日 DA 新契约产出后复跑")
    print("=" * 66)


if __name__ == "__main__":
    main()
