#!/usr/bin/env python3
"""交易记录数据质量普查 (2026-08-22 audit C, 只读).

背景: XLF id=875 脏行实锤 (名义 ~$114万 vs 99分位 <$15k; exit<entry 倒挂;
回补痕迹), 且脏簇集中在 6/2-6/4 broker-mismatch 期. 本工具把该次人工排查
固化成可重跑的只读普查, 供任何回放/统计前先看一眼数据成色.

检查项:
  A. 名义金额离群 (> $50k = 主链路 NOTIONAL_CAP, id=875 类)
  B. 时间倒挂 / 持仓时长不自洽 (exit<entry; holding_hours 偏差>10%)
  C. 回补嫌疑 (created_at 比 entry_time 晚 >24h, 或缺 futu_order_id)
  D. 盈亏不自洽 (realized_pnl 与 方向×价差×数量 偏差 >1% 或 >$1)
  E. 未平仓形态 (exit_price=0/NULL 按 source_type 分组)
  F. 词表普查 (source_type / exit_reason 计数 — 为将来词表统一攒基线)

只读: 输出控制台摘要 + data/trade_quality_census_latest.json.
"""
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DB = Path("data/ftseek_us.db")
OUT = Path("data/trade_quality_census_latest.json")
NOTIONAL_CAP = 50_000.0  # 与回放脚本口径一致 (XLF id=875 后确立的硬过滤线)

COLS = (
    "id, trade_date, symbol, direction, entry_time, exit_time, entry_price, "
    "exit_price, quantity, realized_pnl, exit_reason, holding_hours, "
    "futu_order_id, created_at, source_type"
)


