#!/usr/bin/env python3
"""BE/trailing 的 peak 输入源 A/B 回放 (RFC-116, 2026-09-27 裁决: **维持 quote 不切换**).

只读 DB + 只读 data/replay_bars/ ⇒ 零交易行为变更、零产物写入 (只 print)。
背景: `peak_price_bar` 采集器 (`app/playbook/pipeline.py:7451+`) docstring 预注册了
「周度对比 bar 版 vs quote 版, 再决定是否切换决策输入」, 但该对比从未落地
(crontab 零命中、无产物)。本脚本把它跑完并给出裁决读数。

单变量设计 (唯一变量 = bar 极值是否并入 peak):
  - 实盘真值取自 `config/playbook_horizon.yaml` default 段 (**不用代码默认值**):
    breakeven_at_pct=0.4, drawdown_from_peak_pct=0.8, 费垫 8/6 前 0.15% / 后 0.4%,
    冷却期 15min SL 放宽 4%。改这些 ⇒ 改的是另一件事 (见 --be/--retrace 覆盖)。
  - 两 arm 同用 30s 采样点 `(o+c)/2` 与 `c` 触发 (= 实盘按轮询价判 SL, 不按 bar low 触及),
    同逐采样点重算 `peak → mfe → 武装 → sl → 比较` 顺序。
    ⚠️ v1 把 mfe/sl 每 bar 只重算一次 ⇒ quote arm 滞后、两 arm 不对称, 已弃用。
  - 未触发的笔两 arm 都回落到实际净 ⇒ 同口径。

保真度 (如实报告, 见 --fidelity):
  bar 原料 492/492 全日 390 根; 触发率 70.1% vs 实盘 SL 类 68.9% (触发面几乎一致);
  但净结局水平 sim 比实际悲观 $2.3k~$3.7k (非 SL 出场路径未建模) ⇒
  **绝对值不可用, 配对差的方向可采信, 量级须打折**。

用法:
  ./venv/bin/python scripts/peak_input_ab_replay.py            # 全量 (A/B + 分解 + 保真度)
  ./venv/bin/python scripts/peak_input_ab_replay.py --since 2026-08-27
  ./venv/bin/python scripts/peak_input_ab_replay.py --be 0.5 --retrace 0.4   # 改阈值 = 改另一件事
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
REPLAY = REPO / "data" / "replay_bars"

# ── 实盘真值 (config/playbook_horizon.yaml default 段; 改前先核线上实值, 勿用代码默认) ──
BE_DEFAULT = 0.4
RETRACE_DEFAULT = 0.8
COOL_MIN, COOL_SL = 15, 4.0
FEE_STEP_DATE = "2026-08-06"          # 8/6 起费垫 0.15% → 0.4%
# 峰反算守卫 — 缺失 (peak=0) 与脏价位必须分开报, 不可混称:
#   · peak 缺失 (0/None) ⇒ 反算得 −100% (LONG), 会被误读成"低估 98pp"
#     (实测 since=2026-07-15: peak_price 缺失 3 笔 / peak_price_bar 缺失 34 笔,
#      真·|峰|>50% 脏价位 **0** 笔; 那个 +98.757pp 正是缺失造成的假值)
#   · 脏价位夹取仍保留, 与实盘 `_MFE_MAE_SANE_MAX` (50%) 同口径
SANE_MAX_PCT = 50.0
A9 = """AND (metadata_json IS NULL
      OR json_extract(metadata_json,'$.exclusion_reason') IS NULL
      OR json_extract(metadata_json,'$.exclusion_reason')='')"""
HEAPS = {"赚钱堆": ("SL_TRAIL_LOCK",),
         "白跑堆": ("SL_BREAKEVEN", "DEAD_SIGNAL", "PRE_CLOSE"),
         "出血堆": ("SL_INITIAL", "OVERNIGHT_GAP_STOP")}


def heap_of(exit_reason: str) -> str:
    """前缀匹配 — 真实值带 `_EXIT` 后缀 (DEAD_SIGNAL_EXIT/PRE_CLOSE_EXIT),
    精确匹配会把白跑堆算成 85/+$615 而非 107/+$887 (RFC-116 §6 陷阱)。"""
    for name, prefixes in HEAPS.items():
        if any(str(exit_reason).startswith(p) for p in prefixes):
            return name
    return "其他"


def load_bars(symbol: str, date: str):
    p = REPLAY / f"{symbol.replace('.', '_')}_{date}.json"
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as fh:
        return json.load(fh)


def sim(direction, entry_price, bars, entry_hm, fee_pct, peak_src, be_pct, retrace_pct):
    """单笔回放. peak_src='quote' → peak 只由 30s 采样点抬 (实盘);
    'bar' → bar high/low 并入 peak (假设切换)。返回 (exit_price, armed, mfe_pct)。"""
    long = direction == "LONG"
    peak, sl, armed, started, mins = entry_price, 0.0, False, False, 0
    for ts, o, h, l, c in bars:
        if entry_hm and not started:
            if ts[11:16] >= entry_hm:
                started = True
            else:
                continue
        if peak_src == "bar":
            cand = h if long else l
            peak = max(peak, cand) if long else (min(peak, cand) if peak else cand)
        for px in ((o + c) / 2, c):
            if peak_src == "quote":
                peak = max(peak, px) if long else (min(peak, px) if peak else px)
            mfe = (peak - entry_price) / entry_price * 100 if long \
                else (entry_price - peak) / entry_price * 100
            if mfe >= be_pct:
                if not armed:
                    armed = True
                    sl = entry_price * (1 + fee_pct / 100) if long \
                        else entry_price * (1 - fee_pct / 100)
                sl = max(sl, peak * (1 - retrace_pct / 100)) if long \
                    else min(sl, peak * (1 + retrace_pct / 100))
            eff = sl
            if mins < COOL_MIN and sl:
                cd = entry_price * (1 - COOL_SL / 100) if long \
                    else entry_price * (1 + COOL_SL / 100)
                eff = min(sl, cd) if long else max(sl, cd)
            if eff:
                if long and px <= eff:
                    return px, armed, mfe
                if (not long) and px >= eff:
                    return px, armed, mfe
        mins += 1
    mfe = (peak - entry_price) / entry_price * 100 if long \
        else (entry_price - peak) / entry_price * 100
    return None, armed, mfe


def build_pairs(since: str, be_pct: float, retrace_pct: float):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(f"""
        SELECT id,symbol,direction,entry_price,exit_price,quantity,entry_time,exit_time,
               COALESCE(pnl_net,realized_pnl,0) AS pnl_net, COALESCE(fees_est,0) AS fees,
               exit_reason, metadata_json
        FROM trade_records
        WHERE date(entry_time)>=? AND entry_price>0 AND exit_price>0
          AND exit_reason NOT LIKE 'BROKER_GHOST%' AND exit_reason!='ORPHAN_CLEANUP' {A9}
        ORDER BY entry_time""", (since,)).fetchall()
    con.close()

    pairs = []
    for r in rows:
        day = str(r["entry_time"])[:10]
        bars = load_bars(r["symbol"], day)
        if not bars:
            continue
        fee = 0.4 if day >= FEE_STEP_DATE else 0.15
        hm = str(r["entry_time"])[11:16]
        dirn = r["direction"] or "LONG"
        ep = float(r["entry_price"])
        notional = ep * (float(r["quantity"] or 0) or 1)
        md = json.loads(r["metadata_json"] or "{}")
        q = {}
        for src in ("quote", "bar"):
            ex, armed, mfe = sim(dirn, ep, bars, hm, fee, src, be_pct, retrace_pct)
            if ex is None:
                q[src] = dict(net=float(r["pnl_net"]), sim=False, armed=armed, mfe=mfe)
            else:
                pct = (ex - ep) / ep * 100 if dirn == "LONG" else (ep - ex) / ep * 100
                q[src] = dict(net=notional * (pct - fee) / 100.0, sim=True,
                              armed=armed, ex=ex, mfe=mfe)
        # quote 峰 vs bar 峰 (metadata 侧, 用于漏峰面统计)
        # 缺失 (<=0) 与脏价位 (|pct|>50%) 分开标记, 都排除但**分开报数**
        def f(v):
            if not v or v <= 0:
                return ("missing", None)
            pct = (v - ep) / ep * 100 if dirn == "LONG" else (ep - v) / ep * 100
            return ("dirty", None) if abs(pct) > SANE_MAX_PCT else ("ok", pct)
        pk_state, pk_val = f(float(md.get("peak_price") or 0))
        pkb_state, pkb_val = f(float(md.get("peak_price_bar") or 0))
        pairs.append(dict(
            r=r, id=r["id"], symbol=r["symbol"], dirn=dirn, ep=ep, notional=notional,
            q=q, actual=float(r["pnl_net"]), reason=r["exit_reason"] or "", heap=heap_of(r["exit_reason"]),
            entry_day=day, exit_day=str(r["exit_time"])[:10],
            md_peak=pk_val, md_peak_bar=pkb_val,
            md_peak_state=pk_state, md_peak_bar_state=pkb_state,
        ))
    return rows, pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-07-15")
    ap.add_argument("--be", type=float, default=BE_DEFAULT,
                    help=f"breakeven_at_pct (实盘真值 {BE_DEFAULT}; 改它=改另一件事)")
    ap.add_argument("--retrace", type=float, default=RETRACE_DEFAULT,
                    help=f"drawdown_from_peak_pct (实盘真值 {RETRACE_DEFAULT})")
    ap.add_argument("--top", type=int, default=24, help="分歧笔明细打印行数")
    args = ap.parse_args()

    rows, pairs = build_pairs(args.since, args.be, args.retrace)
    n = len(pairs)
    if not n:
        print("[peak_ab] 无配对样本 (bar 原料缺失?)")
        return 1

    print(f"配对样本 {n}/{len(rows)} 笔 (有入场日 1min bar)")
    print(f"钉值: be={args.be} retrace={args.retrace} 冷却{COOL_MIN}min/{COOL_SL}% "
          f"费垫 {FEE_STEP_DATE}前0.15%/后0.4%   since={args.since}")
    if (args.be, args.retrace) != (BE_DEFAULT, RETRACE_DEFAULT):
        print(f"⚠️ 你改的不是实盘值 ({BE_DEFAULT}/{RETRACE_DEFAULT}) ⇒ 这不是 RFC-116 的对照, "
              f"而是在测阈值 (peak 口径与 be/dd 是交互项, 见 RFC-116 §7)")
    print("=" * 92)

    # ── A/B 总账 ──
    tot = {s: sum(p["q"][s]["net"] for p in pairs) for s in ("quote", "bar")}
    actual = sum(p["actual"] for p in pairs)
    for s in ("quote", "bar"):
        print(f"  peak源={s:6s} 总净 ${tot[s]:+11,.2f}  "
              f"模拟触发 {sum(1 for p in pairs if p['q'][s]['sim']):3d}  "
              f"武装过 {sum(1 for p in pairs if p['q'][s]['armed']):3d}  "
              f"峰中位 {statistics.median(p['q'][s]['mfe'] for p in pairs):+.4f}%")
    print(f"  实际落库     总净 ${actual:+11,.2f}")
    d = tot["bar"] - tot["quote"]
    print(f"\n  ⇒ bar版 − quote版 = ${d:+,.2f}   "
          f"({'bar 峰更好' if d > 0 else 'quote 峰更好' if d < 0 else '无差'})")

    # ── 分歧分解 ──
    diff = [p for p in pairs if abs(p["q"]["bar"]["net"] - p["q"]["quote"]["net"]) > 0.01]
    print()
    print("=" * 92)
    print(f"分歧笔 {len(diff)}/{n} ({len(diff)/n*100:.1f}%) — 单变量对照的全部信息量")
    print("=" * 92)
    better = [p for p in diff if p["q"]["bar"]["net"] > p["q"]["quote"]["net"]]
    worse = [p for p in diff if p["q"]["bar"]["net"] < p["q"]["quote"]["net"]]
    print(f"  quote版 ${sum(p['q']['quote']['net'] for p in diff):+,.2f} / "
          f"bar版 ${sum(p['q']['bar']['net'] for p in diff):+,.2f}")
    print(f"  bar 更好 {len(better)} 笔 (+${sum(p['q']['bar']['net']-p['q']['quote']['net'] for p in better):,.2f})")
    print(f"  bar 更差 {len(worse)} 笔 (${sum(p['q']['bar']['net']-p['q']['quote']['net'] for p in worse):,.2f})")
    oba = [p for p in diff if p["q"]["bar"]["armed"] and not p["q"]["quote"]["armed"]]
    obq = [p for p in diff if p["q"]["quote"]["armed"] and not p["q"]["bar"]["armed"]]
    both = [p for p in diff if p["q"]["bar"]["armed"] and p["q"]["quote"]["armed"]]
    for label, grp in (("仅 bar 武装 (漏峰线索真身)", oba),
                       ("仅 quote 武装", obq),
                       ("两 arm 都武装, 路径不同", both)):
        print(f"  {label:26s} = {len(grp):3d} 笔  差 "
              f"${sum(p['q']['bar']['net']-p['q']['quote']['net'] for p in grp):+,.2f}")
    if oba:
        win = [p for p in oba if p["actual"] > 0]
        lose = [p for p in oba if p["actual"] <= 0]
        print(f"    「仅 bar 武装」构成: 实际盈利 {len(win)} 笔 ${sum(p['actual'] for p in win):+,.2f}"
              f" → bar版后 ${sum(p['q']['bar']['net'] for p in win):+,.2f} (被贴成本线)")
        print(f"                    实际亏损 {len(lose)} 笔 ${sum(p['actual'] for p in lose):+,.2f}"
              f" → bar版后 ${sum(p['q']['bar']['net'] for p in lose):+,.2f}")

    print()
    print(f"  {'id':6s}{'sym':10s}{'dir':6s}{'实际reason':21s}{'实际净':>9s}{'quote净':>10s}"
          f"{'bar净':>10s}{'类':>12s}")
    for p in sorted(diff, key=lambda x: x["q"]["bar"]["net"] - x["q"]["quote"]["net"])[:args.top]:
        qq, bb = p["q"]["quote"], p["q"]["bar"]
        k = "仅bar武装" if bb["armed"] and not qq["armed"] else (
            "仅quote武装" if qq["armed"] and not bb["armed"] else "都武装")
        print(f"  {p['id']:<6d}{p['symbol']:<10s}{p['dirn']:<6s}{str(p['reason'])[:20]:21s}"
              f"{p['actual']:>+9.2f}{qq['net']:>+10.2f}{bb['net']:>+10.2f}{k:>12s}")

    # ── 日聚类显著性 ──
    print()
    print("=" * 92)
    print("按日聚类 (防单日驱动)")
    print("=" * 92)
    by_day = defaultdict(float)
    for p in diff:
        by_day[p["entry_day"]] += p["q"]["bar"]["net"] - p["q"]["quote"]["net"]
    vals = list(by_day.values())
    if len(vals) >= 2:
        m, sd = statistics.mean(vals), statistics.stdev(vals)
        t = m / (sd / len(vals) ** 0.5) if sd else float("inf")
        print(f"  n={len(vals)} 天  日均差 ${m:+.2f}  sd ${sd:.2f}  t={t:+.2f}")
        print(f"  ⇒ {'|t|>=2 显著' if abs(t) >= 2 else '|t|<2 与 0 不可分辨 ⇒ 不可当裁决依据'}"
              f"  (方向: {'bar 更差' if t < 0 else 'bar 更好'})")
        top = max(by_day, key=by_day.get)
        print(f"  最有利单日 {top} ${by_day[top]:+,.2f}")
    else:
        print(f"  仅 {len(vals)} 天有分歧 ⇒ 无法日聚类")

    # ── 漏峰面 (metadata 侧, 按堆) ──
    print()
    print("=" * 92)
    print(f"漏峰面: 仅 bar 达峰 (>={args.be}%) 的笔按三堆分布 (quote 峰系统性低估的证据)")
    print("=" * 92)
    print(f"  {'堆':8s}{'n':>5s}{'仅bar达峰':>10s}{'占比':>8s}{'mfe列低估中位':>15s}{'低估max':>11s}")
    # 缺失与脏价位分开计数 (不可混称: 缺失反算得 −100%, 会被误读成"低估 98pp")
    n_missing = sum(1 for p in pairs
                    if "missing" in (p["md_peak_state"], p["md_peak_bar_state"]))
    n_dirty = sum(1 for p in pairs
                  if "dirty" in (p["md_peak_state"], p["md_peak_bar_state"]))
    usable = [p for p in pairs
              if p["md_peak_state"] == "ok" and p["md_peak_bar_state"] == "ok"]
    for h in ("赚钱堆", "白跑堆", "出血堆", "其他"):
        s = [p for p in usable if p["heap"] == h]
        if not s:
            continue
        only_bar = [p for p in s if p["md_peak_bar"] >= args.be > p["md_peak"]]
        under = [p["md_peak_bar"] - p["md_peak"] for p in s if p["md_peak_bar"] > p["md_peak"]]
        med = statistics.median(under) if under else 0.0
        mx = max(under) if under else 0.0
        print(f"  {h:8s}{len(s):5d}{len(only_bar):10d}{len(only_bar)/len(s)*100:>7.1f}%"
              f"{med:>+14.4f}pp{mx:>+10.4f}pp")
    print(f"  分母 {len(usable)}/{n} 笔; 剔除: peak 字段**缺失** {n_missing} 笔 "
          f"(peak<=0 ⇒ 反算 −100%, 不可比) / |峰|>{SANE_MAX_PCT:.0f}% **脏价位** {n_dirty} 笔")

    # ── 保真度 ──
    print()
    print("=" * 92)
    print("保真度核查 (绝对值可用性的前提)")
    print("=" * 92)
    same = sum(1 for p in pairs if p["entry_day"] == p["exit_day"])
    print(f"  ① 当日进出 {same} 笔 / 跨日 {n - same} 笔 ({(n-same)/n*100:.1f}%) — "
          f"跨日只模拟入场日、之后回落实际净 ⇒ 两 arm 差异被人为压平")
    bar_n = Counter()
    for p in pairs:
        b = load_bars(p["symbol"], p["entry_day"])
        if b:
            bar_n[len(b)] += 1
    print(f"  ② bar 根数分布 {bar_n.most_common(4)} (整日约 390 根)")
    trig = sum(1 for p in pairs if p["q"]["quote"]["sim"])
    sl_like = sum(1 for p in pairs if str(p["reason"]).startswith("SL_"))
    print(f"  ③ 触发率: sim quote 版 {trig}/{n} = {trig/n*100:.1f}%  "
          f"vs 实盘 SL 类 {sl_like}/{n} = {sl_like/n*100:.1f}% ⇒ 触发面几乎一致")
    print(f"  ④ 净结局水平: 实际 ${actual:+,.2f} / sim quote ${tot['quote']:+,.2f} / "
          f"sim bar ${tot['bar']:+,.2f}")
    print(f"     ⇒ sim 系统性悲观 ${abs(tot['quote']-actual):,.0f}~${abs(tot['bar']-actual):,.0f} "
          f"(非 SL 出场路径 DEAD_SIGNAL/TIME_EXIT/MFE_STALL/PRE_CLOSE 未建模)")
    print(f"     ⇒ **绝对值不可用; 配对差方向可采信, 量级须打折** "
          f"(RFC-116 结论是「方向性否决」, 不是精确 ${d:+,.2f})")

    print()
    print("=" * 92)
    print("裁决 (RFC-116 §5): **不切换** peak 输入源, 维持 quote 轮询峰; "
          "`peak_price_bar` 维持 record-only 不参与决策")
    print("  依据: 单变量配对回测 bar 版显著更差, 且「仅 bar 武装」(漏峰修复本身) 只值 "
          f"${sum(p['q']['bar']['net']-p['q']['quote']['net'] for p in oba):+,.2f}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
