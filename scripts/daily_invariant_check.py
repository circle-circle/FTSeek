#!/usr/bin/env python3
"""每日测量不变量自检 (2026-07-28, 用户拷问"为什么修尺子问题都是我发现"的制度性回答)。

原理: 本周修掉的每个尺子问题事后看都违反某个跨字段物理不变量 (盈亏≠价差 /
bar峰<成交价 / 负MFE / 时间乱序 / 审计桶加总≠skips), 但此前无人每天检查 —
用户看"钱对不对"这个终极不变量所以总是先发现。本脚本把人肉对账变成机器每晚对账。

检查面 (近 7 天已平仓, 对齐判分窗):
  I1 realized_pnl ≈ (exit-entry)*qty*sign     I5 mfe/mae ≥ 0
  I2 pnl_pct 符号 = pnl 符号                   I6 bar 峰谷物理性 (LONG bar_peak ≥ 0.995*max(e,x))
  I3 peak/trough 最有利/最不利语义             I7 exit_time > entry_time
  I4 mfe 与 peak 重算一致 (±0.05pp)            F  字段健康度 (关键测量字段非空率骤降/常量化)

输出: 违反数 >0 → logger.error (被 error_alert.sh 15min 扫描拾取推钉钉) + exit 1;
     全过 → 一行 OK + exit 0。结果落 data/invariant_check_latest.json。
cron: 每交易日盘后 (见 crontab '05 5 * * 2-6')。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.utils.timezone import now_et  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("invariant_check")

DB = REPO / "data" / "ftseek_us.db"
OUT = REPO / "data" / "invariant_check_latest.json"


def main() -> int:
    since = (now_et().date() - timedelta(days=7)).isoformat()
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM trade_records WHERE trade_date>=? AND source_type LIKE 'PLAYBOOK%' "
        "AND exit_time IS NOT NULL", (since,)).fetchall()
    c.close()

    viol: Counter = Counter()
    examples: dict = {}

    def rec(k: str, rid, sym, msg: str):
        viol[k] += 1
        examples.setdefault(k, f"id={rid} {sym} {msg}")

    for r in rows:
        m = json.loads(r["metadata_json"] or "{}")
        e = float(r["entry_price"] or 0)
        x = float(r["exit_price"] or 0)
        q = float(r["quantity"] or 0)
        pnl = float(r["realized_pnl"] or 0)
        pct, mfe, mae = r["pnl_pct"], r["mfe_pct"], r["mae_pct"]
        pk, tr = m.get("peak_price"), m.get("trough_price")
        short = str(r["direction"]).upper() in ("SHORT", "SELL_SHORT")
        sign = -1 if short else 1
        sym = str(r["symbol"])[3:]
        if e > 0 and x > 0 and q > 0 and abs(pnl - (x - e) * q * sign) > max(0.02, abs(pnl) * 0.02):
            rec("I1_pnl_vs_prices", r["id"], sym, f"pnl={pnl} calc={(x-e)*q*sign:.2f}")
        if pct is not None and pnl != 0 and (float(pct) > 0) != (pnl > 0):
            rec("I2_pct_sign", r["id"], sym, f"pnl={pnl} pct={pct}")
        if pk:
            pkf = float(pk)
            if not short and pkf + 1e-6 < max(e, x):
                rec("I3_peak_semantics", r["id"], sym, f"peak={pkf} e={e} x={x}")
            if short and pkf - 1e-6 > min(e, x):
                rec("I3_peak_semantics", r["id"], sym, f"SHORT peak={pkf} e={e} x={x}")
        if pk and e > 0 and mfe is not None:
            calc = max(0.0, (float(pk) - e) * sign / e * 100)
            if abs(calc - float(mfe)) > 0.05:
                rec("I4_mfe_recalc", r["id"], sym, f"mfe={mfe} recalc={calc:.3f}")
        if (mfe is not None and float(mfe) < -1e-6) or (mae is not None and float(mae) < -1e-6):
            rec("I5_neg_mfe_mae", r["id"], sym, f"mfe={mfe} mae={mae}")
        pkb = m.get("peak_price_bar")
        if pkb and not short and float(pkb) < max(e, x) * 0.995:
            rec("I6_bar_peak_impossible", r["id"], sym, f"bar_peak={pkb} e={e} x={x}")
        if r["entry_time"] and r["exit_time"] and str(r["exit_time"]) <= str(r["entry_time"]):
            rec("I7_time_order", r["id"], sym, f"{r['entry_time']} → {r['exit_time']}")

    # F: 字段健康度 — 非空率骤降即测量链断线 (常量退化族预警)
    field_health = {}
    if rows:
        checks = {
            "slippage_bps": lambda r, m: (m.get("execution") or {}).get("slippage_bps"),
            "exit_telemetry": lambda r, m: m.get("exit_telemetry"),
            "entry_price_source": lambda r, m: m.get("entry_price_source"),
            "attribution": lambda r, m: m.get("attribution"),  # 2026-07-28 上线, 逐步爬坡
        }
        for name, fn in checks.items():
            nn = sum(1 for r in rows if fn(r, json.loads(r["metadata_json"] or "{}")) not in (None, "", {}))
            field_health[name] = f"{nn}/{len(rows)}"
            # attribution 是新字段, 首周不告警; 其余跌破 50% 告警
            if name != "attribution" and len(rows) >= 10 and nn < len(rows) * 0.5:
                rec("F_field_dropout", "-", name, f"非空率 {nn}/{len(rows)}")

    result = {
        "checked_at": now_et().isoformat(), "window_since": since, "sample": len(rows),
        "violations": dict(viol), "examples": examples, "field_health": field_health,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1))

    if viol:
        logger.error(f"[不变量自检] {sum(viol.values())} 处违反: "
                     + "; ".join(f"{k}×{n} (例 {examples[k]})" for k, n in viol.most_common()))
        return 1
    logger.info(f"[不变量自检] 全过 ✓ (样本 {len(rows)} 笔, 字段健康 {field_health})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
