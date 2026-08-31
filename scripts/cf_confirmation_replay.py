#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认位反事实重放 (固化版, DEB-20260831 §二).

口径: 对带显式确认位 (限价/回踩/等企稳) 的已成交亏损单, 重放"若按 thesis 确认位
入场而非开盘市价"的路径. 只读分析, 不写行为路径.

无未来函数声明:
  - 逐分钟扫描, 确认位触及用当根 bar low 判定, 成交价 = min(确认位, bar开盘价)
  - 止损/收盘用触及后路径模拟; 失效位 (如开盘30min破位取消) 先于确认位判定
  - 量价确认条件 (量能萎缩/RSI/站稳) 无法重放 — bar 无成交量列, 仅重放价格条件,
    故结果为"价格条件下的下界改善", 实际带量价过滤只会更保守
数据源: data/replay_bars/US_<SYM>_<date>.json ([time,o,h,l,c])
用法: python3 scripts/cf_confirmation_replay.py [--json out.json]
"""
import argparse
import json
import sqlite3
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
BAR_DIR = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"

# 确认位从 thesis/entry_strategy 人工抽取 (8/31 溯源, 原文见 DEB-20260831 §二表格).
# 结构: (decision_id, confirm_level, invalidation) — None=thesis 设计即首批市价
CONFIRM_LEVELS = {
    "DA-20260814191538-US.SNDK-6359": (None, None),       # 首批=盘前现价直入(设计)
    "DA-20260819192417-US.SNDK-fce6": (1585.0, None),     # 首批$1585+15min站稳
    "DA-20260818191949-US.FIX-aa59":  (1800.0, None),     # 首批$1800+30min量能
    "DA-20260826191829-US.UBER-3614": (81.00, None),      # 首批$81.00+5min放量
    "DA-20260828191744-US.RBRK-d8e9": (101.0, None),      # 首批$100-101限价(取上沿)
    "DA-20260828191744-US.AFRM-d8e9": (84.0, 79.8),       # 等$83.5-84; 开盘30min破79.8取消
}


def load_bars(sym: str, date: str):
    p = BAR_DIR / f"{sym.replace('.', '_')}_{date}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def replay_case(sym, date, bars, qty, sl, lvl, inv):
    """返回 (status, entry, exit_px, exit_why, cf_pnl). status: entered/never/invalidated."""
    if inv is not None:
        for b in bars:
            if b[0][11:16] >= "10:00":
                break
            if b[3] <= inv:
                return "invalidated", None, None, "CANCEL", 0.0
    entry = None
    for b in bars:
        if b[0][11:16] < "09:31":
            continue
        if b[3] <= lvl:
            entry = (b[0][11:16], min(lvl, b[1]))
            break
    if entry is None:
        return "never_touched", None, None, "NO_ENTRY", 0.0
    et, ep = entry
    exit_px, exit_why = None, None
    for b in bars:
        if b[0][11:16] <= et:
            continue
        if b[3] <= sl:
            exit_px, exit_why = min(sl, b[1]), "SL"
            break
    if exit_px is None:
        last = bars[-1]
        exit_px, exit_why = last[4], "EOD"
    return "entered", entry, exit_px, exit_why, (exit_px - ep) * qty


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="结果落盘路径", default="")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    out = []
    for did, (lvl, inv) in CONFIRM_LEVELS.items():
        r = db.execute(
            "SELECT t.symbol, substr(t.entry_time,1,10) d, t.quantity q, "
            "t.pnl_net, t.mfe_pct, i.stop_loss sl "
            "FROM trade_records t JOIN playbook_instructions i ON i.decision_id = t.decision_id "
            "WHERE t.decision_id=?", (did,)).fetchone()
        if not r:
            print(f"MISS {did}")
            continue
        bars = load_bars(r["symbol"], r["d"])
        if bars is None:
            print(f"{did}: 无bar数据")
            continue
        if lvl is None:
            out.append({"decision_id": did, "symbol": r["symbol"], "date": r["d"],
                        "status": "by_design_market", "actual_pnl": r["pnl_net"], "cf_pnl": r["pnl_net"]})
            print(f"{r['symbol']} {r['d']}: thesis设计首批即市价, 不重放 | 实际净={r['pnl_net']:.0f}")
            continue
        st, ent, xp, xw, cf = replay_case(r["symbol"], r["d"], bars, r["q"], r["sl"], lvl, inv)
        out.append({"decision_id": did, "symbol": r["symbol"], "date": r["d"],
                    "status": st, "actual_pnl": r["pnl_net"], "cf_pnl": cf,
                    "cf_entry": ent[1] if ent else None, "cf_entry_t": ent[0] if ent else None,
                    "cf_exit": xp, "cf_exit_why": xw, "confirm_level": lvl})
        tag = {"entered": f"CF入场{ent[1]:.2f}@{ent[0]} 出场{xp:.2f}{xw}",
               "never_touched": f"确认位${lvl}全天未触及→不入场",
               "invalidated": f"开盘30min破失效位${inv}→取消"}[st]
        print(f"{r['symbol']} {r['d']}: 实际净={r['pnl_net']:.0f} CF={cf:.0f} | {tag}")

    tot_a = sum(o["actual_pnl"] for o in out)
    tot_c = sum(o["cf_pnl"] for o in out)
    print(f"\n合计: 实际={tot_a:.0f} CF={tot_c:.0f} 改善={tot_c - tot_a:+.0f} "
          f"(价格条件下界; 量价条件未重放)")
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1))
        print(f"落盘: {args.json}")


if __name__ == "__main__":
    main()
