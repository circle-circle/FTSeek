#!/usr/bin/env python3
"""平仓价历史回填 (2026-07-28, DEB-entry-fill §九 用户裁"需要回填")。

范围: exit_price_source ∈ {broker_closed_no_price(全史), NULL(2026-07-14 起, 字段
07-03 才有, 更早的缺失不碰)} — 平仓价被报价快照兜底的存量。
匹配: history_order_list_query 当日订单, 平仓单方向与持仓相反 (LONG→SELL / SHORT→BUY),
qty 精确 + 多候选时 exit_time 邻近 <120s。置信度 HIGH/MEDIUM/NONE 同入场侧口径。
重算: realized_pnl / pnl_pct (exit 价不影响 mfe/mae, 不动)。
审计: exit_price_orig / exit_price_source_orig / exit_corrected / exit_backfill_order_id。
安全: 默认 dry-run 报告; --apply 先 sqlite backup 在线备份; 幂等 (exit_corrected 跳过)。
用法: PYTHONPATH=. ./venv/bin/python scripts/exit_fill_backfill.py [--apply]
"""
from __future__ import annotations

import argparse
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
OUT = REPO / "data" / "exit_fill_impact_report.json"


def load_affected() -> list[dict]:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT id, symbol, trade_date, direction, exit_time, entry_price, exit_price, "
        "quantity, realized_pnl, metadata_json FROM trade_records "
        "WHERE exit_time IS NOT NULL AND exit_time != ''").fetchall()
    c.close()
    out = []
    for r in rows:
        m = json.loads(r["metadata_json"] or "{}")
        src = m.get("exit_price_source")
        if m.get("exit_corrected"):
            continue  # 幂等
        if src == "broker_closed_no_price" or (src is None and r["trade_date"] >= "2026-07-14"):
            d = dict(r)
            d["_src"] = src or "NULL"
            out.append(d)
    return out


def fetch_day_orders(ctx, trd_env, day: str) -> list[dict]:
    from futu import RET_OK
    ret, data = ctx.history_order_list_query(start=day, end=day, trd_env=trd_env)
    time.sleep(3.2)  # 限频保护
    if ret != RET_OK or data is None or data.empty:
        return []
    out = []
    for _, o in data.iterrows():
        if float(o.get("dealt_qty", 0) or 0) <= 0:
            continue
        out.append({
            "code": str(o.get("code", "")),
            "trd_side": str(o.get("trd_side", "")).upper(),
            "dealt_qty": float(o.get("dealt_qty", 0) or 0),
            "dealt_avg_price": float(o.get("dealt_avg_price", 0) or 0),
            "create_time": str(o.get("create_time", "")),
            "order_id": str(o.get("order_id", "")),
        })
    return out


def _exit_side(direction: str) -> str:
    """平仓单方向与持仓相反: LONG 平仓=SELL, SHORT 平仓=BUY."""
    return "BUY" if str(direction).upper() in ("SHORT", "SELL_SHORT") else "SELL"


