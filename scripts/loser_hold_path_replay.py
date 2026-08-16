#!/usr/bin/env python3
"""亏损单持有的路径感知回放 (DEB-20260806 反方攻击2 主控★复核, 2026-08-06, 只读).

close锚定 (loser_hold_counterfactual.py) 忽略盘中路径: "持有到T+1收盘"的策略若先碰
初始SL, 真实结局是SL离场而非T+1收盘。本脚本用 data/replay_bars 1-min缓存做路径判定:
  exit日+T+1日全日 min(low)/max(high) 触及初始SL → 按SL价离场 (乐观: 无跳空穿价建模),
  否则按 T+1 收盘价。对照 close锚定 同一覆盖子集。
exit_time 列60日窗仅28/232非空 → 用全日极值 (有效: exit前若已触SL, 出场原因必为SL类)。

输出: 控制台 + data/loser_hold_path_replay_latest.json
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
BAR = ROOT / "data" / "replay_bars"
OUT = ROOT / "data" / "loser_hold_path_replay_latest.json"


def next_td(d, n=1):
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT date, symbol, direction, entry_price, exit_price, stop_loss,
                  fwd_return_t1, exit_reason
           FROM playbook_instructions
           WHERE triggered=1 AND executed=1 AND exit_price>0 AND entry_price>0
             AND fwd_return_t1 IS NOT NULL AND date>=date('now','-60 days')""").fetchall()

    cov = touch = flips_close = flips_path = 0
    inc_close, inc_path = [], []
    ghost_flips = []
    for r in rows:
        sign = 1.0 if r["direction"] == "BUY" else -1.0
        er = sign * (r["exit_price"] - r["entry_price"]) / r["entry_price"]
        if er >= 0 or not r["stop_loss"]:
            continue
        sym = r["symbol"].replace(".", "_")
        d0 = datetime.strptime(r["date"], "%Y-%m-%d").date()
        d1 = next_td(d0)
        f0, f1 = BAR / f"{sym}_{d0}.json", BAR / f"{sym}_{d1}.json"
        if not (f0.exists() and f1.exists()):
            continue
        bars = json.load(open(f0)) + json.load(open(f1))
        lows = [b[3] for b in bars]
        highs = [b[2] for b in bars]
        cov += 1
        sl = r["stop_loss"]
        t = (min(lows) <= sl) if sign > 0 else (max(highs) >= sl)
        close_t1 = r["entry_price"] * (1 + sign * r["fwd_return_t1"])
        i_close = sign * (close_t1 - r["exit_price"]) / r["exit_price"]
        i_path = sign * (sl - r["exit_price"]) / r["exit_price"] if t else i_close
        if t:
            touch += 1
        fc = (1 + er) * (1 + i_close) > 1
        fp = (1 + er) * (1 + i_path) > 1
        flips_close += fc
        flips_path += fp
        if fc and not fp:
            ghost_flips.append({"date": r["date"], "symbol": r["symbol"],
                                "exit_reason": r["exit_reason"],
                                "close_incr_pct": round(i_close * 100, 2)})
        inc_close.append(i_close)
        inc_path.append(i_path)

    m_c = sum(inc_close) / cov
    m_p = sum(inc_path) / cov
    res = {"window_days": 60, "coverage": cov, "sl_touch_n": touch,
           "sl_touch_rate": round(touch / cov, 3),
           "flip_close_anchor": f"{flips_close}/{cov}",
           "flip_path_aware": f"{flips_path}/{cov}",
           "incr_mean_close_anchor": round(m_c, 5),
           "incr_mean_path_aware": round(m_p, 5),
           "ghost_flips": ghost_flips,
           "caveats": ["SL按价成交无跳空穿价建模 (真实只会更差)",
                        "全日极值 (exit_time大多为空), exit前触碰不可能 (否则exit_reason=SL类)",
                        "coverage受replay_bars缓存限制"]}
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"覆盖 {cov}/99 | SL触碰 {touch}/{cov} = {touch / cov:.1%}")
    print(f"翻盘率 close锚 {flips_close}/{cov}={flips_close / cov:.1%} → 路径感知 {flips_path}/{cov}={flips_path / cov:.1%}")
    print(f"增量均值 close锚 {m_c * 100:+.3f}% → 路径感知 {m_p * 100:+.3f}%")
    print(f"幽灵翻盘 {len(ghost_flips)} 笔: {json.dumps(ghost_flips, ensure_ascii=False)}")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
