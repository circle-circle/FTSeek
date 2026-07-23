#!/usr/bin/env python3
"""TIME_EXIT 反事实 shadow (record-only) — 2026-07-22 经批准落地.

测量问题: TIME_EXIT (持仓超时+浮亏认赔) 是否"割在低点"?
方法: 每个 ET 交易日结束后, 对当日全部 TIME_EXIT 平仓, 用当日收盘价做反事实对比:
  counterfactual_pnl = 若持有到收盘的盈亏; delta = counterfactual - realized.
  delta > 0 → TIME_EXIT 割早了 (亏损扩大); delta < 0 → TIME_EXIT 判对.
产出: data/shadow/time_exit_counterfactual.jsonl (逐笔 + 当日汇总, append-only, 幂等).
裁决: 连续测量 ~2 周 (≥10 个交易日) 后, 用累计 delta 裁决 time_exit 参数是否调整.
     首验: 2026-07-21 手测 6 笔合计 delta=-$11 (TIME_EXIT 判对).
     2026-07-23 第 8 轮判决修复: 原实现误把 counterfactual_pnl 算成增量 (close-exit_price)*qty,
     delta 再减 realized 时重复计入 entry→exit 段, 虚增 |realized| 致亏损单系统性误判"割早"
     (与首验手测注记矛盾). 现改回 docstring 语义: counterfactual_pnl=总量 (close-entry_price)*qty,
     delta=counterfactual-realized=(close-exit_price)*qty. 见 DEB-20260723-da-execution-mismatch-postmortem 第 8 轮.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "ftseek_us.db")
OUT = os.path.join(BASE, "data", "shadow", "time_exit_counterfactual.jsonl")
ET = ZoneInfo("America/New_York")


def _already_done(trade_date: str) -> bool:
    if not os.path.exists(OUT):
        return False
    with open(OUT) as f:
        for line in f:
            try:
                if json.loads(line).get("trade_date") == trade_date:
                    return True
            except Exception:
                continue
    return False


def main() -> int:
    # 目标交易日: 最近一个已结束的 ET 交易日 (盘后跑=当天, 盘前跑=上一交易日)
    now_et = datetime.now(ET)
    d = now_et.date()
    if now_et.hour < 16:  # 16:00 ET 前 → 上一交易日
        from datetime import timedelta
        d = (now_et - timedelta(days=1)).date()
        while d.weekday() >= 5:
            d = (d - timedelta(days=1)).date()
    trade_date = d.isoformat()

    if _already_done(trade_date):
        print(f"[time_exit_cf] {trade_date} 已记录, 跳过 (幂等)")
        return 0

    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, symbol, direction, quantity, entry_price, exit_price, realized_pnl, exit_time
        FROM trade_records
        WHERE source_type LIKE 'PLAYBOOK%' AND trade_date = ? AND exit_reason = 'TIME_EXIT'
    """, (trade_date,)).fetchall()
    conn.close()
    if not rows:
        print(f"[time_exit_cf] {trade_date} 无 TIME_EXIT 记录")
        return 0

    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    records = []
    tot_realized = tot_cf = 0.0
    try:
        for r in rows:
            sym, xp, qty = r["symbol"], float(r["exit_price"]), float(r["quantity"])
            entry = float(r["entry_price"])
            direction = r["direction"]
            ret, k, _ = ctx.request_history_kline(sym, start=trade_date, end=trade_date, ktype="K_15M")
            if ret != RET_OK or k.empty:
                print(f"[time_exit_cf] {sym} K线获取失败, 跳过")
                continue
            close = float(k.iloc[-1]["close"])
            # 持有到收盘的 PnL (总量, entry→close 参考系). 注意必须用 entry 而非 xp:
            # (close-xp)*qty 只是增量(exit→close), 若把它当 counterfactual 再减 realized
            # 会重复计入 entry→exit 段, 虚增 |realized|, 对亏损单系统性误判"割早"(第 8 轮修复).
            cf_pnl = round((close - entry) * qty if direction == "LONG" else (entry - close) * qty, 2)
            realized = round(float(r["realized_pnl"] or 0), 2)
            rec = {
                "trade_date": trade_date, "trade_id": r["id"], "symbol": sym,
                "direction": direction, "exit_price": xp, "close_price": close,
                "exit_time_et": r["exit_time"], "qty": qty,
                "realized_pnl": realized, "counterfactual_pnl": cf_pnl,
                "delta": round(cf_pnl - realized, 2),
            }
            records.append(rec)
            tot_realized += realized
            tot_cf += cf_pnl
    finally:
        ctx.close()

    if not records:
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.write(json.dumps({
            "trade_date": trade_date, "type": "daily_summary", "n": len(records),
            "realized_total": round(tot_realized, 2), "counterfactual_total": round(tot_cf, 2),
            "delta_total": round(tot_cf - tot_realized, 2),
            "verdict": "TIME_EXIT判对" if tot_cf < tot_realized else "TIME_EXIT割早",
        }, ensure_ascii=False) + "\n")
    print(f"[time_exit_cf] {trade_date}: {len(records)} 笔, realized={tot_realized:.2f} "
          f"cf={tot_cf:.2f} delta={tot_cf - tot_realized:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
