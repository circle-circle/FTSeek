#!/usr/bin/env python3
"""全链路复盘复现器 (AUDIT-20261106)。只读: 仅 SELECT, 不写库/不改配置/不下单。

用途: 让 docs/audits/AUDIT-20261106-full-chain-review.md 里每个数字都可一条命令复现。
口径: A10 era-clean — 复用 app.core.db_utils.ERA_CLEAN_PERFORMANCE_WHERE (单一真源)。

用法:
  cd /opt/ftseek/FTSeek-US && venv/bin/python3.11 scripts/audit_full_chain_20261106.py
  ... --db data/ftseek_us.db --days 30
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db_utils import ERA_CLEAN_PERFORMANCE_WHERE  # noqa: E402


def _pct(x: float) -> str:
    return f"{x:.1%}"


def funnel(c: sqlite3.Cursor, since: str) -> None:
    print("== ① 漏斗 (playbook_instructions, 近 %s 天起)" % since)
    rows = list(c.execute(
        "select row_kind, count(*), sum(executed) from playbook_instructions "
        "where date >= ? group by row_kind order by 2 desc", (since,)))
    for kind, n, ex in rows:
        print(f"   {kind or 'NULL':20s} n={n:5d} executed={ex or 0:4d}")
    total = sum(n for _, n, _ in rows)
    instr = sum(n for k, n, _ in rows if k == "instruction")
    exe = sum(ex or 0 for k, n, ex in rows if k == "instruction")
    print(f"   合计 {total} 行 / instruction {instr} 行 / 成交 {exe} 行 "
          f"= {_pct(exe / instr) if instr else 'n/a'}")
    print("   -- 拒单构成 (仅 instruction 行)")
    buckets: Counter = Counter()
    for (reason, n) in c.execute(
            "select skip_reason, count(*) from playbook_instructions "
            "where date >= ? and row_kind='instruction' group by skip_reason", (since,)):
        r = reason or ""
        if not r:
            k = "EXECUTED"
        elif "entry_confirm" in r:
            k = "entry_confirm_reversed"
        elif "thesis_drift" in r:
            k = "thesis_drift"
        elif "no_trigger" in r or "未触发" in r:
            k = "no_trigger(归因黑洞, 低估)"
        elif "vwap" in r:
            k = "vwap"
        elif "falsification" in r:
            k = "falsification_slope"
        elif "非当前通道" in r:
            k = "non_candidate"
        else:
            k = "other"
        buckets[k] += n
    for k, n in buckets.most_common():
        print(f"   {k:28s} {n:5d}")


def perf(c: sqlite3.Cursor) -> None:
    print("\n== ② A10 口径总账 (era-clean)")
    rows = list(c.execute(
        f"select pnl_net, realized_pnl, fees_est, mfe_pct, mae_pct, signal_confidence, "
        f"direction, entry_price, quantity, market_regime, entry_session, exit_reason "
        f"from trade_records where exit_price > 0 {ERA_CLEAN_PERFORMANCE_WHERE}"))
    n = len(rows)
    nets = [r[0] or 0 for r in rows]
    gross = sum(r[1] or 0 for r in rows)
    fees = sum(r[2] or 0 for r in rows)
    win = [x for x in nets if x > 0]
    loss = [x for x in nets if x <= 0]
    print(f"   n={n} net={sum(nets):.2f} gross={gross:.2f} fees={fees:.2f} "
          f"fee/gross={fees / abs(gross):.0%}")
    print(f"   胜率={_pct(len(win) / n)} avg_win={sum(win) / len(win):.2f} "
          f"avg_loss={sum(loss) / len(loss):.2f} "
          f"payoff={abs((sum(win) / len(win)) / (sum(loss) / len(loss))):.2f} "
          f"中位={st.median(nets):.2f}")
    s = sorted(nets)
    print(f"   top10={sum(s[-10:]):.2f} bottom10={sum(s[:10]):.2f}")

    print("   -- exit_reason (净额升序)")
    ex: dict = defaultdict(lambda: [0, 0.0])
    for r in rows:
        k = r[11] or "NULL"
        ex[k][0] += 1
        ex[k][1] += r[0] or 0
    for k, (cnt, net) in sorted(ex.items(), key=lambda kv: kv[1][1]):
        print(f"   {k:22s} n={cnt:4d} net={net:9.2f} avg={net / cnt:8.2f}")

    print("   -- confidence 分桶 (注明: 2026-11-05 已裁 conf 无判别力, 见报告 §七)")
    b: dict = defaultdict(list)
    for r in rows:
        cf = r[5]
        if cf is None:
            b["None"].append(r[0] or 0)
            continue
        cf = float(cf)
        cf = cf * 100 if cf <= 1 else cf
        k = "<50" if cf < 50 else "50-60" if cf < 60 else "60-70" if cf < 70 else "70-80" if cf < 80 else ">=80"
        b[k].append(r[0] or 0)
    for k in ("<50", "50-60", "60-70", "70-80", ">=80", "None"):
        v = b.get(k) or []
        if v:
            print(f"   {k:6s} n={len(v):4d} net={sum(v):9.2f} avg={sum(v) / len(v):8.2f} "
                  f"win={_pct(sum(1 for x in v if x > 0) / len(v))}")

    print("   -- regime / 方向 / 时段")
    for label, idx in (("regime", 9), ("direction", 6), ("session", 10)):
        g: dict = defaultdict(list)
        for r in rows:
            g[r[idx] or "None"].append(r[0] or 0)
        for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))[:6]:
            print(f"   {label}:{str(k):16s} n={len(v):4d} net={sum(v):9.2f} "
                  f"avg={sum(v) / len(v):8.2f}")

    mf = [r[3] for r in rows if r[3] is not None]
    if mf:
        print(f"   -- MFE: n={len(mf)} 中位={st.median(mf):.2f}% "
              f"p25={sorted(mf)[len(mf) // 4]:.2f}% "
              f"<0.2% 占比={_pct(sum(1 for x in mf if x < 0.2) / len(mf))}")
    notion = [(r[7] or 0) * (r[8] or 0) for r in rows if r[7] and r[8]]
    if notion:
        ns = sorted(notion)
        print(f"   -- 名义额: 中位=${st.median(notion):.0f} p25=${ns[len(ns) // 4]:.0f} "
              f"p75=${ns[len(ns) * 3 // 4]:.0f} | 佣金地板($1.99/腿)盈亏平衡≈$8,964")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "ftseek_us.db"))
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    c = con.cursor()
    since = c.execute("select date('now', ?)", (f"-{args.days} day",)).fetchone()[0]
    funnel(c, since)
    perf(c)
    print("\n说明: 全部为只读 SELECT; 缺口(事件时延/契约违约/entry_confirm 结算)见报告 §三/§四/§五。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())