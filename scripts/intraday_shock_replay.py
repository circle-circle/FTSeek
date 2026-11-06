#!/usr/bin/env python3
"""盘中指数冲击 × LONG 结局 只读回放 (DEB-20261106 Phase 0).

与 DEB-20260924 的差别（**新特征**，逐字遵守其 §三.6「重开须新特征 + 重新预注册」条款）:
  - 20260924 轴 = **盘前 08:00 ET** 指数涨跌（决策时点 = DA 盘前生成）
  - 本轴      = **入场那一刻** SPY 相对上一交易日收盘的盘中涨跌
                （决策时点 = 执行轮询，覆盖 09:30–16:00 全时段，含盘中崩盘）

数据源（全部只读）:
  - data/ftseek_us.db::trade_records        方向/入场时刻/结局 (净额列 pnl_net)
  - data/cache/daily_kline_v1.json::US.SPY  日K, 只取「上一交易日收盘」当基准
  - 富途历史 5m K 线 (US.SPY, session=ALL)  按日拉取并缓存, 见 --cache-dir

零副作用: 不写 DB / 不改 config / 无 cron / 不下单 / 不产出被任何生产代码消费的文件。

用法:
  cd /opt/ftseek/FTSeek-US && PYTHONPATH=. venv/bin/python3.11 scripts/intraday_shock_replay.py \
      --since 2026-07-02 --until 2026-11-05

回滚: 删本文件（+ 删 --cache-dir 下的缓存）。
"""
import argparse
import json
import os
import random
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ftseek_us.db")
KLINE_JSON = os.path.join(ROOT, "data", "cache", "daily_kline_v1.json")
GETK = "/root/.hermes/skills/futuapi/scripts/quote/get_kline.py"
PY = os.path.join(ROOT, "venv", "bin", "python3.11")
ERA = ("AND trade_date >= '2026-07-01' AND COALESCE(data_quality,'') IN ('clean','real') "
       "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL')")


def prev_closes():
    """{日期: (前一交易日, 前收)} —— 只用于把 5m close 折成「相对昨收的盘中涨跌%」。"""
    bars = json.load(open(KLINE_JSON))["symbols"]["US.SPY"]["bars"]
    out = {}
    for i in range(1, len(bars)):
        out[bars[i]["time"][:10]] = (bars[i - 1]["time"][:10], bars[i - 1]["close"])
    return out


