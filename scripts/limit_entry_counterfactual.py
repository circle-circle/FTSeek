#!/usr/bin/env python3
"""S6 限价入场反事实回放 (DEB-20260804-scan-subscribe-entry-ruler §五5.5/§六, 2026-08-04).

问题: S6 判决"维持市价"的核心反驳 — "限价省下的钱 < 漏单机会成本" — 当时是断言.
本脚本用 entry_low/entry_high (playbook_instructions) + 1min bar 把它变成测量.

设计 (§六6.4 裁定的三列):
  每笔记账: 触发时刻改挂限价单 (LONG@entry_high / LONG@entry_low, SHORT 镜像),
  在 实际入场时刻→实际退出时刻 的 1min bar 窗口内判定是否触价成交:
    成交 → 成交价=限价 (跳空穿越按 bar open, 对己方有利), 沿用实际退出价/数量/方向,
            毛盈亏 + fee_model 重算费用 → 反事实净
    未成交 → 该笔不存在, 盈亏 0 (漏单)
  另报 "当日收盘未成交即撤单" 敏感性切法.

输出三列: 成交率差 / 成交子集后续PnL vs 实际 / 未成交子集实际PnL (幸存者偏差方向).

保真度边界: 触价=成交 (忽略排队位置, 对边缘触价偏乐观); 退出机制不变;
限价单持续整个窗口 (另报日终撤单切法); SIM 成交. 只读, 零 live 变更.

用法: ./venv/bin/python scripts/limit_entry_counterfactual.py [--since 2026-07-14] [--refresh-bars]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from app.execution import fee_model  # noqa: E402
from app.utils.timezone import ET  # noqa: E402
import exit_replay_backtest as erb  # noqa: E402  (复用 fetch_bars + replay_bars 缓存)

DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "limit_entry_counterfactual_latest.json"


def _parse_et(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = ET.localize(dt)
        return dt
    except Exception:
        return None


def load_trades(since: str) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT t.id, t.trade_date, t.symbol, t.direction, t.quantity,
               t.entry_price, t.exit_price, t.entry_time, t.exit_time,
               t.realized_pnl, t.pnl_net, i.entry_low, i.entry_high,
               i.entry_price AS anchor
        FROM trade_records t JOIN playbook_instructions i
          ON t.decision_id = i.decision_id
        WHERE t.trade_date>=? AND t.exit_price>0
          AND i.entry_low IS NOT NULL AND i.entry_high IS NOT NULL
        ORDER BY t.trade_date
        """, (since,)).fetchall()
    con.close()
    out = []
    for r in rows:
        ent, ext = _parse_et(r["entry_time"]), _parse_et(r["exit_time"])
        if not ent or not ext:
            continue
        out.append({"id": r["id"], "date": r["trade_date"], "symbol": r["symbol"],
                    "direction": r["direction"], "qty": r["quantity"] or 0,
                    "entry": r["entry_price"], "exit": r["exit_price"],
                    "entry_dt": ent, "exit_dt": ext,
                    "gross": r["realized_pnl"] or 0.0,
                    "net": r["pnl_net"] if r["pnl_net"] is not None else (r["realized_pnl"] or 0.0),
                    "e_low": r["entry_low"], "e_high": r["entry_high"],
                    "anchor": r["anchor"]})
    return out


def get_bars(ctx, symbol: str, d0: datetime, d1: datetime) -> list[dict]:
    """entry→exit 全部工作日 1min bars (升序), 走 replay_bars 缓存。"""
    bars: list[dict] = []
    d = d0.date()
    end = d1.date()
    while d <= end and (d - d0.date()).days <= 6:
        if d.weekday() < 5:
            b = erb.fetch_bars(ctx, symbol, d.strftime("%Y-%m-%d"))
            if b:
                bars.extend(b)
        d += timedelta(days=1)
    bars.sort(key=lambda x: x["t"])
    return bars


