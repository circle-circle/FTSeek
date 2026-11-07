#!/usr/bin/env python3
"""入场延迟 N 分钟扫描 (DEB-20261106 §40)。

现有 2 分钟门 (`entry_confirm_delay_sec: 120`) 与"开盘延后 5 分钟"来自 `entry_delay_confirm`
影子的 DELAYS_MIN=(2,5) 两档。本脚本把 N 扩成网格，回答"10/15/20/30 分钟是否更好"。

口径与 `scripts/entry_delay_confirm_shadow.py` **逐字对齐**（复用其 `_price_at`/`_eval_delay`）：
  price_at(t) = 首根 bar_time ≥ t 的 bar **开盘价**
  still_triggers = LONG: px ≥ entry / SHORT: px ≤ entry
  opp_cost_pct   = 方向归一漂移 (正 = 延迟让你成交更贵)
  回避价值       = −实际 pnl_net (回避掉亏损为正, 误杀盈利为负)

净效应($) = Σ(回避笔: −pnl_net) − Σ(仍触发笔: opp_cost/100 × notional)
          = "若统一延后 N 分钟, 这套系统的结果会差多少"（正 = 延后有利）

性质: 只读 DB + bars；无 API、无写入 playbook。
用法:
  venv/bin/python scripts/entry_delay_sweep.py [--days 120] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
BARS_DIRS = (ROOT / "data" / "replay_bars", ROOT / "data" / "replay_bars_1m")
GRID = (0, 1, 2, 3, 5, 8, 10, 15, 20, 30, 45, 60)


def bars_of(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        for name in (f"US_{sym}_{day}.json", f"{sym}_{day}.json"):
            p = d / name
            if p.exists():
                return json.loads(p.read_text())
    return None


def hhmm_to_min(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def min_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def price_at(bars: List[list], target_hhmm: str) -> Optional[float]:
    """首根 bar_time ≥ target 的 bar 开盘价（与影子同口径）。"""
    for b in bars:
        if b[0][11:16] >= target_hhmm:
            return float(b[1])
    return None


def eval_delay(direction: str, entry_price: float, px: float):
    if direction == "LONG":
        return px >= entry_price, (px - entry_price) / entry_price * 100
    return px <= entry_price, (entry_price - px) / entry_price * 100


def load_trades(days: int) -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT id, symbol, trade_date, entry_time, direction, entry_price, quantity,
                  pnl_net, realized_pnl
           FROM trade_records
           WHERE trade_date >= '2026-07-01' AND realized_pnl IS NOT NULL
             AND entry_time IS NOT NULL
           ORDER BY trade_date""")]
    conn.close()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    trades = load_trades(0)
    print("已平仓交易 %d 笔（2026-07-01 起）" % len(trades))

    cache = {}
    usable = []
    for t in trades:
        key = (t["symbol"], t["trade_date"])
        if key not in cache:
            cache[key] = bars_of(t["symbol"], t["trade_date"])
        b = cache[key]
        if not b or len(b) < 30:
            continue
        t["_bars"] = b
        usable.append(t)
    print("有 1min bar %d 笔" % len(usable))

    results = []
    for n in GRID:
        avoided, delayed = [], []
        for t in usable:
            bars = t["_bars"]
            et = t["entry_time"][11:16]
            target = min_to_hhmm(hhmm_to_min(et) + n)
            px = price_at(bars, target)
            if px is None:
                continue
            still, opp = eval_delay(t["direction"], t["entry_price"], px)
            rec = {"day": t["trade_date"], "sym": t["symbol"], "net": t["pnl_net"] or 0,
                   "notional": (t["entry_price"] or 0) * (t["quantity"] or 0), "opp": opp}
            (delayed if still else avoided).append(rec)
        total = len(avoided) + len(delayed)
        if not total:
            continue
        gain_avoid = -sum(r["net"] for r in avoided)                     # 回避掉的实际净额
        cost_opp = sum(r["opp"] / 100 * r["notional"] for r in delayed)  # 延迟多付的钱
        net_effect = gain_avoid - cost_opp
        # 日聚类 t: 每日 net_effect 之和
        daily = defaultdict(float)
        for r in avoided:
            daily[r["day"]] += -r["net"]
        for r in delayed:
            daily[r["day"]] -= r["opp"] / 100 * r["notional"]
        vals = list(daily.values())
        tstat = None
        if len(vals) >= 3:
            m, sd = statistics.mean(vals), statistics.stdev(vals)
            if sd:
                tstat = round(m / (sd / math.sqrt(len(vals))), 2)
        opp_mean = statistics.mean([r["opp"] for r in delayed]) if delayed else None
        results.append({
            "delay_min": n, "n": total,
            "avoid_rate_pct": round(100 * len(avoided) / total, 1),
            "opp_cost_mean_pct": round(opp_mean, 4) if opp_mean is not None else None,
            "gain_avoid_usd": round(gain_avoid, 2),
            "cost_opp_usd": round(cost_opp, 2),
            "net_effect_usd": round(net_effect, 2),
            "net_effect_per_trade": round(net_effect / total, 2),
            "t_day": tstat,
            "era_7_8": round(sum(v for d, v in daily.items() if d < "2026-09-01"), 2),
            "era_9_11": round(sum(v for d, v in daily.items() if d >= "2026-09-01"), 2),
        })

    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print("\n%-6s %5s %8s %10s %10s %10s %11s %8s %10s %10s" % (
        "N(分)", "n", "回避%", "机会成本%", "回避收益$", "延迟成本$", "净效应$", "每笔$", "日t", "7-8/9-11"))
    for r in results:
        print("%-6d %5d %7.1f%% %10s %10.0f %10.0f %11.0f %8.2f %8s %6.0f/%6.0f" % (
            r["delay_min"], r["n"], r["avoid_rate_pct"],
            ("%.4f" % r["opp_cost_mean_pct"]) if r["opp_cost_mean_pct"] is not None else "n/a",
            r["gain_avoid_usd"], r["cost_opp_usd"], r["net_effect_usd"], r["net_effect_per_trade"],
            r["t_day"] if r["t_day"] is not None else "n/a", r["era_7_8"], r["era_9_11"]))
    print("\n净效应>0 = 统一延后 N 分钟更赚；<0 = 更亏（延迟成本吃掉回避收益）。")
    print("口径: price_at = 首根 bar≥目标时刻的开盘价；回避=价格已转回不利侧；opp=方向归一漂移。")
    return 0


if __name__ == "__main__":
    sys.exit(main())