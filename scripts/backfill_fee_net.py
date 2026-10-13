#!/usr/bin/env python3
"""费用净额历史回填 (DEB-20260730-fee-net-pnl D3, 用户裁"执行" 2026-07-30)。

做什么:
  1. trade_records 幂等 ALTER 加四列: fees_est / pnl_net / fees_source / net_pnl_pct
  2. 全部已平仓行按 fee_model 回填 fees_est + pnl_net + net_pnl_pct;
     fees_source='est' (仅当现值非 broker/reconciled 时覆盖 — 三态只升不降)
  3. 白名单行 (source_type IN PLAYBOOK_SIM/PLAYBOOK_LIVE) 额外写 metadata.fee_breakdown 审计明细
  4. 报告只出白名单口径 (§九.2 正向白名单, 防跨引擎拼接事故第四次重演): 月度汇总 + 假盈利单清单

口径:
  - 每行 = 入场 1 单 + 出场 1 单 (T2 实证: 分批 2/157 可忽略)
  - SEC 费按成交日选时间序列费率 (2026-04-04 前 $0)
  - 借券费: 白名单 SHORT 全部日内 (holding<1d) → 0, 不近似不猜; 隔夜 SHORT 若未来出现由
    go-forward 记账处理
安全: 默认 dry-run 只打印; --apply 先 sqlite backup API 在线备份; 幂等可重跑 (确定性重算)。
用法: PYTHONPATH=. ./venv/bin/python scripts/backfill_fee_net.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.execution.fee_model import estimate_round_trip  # noqa: E402

DB = REPO / "data" / "ftseek_us.db"
WHITELIST = ("PLAYBOOK_SIM", "PLAYBOOK_LIVE")  # §九.2 正向白名单 — 裁决口径唯一合法子集
REPORT = REPO / "data" / "fee_backfill_report.json"


def ensure_columns(conn: sqlite3.Connection) -> list[str]:
    cols = [x[1] for x in conn.execute("PRAGMA table_info(trade_records)")]
    added = []
    for name, ddl in (
        ("fees_est", "REAL"), ("pnl_net", "REAL"), ("fees_source", "TEXT"),
        ("net_pnl_pct", "REAL"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE trade_records ADD COLUMN {name} {ddl}")
            added.append(name)
    return added


def repair_duplicate_external_close_rows(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "UPDATE trade_records SET fees_est=NULL, pnl_net=NULL, fees_source=NULL "
        ", net_pnl_pct=NULL "
        "WHERE data_quality='duplicate_external_close' AND realized_pnl IS NULL"
    )
    return cursor.rowcount


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写 (缺省 dry-run)")
    args = ap.parse_args()

    if args.apply:
        bak = REPO / "data" / f"ftseek_us.db.bak_feenet_{datetime.now():%Y%m%d_%H%M%S}"
        src = sqlite3.connect(str(DB))
        dst = sqlite3.connect(str(bak))
        src.backup(dst)
        dst.close()
        src.close()
        print(f"备份: {bak.name}")

    conn = sqlite3.connect(str(DB) if args.apply else f"file:{DB}?mode=ro", uri=not args.apply)
    conn.row_factory = sqlite3.Row
    added = ensure_columns(conn) if args.apply else []
    if added:
        print(f"ALTER 加列: {added}")

    rows = conn.execute(
        "SELECT id, symbol, trade_date, direction, entry_price, exit_price, quantity, "
        "realized_pnl, source_type, exit_time, metadata_json FROM trade_records "
        "WHERE exit_time IS NOT NULL AND exit_time != '' AND exit_price > 0 "
        "AND entry_price > 0 AND quantity > 0 AND realized_pnl IS NOT NULL"
    ).fetchall()
    repaired = repair_duplicate_external_close_rows(conn) if args.apply else 0

    n_upd = n_meta = 0
    wl_month = defaultdict(lambda: [0, 0.0, 0.0])  # month -> [n, gross, fees]
    fake_wins = []
    for r in rows:
        exit_date = str(r["exit_time"])[:10] or r["trade_date"]
        rt = estimate_round_trip(r["quantity"], r["entry_price"], r["exit_price"],
                                 r["direction"], r["trade_date"], exit_date)
        fees = round(rt["total"], 2)
        gross = float(r["realized_pnl"] or 0)
        net = round(gross - fees, 2)
        notional = float(r["quantity"]) * float(r["entry_price"])
        net_pct = round(net / notional, 4) if notional > 0 else None
        if args.apply:
            # 三态只升不降: broker/reconciled 不被 est 覆盖
            conn.execute(
                "UPDATE trade_records SET fees_est=?, pnl_net=?, net_pnl_pct=?, "
                "fees_source=CASE WHEN fees_source IN ('broker','reconciled') "
                "THEN fees_source ELSE 'est' END WHERE id=?",
                (fees, net, net_pct, r["id"]))
            n_upd += 1
        if r["source_type"] in WHITELIST:
            mo = str(r["trade_date"])[:7]
            wl_month[mo][0] += 1
            wl_month[mo][1] += gross
            wl_month[mo][2] += fees
            if gross > 0 >= net:
                fake_wins.append({"id": r["id"], "symbol": r["symbol"][3:],
                                  "date": r["trade_date"], "gross": round(gross, 2),
                                  "fees": fees, "net": net})
            if args.apply:
                # 白名单行落审计明细 (G2 对账与未来 broker 升级逐项 diff 依赖)
                try:
                    m = json.loads(r["metadata_json"] or "{}")
                    m["fee_breakdown"] = {**rt, "source": "est",
                                          "backfilled_at": datetime.now().isoformat()[:19]}
                    conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                                 (json.dumps(m, ensure_ascii=False, default=str), r["id"]))
                    n_meta += 1
                except Exception as e:
                    print(f"  [warn] id={r['id']} metadata 写入跳过: {e}")

    if args.apply:
        conn.commit()

    wl_n = sum(v[0] for v in wl_month.values())
    wl_gross = sum(v[1] for v in wl_month.values())
    wl_fees = sum(v[2] for v in wl_month.values())
    print(f"\n{'[APPLY]' if args.apply else '[DRY-RUN]'} 全表候选 {len(rows)} 行"
          + (f", 已回填 {n_upd} (白名单明细 {n_meta})" if args.apply else ""))
    if repaired:
        print(f"重复外部平仓行净额清理: {repaired} 行")
    print(f"═══ 白名单口径 (source_type IN {WHITELIST}) ═══")
    print(f"全史: n={wl_n} 毛={wl_gross:+.2f} 费={wl_fees:.2f} 净={wl_gross - wl_fees:+.2f}")
    for mo in sorted(wl_month):
        n, g, f = wl_month[mo]
        print(f"  {mo}: n={n:<4} 毛={g:+9.2f} 费={f:8.2f} 净={g - f:+9.2f}")
    print(f"假盈利单 (毛>0 净≤0): {len(fake_wins)} 笔")
    for fw in fake_wins[:8]:
        print(f"  id={fw['id']} {fw['symbol']:<6} {fw['date']} "
              f"毛{fw['gross']:+.2f} 费{fw['fees']:.2f} 净{fw['net']:+.2f}")

    REPORT.write_text(json.dumps({
        "ran_at": datetime.now().isoformat()[:19], "applied": args.apply,
        "candidates": len(rows), "updated": n_upd,
        "whitelist": {"n": wl_n, "gross": round(wl_gross, 2), "fees": round(wl_fees, 2),
                      "net": round(wl_gross - wl_fees, 2),
                      "by_month": {k: {"n": v[0], "gross": round(v[1], 2),
                                       "fees": round(v[2], 2)} for k, v in sorted(wl_month.items())}},
        "fake_wins": fake_wins,
    }, ensure_ascii=False, indent=1))
    print(f"报告: {REPORT.name}")

    # G3: 白名单已平仓行 fees_est 非空率 100%
    if args.apply:
        n_null = conn.execute(
            "SELECT COUNT(*) FROM trade_records WHERE exit_time IS NOT NULL AND exit_time != '' "
            "AND exit_price > 0 AND entry_price > 0 AND quantity > 0 AND fees_est IS NULL "
            f"AND source_type IN {WHITELIST}").fetchone()[0]
        print(f"G3 白名单 fees_est 空值残留: {n_null} (判据=0)")
        conn.close()
        return 0 if n_null == 0 else 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