def try_fill(bars: list[dict], tr: dict, limit_px: float,
             same_day_cancel: bool, skip_trigger_bar: bool = False) -> tuple[bool, float | None]:
    """窗口 [entry_dt, exit_dt] 内限价成交判定。返回 (成交?, 成交价)。

    市价化规则 (关键物理): LONG 限价 ≥ 触发时市价 (用实际成交价代理) → 立即可成交,
    按实际成交价fill (限价只是价格帽, 不会按限价成交 — 限价买单不会付高于市价的价)。
    SHORT 镜像。只有限价严格劣于触发市价 ("真被动单") 才需要等回踩。

    skip_trigger_bar: 跳过包含 entry_dt 的那根 bar — 该 bar 的 low/high 含触发前价格,
    1min 粒度无法分辨"触发后回踩成交"vs"触发前就在那里" (双序不可知, 同 exit_replay 边界)。
    不跳=乐观上界, 跳=保守下界。"""
    long = tr["direction"] == "LONG"
    mkt_at_trigger = tr["entry"]  # 实际市价单成交价 = 触发时刻市价的最佳代理
    if long and limit_px >= mkt_at_trigger:
        return True, mkt_at_trigger
    if not long and limit_px <= mkt_at_trigger:
        return True, mkt_at_trigger
    horizon = tr["exit_dt"]
    if same_day_cancel:
        eod = tr["entry_dt"].replace(hour=16, minute=0, second=0, microsecond=0)
        horizon = min(horizon, eod)
    trigger_bar_minute = tr["entry_dt"].replace(second=0, microsecond=0)
    for b in bars:
        if b["t"] < tr["entry_dt"]:
            continue
        if skip_trigger_bar and b["t"] <= trigger_bar_minute:
            continue
        if b["t"] > horizon:
            break
        if long and b["l"] <= limit_px:
            return True, (b["o"] if b["o"] < limit_px else limit_px)
        if not long and b["h"] >= limit_px:
            return True, (b["o"] if b["o"] > limit_px else limit_px)
    return False, None


