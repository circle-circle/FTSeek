#!/usr/bin/env python3
"""RFC-116 撤回依据: peak 输入源 A/B 的**采样密度敏感性** — 该轴用此仪器不可判.

实盘退出快循环 = 10s (config/unified_config.yaml playbook_exit_fast_loop_interval_sec: 10,
2026-07-22 `a4c1207e9` 引入 ⇒ RFC-116 回测窗 since=2026-07-15 的绝大部分是 10s 粒度).

实测 (492 笔配对, 只读 DB + 只读 data/replay_bars/, 零产物写入):
  N=2 (30s, RFC-116 原口径):  差 −1,163.53  仅bar武装 16 笔  t=−3.22  pess≡opt
  N=6 (10s, **实盘粒度**):    pess 差 −1,059.30 / opt 差 **+30.52**  仅bar武装 **0** 笔
  N=12 (5s):                  pess 差 −1,138.22 / opt 差 −161.13     仅bar武装 **0** 笔

⇒ 两个致命问题:
  ① N=6 pess/opt **反号** (−1,059 vs +31) ⇒ bar 内 high/low 先后不可知时区间横跨 0 = 不可判
  ② N=2 时采样点 [(o+c)/2, c] **完全不触及 bar 内 high/low** (且 pess≡opt, 当时该当成红旗),
     N>=6 时采样点**包含**极值 ⇒ quote arm 退化成 bar arm。
     ⇒ 两头都不是真 quote 口径: 真实 10s 轮询是**独立于 bar 结构的随机相位采样**,
       有时碰到极值有时碰不到, 而本仪器的 path_points 是确定性折线等距采样。
  ③「仅 bar 武装」N=2→6 从 16 笔降到 **0** 笔 ⇒ RFC-116 §2/§3 那条"漏峰线索"
     是采样密度制造的假象, 不是实盘现象。

结论: **不能**用本仪器裁「是否切换 peak 输入源」。要裁须先修仪器 —
用真实轮询时刻序列 (logs/ 里的退出快循环取价时间戳) 而非 bar 内等距折线重放。

注: RFC-116 §2 的漏峰面统计取自 **metadata 实测落库值** (peak_price vs peak_price_bar),
不受本仪器缺陷影响 ⇒ 那部分数据仍成立 (出血堆 34.4% 仅 bar 达峰);
被撤回的是 §3/§5 的**裁决**与「仅 bar 武装 19 笔只值 +$58」那个分解。

用法: ./venv/bin/python scripts/peak_density_sensitivity.py
"""
import json, sqlite3, statistics, sys
from collections import defaultdict
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data" / "ftseek_us.db"
REPLAY = REPO / "data" / "replay_bars"
BE, RETRACE, COOL_MIN, COOL_SL = 0.4, 0.8, 15, 4.0
FEE_STEP = "2026-08-06"
A9 = """AND (metadata_json IS NULL OR json_extract(metadata_json,'$.exclusion_reason') IS NULL
      OR json_extract(metadata_json,'$.exclusion_reason')='')"""


def path_points(o, h, l, c, n, long, ordering):
    """bar 内 n 个采样点 (折线等距). 顶点序按 pess(不利先)/opt(有利先)."""
    adv, fav = (l, h) if long else (h, l)
    verts = [o, adv, fav, c] if ordering == "pess" else [o, fav, adv, c]
    segs = len(verts) - 1
    pts = []
    for i in range(n):
        f = (i + 1) / n * segs
        k = min(int(f), segs - 1)
        frac = f - k
        pts.append(verts[k] + (verts[k + 1] - verts[k]) * frac)
    return pts


def sim(direction, ep, bars, hm, fee, peak_src, n_pts, ordering):
    long = direction == "LONG"
    peak, sl, armed, started, mins = ep, 0.0, False, False, 0
    for bar in bars:
        ts, o, h, l, c = bar["t"], bar["o"], bar["h"], bar["l"], bar["c"]
        if hm and not started:
            if ts[11:16] >= hm:
                started = True
            else:
                continue
        if peak_src == "bar":
            cand = h if long else l
            peak = max(peak, cand) if long else (min(peak, cand) if peak else cand)
        for px in path_points(o, h, l, c, n_pts, long, ordering):
            if peak_src == "quote":
                peak = max(peak, px) if long else (min(peak, px) if peak else px)
            mfe = (peak - ep) / ep * 100 if long else (ep - peak) / ep * 100
            if mfe >= BE:
                if not armed:
                    armed = True
                    sl = ep * (1 + fee / 100) if long else ep * (1 - fee / 100)
                sl = max(sl, peak * (1 - RETRACE / 100)) if long else min(sl, peak * (1 + RETRACE / 100))
            eff = sl
            if mins < COOL_MIN and sl:
                cd = ep * (1 - COOL_SL / 100) if long else ep * (1 + COOL_SL / 100)
                eff = min(sl, cd) if long else max(sl, cd)
            if eff:
                if long and px <= eff:
                    return px, armed, mfe
                if (not long) and px >= eff:
                    return px, armed, mfe
        mins += 1
    mfe = (peak - ep) / ep * 100 if long else (ep - peak) / ep * 100
    return None, armed, mfe


