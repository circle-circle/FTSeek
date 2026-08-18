#!/usr/bin/env python3
"""DEB-20260818-overnight-reprice-shadow 基线 ET 口径核查 (Qoder 独立复算).

背景: DEB §一初版基线 (89笔/+$18,788/58%; 首30分钟 12笔 avg+0.91% worst-0.15%)
用混合时区字符串日期比较算出。trade_records 时间戳存在三种写法:
  - naive 无后缀 = CST 本地 (与 open_positions.entry_time 同一踩坑)
  - '-04:00' 后缀 = ET
  - '+00:00' 后缀 = UTC
且同一行 entry/exit 可能混用两种写法 (XLK 6月案例)。字符串 date() 比较在
CST 午夜跨越的日内单上会误计为隔夜 (虚增), 在 entry 为 naive 的真隔夜单上
会漏计 (首30分钟 cohort 幸存者偏差, 漏掉的净 -$6,549)。

本脚本统一规范化到 ET 后重算, 并输出 cohort 的时间戳类型/月份分布供时代切分。
"""
import datetime
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"


def to_et(ts):
    if not ts:
        return None
    if ts.endswith('+00:00'):
        off = -4   # UTC -> EDT
    elif ts.endswith('-04:00'):
        off = 0
    elif ts.endswith('-05:00'):
        off = 1
    elif ts.endswith('+08:00'):
        off = -12
    else:
        off = -12  # naive = CST (open_positions 同源踩坑, DEB §四.1)
    try:
        return datetime.datetime.fromisoformat(ts) + datetime.timedelta(hours=off)
    except ValueError:
        return None


def tzkind(ts):
    if ts.endswith('+00:00'):
        return 'utc'
    if ts[-6:].startswith('-'):
        return 'et'
    return 'naive-cst'


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT symbol, pnl_net, pnl_pct, exit_time, entry_time FROM trade_records "
        "WHERE exit_time IS NOT NULL").fetchall()
    conn.close()

    # 对照口径: 字符串日期比较 (初版基线的算法)
    str_on = [r for r in rows if str(r[3])[:10] > str(r[4])[:10]]
    # ET 规范化口径
    et_on = []
    for s, pn, pp, xt, ent in rows:
        e, x = to_et(ent), to_et(xt)
        if e and x and x.date() > e.date():
            et_on.append((s, pn, pp or 0, x, e))

    def stats(cohort, label):
        n = len(cohort)
        net = sum((r[1] or 0) for r in cohort)
        wins = sum(1 for r in cohort if (r[1] or 0) > 0)
        wr = wins / n * 100 if n else 0
        print(f"{label}: n={n} net={net:+,.0f} WR={wins}/{n}={wr:.1f}%")

    stats(str_on, "字符串混合口径 (初版基线算法)")
    stats(et_on, "ET 规范化口径 (真值)        ")

    e30 = [r for r in et_on if '09:30' <= r[3].strftime('%H:%M') <= '10:00']
    avg = sum(r[2] * 100 for r in e30) / len(e30)
    worst = min(r[2] * 100 for r in e30)
    net30 = sum((r[1] or 0) for r in e30)
    wins30 = sum(1 for r in e30 if (r[1] or 0) > 0)
    print(f"\n首30分钟退出 (ET 真值): n={len(e30)} avg={avg:+.2f}% worst={worst:+.2f}% "
          f"net={net30:+,.0f} wins={wins30}")
    for r in sorted(e30, key=lambda x: x[2]):
        print(f"  {r[0]:10s} exit_tz={tzkind(str(r[3])):9s} "
              f"entry_month_et={r[4].strftime('%Y-%m')} "
              f"pnl_pct={r[2]*100:+.2f}% net={r[1]}")


if __name__ == "__main__":
    main()
