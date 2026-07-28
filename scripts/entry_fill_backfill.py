#!/usr/bin/env python3
"""入场价历史回填执行器 (2026-07-27, DEB-entry-fill §七 用户裁"执行")。

消费 data/entry_fill_impact_report.json (影响面报告逐笔明细, HIGH/MEDIUM 匹配),
将 quote_fallback 假入场价回填为券商真实成交均价, 并一致重算全部衍生字段:
  entry_price / realized_pnl / pnl_pct / mfe_pct / mae_pct (基于 metadata peak/trough)

安全设计:
  - 默认 dry-run 只打印; --apply 才写
  - --apply 先用 sqlite3 backup API 在线备份 (服务运行中 WAL 安全)
  - 逐笔审计字段: metadata.entry_price_orig / entry_corrected / entry_corrected_at /
    entry_price_source_orig (原 source 保留), entry_price_source → broker_deal_backfill
  - MEDIUM 置信 (1 笔): |Δentry|<1% 自动并入并打印明细, 否则跳过
  - NONE (6 笔): 不改价, metadata 标 entry_uncorrectable=true
回滚: 恢复备份文件, 或按 entry_price_orig 逐笔还原。
用法: PYTHONPATH=. ./venv/bin/python scripts/entry_fill_backfill.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB = REPO / "data" / "ftseek_us.db"
REPORT = REPO / "data" / "entry_fill_impact_report.json"


def recompute(direction: str, entry: float, exit_p: float | None, qty: float,
              peak: float | None, trough: float | None) -> dict:
    """一致重算衍生字段 (口径与库内样例对齐: pnl_pct=小数收益率, mfe/mae=正百分比)。

    2026-07-28 修正 (不变量拉网揭出自引 bug): 库内 peak/trough 语义是“最有利/最不利”
    而非物理高/低价 (坑库既有经验, 首版未查) — SHORT 旧公式把两者用反, 21 笔 mfe/mae
    符号错; 且 mfe/mae 重算条件拆开 (旧版 trough 缺失时连 mfe 也跳过 → 改 entry 不跟 mfe
    的半更新, LONG 6 笔)。方向无关统一式: mfe=(peak-entry)*sign/entry, mae=(entry-trough)*sign/entry。
    """
    short = str(direction).upper() in ("SHORT", "SELL_SHORT")
    sign = -1 if short else 1
    out = {}
    if exit_p and exit_p > 0 and entry > 0:
        out["realized_pnl"] = round((exit_p - entry) * qty * sign, 4)
        out["pnl_pct"] = round((exit_p - entry) / entry * sign, 6)
    # peak=最有利价 (LONG 高/SHORT 低), trough=最不利价 — 条件拆开, 缺一不影响另一。
    # clamp ≥0: MFE/MAE 定义为非负偏移量 — 回填改小 entry 后旧 trough 可能优于新 entry
    # (从未不利 → mae=0), 负值是语义错而非数据错。
    if peak and entry > 0:
        out["mfe_pct"] = round(max(0.0, (float(peak) - entry) * sign / entry * 100), 4)
    if trough and entry > 0:
        out["mae_pct"] = round(max(0.0, (entry - float(trough)) * sign / entry * 100), 4)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写 (缺省 dry-run)")
    args = ap.parse_args()

    rep = json.loads(REPORT.read_text())
    details = rep["details"]
    to_fill = [d for d in details if d["match"] == "HIGH"]
    medium = [d for d in details if d["match"] == "MEDIUM"]
    none_rows = [d for d in details if d["match"] == "NONE"]

    for d in medium:  # MEDIUM: 明细打印 + |Δ|<1% 并入
        print(f"[MEDIUM 过目] id={d['id']} {d['symbol']} {d['date']} "
              f"entry {d['entry_old']}→{d.get('entry_new')} (Δ{d.get('d_entry_pct')}%) "
              f"order={d.get('order_id')}")
        if d.get("d_entry_pct") is not None and abs(d["d_entry_pct"]) < 1.0:
            to_fill.append(d)
            print("  → |Δ|<1% 并入回填")
        else:
            none_rows.append(d)
            print("  → Δ 异常, 转 uncorrectable")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n[{mode}] 回填 {len(to_fill)} 笔 / 标记 uncorrectable {len(none_rows)} 笔")

    if args.apply:
        bak = DB.with_name(f"ftseek_us.db.bak_entryfill_{datetime.now():%Y%m%d_%H%M%S}")
        src = sqlite3.connect(str(DB))
        dst = sqlite3.connect(str(bak))
        src.backup(dst)  # 在线备份 (WAL 安全)
        dst.close(); src.close()
        print(f"DB 已在线备份: {bak.name}")

    conn = sqlite3.connect(str(DB) if args.apply else f"file:{DB}?mode=ro", uri=not args.apply)
    conn.row_factory = sqlite3.Row
    n_done, flips, dpnl_sum = 0, 0, 0.0
    try:
        for d in to_fill:
            row = conn.execute(
                "SELECT direction, entry_price, exit_price, quantity, realized_pnl, "
                "metadata_json FROM trade_records WHERE id=?", (d["id"],)).fetchone()
            if not row:
                continue
            meta = json.loads(row["metadata_json"] or "{}")
            if meta.get("entry_corrected"):
                continue  # 幂等: 已回填过跳过
            new_entry = float(d["entry_new"])
            der = recompute(row["direction"], new_entry,
                            float(row["exit_price"] or 0) or None,
                            float(row["quantity"] or 0),
                            meta.get("peak_price"), meta.get("trough_price"))
            old_pnl = float(row["realized_pnl"] or 0)
            new_pnl = der.get("realized_pnl", old_pnl)
            dpnl_sum += new_pnl - old_pnl
            if (old_pnl > 0) != (new_pnl > 0):
                flips += 1
                print(f"  [翻转] id={d['id']} {d['symbol']} pnl {old_pnl:+.2f} → {new_pnl:+.2f}")
            meta.update({
                "entry_price_orig": row["entry_price"],
                "entry_price_source_orig": meta.get("entry_price_source"),
                "entry_price_source": "broker_deal_backfill",
                "entry_corrected": True,
                "entry_corrected_at": datetime.now().isoformat(),
                "entry_backfill_order_id": d.get("order_id"),
            })
            if args.apply:
                sets = ["entry_price=?", "metadata_json=?"]
                vals = [new_entry, json.dumps(meta, ensure_ascii=False)]
                for k in ("realized_pnl", "pnl_pct", "mfe_pct", "mae_pct"):
                    if k in der:
                        sets.append(f"{k}=?")
                        vals.append(der[k])
                vals.append(d["id"])
                conn.execute(f"UPDATE trade_records SET {', '.join(sets)} WHERE id=?", vals)
            n_done += 1
        for d in none_rows:
            if args.apply:
                row = conn.execute("SELECT metadata_json FROM trade_records WHERE id=?",
                                   (d["id"],)).fetchone()
                meta = json.loads(row["metadata_json"] or "{}")
                if not meta.get("entry_uncorrectable"):
                    meta["entry_uncorrectable"] = True
                    conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                                 (json.dumps(meta, ensure_ascii=False), d["id"]))
        if args.apply:
            conn.commit()
    finally:
        conn.close()

    print(f"\n[{mode}] 完成: 回填 {n_done} 笔, ΔPnL 合计 {dpnl_sum:+.2f}, 符号翻转 {flips} 笔, "
          f"uncorrectable {len(none_rows)} 笔")
    if not args.apply:
        print("(dry-run 未写入 — 加 --apply 执行)")


if __name__ == "__main__":
    main()