con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(f"""SELECT id,symbol,direction,entry_price,quantity,entry_time,exit_time,
  COALESCE(pnl_net,realized_pnl,0) AS pnl_net, exit_reason, metadata_json
  FROM trade_records WHERE date(entry_time)>='2026-07-15' AND entry_price>0 AND exit_price>0
  AND exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason!='ORPHAN_CLEANUP' {A9}""").fetchall()
con.close()

jobs = []
for r in rows:
    day = str(r["entry_time"])[:10]
    p = REPLAY / f"{r['symbol'].replace('.', '_')}_{day}.json"
    if not p.exists():
        continue
    raw = json.loads(p.read_text(encoding="utf-8"))
    # 兼容两种 bar 格式: list[ts,o,h,l,c] 或 dict
    bars = []
    for b in raw:
        if isinstance(b, dict):
            bars.append({"t": str(b.get("t") or b.get("time_key") or b.get("time")),
                         "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"])})
        else:
            bars.append({"t": str(b[0]), "o": float(b[1]), "h": float(b[2]),
                         "l": float(b[3]), "c": float(b[4])})
    if not bars:
        continue
    jobs.append(dict(r=r, bars=bars, day=day,
                     fee=0.4 if day >= FEE_STEP else 0.15,
                     hm=str(r["entry_time"])[11:16],
                     dirn=r["direction"] or "LONG", ep=float(r["entry_price"]),
                     notional=float(r["entry_price"]) * (float(r["quantity"] or 0) or 1),
                     actual=float(r["pnl_net"])))

print(f"配对样本 {len(jobs)} 笔")
print(f"实盘粒度: playbook_exit_fast_loop_interval_sec=10 ⇒ 1min bar 内 **6** 次取价")
print("=" * 96)
print(f"{'密度':>6s}{'序':>6s}{'quote总净':>13s}{'bar总净':>13s}{'差(bar-quote)':>15s}{'仅bar武装':>10s}{'其差':>10s}{'t(日聚类)':>11s}")
print("=" * 96)

results = {}
for n_pts in (2, 6, 12):
    for ordering in ("pess", "opt"):
        tot = {"quote": 0.0, "bar": 0.0}
        per = {}
        for j in jobs:
            per[j["r"]["id"]] = {}
            for src in ("quote", "bar"):
                ex, armed, mfe = sim(j["dirn"], j["ep"], j["bars"], j["hm"], j["fee"],
                                     src, n_pts, ordering)
                if ex is None:
                    net = j["actual"]
                else:
                    pct = (ex - j["ep"]) / j["ep"] * 100 if j["dirn"] == "LONG" \
                        else (j["ep"] - ex) / j["ep"] * 100
                    net = j["notional"] * (pct - j["fee"]) / 100.0
                per[j["r"]["id"]][src] = dict(net=net, armed=armed)
                tot[src] += net
        # 分歧分解
        oba, oba_d = [], 0.0
        by_day = defaultdict(float)
        for j in jobs:
            i = j["r"]["id"]
            q, b = per[i]["quote"], per[i]["bar"]
            d = b["net"] - q["net"]
            if abs(d) > 0.01:
                by_day[j["day"]] += d
                if b["armed"] and not q["armed"]:
                    oba.append(i); oba_d += d
        vals = list(by_day.values())
        if len(vals) >= 2:
            m, sd = statistics.mean(vals), statistics.stdev(vals)
            t = m / (sd / len(vals) ** 0.5) if sd else float("inf")
        else:
            t = float("nan")
        results[(n_pts, ordering)] = dict(tot=tot, oba=len(oba), oba_d=oba_d, t=t)
        print(f"{n_pts:>5d}p{ordering:>6s}{tot['quote']:>+13,.2f}{tot['bar']:>+13,.2f}"
              f"{tot['bar']-tot['quote']:>+15,.2f}{len(oba):>10d}{oba_d:>+10,.2f}{t:>+11.2f}")

print("=" * 96)
print()
print("判读:")
print("  · N=2 是 RFC-116 已提交的口径 (30s 采样, 路径假设不同故与已提交数略有差)")
print("  · **N=6 = 实盘 10s 粒度** ⇒ 这一行才是与实盘可比的读数")
print("  · 若 N=2→6 时「差」显著缩小 ⇒ RFC-116 的 −$1,370.83 高估了切换损害 (仪器偏保守)")
print("  · 若 N=6 下「差」仍显著为负 (|t|>=2) ⇒ 裁决「不切换」稳健")
print("  · pess/opt 双序区间 = bar 内 high/low 先后不可知的诚实表达")
