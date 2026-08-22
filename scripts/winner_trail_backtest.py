#!/usr/bin/env python3
"""赢家侧"让利润奔跑"替代方案回测 — 蓝图 L3 出场语义改造的前置证据 (用户问"是否有回测过").

此前证据只有 t5_hold_counterfactual (持有到 T+5 的粗反事实, Δ+19.09 trail-lock 桶),
不是 trailing 规则的真回测。本脚本在真实赢家队列上模拟三种出场替代方案 vs 实际出场:

  口径: 队列 = 2026-06-01 起 pnl_net>0 的已平仓赢家; 基线 = 实际 exit_price;
        反事实从出场日收盘起按日K推进 (与 trend_gate_replay/multiday_replay 同款约定);
        Δ = sign×(反事实出场价 − 实际出场价)×qty, 两臂费用同构 (往返费已付, 纯价差)。

  臂:
    HOLD_T5      原SL作硬地板, 持有5个交易日 (纯耐心臂, 对齐 t5cf 口径)
    TRAIL_2.0    2.0×ATR14 追踪止损 (峰值收盘回撤), 原SL为地板, 上限10交易日
    TRAIL_1.0    1.0×ATR14 同上 (更紧的追踪)

  脏数据防线: 名义 >$50k 剔除显式计数 (XLF id=875 教训)。

预注册边界: 描述性回测供 L3 出场语义影子 A/B 设计参考, 不构成启停判据;
日K粒度, SL/trail 触碰按日内极值近似成交; 单一 regime 样本。

回滚: 删本文件 + data/winner_trail_backtest_latest.json。
"""
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import importlib.util

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location(
    "tsr", REPO / "scripts" / "trend_state_recorder.py")
tsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tsr)

DB = REPO / "data" / "ftseek_us.db"
DAILY_DIR = REPO / "data" / "replay_bars_daily"
MIN = "2026-06-01"
NOTIONAL_CAP = 50_000


def load_bars(symbol):
    cp = DAILY_DIR / f"{symbol.replace('.', '_')}.json"
    if cp.exists():
        try:
            rows = json.loads(cp.read_text())
            if rows:
                return rows
        except Exception:
            pass
    return tsr._load_bars(symbol, allow_fetch=True)