def match_order(rec: dict, day_orders: list[dict]) -> tuple[dict | None, str]:
    side = _exit_side(rec["direction"])
    cands = [o for o in day_orders
             if o["code"] == rec["symbol"] and side in o["trd_side"]
             and o["dealt_avg_price"] > 0]
    qty_match = [o for o in cands if abs(o["dealt_qty"] - float(rec["quantity"])) < 0.5]
    if len(qty_match) == 1:
        return qty_match[0], "HIGH"
    if len(qty_match) > 1:
        try:
            xt = datetime.fromisoformat(rec["exit_time"])
            xt = xt.astimezone(ET) if xt.tzinfo else to_et(xt)
            best, best_gap = None, 1e9
            for o in qty_match:
                odt = ET.localize(datetime.strptime(o["create_time"][:19], "%Y-%m-%d %H:%M:%S"))
                gap = abs((odt - xt).total_seconds())
                if gap < best_gap:
                    best, best_gap = o, gap
            if best is not None and best_gap < 120:
                return best, "MEDIUM"
        except Exception:
            pass
    return None, "NONE"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    recs = load_affected()
    days = sorted({r["trade_date"] for r in recs})
    print(f"平仓价回填: {len(recs)} 笔候选 × {len(days)} 交易日 "
          f"(broker_closed_no_price {sum(1 for r in recs if r['_src'] != 'NULL')} / "
          f"NULL {sum(1 for r in recs if r['_src'] == 'NULL')})")

    from futu import OpenSecTradeContext, TrdEnv, TrdMarket, SecurityFirm
    ctx = OpenSecTradeContext(
        filter_trdmarket=TrdMarket.US,
        host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
        port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
        security_firm=SecurityFirm.FUTUSECURITIES, is_encrypt=False)
    day_cache = {}
    try:
        for i, day in enumerate(days):
            day_cache[day] = fetch_day_orders(ctx, TrdEnv.SIMULATE, day)
            print(f"  [{i+1}/{len(days)}] {day}: {len(day_cache[day])} 笔订单")
    finally:
        ctx.close()

    if args.apply:
        bak = DB.with_name(f"ftseek_us.db.bak_exitfill_{datetime.now():%Y%m%d_%H%M%S}")
        src = sqlite3.connect(str(DB)); dst = sqlite3.connect(str(bak))
        src.backup(dst); dst.close(); src.close()
        print(f"DB 已在线备份: {bak.name}")

    conn = sqlite3.connect(str(DB) if args.apply else f"file:{DB}?mode=ro", uri=not args.apply)
    conn.row_factory = sqlite3.Row
    conf_cnt = {"HIGH": 0, "MEDIUM": 0, "NONE": 0}
    n_done, flips, dpnl_sum, d_exits = 0, 0, 0.0, []
    wins_old = wins_new = 0
    details = []
    for r in recs:
        order, conf = match_order(r, day_cache.get(r["trade_date"], []))
        conf_cnt[conf] += 1
        row = {"id": r["id"], "symbol": r["symbol"], "date": r["trade_date"],
               "exit_old": r["exit_price"], "pnl_old": r["realized_pnl"], "match": conf}
        if order:
            new_exit = order["dealt_avg_price"]
            short = str(r["direction"]).upper() in ("SHORT", "SELL_SHORT")
            sign = -1 if short else 1
            qty = float(r["quantity"] or 0)
            entry = float(r["entry_price"] or 0)
            old_pnl = float(r["realized_pnl"] or 0)
            new_pnl = round((new_exit - entry) * qty * sign, 4)
            new_pct = round((new_exit - entry) / entry * sign, 6) if entry > 0 else None
            d_exit_pct = ((new_exit - float(r["exit_price"])) / float(r["exit_price"]) * 100
                          if r["exit_price"] else None)
            row.update({"exit_new": new_exit, "pnl_new": new_pnl,
                        "d_exit_pct": round(d_exit_pct, 4) if d_exit_pct is not None else None,
                        "order_id": order["order_id"]})
            d_exits.append(d_exit_pct or 0)
            dpnl_sum += new_pnl - old_pnl
            wins_old += old_pnl > 0
            wins_new += new_pnl > 0
            if (old_pnl > 0) != (new_pnl > 0):
                flips += 1
                row["flip"] = True
                print(f"  [翻转] id={r['id']} {r['symbol']} pnl {old_pnl:+.2f} → {new_pnl:+.2f}")
            if args.apply:
                meta = json.loads(r["metadata_json"] or "{}")
                meta.update({
                    "exit_price_orig": r["exit_price"],
                    "exit_price_source_orig": meta.get("exit_price_source"),
                    "exit_price_source": "broker_deal_backfill",
                    "exit_corrected": True,
                    "exit_corrected_at": datetime.now().isoformat(),
                    "exit_backfill_order_id": order["order_id"],
                })
                sets = "exit_price=?, realized_pnl=?, metadata_json=?"
                vals = [new_exit, new_pnl, json.dumps(meta, ensure_ascii=False)]
                if new_pct is not None:
                    sets += ", pnl_pct=?"
                    vals.append(new_pct)
                vals.append(r["id"])
                conn.execute(f"UPDATE trade_records SET {sets} WHERE id=?", vals)
            n_done += 1
        elif args.apply:
            meta = json.loads(r["metadata_json"] or "{}")
            if not meta.get("exit_uncorrectable"):
                meta["exit_uncorrectable"] = True
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (json.dumps(meta, ensure_ascii=False), r["id"]))
        details.append(row)
    if args.apply:
        conn.commit()
    conn.close()

    n = n_done
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n[{mode}] 汇总: 匹配 {n}/{len(recs)} (HIGH {conf_cnt['HIGH']}/MEDIUM {conf_cnt['MEDIUM']}/NONE {conf_cnt['NONE']})")
    if d_exits:
        ds = sorted(d_exits)
        print(f"Δexit 分布(%): median {ds[len(ds)//2]:+.3f} p10 {ds[len(ds)//10]:+.3f} p90 {ds[len(ds)*9//10]:+.3f}")
        print(f"ΔPnL 合计: {dpnl_sum:+.2f} | 胜率(此切片): {wins_old}/{n} → {wins_new}/{n} | 翻转 {flips}")
    OUT.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(), "mode": mode,
        "total": len(recs), "matched": n, "confidence": conf_cnt,
        "delta_pnl_total": round(dpnl_sum, 2), "flips": flips, "details": details,
    }, ensure_ascii=False, indent=1))
    print(f"落盘: {OUT}")


if __name__ == "__main__":
    main()
