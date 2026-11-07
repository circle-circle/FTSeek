#!/usr/bin/env python3
"""入场 15 分钟超时转市价 —— record-only 影子 (DEB-20261106 §38 / RFC-191 P1 影子)。

对每张 DA 方向卡，用真实 1min bar 记录两条腿：
  legA 限价腿（现况）：限价在 15 分钟内被触及才算成交，否则该笔不入场（0 收益）
  legB 超时腿：15 分钟内未触及 → 按第 15 根 bar 收盘价市价进；触及则与 legA 相同
两条腿的出场**完全一致**：卡片自己的 stop/tp，同 bar 内先判止损，都没打到则收盘平；扣 7.6bps 双边。

同时回读 `trade_records` 的真实净额（若有成交），用于事后对照"影子 vs 实盘"。

零交易影响：只读 DB + bars，只写 data/shadow/*.jsonl；不生成指令、不下单、不改配置。
判据（预注册，攒够样本才裁）：当「限价 15 分钟内未成交」样本 ≥ 30 时，
若 legB 相对 legA 的累计收益差 > 2× 其日聚类标准误，且双时代同向，才提交执行链变更提案。

用法:
  venv/bin/python scripts/entry_timeout_shadow.py --start 2026-07-01 --end 2026-11-06
  venv/bin/python scripts/entry_timeout_shadow.py --days 30 --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
BARS_DIRS = (ROOT / "data" / "replay_bars", ROOT / "data" / "replay_bars_1m")
OUT = ROOT / "data" / "shadow" / "entry_timeout_shadow.jsonl"
FEE_PCT = 0.076
TIMEOUT_BARS = 15


def bars_of(symbol: str, day: str) -> Optional[List[list]]:
    sym = symbol.replace("US.", "")
    for d in BARS_DIRS:
        for name in (f"US_{sym}_{day}.json", f"{sym}_{day}.json"):
            p = d / name
            if p.exists():
                return json.loads(p.read_text())
    return None


def num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except Exception:
        return None


def sim_exit(bars: List[list], sign: int, entry: float, stop: float, tp: float,
             start: int) -> float:
    for b in bars[start:]:
        hi, lo = b[2], b[3]
        if sign > 0:
            if lo <= stop:
                return sign * (stop - entry) / entry * 100 - FEE_PCT
            if hi >= tp:
                return sign * (tp - entry) / entry * 100 - FEE_PCT
        else:
            if hi >= stop:
                return sign * (stop - entry) / entry * 100 - FEE_PCT
            if lo <= tp:
                return sign * (tp - entry) / entry * 100 - FEE_PCT
    return sign * (bars[-1][4] - entry) / entry * 100 - FEE_PCT


def load_cards(start: str, end: str) -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT i.decision_id, i.symbol, i.date, i.direction, i.entry_price, i.stop_loss,
                  i.take_profit, i.executed, t.pnl_net, t.exit_reason
           FROM playbook_instructions i
           LEFT JOIN (SELECT decision_id, SUM(pnl_net) AS pnl_net, MAX(exit_reason) AS exit_reason
                        FROM trade_records GROUP BY decision_id) t ON t.decision_id = i.decision_id
           WHERE i.date BETWEEN ? AND ? AND i.row_kind='instruction'
             AND i.decision_id LIKE 'DA-%' AND i.direction IN ('BUY','SELL_SHORT')""",
        (start, end))]
    conn.close()
    return rows


def simulate(cards: List[dict]) -> List[dict]:
    out = []
    for c in cards:
        entry, stop, tp = num(c["entry_price"]), num(c["stop_loss"]), num(c["take_profit"])
        if not (entry and stop and tp):
            continue
        bars = bars_of(c["symbol"], c["date"])
        if not bars or len(bars) < TIMEOUT_BARS + 5:
            continue
        sign = 1 if c["direction"] == "BUY" else -1
        touch = None
        for i, b in enumerate(bars):
            if (b[3] <= entry <= b[2]):
                touch = i
                break
        filled_15 = touch is not None and touch < TIMEOUT_BARS
        # legA 限价腿
        if touch is not None:
            leg_a = sim_exit(bars, sign, entry, stop, tp, touch)
        else:
            leg_a = 0.0
        # legB 超时腿
        if touch is not None and touch < TIMEOUT_BARS:
            leg_b = leg_a
        else:
            m_entry = bars[TIMEOUT_BARS][4]
            shift = m_entry - entry
            leg_b = sim_exit(bars, sign, m_entry, stop + shift, tp + shift, TIMEOUT_BARS)
        out.append({"date": c["date"], "symbol": c["symbol"], "direction": c["direction"],
                    "entry": entry, "touch_min": touch, "filled_15": filled_15,
                    "leg_a_pct": round(leg_a, 3), "leg_b_pct": round(leg_b, 3),
                    "executed_real": bool(c["executed"]), "real_net": c["pnl_net"],
                    "real_exit": c["exit_reason"]})
    return out