def spy5m(day, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    fp = os.path.join(cache_dir, day + ".json")
    if os.path.exists(fp):
        return json.load(open(fp))
    r = subprocess.run([PY, GETK, "US.SPY", "--ktype", "5m", "--start", day,
                        "--end", day, "--session", "ALL", "--json"],
                       capture_output=True, text=True, timeout=180)
    line = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
    if not line:
        print("  [warn] no kline json for %s: %s" % (day, r.stderr[-160:]), file=sys.stderr)
        return None
    data = json.loads(line[0])["data"]
    out = [(b["time"][:19], b["close"]) for b in data]
    json.dump(out, open(fp, "w"))
    return out


def chg_at(bars, ts):
    """ts='YYYY-MM-DD HH:MM:SS' (ET). 取 <= ts 的最后一根 5m close。"""
    best = None
    for t, c in bars:
        if t <= ts:
            best = c
        else:
            break
    return best


def bucket(x):
    if x <= -2.0:
        return "A <=-2%"
    if x <= -1.0:
        return "B -2~-1%"
    if x <= -0.5:
        return "C -1~-0.5%"
    if x <= 0:
        return "D -0.5~0%"
    if x <= 0.5:
        return "E 0~+0.5%"
    return "F >+0.5%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-02")
    ap.add_argument("--until", default="2026-11-05")
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, "data", "replay_cache", "spy5m"))
    ap.add_argument("--trace-day", default=None,
                    help="打印该交易日 SPY 5m 相对昨收的路径与首次穿越阈值时刻 (审计用)")
    args = ap.parse_args()

    pc = prev_closes()
    if args.trace_day:
        day = args.trace_day
        bars = spy5m(day, args.cache_dir)
        if not bars:
            print("no bars for", day)
            return
        prev = pc[day][1]
        print("== %s SPY 5m 相对昨收 %.2f 的盘中路径 ==" % (day, prev))
        for t, c in bars:
            hhmm = t[11:16]
            if hhmm in ("09:30", "10:00", "10:30", "11:00", "12:00", "13:00", "14:00", "15:00", "15:55"):
                print("  %s close=%.2f chg=%+.2f%%" % (hhmm, c, (c / prev - 1) * 100))
        for th in (-0.5, -1.0, -1.5, -2.0, -3.0):
            hit = next(((t, c) for t, c in bars if (c / prev - 1) * 100 <= th), None)
            print("  首次 <=%+.1f%%: %s" % (th, (hit[0] + " (%.2f%%)" % ((hit[1] / prev - 1) * 100)) if hit else "从未"))
        return
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT trade_date, symbol, direction, owner_strategy, entry_time, pnl_net, "
        "exit_reason, entry_session FROM trade_records WHERE direction='LONG' AND exit_price>0 "
        + ERA + " AND trade_date BETWEEN ? AND ? ORDER BY trade_date", (args.since, args.until))]
    print("LONG n=%d dates=%d window=%s..%s" %
          (len(rows), len({r["trade_date"] for r in rows}), args.since, args.until))

    feats, miss = [], 0
    for r in rows:
        day = r["trade_date"][:10]
        ts = (r["entry_time"] or "").replace("T", " ")[:19]
        if day not in pc or not ts:
            miss += 1
            continue
        bars = spy5m(day, args.cache_dir)
        if not bars:
            miss += 1
            continue
        px = chg_at(bars, ts)
        if not px:
            miss += 1
            continue
        r2 = dict(r)
        r2["spy_chg_at_entry"] = (px / pc[day][1] - 1) * 100
        feats.append(r2)
    print("feature coverage: %d/%d (miss=%d)" % (len(feats), len(rows), miss))

    print("\n== 入场时点 SPY 盘中涨跌 × LONG 结局 (pnl_net, 净$)")
    for b in sorted({bucket(r["spy_chg_at_entry"]) for r in feats}):
        g = [r for r in feats if bucket(r["spy_chg_at_entry"]) == b]
        net = sum(r["pnl_net"] or 0 for r in g)
        win = sum(1 for r in g if (r["pnl_net"] or 0) > 0)
        print("  %-11s n=%3d 日=%2d net=%9.2f avg=%7.2f win=%3d/%3d" %
              (b, len(g), len({r["trade_date"] for r in g}), net, net / len(g), win, len(g)))

    print("\n== 阈值敏感性（入场时点 SPY<=th 的 LONG = 若拦截可避免额）")
    for th in (-0.5, -1.0, -1.5, -2.0):
        g = [r for r in feats if r["spy_chg_at_entry"] <= th]
        if not g:
            print("  th=%+.1f%%  n=0" % th)
            continue
        rest = [r for r in feats if r["spy_chg_at_entry"] > th]
        gd = len({r["trade_date"] for r in g})
        rd = len({r["trade_date"] for r in rest})
        # 日聚类均值差 (分析单位=交易日, 防单日/单笔主导)
        gday, rday = {}, {}
        for r in g:
            gday.setdefault(r["trade_date"], []).append(r["pnl_net"] or 0)
        for r in rest:
            rday.setdefault(r["trade_date"], []).append(r["pnl_net"] or 0)
        gv = [sum(v) / len(v) for v in gday.values()]
        rv = [sum(v) / len(v) for v in rday.values()]
        if len(gv) >= 2 and len(rv) >= 2:
            random.seed(20261106)
            ds = []
            for _ in range(2000):
                sa = [random.choice(gv) for _ in gv]
                sb = [random.choice(rv) for _ in rv]
                ds.append(sum(sa) / len(sa) - sum(sb) / len(sb))
            ds.sort()
            ci = "CI95[%+.2f,%+.2f]" % (ds[50], ds[1949])
        else:
            ci = "[unpowered: 冲击组仅 %d 日, 不报区间]" % len(gv)
        print("  th=%+.1f%%  n=%3d 日=%2d net=%9.2f win=%3d/%3d | 其余 n=%3d 日=%2d net=%9.2f | 日聚类差 %s" %
              (th, len(g), gd, sum(r["pnl_net"] or 0 for r in g),
               sum(1 for r in g if (r["pnl_net"] or 0) > 0), len(g),
               len(rest), rd, sum(r["pnl_net"] or 0 for r in rest), ci))

    print("\n== 日聚类 bootstrap: 冲击组(-1.5%以下) vs 其余, 净$/笔 均值差 (B=2000)")
    a = [r for r in feats if r["spy_chg_at_entry"] <= -1.5]
    b = [r for r in feats if r["spy_chg_at_entry"] > -1.5]
    da, dbb = {}, {}
    for r in a:
        da.setdefault(r["trade_date"], []).append(r["pnl_net"] or 0)
    for r in b:
        dbb.setdefault(r["trade_date"], []).append(r["pnl_net"] or 0)
    ka = [sum(v) / len(v) for v in da.values()]
    kb = [sum(v) / len(v) for v in dbb.values()]
    if not ka or not kb:
        print("  [unpowered] 某臂无样本: 冲击组日=%d 其余日=%d" % (len(ka), len(kb)))
    else:
        random.seed(20261106)
        diffs = []
        for _ in range(2000):
            sa = [random.choice(ka) for _ in ka]
            sb = [random.choice(kb) for _ in kb]
            diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
        diffs.sort()
        print("  obs=%+.2f CI95[%+.2f,%+.2f] 冲击组日=%d 其余日=%d" % (
            sum(ka) / len(ka) - sum(kb) / len(kb), diffs[50], diffs[1949], len(ka), len(kb)))

    print("\n== 冲击组(-1.5%以下) 逐笔")
    for r in sorted(a, key=lambda x: x["spy_chg_at_entry"]):
        print("  %s %-9s %-22s spy_chg=%+6.2f%% net=%9.2f %s" % (
            r["trade_date"], r["symbol"], r["owner_strategy"], r["spy_chg_at_entry"],
            r["pnl_net"] or 0, r["exit_reason"]))


if __name__ == "__main__":
    main()