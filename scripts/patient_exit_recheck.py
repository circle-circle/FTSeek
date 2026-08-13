#!/usr/bin/env python3
"""耐心出场 §十四 独立复跑 (DEB-20260813 §十八 出入1 仲裁用, 持久版)

作用域血统 (§18.3.4): 51 死单 → 50 (去重 RIVN id=1069, 与 id=1060 同仓位双写)
→ 47 (剔 PRE_CLOSE_EXIT 2 + BROKER_GHOST_CLOSE 1) → bar 可定位如实报告。
约定: 信号 bar=首根 time>=exit_time, 跳过之; 同 bar 先止损后限价; 限价按 entry 成交;
止损 A 口径 = min(open, stop) (下穿保守), B 口径 = stop 价; 兜底 = 15:50 前末根 bar 收盘。
比毛口径 (两臂均一趟往返费)。python3 scripts/patient_exit_recheck.py
"""
import json
import sqlite3
from datetime import datetime

DB = "data/ftseek_us.db"
DEDUP_IDS = {1069}
EXCL_REASONS = ("PRE_CLOSE_EXIT", "BROKER_GHOST_CLOSE")


def bars_for(sym, dt):
    return json.loads(open("data/replay_bars/%s_%s.json" % (sym.replace(".", "_"), dt)).read())


def sim(after, entry, qty, lg, stop_pct, stop_conv):
    stop = entry * (1 - stop_pct / 100) if lg else entry * (1 + stop_pct / 100)
    for b in after:
        if b[0][11:16].replace(":", "") > "1550":
            break
        o, h, l, c = b[1], b[2], b[3], b[4]
        if lg:
            if l <= stop:
                px = min(o, stop) if stop_conv == "A" else stop
                return (px - entry) * qty, "stop"
            if h >= entry:
                return 0.0, "breakeven"
        else:
            if h >= stop:
                px = max(o, stop) if stop_conv == "A" else stop
                return (entry - px) * qty, "stop"
            if l <= entry:
                return 0.0, "breakeven"
    last = [b for b in after if b[0][11:16].replace(":", "") <= "1550"]
    px = last[-1][4] if last else None
    if px is None:
        return None, "no_bar"
    return ((px - entry) * qty if lg else (entry - px) * qty), "eod"


def main():
    db = sqlite3.connect(DB)
    rows = db.execute(
        "SELECT id, symbol, trade_date, direction, entry_price, exit_price, exit_time,"
        " realized_pnl, quantity, exit_reason FROM trade_records"
        " WHERE trade_date>='2026-07-15' AND trade_date<='2026-08-12' AND exit_time IS NOT NULL"
        " AND COALESCE(mfe_pct,0)<0.2 ORDER BY id").fetchall()
    rows = [r for r in rows if r[0] not in DEDUP_IDS]
    scoped, excluded = [], []
    for r in rows:
        if r[9] in EXCL_REASONS:
            excluded.append(r[0])
        else:
            scoped.append(r)
    print("血统: 原始51 → 去重后 %d → SCOPE %d (剔除 %s)" % (len(rows), len(scoped), excluded))

    for conv in ("A", "B"):
        print("--- 止损口径 %s (%s) ---" % (conv, "min(open,stop)" if conv == "A" else "stop价"))
        for sp in (1.0, 1.5, 2.0):
            tot, n, cnt = 0.0, 0, {"breakeven": 0, "stop": 0, "imm": 0, "eod": 0, "no_bar": 0}
            for tid, sym, dt, direc, entry, exitp, extime, gross, qty, _reason in scoped:
                bars = bars_for(sym, dt)
                sig = datetime.fromisoformat(extime).replace(tzinfo=None)
                idx = next((i for i, b in enumerate(bars)
                            if datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S") >= sig), None)
                if idx is None:
                    cnt["no_bar"] += 1
                    continue
                uw = -(exitp / entry - 1) * 100 if direc == "LONG" else -(1 - exitp / entry) * 100
                if uw >= sp:
                    cnt["imm"] += 1
                    n += 1
                    continue
                cf, outcome = sim(bars[idx + 1:], entry, qty, direc == "LONG", sp, conv)
                if cf is None:
                    cnt["no_bar"] += 1
                    continue
                tot += cf - gross
                cnt[outcome] += 1
                n += 1
            print("止损%.1f%%: n=%d 保本%d 止损%d 立即%d 兜底%d 无bar%d | delta=%+.1f"
                  % (sp, n, cnt["breakeven"], cnt["stop"], cnt["imm"], cnt["eod"], cnt["no_bar"], tot))


if __name__ == "__main__":
    main()