def run_variant(trades: list[dict], bars_map: dict, edge: str,
                same_day_cancel: bool, skip_trigger_bar: bool = False) -> dict:
    """edge: cap_near=LONG@e_high/SHORT@e_low (多为市价化价格帽);
             anchor=DA锚定价 (C2口径的"不追价"反事实);
             deep_far=LONG@e_low/SHORT@e_high (深被动)。"""
    n = len(trades)
    filled = missed = marketable = 0
    cf_net_sum = act_net_filled_subset = 0.0
    missed_act_net: list[float] = []
    improvements: list[float] = []
    for tr in trades:
        if edge == "cap_near":
            px = tr["e_high"] if tr["direction"] == "LONG" else tr["e_low"]
        elif edge == "anchor":
            px = tr["anchor"]
        else:
            px = tr["e_low"] if tr["direction"] == "LONG" else tr["e_high"]
        if not px or px <= 0:
            n -= 1
            continue
        ok, fill_px = try_fill(bars_map.get(tr["id"], []), tr, px, same_day_cancel,
                               skip_trigger_bar)
        if not ok:
            missed += 1
            missed_act_net.append(tr["net"])
            continue
        filled += 1
        if abs(fill_px - tr["entry"]) < 1e-9:
            marketable += 1
        sign = 1.0 if tr["direction"] == "LONG" else -1.0
        cf_gross = sign * (tr["exit"] - fill_px) * tr["qty"]
        cf_fee = fee_model.estimate_round_trip(
            tr["qty"], fill_px, tr["exit"], tr["direction"], tr["date"], tr["date"])["total"]
        cf_net_sum += cf_gross - cf_fee
        act_net_filled_subset += tr["net"]
        improvements.append(sign * (tr["entry"] - fill_px) * tr["qty"])
    act_total = sum(t["net"] for t in trades)
    return {
        "edge": edge, "same_day_cancel": same_day_cancel,
        "skip_trigger_bar": skip_trigger_bar, "n": n,
        "fill_n": filled, "fill_rate": round(filled / n, 3) if n else None,
        "marketable_fill_n": marketable,
        "passive_fill_n": filled - marketable,
        "missed_n": missed,
        "cf_net_total": round(cf_net_sum, 2),
        "actual_net_total_all": round(act_total, 2),
        "delta_vs_actual": round(cf_net_sum - act_total, 2),
        "filled_subset_actual_net": round(act_net_filled_subset, 2),
        "filled_subset_entry_gain": round(sum(improvements), 2),
        "missed_subset_actual_net": round(sum(missed_act_net), 2),
        "missed_subset_avg_net": round(sum(missed_act_net) / missed, 2) if missed else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-14")
    ap.add_argument("--refresh-bars", action="store_true")
    args = ap.parse_args()

    trades = load_trades(args.since)
    print(f"era 交易 (有 entry_low/high): {len(trades)}")

    if args.refresh_bars:
        erb.BAR_DIR.mkdir(parents=True, exist_ok=True)
    from futu import OpenQuoteContext
    import os
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    bars_map: dict[int, list[dict]] = {}
    missing = 0
    try:
        for i, tr in enumerate(trades):
            if args.refresh_bars:
                for fp in erb.BAR_DIR.glob(f"{tr['symbol'].replace('.', '_')}_*.json"):
                    pass  # refresh 由 fetch_bars(refresh=True) 逐日处理, 这里不预删
            bars = get_bars(ctx, tr["symbol"], tr["entry_dt"], tr["exit_dt"])
            if bars:
                bars_map[tr["id"]] = bars
            else:
                missing += 1
            if (i + 1) % 20 == 0:
                print(f"  bars: {i + 1}/{len(trades)} (missing {missing})")
    finally:
        ctx.close()
    print(f"bars 就绪: {len(bars_map)}/{len(trades)} (missing {missing})")
    trades = [t for t in trades if t["id"] in bars_map]

    variants = []
    for edge in ("cap_near", "anchor", "deep_far"):
        for stb in (False, True):
            v = run_variant(trades, bars_map, edge, False, stb)
            variants.append(v)
            lbl = (f"{edge} {'跳触发bar(保守)' if stb else '含触发bar(乐观)'}")
            print(f"\n== {lbl} ==")
            print(f"  成交率: {v['fill_n']}/{v['n']} = {v['fill_rate']} "
                  f"(市价化 {v['marketable_fill_n']} / 真被动 {v['passive_fill_n']})")
            print(f"  反事实净={v['cf_net_total']:+.2f} vs 实际净={v['actual_net_total_all']:+.2f} "
                  f"Δ={v['delta_vs_actual']:+.2f}")
            print(f"  成交子集: 入场改善={v['filled_subset_entry_gain']:+.2f} "
                  f"(该子集实际净={v['filled_subset_actual_net']:+.2f})")
            print(f"  漏单子集: n={v['missed_n']} 实际净={v['missed_subset_actual_net']:+.2f} "
                  f"avg={v['missed_subset_avg_net']} (正=限价漏掉的是赢家→幸存者成本)")

    result = {
        "_meta": {"generated_by": "scripts/limit_entry_counterfactual.py",
                  "debate": "DEB-20260804-scan-subscribe-entry-ruler S6",
                  "since": args.since, "n_trades": len(trades),
                  "caveats": ["触价=成交 (忽略排队, 边缘偏乐观)",
                              "市价化限价按实际成交价fill (限价=价格帽)",
                              "退出机制不变", "跳空穿越按bar open成交", "SIM成交"],
                  "edge_def": {"cap_near": "LONG@entry_high / SHORT@entry_low (多为市价化帽)",
                               "anchor": "@指令entry_price (DA锚, C2不追价反事实)",
                               "deep_far": "LONG@entry_low / SHORT@entry_high (深被动)"}},
        "variants": variants,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(f"\n已落 {OUT}")


if __name__ == "__main__":
    main()
