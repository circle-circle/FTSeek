#!/usr/bin/env python3
"""VWAP 门离线评估 (只读, 不碰任何交易逻辑)。

目的: 判定 entry VWAP 方向门是"救命"还是"扼杀" — 被它拦掉的单
  (breakout_triggered=True 且 gate_reason=wrong_side), 若放行的假想盈亏方向。

2026-08-06 修尺子 (DEB-20260806-source-control §二, 宪章白名单"修尺子"):
  ── 原实现的三个度量缺陷 (全部导致结论失真) ──
  1. **不可测量样本被静默计为 +0.00% 并算进均值** (最严重):
     `aligned` 组 144/144 事件的 forward 轨迹长度都是 1 — 因为门放行后该标的
     即成为持仓, entry_timing 影子停止轮询它 → 事件永远是当日最后一个采样点
     → pnl 恒为 (kill-kill)/kill = 0。原脚本把这 144 个"无法测量"当成
     "测得恰好为 0", 输出"正0/负144 均值+0.00%", 读者会误读为"放行组零边际"。
     → 修法: forward 采样点 <2 一律标 UNMEASURABLE 并**排除出统计**, 单独报数。
  2. **出场标签 CLOSE 名不副实**: 轨迹终点是"最后一次轮询"(中位仅 6 个采样点,
     跨度常为分钟级), 不是当日收盘。原标签让读者以为持有到收盘。
     → 修法: 改标 LAST_POLL 并输出实际测量跨度 (分钟), 让读者自己判断口径。
  3. **对照组不可用 → 无基线**: 放行组的真实结果其实在 trade_records 里
     (它们真成交了), 不该从影子轨迹里找。
     → 修法: 对照组改从 trade_records 取**已实现 pnl**, 并明示两组测量基不同、
     不可直接相减。

  ── 仍未解决的混淆 (结论必须打折看待) ──
  - **方向 beta 混淆**: 被拦样本以 BUY 为主, 若窗口内大盘上行, +0.25% 可能是
    市场漂移而非"门拦错"。故本脚本强制输出 BUY/SHORT 分列 — 若仅 BUY 为正而
    SHORT 为负, 应优先解释为 beta 而非门的错误。
  - 离散采样非连续行情 → 日内极值低估, SL/TP 触发时点滞后。

数据源 (全部本地, 零外部调用):
  1. data/entry_timing_shadow.jsonl — 轮询记录 current_price + gate 判定。
  2. data/ftseek_us.db :: playbook_instructions — 真实 SL/TP。
  3. data/ftseek_us.db :: trade_records — 放行组真实已实现 pnl (对照)。

用法:
  ./venv/bin/python scripts/eval_vwap_gate.py
  ./venv/bin/python scripts/eval_vwap_gate.py --days 5
  ./venv/bin/python scripts/eval_vwap_gate.py --min-forward 3   # 提高可测量门槛
"""
import argparse
import json
import os
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHADOW = os.path.join(_ROOT, "data", "entry_timing_shadow.jsonl")
_DB = os.path.join(_ROOT, "data", "ftseek_us.db")

UNMEASURABLE = "UNMEASURABLE"