def _parse_dt(s):
    try:
        d = datetime.fromisoformat(str(s))
    except Exception:
        return None
    # 库内混存 naive 与带时区两种格式, 统一剥 tzinfo 再比较 (仅比先后, 不涉跨区换算)
    return d.replace(tzinfo=None)


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT {COLS} FROM trade_records").fetchall()
    conn.close()

    issues_a, issues_b, issues_c, issues_d = [], [], [], []
    open_shapes = Counter()
    src_counter, reason_counter = Counter(), Counter()
    anomaly_by_date = defaultdict(set)

    for r in rows:
        sym, rid = r["symbol"], r["id"]
        entry_p, exit_p = r["entry_price"] or 0, r["exit_price"] or 0
        qty = r["quantity"] or 0
        et, xt = _parse_dt(r["entry_time"]), _parse_dt(r["exit_time"])
        ct = _parse_dt(r["created_at"])
        src_counter[r["source_type"] or "?"] += 1
        reason_counter[r["exit_reason"] or "?"] += 1

        # 未平仓形态
        if not exit_p:
            open_shapes[r["source_type"] or "?"] += 1
            continue

        # A. 名义离群
        notional = float(entry_p) * float(qty)
        if notional > NOTIONAL_CAP:
            issues_a.append({"id": rid, "trade_date": r["trade_date"], "symbol": sym,
                             "source": r["source_type"] or "?",
                             "notional": round(notional), "qty": qty,
                             "entry": entry_p, "backfilled_like_id875": bool(ct and et and (ct - et) > timedelta(hours=24))})
            anomaly_by_date[r["trade_date"]].add("A_notional")
        # B. 时间倒挂 / 时长不自洽
        if et and xt and xt < et:
            issues_b.append({"id": rid, "trade_date": r["trade_date"], "symbol": sym,
                             "issue": "exit_before_entry"})
            anomaly_by_date[r["trade_date"]].add("B_inversion")
        elif et and xt and r["holding_hours"] is not None:
            true_h = (xt - et).total_seconds() / 3600.0
            if true_h > 0 and abs(true_h - float(r["holding_hours"])) / true_h > 0.10:
                issues_b.append({"id": rid, "trade_date": r["trade_date"], "symbol": sym,
                                 "issue": f"holding_hours_mismatch(rec={r['holding_hours']:.3f} true={true_h:.3f})"})
                anomaly_by_date[r["trade_date"]].add("B_holding")
        # C. 回补嫌疑
        if ct and et and (ct - et) > timedelta(hours=24):
            issues_c.append({"id": rid, "trade_date": r["trade_date"], "symbol": sym,
                             "lag_hours": round((ct - et).total_seconds() / 3600, 1),
                             "has_order_id": bool(r["futu_order_id"])})
            anomaly_by_date[r["trade_date"]].add("C_backfill")
        # D. 盈亏自洽 (毛口径; 手续费/净额字段另算, 这里容忍 1% 或 $1)
        if entry_p > 0 and exit_p > 0 and qty > 0:
            gross = ((float(exit_p) - float(entry_p)) if r["direction"] == "LONG"
                     else (float(entry_p) - float(exit_p))) * float(qty)
            rp = r["realized_pnl"]
            if rp is not None and abs(gross - float(rp)) > max(abs(gross) * 0.01, 1.0):
                issues_d.append({"id": rid, "trade_date": r["trade_date"], "symbol": sym,
                                 "gross_calc": round(gross, 2), "recorded": round(float(rp), 2)})
                anomaly_by_date[r["trade_date"]].add("D_pnl")

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "db": str(DB),
        "rows_total": len(rows),
        "checks": {
            "A_notional_gt_cap": {"cap": NOTIONAL_CAP, "n": len(issues_a), "rows": issues_a[:50]},
            "B_timestamp": {"n": len(issues_b), "rows": issues_b[:50]},
            "C_backfill_suspect": {"lag_gt_hours": 24, "n": len(issues_c), "rows": issues_c[:50]},
            "D_pnl_mismatch": {"tol": "1% or $1", "n": len(issues_d), "rows": issues_d[:50]},
            "E_open_shape_by_source": dict(open_shapes.most_common()),
        },
        "vocabulary": {
            "source_type": dict(src_counter.most_common()),
            "exit_reason_top30": dict(reason_counter.most_common(30)),
            "exit_reason_distinct": len(reason_counter),
        },
        "anomaly_cluster_by_date": {
            d: sorted(v) for d, v in sorted(anomaly_by_date.items())
        },
        "verdict": "",
    }

    n_bad = len(issues_a) + len(issues_b) + len(issues_c) + len(issues_d)
    dirty_dates = sorted(d for d, v in anomaly_by_date.items() if v)
    result["verdict"] = (
        f"{len(rows)} 行中 {n_bad} 行触警; 异常日期 "
        f"{dirty_dates[:12]}{'...' if len(dirty_dates) > 12 else ''}. "
        f"回放/统计请沿用 NOTIONAL_CAP>{int(NOTIONAL_CAP)} 过滤"
        if n_bad else f"{len(rows)} 行全部通过四项检查, 数据成色干净"
    )

    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"═══ 交易数据质量普查 ({result['generated_at'][:10]}) ═══")
    print(f"总行数: {len(rows)}")
    print(f"A 名义>${NOTIONAL_CAP:.0f}: {len(issues_a)} 行 (按来源: "
          f"{dict(Counter(x['source'] for x in issues_a).most_common())})")
    for x in issues_a[:8]:
        print(f"   id={x['id']} {x['trade_date']} {x['symbol']}[{x['source']}] notional=${x['notional']:,} qty={x['qty']}")
    print(f"B 时间异常: {len(issues_b)} 行")
    print(f"C 回补嫌疑(>24h): {len(issues_c)} 行")
    for x in issues_c[:8]:
        print(f"   id={x['id']} {x['trade_date']} {x['symbol']} lag={x['lag_hours']}h order_id={'有' if x['has_order_id'] else '无'}")
    print(f"D 盈亏不自洽: {len(issues_d)} 行")
    for x in issues_d[:8]:
        print(f"   id={x['id']} {x['trade_date']} {x['symbol']} calc=${x['gross_calc']} rec=${x['recorded']}")
    if dirty_dates:
        print(f"异常日期簇: {dirty_dates}")
    print(f"词表: source_type×{len(src_counter)} / exit_reason×{result['vocabulary']['exit_reason_distinct']}")
    print(f"\n裁决: {result['verdict']}")
    print(f"输出: {OUT}")


if __name__ == "__main__":
    main()
