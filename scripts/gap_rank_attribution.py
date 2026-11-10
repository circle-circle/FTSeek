#!/usr/bin/env python3
"""|盘前跳空| 排序的每日只读记账（DEB-20261106 §43/§44/§45 落地件）。

回答的问题只有一个：
  今天 DA 实际做的那几笔里，如果只做「|盘前跳空| 最小」的 3 只，结果会怎样？

性质：**只读**。不写 playbook、不下单、不改任何生产表；只追加 data/shadow/gap_rank_attribution.jsonl。
      每天一行，按日期幂等（重跑覆盖当天，不会重复计数）。

为什么记账而不是直接改策略：
  §43/§44 两个窗口同号（60 天 +$10.67/笔 t=2.73；23 天 +$8.92/笔 t=0.93），
  但都是**事后分组**、全 sim、组合层规则（会少做 61% 的笔数），
  够不上"改执行"的门槛；先记账攒足前向样本，再单独 RFC 决定是否上执行层。

用法:
  venv/bin/python scripts/gap_rank_attribution.py                 # 最近一个有成交的交易日
  venv/bin/python scripts/gap_rank_attribution.py --date 2026-11-06
  venv/bin/python scripts/gap_rank_attribution.py --backfill      # 回填全部历史
  venv/bin/python scripts/gap_rank_attribution.py --summary       # 只看累计
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "shadow" / "gap_rank_attribution.jsonl"
TOP_N = 3


def _gap(meta_json: str | None) -> float | None:
    try:
        m = json.loads(meta_json or "{}")
    except Exception:
        return None
    eg = m.get("entry_gap") or {}
    if not isinstance(eg, dict):
        return None
    v = eg.get("aligned_gap_pct")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def day_row(conn: sqlite3.Connection, day: str) -> dict | None:
    rows = list(conn.execute(
        """SELECT symbol, direction, pnl_net, metadata_json
           FROM trade_records
           WHERE trade_date = ? AND realized_pnl IS NOT NULL AND decision_id LIKE 'DA-%'""",
        (day,)))
    if not rows:
        return None
    items, missing = [], 0
    for r in rows:
        g = _gap(r[3])
        if g is None:
            missing += 1
            continue
        items.append({"symbol": r[0], "dir": r[1], "net": r[2] or 0.0, "gap": g})
    if len(items) < 2:
        return {"date": day, "all_n": len(rows), "gap_missing": missing,
                "note": "有 gap 的笔数 <2，无法排序", "pick": [], "net_pick": None,
                "net_all": round(sum(r[2] or 0 for r in rows), 2), "delta": None}
    pick = sorted(items, key=lambda x: abs(x["gap"]))[:TOP_N]
    net_all = sum(x["net"] for x in items)
    net_pick = sum(x["net"] for x in pick)
    return {
        "date": day,
        "all_n": len(items),
        "gap_missing": missing,
        "all_per_trade": round(net_all / len(items), 2),
        "net_all": round(net_all, 2),
        "pick": [x["symbol"] for x in pick],
        "pick_gap": [round(abs(x["gap"]), 3) for x in pick],
        "net_pick": round(net_pick, 2),
        "pick_per_trade": round(net_pick / len(pick), 2),
        "delta_per_trade": round(net_pick / len(pick) - net_all / len(items), 2),
        "delta_total": round(net_pick - net_all, 2),
    }


def load_ledger() -> dict:
    if not OUT.exists():
        return {}
    led = {}
    for line in OUT.read_text().splitlines():
        try:
            r = json.loads(line)
            led[r["date"]] = r
        except Exception as exc:
            print(f"[gap_rank_attribution] 坏台账行跳过: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
    return led


def save(led: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for d in sorted(led):
            fh.write(json.dumps(led[d], ensure_ascii=False) + "\n")


def summarize(led: dict, tail: int = 12) -> None:
    rows = [r for r in led.values() if r.get("delta_per_trade") is not None]
    if not rows:
        print("账本为空")
        return
    rows.sort(key=lambda r: r["date"])
    diffs = [r["delta_per_trade"] for r in rows]
    m, sd = statistics.mean(diffs), statistics.pstdev(diffs)
    t = m / (sd / len(diffs) ** 0.5) if sd and len(diffs) > 2 else None
    pos_days = sum(1 for d in diffs if d > 0)
    print("\n最近 %d 天:" % min(tail, len(rows)))
    print("  %-12s %4s %10s %10s %10s %8s" % ("日期", "笔数", "全体笔均", "取3笔均", "差额/笔", "选中"))
    for r in rows[-tail:]:
        print("  %-12s %4d %10.2f %10.2f %10.2f %8s" % (
            r["date"], r["all_n"], r["all_per_trade"], r["pick_per_trade"],
            r["delta_per_trade"], ",".join(s.replace("US.", "") for s in r["pick"])[:28]))
    print("\n累计 %d 天: 差额笔均 %+.2f | 日聚类 t=%s | 正差天数 %d/%d" % (
        len(rows), m, ("%.2f" % t) if t is not None else "n/a", pos_days, len(rows)))
    print("口径: 差额 = 取|gap|最小时每日笔均 − 当日全体笔均（同日配对，只含 gap 非空的笔）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--brief", action="store_true", help="cron 用：只输出一行；无成交则静默退出 1")
    a = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    led = load_ledger()

    if a.summary:
        summarize(led)
        return 0

    if a.backfill:
        days = [r[0] for r in conn.execute(
            """SELECT DISTINCT trade_date FROM trade_records
               WHERE realized_pnl IS NOT NULL AND decision_id LIKE 'DA-%' ORDER BY trade_date""")]
        added = 0
        for d in days:
            row = day_row(conn, d)
            if row:
                led[d] = row
                added += 1
        save(led)
        print("回填 %d 个交易日 → %s" % (added, OUT))
        summarize(led)
        return 0

    day = a.date
    if not day:
        r = conn.execute("""SELECT MAX(trade_date) FROM trade_records
                            WHERE realized_pnl IS NOT NULL AND decision_id LIKE 'DA-%'""").fetchone()
        day = r[0]
    row = day_row(conn, day)
    if not row:
        print("%s 无 DA 已平仓记录" % day)
        return 1
    led[day] = row
    save(led)
    if a.brief:
        rows = [x for x in led.values() if x.get("delta_per_trade") is not None]
        diffs = [x["delta_per_trade"] for x in rows]
        m, sd = statistics.mean(diffs), statistics.pstdev(diffs)
        t = m / (sd / len(diffs) ** 0.5) if sd and len(diffs) > 2 else None
        print("📊 %s | %d 笔 全体 %+.2f/笔 → 取3只 %+.2f/笔 (差 %+.2f) | 选中 %s | 累计 %d 天 差 %+.2f/笔 t=%s" % (
            row["date"], row["all_n"], row["all_per_trade"] or 0, row["pick_per_trade"] or 0,
            row["delta_per_trade"] or 0, ",".join(s.replace("US.", "") for s in row["pick"]),
            len(rows), m, ("%.2f" % t) if t is not None else "n/a"))
        return 0
    print(json.dumps(row, ensure_ascii=False, indent=2))
    summarize(led)
    return 0


if __name__ == "__main__":
    sys.exit(main())