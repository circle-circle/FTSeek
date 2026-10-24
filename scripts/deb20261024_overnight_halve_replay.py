"""隔夜减半 replay (判据 C14 的复现脚本, 只读)。

结论用途: 证伪「在 EOD 时点把豁免持夜仓的名义减半」作为隔夜跳空尾部的应对手段。

口径 (逐字):
  人群   = `trade_records` 中 `metadata_json.overnight_arm` 存在 (被 EOD 豁免判定授予过夜)
           的**已平仓**成交, 现役时代口径 `app/core/db_utils.ERA_CLEAN_PERFORMANCE_WHERE`
           (trade_date>=2026-07-01 ∧ data_quality IN (clean,real) ∧ 剔 POSITION_SYNC/MANUAL)。
  动作   = 入场日 EOD 决策前 (默认 15:52 ET, 网格 15:45/15:52/15:57) 市价卖出**一半数量**,
           余下一半按真实路径走完 (`exit_price` / `exit_reason` 不变)。
  两臂   = 实际 `pnl_net`  vs  减半 `0.5*毛@P_half + 0.5*毛@exit − fees_est − 额外半腿费`。
  价格   = `data/replay_bars/US_<SYM>_<entry_date>.json` 的 1m bar **收盘价** (该分钟缺则 ±1min)。
  额外费 = `app.execution.fee_model.estimate_fee(Q/2, P_half, 平仓方向)` — 半仓多出的一条平仓腿。

时点依据 (为什么是 15:52 而不是 16:00):
  EOD 兜底 sweeper 实测在 ET 16:00–16:05 才跑到豁免判定 (生产日志豁免行 = CST 04:00–04:05),
  而 `overnight_cleanup.eod_close_verdict()` 只在 TRADING 相位或收盘+10min 内放行平仓腿;
  另 2026-10-21 TSLA P0 实证盘后下 RTH 市价腿会被券商 CANCELLED_ALL 卡死意图。
  ⇒ RTH 内唯一可执行的减半点 = PRE_CLOSE 规则[3] 的 15:52 (与 sweeper 用同一个
  `_loser_overnight_exempt` 判据, 判据不漂移)。

只读承诺: sqlite `mode=ro`; 只读 replay_bars; 不写库、不下单、不碰任何交易路径。

用法:
    PYTHONPATH=. venv/bin/python scripts/deb20261024_overnight_halve_replay.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.db_utils import ERA_CLEAN_PERFORMANCE_WHERE  # noqa: E402
from app.execution.fee_model import estimate_fee  # noqa: E402

DB = os.path.join(ROOT, "data", "ftseek_us.db")
BARS = os.path.join(ROOT, "data", "replay_bars")
GRID = ("15:45", "15:52", "15:57")
GAP_REASON = "OVERNIGHT_GAP_STOP"


def _meta(r):
    try:
        return json.loads(r["metadata_json"] or "{}")
    except Exception:
        return {}


def _bars(sym, date):
    p = os.path.join(BARS, "%s_%s.json" % (sym.replace(".", "_"), date))
    if not os.path.exists(p):
        return None
    try:
        return {b[0][11:16]: b for b in json.load(open(p))}
    except Exception:
        return None


def _px(bars, hhmm):
    h, m = int(hhmm[:2]), int(hhmm[3:])
    for d in (0, 1, -1):
        mm = m + d
        key = "%02d:%02d" % (h + (1 if mm > 59 else 0), mm % 60 if mm >= 0 else 59)
        if key in bars:
            return bars[key][4], key
    return None, None


def main():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trade_records WHERE exit_time IS NOT NULL AND pnl_net IS NOT NULL "
        + ERA_CLEAN_PERFORMANCE_WHERE + " ORDER BY trade_date")]

    pop, missing = [], []
    for r in rows:
        arm = _meta(r).get("overnight_arm")
        if not arm:
            continue
        date = str(r["entry_time"])[:10]
        bars = _bars(r["symbol"], date)
        if not bars:
            missing.append((r["symbol"], date))
            continue
        pop.append((r, arm, date, bars))

    print("人群: 带 overnight_arm 标记的已平仓成交 n=%d  实际净 $%.2f"
          % (len(pop), sum(r["pnl_net"] for r, _a, _d, _b in pop)))
    print("bar 缺失 %d 笔 %s" % (len(missing), missing[:8]))

    for g in GRID:
        recs = []
        for r, arm, date, bars in pop:
            p, used = _px(bars, g)
            if p is None:
                continue
            sign = 1 if r["direction"] == "LONG" else -1
            q, e, x = float(r["quantity"]), float(r["entry_price"]), float(r["exit_price"] or 0)
            gross = sign * (p - e) * (q / 2) + sign * (x - e) * (q / 2)
            extra = estimate_fee(q / 2, p, "SELL" if sign == 1 else "BUY",
                                 trade_date=date)["total"]
            recs.append(dict(sym=r["symbol"], date=date, dirn=r["direction"],
                             reason=r["exit_reason"], actual=r["pnl_net"],
                             halved=gross - (r["fees_est"] or 0) - extra,
                             under=arm.get("underwater_pct_net"), used=used))
        a = sum(x["actual"] for x in recs)
        h = sum(x["halved"] for x in recs)
        print("\n[%s ET] n=%d  实际 $%.2f  减半 $%.2f  Δ $%+.2f  (减半更优 %d/%d)"
              % (g, len(recs), a, h, h - a, sum(1 for x in recs if x["halved"] > x["actual"]), len(recs)))
        bym = {}
        for x in recs:
            bym.setdefault(x["date"][:7], []).append(x)
        for m in sorted(bym):
            v = bym[m]
            print("    %s n=%2d 实际%8.2f 减半%8.2f Δ%+8.2f"
                  % (m, len(v), sum(y["actual"] for y in v), sum(y["halved"] for y in v),
                     sum(y["halved"] - y["actual"] for y in v)))
        if g != "15:52":
            continue

        gap = [x for x in recs if x["reason"] == GAP_REASON]
        non = [x for x in recs if x["reason"] != GAP_REASON]
        print("    GAP族   n=%2d 实际%8.2f 减半%8.2f Δ%+8.2f"
              % (len(gap), sum(x["actual"] for x in gap), sum(x["halved"] for x in gap),
                 sum(x["halved"] - x["actual"] for x in gap)))
        print("    非GAP   n=%2d 实际%8.2f 减半%8.2f Δ%+8.2f"
              % (len(non), sum(x["actual"] for x in non), sum(x["halved"] for x in non),
                 sum(x["halved"] - x["actual"] for x in non)))
        print("    额外半腿费合计 $%.2f | Δ 中位 $%.2f"
              % (sum(estimate_fee(float(r["quantity"]) / 2, _px(b, g)[0],
                                  "SELL" if r["direction"] == "LONG" else "BUY",
                                  trade_date=date)["total"]
                    for r, _a, date, b in pop if _px(b, g)[0] is not None),
                 st.median([x["halved"] - x["actual"] for x in recs])))

        print("\n    变体扫描 (探索性, 禁作裁决依据):")
        for label, sel in (
            ("只减 LONG", lambda x: x["dirn"] == "LONG"),
            ("只减 SHORT", lambda x: x["dirn"] == "SHORT"),
            ("只减 水深<=-2%", lambda x: (x["under"] or 0) <= -2.0),
            ("只减 水深<=-3%", lambda x: (x["under"] or 0) <= -3.0),
            ("只减 LONG 且水深<=-2%", lambda x: x["dirn"] == "LONG" and (x["under"] or 0) <= -2.0),
        ):
            v = [x for x in recs if sel(x)]
            if not v:
                continue
            print("      %-22s n=%2d Δ $%+8.2f  更优 %d/%d"
                  % (label, len(v), sum(x["halved"] - x["actual"] for x in v),
                     sum(1 for x in v if x["halved"] > x["actual"]), len(v)))


if __name__ == "__main__":
    main()