#!/usr/bin/env python3
"""VWAP 门离线评估 (只读, 不碰任何交易逻辑).

目的: 判定 entry VWAP 方向门 (pipeline.py:2983-3028) 是"救命"还是"扼杀" —
  即被它拦掉的单 (breakout_triggered=True 但 gate_reason=wrong_side), 若放行
  的假想盈亏为正 (拦错) 还是为负 (拦对)。

数据源 (全部本地, 零外部调用):
  1. data/entry_timing_shadow.jsonl — 每次轮询记录 current_price + gate 判定,
     用它同时充当"被拦事件"来源与"前向价格轨迹"(同 symbol+day 的所有采样点)。
  2. data/ftseek_us.db :: playbook_instructions — 按 symbol+date+direction join
     真实 stop_loss / take_profit, 用于套真实出场规则模拟盈亏。

模拟规则 (保守, 贴近真实):
  从拦截时点起, 按时间顺序遍历该 symbol+day 的后续采样点:
    - 先触 SL → 记为 SL 出场 (亏)
    - 先触 TP → 记为 TP 出场 (盈)
    - 都没触 → 以最后一个采样点 (≈当日收盘) 平仓
  BUY: 涨则盈; SHORT: 跌则盈。无 SL/TP 时退化为"拦截价→收盘"。

局限 (务必如实看待):
  - 轨迹仅为"信号被扫描时点"的离散采样, 非连续分钟行情 → 日内极值可能低估,
    SL/TP 触发时点可能滞后。结论只作方向性参考, 不作下单依据。
  - 样本通常极小 (每日仅数个去重事件)。攒到 30+ 事件再谈统计显著性。

用法:
  ./venv/bin/python scripts/eval_vwap_gate.py            # 全部影子数据
  ./venv/bin/python scripts/eval_vwap_gate.py --days 5   # 仅最近5个交易日
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHADOW = os.path.join(_ROOT, "data", "entry_timing_shadow.jsonl")
_DB = os.path.join(_ROOT, "data", "ftseek_us.db")


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
            # DB direction 用 BUY/SELL_SHORT; 影子用 BUY/SHORT
            d = "SHORT" if str(direction).upper().startswith("SELL") else "BUY"
            out[(sym, date, d)] = (sl, tp)
        c.close()
    except Exception as e:
        print(f"[warn] 读取 playbook_instructions 失败: {e}", file=sys.stderr)
    return out


def _simulate(direction, kill_price, forward, sl, tp):
    """从拦截价沿采样轨迹模拟出场, 返回 (pnl_pct, exit_kind)。"""
    is_short = direction == "SHORT"
    for _, p in forward:
        if sl and sl > 0:
            if (not is_short and p <= sl) or (is_short and p >= sl):
                pnl = (sl - kill_price) / kill_price if not is_short else (kill_price - sl) / kill_price
                return pnl * 100, "SL"
        if tp and tp > 0:
            if (not is_short and p >= tp) or (is_short and p <= tp):
                pnl = (tp - kill_price) / kill_price if not is_short else (kill_price - tp) / kill_price
                return pnl * 100, "TP"
    close = forward[-1][1] if forward else kill_price
    pnl = (close - kill_price) / kill_price if not is_short else (kill_price - close) / kill_price
    return pnl * 100, "CLOSE"


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="仅最近 N 个交易日")
    args = ap.parse_args()

    rows = _load_shadow(args.days)
    if not rows:
        print("无影子数据。")
        return
    traj = _build_trajectories(rows)
    sltp = _load_sltp()

    days = sorted({r.get("ts", "")[:10] for r in rows})
    print(f"影子样本: {len(rows)} 行, 覆盖 {len(days)} 天 ({days[0]}..{days[-1]})\n")

    for reason, label in [("wrong_side", "被VWAP门拦掉"), ("aligned", "VWAP门放行(对照)")]:
        ev = _dedup_events(rows, reason)
        print(f"=== {label} (reason={reason}): {len(ev)} 个去重决策事件 ===")
        if not ev:
            print("  (无)\n")
            continue
        results = []
        print(f"  {'symbol':10} {'dir':5} {'day':11} {'拦截价':>8} {'SL':>8} {'TP':>8} {'假想PnL':>8} {'出场':>6}")
        for key, r in sorted(ev.items()):
            sym, dr, day = key
            kill_price = float(r["current_price"])
            forward = [(ts, p) for (ts, p) in traj.get((sym, day), []) if ts >= r["ts"]]
            sl, tp = sltp.get((sym, day, dr), (None, None))
            pnl, kind = _simulate(dr, kill_price, forward, sl, tp)
            results.append(pnl)
            sl_s = f"{sl:8.2f}" if sl else "     n/a"
            tp_s = f"{tp:8.2f}" if tp else "     n/a"
            print(f"  {sym:10} {dr:5} {day} {kill_price:8.2f} {sl_s} {tp_s} {pnl:+7.2f}% {kind:>6}")
        wins = sum(1 for x in results if x > 0)
        print(f"  → 放行假想盈亏: 正{wins}/负{len(results)-wins}, 合计 {sum(results):+.2f}%, "
              f"均值 {sum(results)/len(results):+.2f}%")
        if reason == "wrong_side":
            print(f"    解读: 合计>0 = VWAP门倾向拦错(扼杀); 合计<0 = 倾向拦对(救命)")
        print()

    print("局限: 轨迹为离散采样非连续行情; 样本小结论仅方向性参考, 不作下单依据。")


if __name__ == "__main__":
    main()
