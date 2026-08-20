#!/usr/bin/env python3
"""W_WEEK 水深上限阈值 EV 回放 (2026-08-20, DEB §48.2 归档, §49.3-1 补档).

用途: loser_overnight_max_depth_pct 的回滚/重裁依据 — 重跑本脚本复核阈值。
样本: ① data/overnight_loser_multiday_2026-08-15.json 回放 15 笔
        (day_pnl=EOD 决策时刻盈亏, w_week_delta=W_WEEK 结局)
     ② 8/16 后实弹 (bars 实算 EOD 15:52 价)
语义: 决策时刻水深 < -c → EOD 出场 (结局=day_pnl); 否则持夜 (结局=w_week_delta/实弹净).

裁决记录 (§48.2): cap=2.0 切 2 笔 (MU/FIX -3.1%, 0修复2灾难), EV +30.25
vs 全持夜 -181.08; cap=1.5 是刀尖拟合 (CAT -1.62 死 / UBER -1.51 活差 0.12pp) 弃用.
"""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"

# 8/16 后 W_WEEK 臂实弹 (新增实弹时在此追加)
LIVE_CASES = [
    ("US.UBER", "2026-08-17"), ("US.MU", "2026-08-18"), ("US.FIX", "2026-08-18"),
    ("US.CAT", "2026-08-18"), ("US.TSM", "2026-08-18"),
]
EOD_HHMM = "15:52"


def load_bars(symbol: str, date_str: str):
    p = ROOT / "data" / "replay_bars" / f"{symbol.replace('.', '_')}_{date_str}.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return data.get("bars", []) if isinstance(data, dict) else data


def eod_price(bars, hhmm=EOD_HHMM):
    px = None
    for b in bars:
        if b[0][11:16] <= hhmm:
            px = b[4]
    return px


def main():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    samples = []  # (date, symbol, depth_pct, eod_outcome, hold_outcome)

    # ① 回放样本
    rep_path = ROOT / "data" / "overnight_loser_multiday_2026-08-15.json"
    if rep_path.exists():
        rep = json.loads(rep_path.read_text())
        for item in rep["detail"]:
            row = con.execute("""
                SELECT entry_price, quantity FROM trade_records
                WHERE symbol=? AND trade_date=? AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_SHADOW')
                ORDER BY entry_time LIMIT 1
            """, (item["symbol"], item["date"])).fetchone()
            notional = None
            if row and row["quantity"]:
                notional = row["entry_price"] * row["quantity"]
            else:
                row2 = con.execute("""
                    SELECT entry_price, position_size FROM playbook_instructions
                    WHERE symbol=? AND date(created_at)=? AND executed=1 LIMIT 1
                """, (item["symbol"], item["date"])).fetchone()
                if row2 and row2["position_size"]:
                    notional = row2["entry_price"] * row2["position_size"]
            if not notional or notional <= 0:
                continue
            depth = item["day_pnl"] / notional * 100.0
            samples.append((item["date"], item["symbol"], depth,
                            item["day_pnl"], item["w_week_delta"]))

    # ② 实弹样本
    for sym, d in LIVE_CASES:
        row = con.execute("""
            SELECT entry_price, quantity, direction, COALESCE(pnl_net, realized_pnl) net
            FROM trade_records WHERE symbol=? AND trade_date=? AND source_type='PLAYBOOK_SIM'
            ORDER BY entry_time LIMIT 1
        """, (sym, d)).fetchone()
        bars = load_bars(sym, d)
        if not row or not bars:
            continue
        px = eod_price(bars)
        if px is None:
            continue
        ep, qty = row["entry_price"], row["quantity"]
        eod_pnl = (px - ep) * qty if row["direction"] == "LONG" else (ep - px) * qty
        depth = eod_pnl / (ep * qty) * 100.0
        samples.append((d, sym, depth, eod_pnl, row["net"]))

    print(f"总样本 n={len(samples)}")
    print(f"{'date':11s} {'symbol':9s} {'EOD水深%':>8s} {'EOD出场':>9s} {'持夜结局':>9s}")
    for s in sorted(samples, key=lambda x: x[2]):
        print(f"{s[0]:11s} {s[1]:9s} {s[2]:8.2f} {s[3]:9.2f} {s[4]:9.2f}")

    all_hold = sum(s[4] for s in samples)
    print(f"\n=== 水深上限 EV 曲线 (对照全持夜 {all_hold:+.2f}) ===")
    print(f"{'上限%':>6s} {'被切笔数':>8s} {'总EV':>10s}")
    for c in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 99]:
        ev, cut = 0.0, 0
        for s in samples:
            if s[2] < -c:
                ev += s[3]
                cut += 1
            else:
                ev += s[4]
        label = "∞(全持夜)" if c == 99 else f"{c}"
        print(f"{label:>6s} {cut:>8d} {ev:10.2f}")


if __name__ == "__main__":
    main()
