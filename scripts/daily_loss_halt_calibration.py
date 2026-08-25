#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日内新开仓熔断定标回放 (2026-08-25, 只读零交易路径)。

背景:
  - 生效中的日内熔断是 app/risk/pre_trade_gate.py::_check_daily_drawdown
    (risk.account.max_daily_drawdown=3% 权益 ≈ −$2,936, 含浮亏);
    config_defaults 的 BUDGET_SYSTEM_MAX_DAILY_LOSS=$6,000 无任何消费端 (装饰键,
    待 config census 清理)。当前 regime 最差日净 −$830 → 熔断从未接近触发。
  - DEB 第5轮终判: 每笔期望净 −$3.60。负期望下任何会触发的熔断机械省钱 (少亏 + 少付地板费)。

方法 (单遍事件回放, 与实盘闸语义对齐的近似):
  按 (入场/出场) 事件时间排序; 维护当日已实现累计净PnL (仅出场事件贡献);
  累计 ≤ −X 时刻起当日剩余新开仓全部取消, 被取消交易的最终盈亏整体剔除。
  近似与偏差声明:
    a) 实盘闸含未实现浮亏 (触发更早) → 本回放偏保守 (低估节损);
    b) 剔除亏损单不改变其余成交 (假设成交独立);
    c) 隔夜仓的出场可能落在次日 — 计入出场日的当日序列 (与实盘口径一致)。
  时间戳归一化复用 overnight_gap_carry_audit.et_date 口径 (裸 CST 截问题已修)。

预注册选值规则:
  在 {改善曲线} 上取"边际改善趋平且零正收益日被误伤"的最大收紧档;
  若不存在零误伤档, 取误伤日合计 ≥ −$50 的最紧档并标注代价。
输出: data/daily_loss_halt_calibration.json + stdout 表。
"""

import json
import sqlite3
import sys
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
sys.path.insert(0, str(BASE / "scripts"))
from overnight_gap_carry_audit import et_date  # noqa: E402

DB = BASE / "data" / "ftseek_us.db"
OUT = BASE / "data" / "daily_loss_halt_calibration.json"

WINDOWS = {
    "fee_window_0725_0824": ("2026-07-25", "2026-08-24"),
    "jul_aug_full": ("2026-07-01", "2026-08-24"),
}
SWEEP = [150, 200, 250, 300, 400, 500, 750, 1000, 1500, 2936, 6000]


def load_events(lo, hi):
    """返回 [(ts_epoch, kind, ...)] 按时间排序的事件流; kind∈{entry,exit}。"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, symbol, direction, entry_time, exit_time,
                  quantity, entry_price, exit_price, realized_pnl, pnl_net, fees_est
           FROM trade_records WHERE exit_time IS NOT NULL AND exit_price > 0"""
    ).fetchall()
    conn.close()
    import datetime as dt

    def epoch(ts):
        ts = str(ts)
        naive = not (len(ts) > 10 and ts[-6] in "+-" and ts[-3] == ":")
        fmt = "%Y-%m-%dT%H:%M:%S"
        d = dt.datetime.strptime(ts[:19], fmt)
        if not naive:
            off = ts[-6:]
            h, m = int(off[1:3]), int(off[4:6])
            delta = dt.timedelta(hours=h, minutes=m)
            d = d - delta if off[0] == "+" else d + delta
        else:
            d = d - dt.timedelta(hours=12)  # 裸戳按 CST 处理 (审计脚本同款)
        return d.replace(tzinfo=dt.timezone.utc).timestamp(), d.strftime("%Y-%m-%d")

    events = []
    for r in rows:
        e_ep, _ = epoch(r["entry_time"])
        x_ep, x_day = epoch(r["exit_time"])
        if not (lo <= x_day <= hi):
            continue
        net = r["pnl_net"] if r["pnl_net"] is not None else (
            (r["realized_pnl"] or 0) - (r["fees_est"] or 0))
        events.append((e_ep, "entry", r["id"], None))
        events.append((x_ep, "exit", r["id"], float(net)))
    events.sort(key=lambda e: (e[0], 0 if e[1] == "exit" else 1))
    return events


def _fixed_point(events, threshold):
    """熔断拦截存在二阶效应 (被拦交易的出场不再贡献当日累计), 不动点迭代至收敛。"""


def _fixed_point(events, threshold):
    prev = set()
    for _ in range(10):
        res = _one_pass(events, threshold, prev)
        if res[2] == prev:
            break
        prev = res[2]
    cum, halted, blocked = res
    return {"cum": round(cum, 2), "halted_days": len(halted),
            "blocked_trades": len(blocked),
            "blocked_net_removed": round(sum(
                n for ep, k, t, n in events if k == "exit" and t in blocked), 2)}


def _one_pass(events, threshold, pre_blocked):
    cum = 0.0
    halted = set()
    blocked = set(pre_blocked)
    import datetime as dt

    for ep, kind, tid, net in events:
        day = dt.datetime.fromtimestamp(ep, dt.timezone.utc).strftime("%Y-%m-%d")
        if kind == "exit":
            if tid in blocked:
                continue
            cum += net
            if threshold is not None and cum <= -threshold:
                halted.add(day)
        else:
            if threshold is not None and day in halted:
                blocked.add(tid)
    return cum, halted, blocked


def main():
    report = {"generated": "2026-08-25", "sweep_levels": SWEEP,
              "note": "近似口径见脚本头; 实盘闸含浮亏触发更早, 本回放保守"}
    for wname, (lo, hi) in WINDOWS.items():
        events = load_events(lo, hi)
        base = _fixed_point(events, None)
        rows = []
        for x in SWEEP:
            r = _fixed_point(events, x)
            r["level"] = x
            r["delta_vs_baseline"] = round(r["cum"] - base["cum"], 2)
            rows.append(r)
        report[wname] = {"baseline_cum_net": base["cum"],
                         "total_exits": sum(1 for e in events if e[1] == "exit"),
                         "rows": rows}
        print(f"\n=== {wname} ({lo}..{hi}) 基线净 {base['cum']} ===")
        print(f"{'X':>6} {'熔断日数':>6} {'拦截笔数':>6} {'剔除净额':>10} {'期末净':>10} {'Δ':>9}")
        for r in rows:
            print(f"{r['level']:>6} {r['halted_days']:>6} {r['blocked_trades']:>6} "
                  f"{r['blocked_net_removed']:>10} {r['cum']:>10} {r['delta_vs_baseline']:>9}")
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n[out] {OUT}")


if __name__ == "__main__":
    main()
