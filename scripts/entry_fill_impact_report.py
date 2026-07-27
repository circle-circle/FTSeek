#!/usr/bin/env python3
"""入场价回填影响面报告 (2026-07-27, DEB-entry-fill §六 阶段2 前置) — 只读干跑, 零写入。

目的: 130 笔 quote_fallback 记录回填前, 按 Q2 预注册判据出影响面报告:
  - 可匹配率 (history_order_list_query 拿真 dealt_avg_price, sim 保留深度实测)
  - Δentry / Δpnl 逐笔分布; 盈亏符号翻转笔数
  - 胜率前后对比 (整体/按日) — |Δ胜率|>3pp 或 翻转>5% → 触发历史裁决重审
匹配口径 (无 order_id 落库, 模糊匹配, 置信度分层如实报告):
  HIGH   = 当日同标的 唯一 BUY(LONG)/SELL(SHORT) 开仓单 且 dealt_qty==记录 qty
  MEDIUM = 多笔候选中 create_time 与 entry_time 差 <120s 且 qty 匹配
  NONE   = 无候选 / 歧义无法定单
用法: PYTHONPATH=. ./venv/bin/python scripts/entry_fill_impact_report.py
落盘: data/entry_fill_impact_report.json (含逐笔明细, 供回填执行时直接消费)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.utils.timezone import ET, to_et  # noqa: E402

DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "entry_fill_impact_report.json"


def load_affected() -> list[dict]:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, symbol, trade_date, direction, entry_time, entry_price, exit_price, "
        "quantity, realized_pnl, exit_reason FROM trade_records "
        "WHERE metadata_json LIKE '%quote_fallback%' ORDER BY trade_date, entry_time").fetchall()
    c.close()
    return [dict(r) for r in rows]


def fetch_day_orders(ctx, trd_env, day: str) -> list[dict]:
    """当日全部已成交订单 (history_order_list_query, position_sync 同款消费惯例)."""
    from futu import RET_OK
    ret, data = ctx.history_order_list_query(start=day, end=day, trd_env=trd_env)
    time.sleep(3.2)  # 限频保护 (10 次/30s)
    if ret != RET_OK or data is None or data.empty:
        return []
    out = []
    for _, o in data.iterrows():
        if float(o.get("dealt_qty", 0) or 0) <= 0:
            continue
        out.append({
            "code": str(o.get("code", "")),
            "trd_side": str(o.get("trd_side", "")),
            "dealt_qty": float(o.get("dealt_qty", 0) or 0),
            "dealt_avg_price": float(o.get("dealt_avg_price", 0) or 0),
            "create_time": str(o.get("create_time", "")),
            "order_id": str(o.get("order_id", "")),
        })
    return out


def _entry_side(direction: str) -> str:
    return "SELL" if str(direction).upper() in ("SHORT", "SELL_SHORT") else "BUY"


def match_order(rec: dict, day_orders: list[dict]) -> tuple[dict | None, str]:
    """→ (匹配订单, 置信度 HIGH/MEDIUM/NONE)."""
    side = _entry_side(rec["direction"])
    cands = [o for o in day_orders
             if o["code"] == rec["symbol"] and side in o["trd_side"].upper()
             and o["dealt_avg_price"] > 0]
    qty_match = [o for o in cands if abs(o["dealt_qty"] - float(rec["quantity"])) < 0.5]
    if len(qty_match) == 1:
        return qty_match[0], "HIGH"
    if len(qty_match) > 1:
        # 多候选: entry_time 邻近 <120s (订单 create_time 为 ET naive 字符串)
        try:
            et_dt = datetime.fromisoformat(rec["entry_time"])
            et_dt = et_dt.astimezone(ET) if et_dt.tzinfo else to_et(et_dt)
            best, best_gap = None, 1e9
            for o in qty_match:
                odt = ET.localize(datetime.strptime(o["create_time"][:19], "%Y-%m-%d %H:%M:%S"))
                gap = abs((odt - et_dt).total_seconds())
                if gap < best_gap:
                    best, best_gap = o, gap
            if best is not None and best_gap < 120:
                return best, "MEDIUM"
        except Exception:
            pass
    return None, "NONE"


def main():
    recs = load_affected()
    days = sorted({r["trade_date"] for r in recs})
    print(f"影响面报告: {len(recs)} 笔 quote_fallback × {len(days)} 交易日 (只读干跑)")

    from futu import OpenSecTradeContext, TrdEnv, TrdMarket, SecurityFirm
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
        security_firm=SecurityFirm.FUTUSECURITIES, is_encrypt=False)
    # sim 记录, 查 SIMULATE 环境
    trd_env = TrdEnv.SIMULATE
    # 解锁非必须 (SIMULATE 查询), 但保持与 position_sync 同款: 有 pwd 则解锁
    try:
        pwd = os.getenv("FUTU_UNLOCK_PWD_MD5", "")
        if pwd:
            ctx.unlock_trade(password_md5=pwd, is_unlock=True)
    except Exception:
        pass

    day_cache: dict[str, list[dict]] = {}
    details = []
    try:
        for i, day in enumerate(days):
            day_cache[day] = fetch_day_orders(ctx, trd_env, day)
            print(f"  [{i+1}/{len(days)}] {day}: {len(day_cache[day])} 笔已成交订单")
    finally:
        ctx.close()

    flips, wins_old, wins_new, matched = 0, 0, 0, 0
    conf_cnt = {"HIGH": 0, "MEDIUM": 0, "NONE": 0}
    d_entries, d_pnls = [], []
    by_day: dict[str, dict] = {}
    for r in recs:
        order, conf = match_order(r, day_cache.get(r["trade_date"], []))
        conf_cnt[conf] += 1
        row = {"id": r["id"], "symbol": r["symbol"], "date": r["trade_date"],
               "direction": r["direction"], "qty": r["quantity"],
               "entry_old": r["entry_price"], "pnl_old": r["realized_pnl"],
               "match": conf}
        slot = by_day.setdefault(r["trade_date"], {"n": 0, "matched": 0,
                                                   "pnl_old": 0.0, "pnl_new": 0.0})
        slot["n"] += 1
        if order:
            matched += 1
            slot["matched"] += 1
            true_entry = order["dealt_avg_price"]
            sign = -1 if _entry_side(r["direction"]) == "SELL" else 1
            qty = float(r["quantity"] or 0)
            exit_p = float(r["exit_price"] or 0)
            pnl_new = (exit_p - true_entry) * qty * sign if exit_p > 0 else None
            row.update({"entry_new": true_entry, "order_id": order["order_id"],
                        "d_entry_pct": round((true_entry - float(r["entry_price"])) /
                                             float(r["entry_price"]) * 100, 4)
                        if r["entry_price"] else None,
                        "pnl_new": round(pnl_new, 2) if pnl_new is not None else None})
            if pnl_new is not None and r["realized_pnl"] is not None:
                d_entries.append(row["d_entry_pct"])
                d_pnls.append(pnl_new - float(r["realized_pnl"]))
                slot["pnl_old"] += float(r["realized_pnl"])
                slot["pnl_new"] += pnl_new
                old_win = float(r["realized_pnl"]) > 0
                new_win = pnl_new > 0
                wins_old += old_win
                wins_new += new_win
                if old_win != new_win:
                    flips += 1
                    row["flip"] = True
        details.append(row)

    n_scored = len(d_pnls)
    wr_old = wins_old / n_scored * 100 if n_scored else 0
    wr_new = wins_new / n_scored * 100 if n_scored else 0
    q2_trigger = (abs(wr_new - wr_old) > 3.0) or (n_scored and flips / n_scored > 0.05)

    print("\n═══ 影响面汇总 ═══")
    print(f"匹配率: {matched}/{len(recs)} (HIGH {conf_cnt['HIGH']} / MEDIUM {conf_cnt['MEDIUM']} / NONE {conf_cnt['NONE']})")
    if n_scored:
        d_entries_s = sorted(d_entries)
        print(f"Δentry 分布 (%): median {d_entries_s[n_scored//2]:+.3f}, "
              f"p10 {d_entries_s[n_scored//10]:+.3f}, p90 {d_entries_s[n_scored*9//10]:+.3f}")
        print(f"ΔPnL 合计: {sum(d_pnls):+.2f} (均 {sum(d_pnls)/n_scored:+.2f}/笔)")
        print(f"胜率: {wr_old:.1f}% → {wr_new:.1f}% (Δ{wr_new-wr_old:+.1f}pp)")
        print(f"盈亏符号翻转: {flips}/{n_scored} ({flips/n_scored*100:.1f}%)")
        print(f"Q2 判据 (|Δ胜率|>3pp 或 翻转>5%): {'🔴 触发 — 历史裁决须重审' if q2_trigger else '🟢 未触发'}")
    print("\n按日 PnL 前后对比:")
    for d in days:
        s = by_day[d]
        print(f"  {d}: n={s['n']} matched={s['matched']} pnl {s['pnl_old']:+.1f} → {s['pnl_new']:+.1f}")

    OUT.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(),
        "total": len(recs), "matched": matched, "confidence": conf_cnt,
        "scored": n_scored, "win_rate_old_pct": round(wr_old, 2),
        "win_rate_new_pct": round(wr_new, 2), "flips": flips,
        "delta_pnl_total": round(sum(d_pnls), 2) if d_pnls else 0,
        "q2_trigger": bool(q2_trigger), "by_day": by_day, "details": details,
    }, ensure_ascii=False, indent=1))
    print(f"\n落盘: {OUT}")


if __name__ == "__main__":
    main()