def summarize(recs: List[dict]) -> dict:
    unfilled = [r for r in recs if not r["filled_15"]]
    a = [r["leg_a_pct"] for r in recs]
    b = [r["leg_b_pct"] for r in recs]
    diff_by_day: Dict[str, float] = defaultdict(float)
    for r in recs:
        diff_by_day[r["date"]] += r["leg_b_pct"] - r["leg_a_pct"]
    days = list(diff_by_day.values())
    res = {
        "n_cards": len(recs),
        "n_unfilled_15m": len(unfilled),
        "fill_rate_15m_pct": round(100 * (len(recs) - len(unfilled)) / len(recs), 1) if recs else None,
        "leg_a_sum_bps": round(sum(a) * 100, 1),
        "leg_b_sum_bps": round(sum(b) * 100, 1),
        "delta_bps": round((sum(b) - sum(a)) * 100, 1),
        "leg_a_mean_pct": round(statistics.mean(a), 3) if a else None,
        "leg_b_mean_pct": round(statistics.mean(b), 3) if b else None,
        "delta_t_day": None,
    }
    if len(days) >= 5:
        m, sd = statistics.mean(days), statistics.pstdev(days)
        if sd:
            res["delta_t_day"] = round(m / (sd / (len(days) ** 0.5)), 2)
    if unfilled:
        ub = [r["leg_b_pct"] for r in unfilled]
        res["unfilled_legB_mean_pct"] = round(statistics.mean(ub), 3)
        res["unfilled_legB_hit_pct"] = round(100 * sum(1 for x in ub if x > 0) / len(ub), 1)
        res["unfilled_legB_sum_bps"] = round(sum(ub) * 100, 1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--append", action="store_true", help="追加而不是覆盖")
    a = ap.parse_args()

    end = a.end or time.strftime("%Y-%m-%d")
    if a.start:
        start = a.start
    elif a.days:
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - a.days * 86400))
    else:
        start = "2026-07-01"

    cards = load_cards(start, end)
    recs = simulate(cards)
    s = summarize(recs)
    if not recs:
        print("无可用卡片 (%s..%s)" % (start, end))
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if a.append else "w"
    with OUT.open(mode) as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0

    print("== 入场 15 分钟超时影子 (%s..%s) ==" % (start, end))
    print("卡片 %d 张 | 15 分钟内成交 %d 张 (%.1f%%) | 未成交 %d 张" % (
        s["n_cards"], s["n_cards"] - s["n_unfilled_15m"], s["fill_rate_15m_pct"] or 0,
        s["n_unfilled_15m"]))
    print("限价腿  : 累计 %+.1f bps | 均 %+.3f%%" % (s["leg_a_sum_bps"], s["leg_a_mean_pct"] or 0))
    print("超时腿  : 累计 %+.1f bps | 均 %+.3f%%" % (s["leg_b_sum_bps"], s["leg_b_mean_pct"] or 0))
    print("差额    : %+.1f bps | 日聚类 t = %s" % (s["delta_bps"], s["delta_t_day"]))
    if s["n_unfilled_15m"]:
        print("未成交子集(超时腿): 均 %+.3f%% | 命中 %.0f%% | 累计 %+.1f bps" % (
            s["unfilled_legB_mean_pct"], s["unfilled_legB_hit_pct"], s["unfilled_legB_sum_bps"]))
    print("预注册门槛: 未成交样本 ≥30 且 |t|≥2 且双时代同向 才提交执行链变更提案")
    print("落盘: %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())