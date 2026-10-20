#!/usr/bin/env python3
"""RFC-155 P2 左尾风险尺寸分层影子（只读反事实）.

对已平 PLAYBOOK_SIM 交易按规则缩放仓位, 比较美元净额/日级回撤/左尾净额:
  R1 atr5      ATR>=5% -> 0.5x (主)
  R2 open15    开盘 0-15min -> 0.5x
  R3 ue        universe_expansion -> 0.5x
  R4 ge2       上述 >=2 旗标 -> 0.5x
  R5 any       任一旗标 -> 0.5x
  R6 product   每旗标 0.5x, 下限 0.25x

只读 DB(mode=ro); 不写交易表/配置; 不触 OpenD.
费用按实际净额线性缩放 (固定佣金非线性已披露). 这是去杠杆, 不改单笔 bps/alpha.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.playbook.risk_size_shadow import RULES, STOP_LIKE, flags_for, multiplier

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "shadow" / "risk_size_tier_latest.json"

logger = logging.getLogger(__name__)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _source(decision_id: str) -> str:
    value = str(decision_id or "")
    if value.startswith("universe_expansion"):
        return "UE"
    if value.startswith("EXT-"):
        return "EXT"
    if value.startswith("DA-"):
        return "DA"
    return "OTHER"


def max_drawdown(daily_net: dict[str, float]) -> float:
    """日级累计净额的最大回撤 (正数, 美元)."""
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for day in sorted(daily_net):
        equity += daily_net[day]
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 2)


def _rr(wins: list[float], losses: list[float]) -> dict:
    avg_win = statistics.mean(wins) if wins else None
    avg_loss = statistics.mean(losses) if losses else None
    return {
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
        "rr": (round(avg_win / abs(avg_loss), 2)
               if avg_win is not None and avg_loss not in (None, 0) else None),
    }


def metrics(rows: list[dict], rule: str) -> dict:
    actual_daily: dict[str, float] = defaultdict(float)
    cf_daily: dict[str, float] = defaultdict(float)
    actual_net = cf_net = actual_notional = cf_notional = 0.0
    actual_wins, actual_losses, cf_wins, cf_losses = [], [], [], []
    left_actual = left_cf = 0.0
    affected = 0
    affected_actual = 0.0
    loss_avoided = upside_given_up = 0.0
    for row in rows:
        flags = flags_for(row)
        mult = multiplier(rule, flags)
        pnl = row["pnl"]
        notional = row["notional"]
        cf_pnl = pnl * mult
        cf_notional_trade = notional * mult
        actual_net += pnl
        cf_net += cf_pnl
        actual_notional += notional
        cf_notional += cf_notional_trade
        actual_daily[row["trade_date"]] += pnl
        cf_daily[row["trade_date"]] += cf_pnl
        (actual_wins if pnl > 0 else actual_losses).append(pnl)
        (cf_wins if cf_pnl > 0 else cf_losses).append(cf_pnl)
        if row["left_tail"]:
            left_actual += pnl
            left_cf += cf_pnl
        if mult < 1.0:
            affected += 1
            affected_actual += pnl
            if pnl > 0:
                upside_given_up += pnl * (1.0 - mult)
            elif pnl < 0:
                loss_avoided += -pnl * (1.0 - mult)
    return {
        "rule": rule,
        "affected_n": affected,
        "affected_pct": round(100.0 * affected / len(rows), 1) if rows else None,
        "affected_actual_net": round(affected_actual, 2),
        "actual_net": round(actual_net, 2),
        "cf_net": round(cf_net, 2),
        "delta_net": round(cf_net - actual_net, 2),
        "loss_avoided": round(loss_avoided, 2),
        "upside_given_up": round(upside_given_up, 2),
        "actual_net_bps": (round(actual_net / actual_notional * 10000.0, 2)
                           if actual_notional else None),
        "cf_net_bps": (round(cf_net / cf_notional * 10000.0, 2)
                       if cf_notional else None),
        "actual_max_drawdown": max_drawdown(actual_daily),
        "cf_max_drawdown": max_drawdown(cf_daily),
        "left_tail_actual_net": round(left_actual, 2),
        "left_tail_cf_net": round(left_cf, 2),
        "win_rate_pct": (round(100.0 * len(actual_wins) / len(rows), 1) if rows else None),
        "actual_rr": _rr(actual_wins, actual_losses),
        "cf_rr": _rr(cf_wins, cf_losses),
    }


def _half_split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    days = sorted({row["trade_date"] for row in rows})
    if len(days) < 2:
        return rows, []
    mid = len(days) // 2
    first_days = set(days[:mid])
    return ([r for r in rows if r["trade_date"] in first_days],
            [r for r in rows if r["trade_date"] not in first_days])


def load_rows(db_path: Path, days: int) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        raw_rows = con.execute(
            "SELECT id,symbol,direction,entry_time,entry_price,quantity,pnl_net,"
            "exit_reason,trade_date,decision_id,metadata_json FROM trade_records "
            "WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL AND exit_time<>'' "
            "AND trade_date >= date('now', ?) ORDER BY trade_date,id",
            (f"-{days} day",),
        ).fetchall()
    finally:
        con.close()
    rows = []
    for r in raw_rows:
        metadata = {}
        try:
            metadata = json.loads(r["metadata_json"] or "{}")
        except Exception as error:
            logger.warning("metadata 坏行 trade=%s: %s", r["id"], error, exc_info=True)
        entry = _num(r["entry_price"])
        qty = _num(r["quantity"])
        atr = metadata.get("atr")
        atr_pct = (_num(atr) / entry * 100.0) if entry and atr else None
        try:
            entry_dt = datetime.fromisoformat(str(r["entry_time"]))
            session_min = entry_dt.hour * 60 + entry_dt.minute - 570
        except Exception:
            session_min = -1
        rows.append({
            "id": r["id"], "symbol": r["symbol"], "direction": r["direction"],
            "trade_date": str(r["trade_date"] or ""), "source": _source(r["decision_id"]),
            "atr_pct": atr_pct, "session_min": session_min,
            "pnl": _num(r["pnl_net"]), "notional": entry * qty,
            "left_tail": str(r["exit_reason"] or "") in STOP_LIKE,
        })
    return rows


def compute(rows: list[dict]) -> dict:
    full = {rule: metrics(rows, rule) for rule in RULES}
    first, second = _half_split(rows)
    halves = {
        "first_half": {rule: metrics(first, rule)["delta_net"] for rule in RULES},
        "second_half": {rule: metrics(second, rule)["delta_net"] for rule in RULES},
    }
    days = sorted({row["trade_date"] for row in rows})
    return {
        "schema": "risk_size_tier_shadow_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sample": {"n": len(rows), "distinct_days": len(days),
                   "first_day": days[0] if days else None,
                   "last_day": days[-1] if days else None,
                   "atr_missing_n": sum(1 for r in rows if r["atr_pct"] is None)},
        "rules": full,
        "halves": halves,
        "caveats": [
            "只读历史反事实; 费用按实际净额线性缩放, 固定佣金非线性未建模。",
            "尺寸缩放不改变单笔 bps/胜率; 这是去杠杆, 不宣称 alpha 改善。",
            "前后半窗拆分仅作稳定性检查; 转生产须前瞻影子 + RFC + 用户批准。",
            "ATR 缺失行不触发 atr5/ge2/any/product 的 ATR 旗标 (保守)。",
        ],
    }


def render(report: dict) -> None:
    sample = report["sample"]
    print("=" * 78)
    print("RFC-155 P2 风险尺寸分层影子 (只读反事实)")
    print(f"样本 n={sample['n']} days={sample['distinct_days']} "
          f"ATR缺失={sample['atr_missing_n']}")
    print(f"{'rule':10s} {'aff':>4s} {'actual':>9s} {'cf':>9s} {'delta':>9s} "
          f"{'save':>8s} {'give':>8s} {'DD_a':>7s} {'DD_cf':>7s}")
    for rule, block in report["rules"].items():
        print(f"{rule:10s} {block['affected_n']:4d} {block['actual_net']:9.2f} "
              f"{block['cf_net']:9.2f} {block['delta_net']:9.2f} "
              f"{block['loss_avoided']:8.2f} {block['upside_given_up']:8.2f} "
              f"{block['actual_max_drawdown']:7.2f} {block['cf_max_drawdown']:7.2f}")
    print("半窗 delta:", report["halves"])
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    rows = load_rows(Path(args.db), args.days)
    report = compute(rows)
    report["window_days"] = args.days
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    render(report)
    print(f"已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