def _load_shadow(days=None):
    rows = []
    if not os.path.exists(_SHADOW):
        return rows
    with open(_SHADOW, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if days:
        all_days = sorted({r.get("ts", "")[:10] for r in rows})
        keep = set(all_days[-days:])
        rows = [r for r in rows if r.get("ts", "")[:10] in keep]
    return rows


def _build_trajectories(rows):
    """(symbol, day) -> 按时间排序的 [(ts, price)] 采样轨迹。"""
    traj = defaultdict(list)
    for r in rows:
        cp = r.get("current_price")
        if cp:
            traj[(r["symbol"], r["ts"][:10])].append((r["ts"], float(cp)))
    for k in traj:
        traj[k].sort()
    return traj


def _load_sltp():
    """(symbol, date, direction) -> (stop_loss, take_profit)。"""
    out = {}
    if not os.path.exists(_DB):
        return out
    try:
        c = sqlite3.connect(_DB)
        for sym, date, direction, sl, tp in c.execute(
            "SELECT symbol, date, direction, stop_loss, take_profit FROM playbook_instructions"
        ):
            d = "SHORT" if str(direction).upper().startswith("SELL") else "BUY"
            out[(sym, date, d)] = (sl, tp)
        c.close()
    except Exception as e:
        print(f"[warn] 读取 playbook_instructions 失败: {e}", file=sys.stderr)
    return out


def _load_realized():
    """(symbol, date, direction) -> 已实现 pnl_pct(%)。放行组的真实结果对照。

    trade_records.direction 用 LONG/SHORT; 影子用 BUY/SHORT → 统一到 BUY/SHORT。
    trade_records.pnl_pct 口径实测为**小数**(如 0.0033=0.33%), 故 ×100 转百分数,
    与本脚本 counterfactual 的百分数口径对齐 (口径不一致会差 100 倍, 必须对齐)。
    """
    out = {}
    if not os.path.exists(_DB):
        return out
    try:
        c = sqlite3.connect(_DB)
        for sym, date, direction, pnl_pct in c.execute(
            "SELECT symbol, trade_date, direction, pnl_pct FROM trade_records "
            "WHERE pnl_pct IS NOT NULL"
        ):
            d = "BUY" if str(direction).upper() in ("LONG", "BUY") else "SHORT"
            out.setdefault((sym, date, d), []).append(float(pnl_pct) * 100.0)
        c.close()
    except Exception as e:
        print(f"[warn] 读取 trade_records 失败: {e}", file=sys.stderr)
    return out


def _span_minutes(forward):
    """轨迹实际测量跨度 (分钟)。用于诚实标注"测了多久", 而非假称持有到收盘。"""
    if len(forward) < 2:
        return 0.0
    try:
        t0 = datetime.fromisoformat(forward[0][0])
        t1 = datetime.fromisoformat(forward[-1][0])
        return (t1 - t0).total_seconds() / 60.0
    except Exception:
        return 0.0


def _simulate(direction, kill_price, forward, sl, tp, min_forward=2):
    """从拦截价沿采样轨迹模拟出场 → (pnl_pct, exit_kind, span_min)。

    forward 采样点不足 min_forward → UNMEASURABLE (pnl=None), 由调用方排除,
    绝不返回 0 冒充"测得为零"。
    """
    if len(forward) < min_forward:
        return None, UNMEASURABLE, 0.0
    is_short = direction == "SHORT"
    span = _span_minutes(forward)
    # forward[0] 是拦截事件自身, 从下一个采样点起判触发
    for _, p in forward[1:]:
        if sl and sl > 0:
            if (not is_short and p <= sl) or (is_short and p >= sl):
                pnl = (sl - kill_price) / kill_price if not is_short else (kill_price - sl) / kill_price
                return pnl * 100, "SL", span
        if tp and tp > 0:
            if (not is_short and p >= tp) or (is_short and p <= tp):
                pnl = (tp - kill_price) / kill_price if not is_short else (kill_price - tp) / kill_price
                return pnl * 100, "TP", span
    last = forward[-1][1]
    pnl = (last - kill_price) / kill_price if not is_short else (kill_price - last) / kill_price
    return pnl * 100, "LAST_POLL", span


def _dedup_events(rows, reason):
    """(symbol,dir,day) -> 该场景最早一条 (代表决策事件, 去重轮询重复)。"""
    ev = {}
    for r in rows:
        if r.get("breakout_triggered") is not True or r.get("gate_reason") != reason:
            continue
        key = (r["symbol"], r["direction"], r["ts"][:10])
        if key not in ev or r["ts"] < ev[key]["ts"]:
            ev[key] = r
    return ev


def _summarize(label, vals, extra=""):
    if not vals:
        print(f"  → {label}: 无可测样本")
        return
    wins = sum(1 for x in vals if x > 0)
    print(f"  → {label}: n={len(vals)} 正{wins}/负{len(vals) - wins} "
          f"合计{sum(vals):+.2f}% 均值{st.mean(vals):+.3f}% 中位{st.median(vals):+.3f}% {extra}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="仅最近 N 个交易日")
    ap.add_argument("--min-forward", type=int, default=2,
                    help="可测量所需的最少前向采样点 (默认2)")
    ap.add_argument("--verbose", action="store_true", help="逐笔明细")
    args = ap.parse_args()

    rows = _load_shadow(args.days)
    if not rows:
        print("无影子数据。")
        return
    traj = _build_trajectories(rows)
    sltp = _load_sltp()
    realized = _load_realized()

    days = sorted({r.get("ts", "")[:10] for r in rows})
    print(f"影子样本: {len(rows)} 行, 覆盖 {len(days)} 天 ({days[0]}..{days[-1]})")
    print(f"可测量门槛: forward 采样点 >= {args.min_forward}\n")

    # ── 处理组: 被门拦掉的单, 反事实 ──
    ev = _dedup_events(rows, "wrong_side")
    print(f"=== [处理组] 被VWAP门拦掉 (wrong_side): {len(ev)} 个去重决策事件 ===")
    vals, spans, unmeasurable = [], [], 0
    by_dir = defaultdict(list)
    kinds = defaultdict(int)
    if args.verbose:
        print(f"  {'symbol':10} {'dir':5} {'day':11} {'拦截价':>9} {'假想PnL':>9} {'跨度min':>8} {'出场':>10}")
    for key, r in sorted(ev.items()):
        sym, dr, day = key
        kill_price = float(r["current_price"])
        forward = [(ts, p) for (ts, p) in traj.get((sym, day), []) if ts >= r["ts"]]
        sl, tp = sltp.get((sym, day, dr), (None, None))
        pnl, kind, span = _simulate(dr, kill_price, forward, sl, tp, args.min_forward)
        kinds[kind] += 1
        if pnl is None:
            unmeasurable += 1
            continue
        vals.append(pnl)
        spans.append(span)
        by_dir[dr].append(pnl)
        if args.verbose:
            print(f"  {sym:10} {dr:5} {day} {kill_price:9.2f} {pnl:+8.2f}% {span:8.1f} {kind:>10}")

    print(f"  排除不可测量: {unmeasurable} 个 (forward<{args.min_forward}) "
          f"| 出场构成: {dict(kinds)}")
    if spans:
        print(f"  实际测量跨度: 中位 {st.median(spans):.1f} 分钟 "
              f"(最长 {max(spans):.1f}) — 注意这不是「持有到收盘」")
    _summarize("放行假想盈亏(全体)", vals)
    for d in ("BUY", "SHORT"):
        _summarize(f"  ├─ {d}", by_dir.get(d, []))

    if vals:
        mean_all = st.mean(vals)
        print()
        print(f"  解读: 均值>0 = VWAP门倾向拦错(扼杀); <0 = 倾向拦对(救命)")
        print(f"        当前 均值={mean_all:+.3f}% → "
              f"{'倾向拦错(扼杀)' if mean_all > 0 else '倾向拦对(救命)'}")
        nb, ns = len(by_dir.get("BUY", [])), len(by_dir.get("SHORT", []))
        if nb and ns and (st.mean(by_dir["BUY"]) > 0) and (st.mean(by_dir["SHORT"]) < 0):
            print("        ⚠ beta混淆: BUY为正而SHORT为负 → 应优先解释为大盘上行漂移, 非门判错")
        elif ns == 0:
            print(f"        ⚠ beta混淆无法排除: SHORT 样本 0 笔, 无法区分「门拦错」与「大盘上行」")

    # ── 对照组: 门放行的单, 用真实成交结果 (非影子轨迹) ──
    ev2 = _dedup_events(rows, "aligned")
    print(f"\n=== [对照组] VWAP门放行 (aligned): {len(ev2)} 个去重决策事件 ===")
    print("  说明: 放行后标的转为持仓, entry影子停止轮询 → 轨迹恒为1点、无法做反事实;")
    print("        故对照组改取 trade_records 的**已实现 pnl** (真实成交, 非模拟)。")
    rvals, matched = [], 0
    rby_dir = defaultdict(list)
    for key, r in sorted(ev2.items()):
        sym, dr, day = key
        got = realized.get((sym, day, dr))
        if got:
            matched += 1
            rvals.extend(got)
            rby_dir[dr].extend(got)
    print(f"  join 到真实成交: {matched}/{len(ev2)} 个事件 ({len(rvals)} 笔成交)")
    _summarize("放行组已实现盈亏", rvals)
    for d in ("BUY", "SHORT"):
        _summarize(f"  ├─ {d}", rby_dir.get(d, []))

    print()
    print("口径警示 (务必如实看待):")
    print("  1. 两组测量基不同 — 处理组=离散轨迹反事实(分钟级), 对照组=已实现盈亏(全持仓期);")
    print("     **不可直接相减**做因果推断, 只能各自看方向。")
    print("  2. 处理组含 beta 混淆: 方向以 BUY 为主时, 正收益可能只是大盘上行。")
    print("  3. 离散采样非连续行情 → 日内极值低估, SL/TP 触发滞后。")
    print("  4. n<30 不下结论 (宪章口径七律第5条)。")


if __name__ == "__main__":
    main()
