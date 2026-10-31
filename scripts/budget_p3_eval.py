#!/usr/bin/env python3
"""P3 (DEB-20261031-budget-design §8) 出口结算器: 三道预算帽生效后的接线与经济读数。

预注册判据 (读数前写死, 见 DEB §8):
  生效门  E1 日均成交笔数 ≥3;  E2 在险峰值 ≤2.5% 权益;  E3 热帽拒绝占比 ≤60%
  经济门  J1 笔均净额 > 基线 −$1.84;  J2 费用率 ≤ 基线 11.54bps;  J3 名义中位 ≥ $5,500
  回滚    R1 连续 10 个交易日 日均笔数 <3;  R2 笔均 < −$1.84;  R3 热帽拒绝占比 >60%
          R4 在险峰值 >2.5% (帽失效 — 代码/接线问题, 立刻回滚)

只读: 不写库、不改生产。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data/ftseek_us.db"
if str(REPO) not in sys.path:          # 直接 `python scripts/xxx.py` 时 cwd 不一定在 path 上
    sys.path.insert(0, str(REPO))
P3_FROM = "2026-10-31"
BASE_FROM, BASE_TO = "2026-07-01", "2026-10-31"
BASE_PER_TRADE, BASE_FEE_BPS = -1.84, 11.54
# ⚠️ 这三个值必须从线上配置读, 不许写死 —— 2026-10-31 自查: 脚本曾写死 2.5%, 而线上已改 4.0%,
#    报告会打印「帽 2.5%」并拿错阈值算 R4 回滚判据 (自相矛盾的报表层, 与 11/01 清账同一类病灶)。
def _live_thresholds():
    try:
        from app.playbook.admission import get_admission_config
        c = get_admission_config(refresh=True)
        return (float(c.get("max_open_risk_pct") or 0.0),
                float(c.get("min_notional_usd") or 0.0),
                float(c.get("max_sl_pct") or 0.0))
    except Exception:
        return (0.0, 0.0, 0.0)


HEAT_CAP_PCT, MIN_NOTIONAL, SL_MAX_PCT = _live_thresholds()
MIN_TRADES_PER_DAY, MAX_HEAT_REJECT_PCT = 3.0, 60.0


def _trades(conn, start, end):
    return conn.execute("""
        SELECT trade_date, entry_price, quantity, pnl_net, metadata_json
        FROM trade_records
        WHERE COALESCE(exit_price,0)>0 AND trade_date>=? AND trade_date<=?
          AND COALESCE(json_extract(metadata_json,'$.gate_mode'),'')!='research_momentum'
    """, (start, end)).fetchall()


def _stats(rows):
    if not rows:
        return {"n": 0}
    net, fee, nom, fees_bps = 0.0, 0.0, [], []
    for r in rows:
        md = json.loads(r["metadata_json"] or "{}")
        f = float((md.get("fee_breakdown") or {}).get("total") or 4.18)
        n = float(r["entry_price"] or 0) * float(r["quantity"] or 0)
        net += r["pnl_net"] or 0
        fee += f
        if n > 0:
            nom.append(n)
            fees_bps.append(f / n * 10000)
    days = len({r["trade_date"] for r in rows})
    return {"n": len(rows), "net": net, "avg": net / len(rows), "days": days,
            "per_day": len(rows) / max(days, 1), "fee_bps": fee / sum(nom) * 10000 if nom else None,
            "notional_median": statistics.median(nom) if nom else None}


def _gate_hits(conn, start):
    """admission 拒绝原因分布 (从 skip_reason 直读, 不猜)。"""
    rows = conn.execute("""
        SELECT skip_reason, COUNT(*) c FROM playbook_instructions
        WHERE date>=? AND skip_reason LIKE 'admission:%' GROUP BY skip_reason
    """, (start,)).fetchall()
    agg = Counter()
    for r in rows:
        why = str(r["skip_reason"] or "")
        for key in ("PORTFOLIO_HEAT", "NOTIONAL_TOO_SMALL", "SL_STOP_TOO_WIDE",
                    "FEE_INEFFICIENT", "PRICE_TOO_LOW", "SHARES_ABOVE_FEE_KNEE"):
            if key in why:
                agg[key] += r["c"]
    return agg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    base = _stats(_trades(conn, BASE_FROM, BASE_TO))
    live = _stats(_trades(conn, P3_FROM, "2099-12-31"))
    hits = _gate_hits(conn, P3_FROM)
    # 在险峰值: 逐日按 open_positions 的 |入场−止损|×股数 加总不可回溯 (表只存当前),
    # 用 trade_records 里当日"在仓"近似不可行 ⇒ 只报当前快照 + 说明。
    try:
        from app.playbook.portfolio_heat import open_risk_usd
        cur_risk, cur_n = open_risk_usd(use_cache=False)
        equity = None
        try:
            from app.risk.capital_safety import get_capital_safety_system
            cs = get_capital_safety_system()
            equity = float(cs._state.current_equity) if cs._state and cs._state.current_equity else None
        except Exception:
            equity = None
    except Exception:
        cur_risk, cur_n, equity = 0.0, 0, None

    heat_rejects = hits.get("PORTFOLIO_HEAT", 0)
    total_rejects = sum(hits.values())
    gates: dict = {}
    if live.get("n", 0) == 0:
        gates["state"] = "NO_LIVE_SAMPLE (P3 生效后无已平仓样本)"
    else:
        gates["E1"] = "PASS" if live["per_day"] >= MIN_TRADES_PER_DAY else "FAIL"
        gates["E3"] = ("PASS" if total_rejects == 0
                       else ("PASS" if heat_rejects / total_rejects * 100 <= MAX_HEAT_REJECT_PCT
                             else "FAIL"))
        gates["J1"] = "PASS" if live["avg"] > BASE_PER_TRADE else "FAIL"
        gates["J2"] = "PASS" if (live["fee_bps"] or 999) <= BASE_FEE_BPS else "FAIL"
        gates["J3"] = "PASS" if (live["notional_median"] or 0) >= MIN_NOTIONAL else "FAIL"
        rollback = []
        if live["per_day"] < MIN_TRADES_PER_DAY:
            rollback.append("R1 日均笔数<3")
        if live["avg"] < BASE_PER_TRADE:
            rollback.append("R2 笔均低于基线")
        if total_rejects and heat_rejects / total_rejects * 100 > MAX_HEAT_REJECT_PCT:
            rollback.append("R3 热帽拒绝>60%")
        if equity and cur_risk > equity * HEAT_CAP_PCT / 100 * 1.02:
            rollback.append("R4 在险超帽(接线故障)")
        gates["rollback"] = rollback or "none"
        gates["state"] = "READY_TO_ADJUDICATE" if (live["n"] >= 60 or live["days"] >= 10) else "KEEP_SAMPLING"

    out = {"baseline": base, "live": live, "gate_hits": dict(hits), "gates": gates,
           "open_risk_now": {"usd": cur_risk, "positions": cur_n, "equity": equity,
                             "pct": (cur_risk / equity * 100) if equity else None},
           "p3_from": P3_FROM,
           "thresholds": {"min_trades_per_day": MIN_TRADES_PER_DAY,
                          "heat_cap_pct": HEAT_CAP_PCT, "min_notional": MIN_NOTIONAL,
                          "sl_max_pct": SL_MAX_PCT,
                          "base_per_trade": BASE_PER_TRADE, "base_fee_bps": BASE_FEE_BPS}}
    try:
        path = REPO / "data/shadow/budget_p3_latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    except OSError as exc:
        print(f"[warn] 写产物失败: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print("== P3 预算帽结算器 (DEB-20261031-budget-design) ==")
    print(f"基线 {BASE_FROM}~{BASE_TO}: n={base.get('n')} 笔均 {base.get('avg', 0):+.2f} "
          f"费用 {base.get('fee_bps') or 0:.2f}bps 名义中位 ${base.get('notional_median') or 0:,.0f}")
    if live.get("n"):
        print(f"P3 {P3_FROM}~今: n={live['n']} 笔均 {live['avg']:+.2f} 日均 {live['per_day']:.1f} 笔 "
              f"费用 {live['fee_bps'] or 0:.2f}bps 名义中位 ${live['notional_median'] or 0:,.0f}")
    else:
        print(f"P3 {P3_FROM}~今: 无已平仓样本")
    print(f"阈值(线上实读): 热帽 {HEAT_CAP_PCT}% / 名义下限 ${MIN_NOTIONAL:,.0f} / 止损上限 {SL_MAX_PCT}%")
    print(f"当前在险 ${cur_risk:,.0f} ({cur_n} 笔)"
          + (f" = {cur_risk / equity * 100:.2f}% 权益 (帽 {HEAT_CAP_PCT}%)" if equity else ""))
    print("门禁拒绝分布:", dict(hits) or "无")
    print("判据:", " ".join(f"{k}={v}" for k, v in gates.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())