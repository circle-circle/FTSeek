# -*- coding: utf-8 -*-
"""关键裁决读数: "落库SL跨DA失效线" 组 vs 未跨组的结局差异 (已平仓子集).

record-only: DB 以 mode=ro 打开; 默认复现 DEB 首轮窗口, --since 可做滚动周读数。
结论必须看按日配对差 + MDE(80%), 不得用 pooled 净额/均值单独裁决。
"""
import argparse
import json
import math
import sqlite3
import statistics as st
from collections import defaultdict

DB_PATH = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
DEFAULT_SINCE = "2026-08-15"


def load_rows(since):
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT i.date, i.symbol, i.direction, i.entry_price, i.stop_loss,
                  i.raw_instruction_json, t.pnl_net, t.exit_reason, t.mfe_pct
           FROM playbook_instructions i
           JOIN trade_records t ON t.decision_id = i.decision_id
                AND t.source_type = 'PLAYBOOK_SIM'
           WHERE i.date >= ? AND i.direction IN ('BUY','SELL_SHORT')
             AND i.entry_price > 0 AND t.pnl_net IS NOT NULL""",
        (since,),
    ).fetchall()
    db.close()
    return rows


def split_rows(rows):
    crossed, clean = [], []
    for row in rows:
        try:
            raw = json.loads(row["raw_instruction_json"] or "{}")
        except Exception:
            continue
        planned_entry = raw.get("planned_entry")
        planned_stop = raw.get("planned_stop_loss")
        landed_entry = row["entry_price"]
        landed_stop = row["stop_loss"]
        values = (planned_entry, planned_stop, landed_entry, landed_stop)
        if not all(isinstance(value, (int, float)) and value > 0 for value in values):
            continue
        short = row["direction"] == "SELL_SHORT"
        price_invs = [
            trigger.get("value")
            for trigger in (raw.get("invalidation_triggers") or [])
            if isinstance(trigger.get("value"), (int, float))
            and trigger.get("value") > 0
            and 0.3 * planned_entry <= trigger.get("value") <= 5.0 * planned_entry
        ]
        if not price_invs:
            continue
        hit = any(
            (short and planned_stop >= inv > landed_stop)
            or (not short and planned_stop <= inv < landed_stop)
            for inv in price_invs
        )
        record = {
            "sym": row["symbol"].split(".")[-1],
            "date": row["date"],
            "pnl": row["pnl_net"],
            "exit": row["exit_reason"],
            "mfe0": row["mfe_pct"] is not None and abs(row["mfe_pct"]) < 1e-9,
        }
        (crossed if hit else clean).append(record)
    return crossed, clean


def group_summary(name, rows):
    if not rows:
        print(f"{name}: n=0")
        return
    pnls = [row["pnl"] for row in rows]
    sl_init = sum(row["exit"] == "SL_INITIAL" for row in rows)
    mfe0 = sum(row["mfe0"] for row in rows)
    print(f"{name}: n={len(rows)} 净{sum(pnls):+.0f} 均{st.mean(pnls):+.1f} "
          f"中位{st.median(pnls):+.1f} "
          f"WR={sum(pnl > 0 for pnl in pnls) / len(pnls) * 100:.0f}% | "
          f"SL_INITIAL {sl_init}({sl_init / len(rows) * 100:.0f}%) "
          f"mfe=0 {mfe0}({mfe0 / len(rows) * 100:.0f}%)")


def day_clustered_t(daily_values):
    """宪章第9律: 日均值序列单样本 t; n<3/零方差 = 不可判, 不得当 0。"""
    n_days = len(daily_values)
    if n_days < 3:
        return None
    day_mean = st.mean(daily_values)
    sd = st.stdev(daily_values)
    if sd == 0:
        return None
    t_value = day_mean / (sd / math.sqrt(n_days))
    return {
        "n_days": n_days,
        "day_mean": day_mean,
        "t": t_value,
        "mde_80pct": 2.802 * sd / math.sqrt(n_days),
        "distinguishable": abs(t_value) >= 2.0,
    }


def paired_day_diffs(crossed, clean):
    crossed_days = defaultdict(list)
    clean_days = defaultdict(list)
    for row in crossed:
        crossed_days[row["date"]].append(row["pnl"])
    for row in clean:
        clean_days[row["date"]].append(row["pnl"])
    return [
        st.mean(crossed_days[date]) - st.mean(clean_days[date])
        for date in sorted(set(crossed_days) & set(clean_days))
    ]


def print_report(since):
    rows = load_rows(since)
    crossed, clean = split_rows(rows)

    print("=" * 78)
    print(f"落库SL跨DA失效线 vs 未跨 — 已平仓结局对比 ({since} 起)")
    print("=" * 78)
    group_summary("跨线组  ", crossed)
    group_summary("未跨组  ", clean)

    diff_stats = day_clustered_t(paired_day_diffs(crossed, clean))
    print()
    if diff_stats is None:
        print("日聚类配对差 (跨-未): 不可判 (<3个双组交易日或零方差)")
    else:
        verdict = "可分辨" if diff_stats["distinguishable"] else "与0不可分辨"
        print(f"日聚类配对差 (跨-未): day_mean {diff_stats['day_mean']:+.2f}/笔 "
              f"t={diff_stats['t']:.2f} n_days={diff_stats['n_days']} "
              f"MDE(80%)=±{diff_stats['mde_80pct']:.2f} ⇒ {verdict}")
    print()
    print("跨线组明细 (看是亏还是赚):")
    for row in sorted(crossed, key=lambda item: item["pnl"]):
        print(f"  {row['date'][5:]} {row['sym']:6s} pnl={row['pnl']:+8.1f} "
              f"{row['exit']:20s} mfe0={row['mfe0']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--since",
        default=DEFAULT_SINCE,
        help="ISO 起始日期 (默认 %(default)s; 周读数可传 7 天前)",
    )
    args = parser.parse_args(argv)
    print_report(args.since)


if __name__ == "__main__":
    main()