def atr_abs(bars, i):
    """ATR14 绝对值 (锚日的); 数据不足返回 None。"""
    if i < 15:
        return None
    trs = []
    for k in range(i - 13, i + 1):
        o, h, l, c = bars[k][1], bars[k][2], bars[k][3], bars[k][4]
        pc = bars[k - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def simulate(bars, i, direction, sl_floor, horizon, trail_k=None):
    """从锚日 i 收盘起推进。trail_k=None 为纯持有臂; 否则峰值收盘 −k×ATR 追踪,
    与原SL取宽者 (LONG 取高者/SHORT 取低者, 尊重 SL 契约只紧不松)。"""
    s = 1.0 if direction == "LONG" else -1.0
    a = atr_abs(bars, i)
    if a is None:
        return None, None, "no_atr"
    peak = bars[i][4]                      # 锚日收盘为初始峰值
    for d in range(1, horizon + 1):
        j = i + d
        if j >= len(bars):
            return bars[-1][4], d - 1, "bars_exhausted"
        o, h, l, c = bars[j][1], bars[j][2], bars[j][3], bars[j][4]
        stop = None
        if trail_k is not None:
            t = peak - s * trail_k * a     # LONG: peak−k·a / SHORT: peak+k·a
            # 有效停止价 = 追踪与原SL中更"保护"的一侧 (原SL缺失时只用追踪)
            if sl_floor is None:
                stop = t
            else:
                stop = max(t, sl_floor) if direction == "LONG" else min(t, sl_floor)
            if sl_floor and o <= sl_floor:
                return o, d, "SL_gap"
            if sl_floor and l <= sl_floor:
                return sl_floor, d, "SL_touch"
        else:
            if sl_floor and o >= sl_floor:
                return o, d, "SL_gap"
            if sl_floor and h >= sl_floor:
                return sl_floor, d, "SL_touch"
        if stop is not None:
            if direction == "LONG" and l <= stop and stop > (sl_floor or -1e18):
                return stop, d, "trail_exit"
            if direction == "SHORT" and h >= stop and stop < (sl_floor or 1e18):
                return stop, d, "trail_exit"
        peak = max(peak, c) if direction == "LONG" else min(peak, c)
    px = bars[min(i + horizon, len(bars) - 1)][4]
    return px, horizon, "horizon"


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price,
               quantity, pnl_net, exit_reason, metadata_json
        FROM trade_records
        WHERE pnl_net IS NOT NULL AND pnl_net > 0 AND exit_time >= ?
        ORDER BY exit_time
    """, (MIN,)).fetchall()
    con.close()

    ARMS = ("HOLD_T5", "TRAIL_2.0", "TRAIL_1.0")
    res = {a: [] for a in ARMS}
    flips = {a: 0 for a in ARMS}
    detail, skipped = [], defaultdict(int)

    for r in rows:
        qty = float(r["quantity"] or 1)
        ep = float(r["entry_price"])
        if ep * qty > NOTIONAL_CAP:
            skipped["notional_implausible"] += 1
            continue
        sym, direction = r["symbol"], r["direction"]
        bars = load_bars(sym)
        if not bars:
            skipped["no_bars"] += 1
            continue
        days = [b[0] for b in bars]
        ex_date = r["exit_time"][:10]
        if ex_date not in days:
            skipped["exit_day_missing"] += 1
            continue
        i = days.index(ex_date)
        if i + 11 >= len(bars):
            skipped["window_censored"] += 1
            continue

        try:
            md = json.loads(r["metadata_json"] or "{}")
            slp = float(md.get("stop_loss") or 0) or None
        except Exception:
            slp = None

        baseline = float(r["exit_price"])
        s = 1.0 if direction == "LONG" else -1.0

        arm_out = {}
        ok = True
        for arm, hk, tk in (("HOLD_T5", 5, None), ("TRAIL_2.0", 10, 2.0), ("TRAIL_1.0", 10, 1.0)):
            px, off, tag = simulate(bars, i, direction, slp, hk, trail_k=tk)
            if px is None:
                ok = False
                skipped["no_atr"] += 1
                break
            d = s * (px - baseline) * qty
            arm_out[arm] = (d, tag)
        if not ok:
            continue

        for arm in ARMS:
            d, tag = arm_out[arm]
            res[arm].append(d)
            if r["pnl_net"] + d <= 0:
                flips[arm] += 1
        detail.append({"date": ex_date, "symbol": sym, "direction": direction,
                       "exit_reason": r["exit_reason"],
                       "pnl": round(float(r["pnl_net"]), 2),
                       **{arm: round(arm_out[arm][0], 2) for arm in ARMS}})

    n = len(detail)
    print(f"═══ 赢家侧让利润奔跑回测 n={n} ({MIN}起, 全部盈利单) ═══")
    print(f"跳过: {dict(skipped)}")
    if not n:
        return
    base_total = sum(d["pnl"] for d in detail)
    print(f"\n基线(实际出场) Σpnl=${base_total:+.0f}")
    print(f"\n{'方案':<12}{'ΣΔ$':>10}{'均笔$':>9}{'中位$':>9}{'赢单变亏':>9}{'灾难(<-$200)':>13}")
    for a in ARMS:
        v = res[a]
        dis = sum(1 for x in v if x < -200)
        print(f"{a:<12}{sum(v):>+10.0f}{sum(v)/n:>+9.2f}{statistics.median(v):>+9.2f}"
              f"{flips[a]:>9}{dis:>13}")

    # 出场理由分解: 钱主要在哪类被截断的赢家里
    print("\n按原出场理由 ΣΔ$ (TOP6):")
    agg = defaultdict(lambda: {a: 0.0 for a in ARMS})
    cnt = defaultdict(int)
    for d in detail:
        cnt[d["exit_reason"]] += 1
        for a in ARMS:
            agg[d["exit_reason"]][a] += d[a]
    for reason in sorted(cnt, key=lambda k: -sum(abs(x) for x in agg[k].values()))[:6]:
        row = agg[reason]
        print(f"  {reason:<18} n={cnt[reason]:<4} " +
              " ".join(f"{a}={row[a]:+.0f}$" for a in ARMS))

    out = REPO / "data" / "winner_trail_backtest_latest.json"
    out.write_text(json.dumps({
        "generated_note": "描述性回测, 预注册边界内嵌脚本头; 非启停判据",
        "since": MIN, "cohort_n": len(rows), "replayed_n": n, "skipped": dict(skipped),
        "baseline_total_pnl": round(base_total, 2),
        "arms": {a: {"total_delta": round(sum(v), 2), "mean": round(sum(v) / n, 2),
                     "median": round(statistics.median(v), 2),
                     "win_to_loss_flips": flips[a],
                     "disasters_lt_200": sum(1 for x in v if x < -200)}
                 for a, v in res.items()},
        "detail": detail,
    }, indent=1, ensure_ascii=False))
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()
