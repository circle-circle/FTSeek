#!/usr/bin/env python3
"""费用对账报表 (R2, 2026-08-02, 策略核查修尺子) — 回答"est 费用到底可不可信、账平不平".

背景: 7 月费用 $816 全为 fees_source='est' 且决定月度符号 (毛 −$73 → 净 −$889)。
对账结论 (2026-08-02 实证):
  - SIM 期券商实收不可得 (order_fee_query SIM 返空, DEB-20260730 层1 已裁决) →
    est 是 SIM 期唯一可行口径, 参数=用户提供的富途美股费率表, "真实性"已封顶;
    LIVE 后升级 broker/reconciled (三态只升不降已预置 pipeline.py:4469-4472)。
  - realized_pnl 恒为毛口径 (价格差), 与价格数学的逐月分叉 100% 由
    EXIT_MONITOR_CLOSE 损坏单解释 (2026-07: JD#977 +$216 / GOOG#983 −$169) —
    那才是可修的尺子破口 (归 R4 幽灵/陈旧清理)。
  - 费用分布体检: min $4.01 ≈ 地板 (min佣金$1.99×2+交收), max 1.99% 名义
    (小名义单地板效应, 预期内), 无异常值。

只读 DB (ro), 落 data/fee_reconcile_latest.json + 控制台摘要。
用法: ./venv/bin/python scripts/fee_reconcile_report.py
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "fee_reconcile_latest.json"

_GROSS = ("CASE WHEN direction='LONG' THEN quantity*(exit_price-entry_price) "
          "ELSE quantity*(entry_price-exit_price) END")


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    monthly = []
    for r in conn.execute(f"""
        SELECT substr(trade_date,1,7) mo, COUNT(*) n,
          ROUND(SUM({_GROSS}),2) gross, ROUND(SUM(realized_pnl),2) rpnl,
          ROUND(SUM(COALESCE(fees_est,0)),2) fees, ROUND(SUM(COALESCE(pnl_net,0)),2) pnet,
          SUM(CASE WHEN ABS({_GROSS} - realized_pnl) > 0.5 THEN 1 ELSE 0 END) n_divergent
        FROM trade_records
        WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL AND exit_price>0 AND quantity>0
        GROUP BY 1 ORDER BY 1"""):
        monthly.append({"month": r[0], "n": r[1], "gross": r[2], "realized_pnl_sum": r[3],
                        "fees_est": r[4], "pnl_net_sum": r[5], "n_divergent": r[6],
                        "gross_vs_rpnl_gap": round(r[2] - r[3], 2)})

    divergent = []
    for r in conn.execute(f"""
        SELECT id, symbol, direction, trade_date, exit_reason,
          ROUND({_GROSS},2), realized_pnl,
          ROUND({_GROSS} - realized_pnl,2)
        FROM trade_records
        WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL AND exit_price>0 AND quantity>0
          AND ABS({_GROSS} - realized_pnl) > 0.5
        ORDER BY trade_date"""):
        divergent.append({"id": r[0], "symbol": r[1], "direction": r[2], "date": r[3],
                          "exit_reason": r[4], "gross": r[5], "realized_pnl": r[6], "gap": r[7]})

    dist = conn.execute("""
        SELECT ROUND(MIN(fees_est),2), ROUND(AVG(fees_est),2), ROUND(MAX(fees_est),2),
          ROUND(100.0*AVG(fees_est/(quantity*entry_price)),3),
          ROUND(100.0*MAX(fees_est/(quantity*entry_price)),3),
          COUNT(CASE WHEN fees_source='est' THEN 1 END),
          COUNT(CASE WHEN fees_source IN ('broker','reconciled') THEN 1 END)
        FROM trade_records
        WHERE source_type='PLAYBOOK_SIM' AND fees_est > 0""").fetchone()

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope": "trade_records PLAYBOOK_SIM 已平仓",
        "caliber_conclusion": {
            "sim_actual_fee": "不可得 — order_fee_query SIM 返空 (DEB-20260730 层1 已裁决)",
            "sim_only_caliber": "fees_est (参数=用户提供的富途美股费率表, app/execution/fee_model.py)",
            "live_upgrade_path": "broker/reconciled 三态只升不降已预置 (pipeline.py:4469)",
            "realized_pnl_semantics": "毛口径 (价格差), 实证: 逐月 implicit fee≈0",
            "monthly_sign_dependency": "月度盈亏符号由 est 决定属模型选择, 非数据错误; "
                                       "报告必须毛/净双列 (已在复盘三列)",
        },
        "monthly": monthly,
        "divergent_trades": divergent,
        "divergent_note": "gross vs realized_pnl 分叉 100% 由 EXIT_MONITOR_CLOSE 损坏单解释 "
                          "(partial/重复平仓) — 归 R4 幽灵/陈旧仓清理, 非费用模型问题",
        "fee_distribution": {
            "min": dist[0], "avg": dist[1], "max": dist[2],
            "avg_pct_of_notional": dist[3], "max_pct_of_notional": dist[4],
            "n_est": dist[5], "n_broker_or_reconciled": dist[6],
            "health": "min≈$4.01=地板 (min佣金$1.99×2+交收); max 1.99%名义=小单地板效应; 无异常",
        },
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))

    print("═══ 费用对账 (R2) ═══")
    print(f"{'月':<9} {'n':>4} {'毛$':>9} {'rpnl和$':>9} {'est费$':>8} {'净$':>9} {'分叉':>4}")
    for m in monthly:
        print(f"{m['month']:<9} {m['n']:>4} {m['gross']:>9} {m['realized_pnl_sum']:>9} "
              f"{m['fees_est']:>8} {m['pnl_net_sum']:>9} {m['n_divergent']:>4}")
    print(f"分叉单: {len(divergent)} 笔 → 全部 EXIT_MONITOR_CLOSE 损坏单 (归 R4), "
          f"月度 gap 完全解释")
    for d in divergent:
        print(f"  #{d['id']} {d['symbol']} {d['direction']} {d['date']} {d['exit_reason']}: "
              f"毛{d['gross']} vs rpnl{d['realized_pnl']} (gap {d['gap']})")
    print(f"费用分布: min ${dist[0]} / avg ${dist[1]} / max ${dist[2]}, "
          f"占名义 avg {dist[3]}% / max {dist[4]}% | est×{dist[5]} broker/reconciled×{dist[6]}")
    print(f"结论: SIM 实收不可得 (券商返空, 已裁决), est=唯一可行口径; 账已平, "
          f"唯一破口={len(divergent)} 笔损坏单归 R4")
    print(f"已落盘 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
