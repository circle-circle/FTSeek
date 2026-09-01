#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[DEB-20260901 Phase B 补盲] 结构化指令标的的全天 bar 保障.

根因: cf_pullback_limit_replay (05:47) 依赖 data/replay_bars/<SYM>_<date>.json,
但唯一写入方 position_path_collector (05:15) 只采当日**有平仓笔**的标的 —
带结构化入场语义却未成交/未平仓的指令会 no_bars → 影子样本静默丢失。

本脚本 (05:35, 介于两者之间): 对指定日期所有带 entry_archetype 的指令标的,
缺 bar 文件的用 exit_replay_backtest.fetch_bars 补拉 (缓存+AuType.NONE+1.2s限流,
与既有回放同一份缓存, 零重复拉取)。只补文件, 不改任何数据/行为。

用法: PYTHONPATH=. ./venv/bin/python scripts/archetype_bars_ensure.py [--date YYYY-MM-DD]
      缺省 date = 昨天 (与 cf_pullback_limit_replay 缺省口径一致)。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

BAR_DIR = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT DISTINCT symbol, raw_instruction_json FROM playbook_instructions "
        "WHERE date = ? AND direction != 'SKIP'", (date,)).fetchall()
    con.close()

    arch_syms, todo = set(), []
    for r in rows:
        raw = _safe(r["raw_instruction_json"])
        if not raw.get("entry_archetype"):
            continue
        arch_syms.add(r["symbol"])
        fp = BAR_DIR / f"{r['symbol'].replace('.', '_')}_{date}.json"
        if not fp.exists() and r["symbol"] not in todo:
            todo.append(r["symbol"])

    print(f"[archetype-bars] {date}: 带archetype指令标的 {len(arch_syms)} 个, 缺 bar {len(todo)} 个: {todo}")
    if not todo:
        return 0

    from exit_replay_backtest import fetch_bars
    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    ok = fail = 0
    try:
        for sym in todo:
            try:
                bars = fetch_bars(ctx, sym, date)
                if bars:
                    ok += 1
                    print(f"  [ok] {sym}: {len(bars)} bars")
                else:
                    fail += 1
                    print(f"  [empty] {sym}: 无 bar (休市/退市合法空)")
            except Exception as e:
                fail += 1
                print(f"  [fail] {sym}: {e}")
    finally:
        try:
            ctx.close()
        except Exception as e:
            # R5 纪律留痕: 收尾异常不吞 (连接已用尽, 无副作用, 仅留痕)
            print(f"[archetype-bars] ctx.close 异常(无副作用): {e}")
    print(f"[archetype-bars] 完成: 补拉成功 {ok}, 失败/空 {fail}")
    return 0


def _safe(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


if __name__ == "__main__":
    sys.exit(main())
