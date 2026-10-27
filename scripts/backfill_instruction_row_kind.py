#!/usr/bin/env python3
"""回填 playbook_instructions.row_kind —— 把历史供给占位行标记成结构化行类型。

背景 [DEB-20261027 B]
----------------------
生成轮每天写 ~50-70 行"供给占位行" (`direction='SKIP'` /
`skip_reason` 前缀「非当前通道候选」/ `model_used='deterministic'` /
`entry_price=0`), 含义 = **被筛过但 DA bridge 未提名的候选**。

这些行与真指令混在同一张表里, 此前只能靠**字符串前缀**识别 ⇒ 12+ 个读数面
把占位行当真指令/真跳过计数 (周报 total_decisions ×4、pre_market_check 报
"当日指令 72 条"实为 17、dashboard 跳过段、decision_view 每天 55-70 条
`decision_id=''` 幽灵条目、get_stats 的 conf 分布被 OTHER 桶摧毁)。

处置: 新增 `row_kind` 列 ('instruction' | 'supply_placeholder')。写路径已在
2026-10-27 改为**结构化赋值**, 本工具只把历史行按既有前缀契约一次性对齐。

范围严格等于既有占位行定义 (前缀「非当前通道候选」) —— 与
`scripts/observe_nightly_supply.py:109` 的 startswith 过滤口径**同源**,
不引入第二套判定。

用法
----
    venv/bin/python scripts/backfill_instruction_row_kind.py            # 只报告 (默认)
    venv/bin/python scripts/backfill_instruction_row_kind.py --apply    # 实际写入

幂等: 写完后再跑一次, 命中行数为 0。
审计: 每次写入前把改前值落到 `data/audits/instruction_row_kind_<ts>.jsonl`。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ftseek_us.db"
DEFAULT_AUDIT_DIR = ROOT / "data" / "audits"

# 与 observe_nightly_supply.py:109 的 startswith 契约同源 (逐字)
PLACEHOLDER_PREFIX = "非当前通道候选"
KIND_PLACEHOLDER = "supply_placeholder"
KIND_INSTRUCTION = "instruction"


def find_rows(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        "SELECT id, date, symbol, direction, skip_reason, model_used, entry_price, "
        "COALESCE(row_kind, ?) AS row_kind "
        "FROM playbook_instructions "
        "WHERE COALESCE(skip_reason, '') LIKE ? "
        "AND COALESCE(row_kind, ?) <> ? "
        "ORDER BY date, id",
        (KIND_INSTRUCTION, PLACEHOLDER_PREFIX + "%", KIND_INSTRUCTION, KIND_PLACEHOLDER),
    ).fetchall()
    return [dict(r) for r in rows]


def apply_rows(conn: sqlite3.Connection, rows: List[dict], audit_path: Path) -> int:
    """写入 row_kind + 落审计 (改前值)。返回写入行数。"""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    with audit_path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            conn.execute(
                "UPDATE playbook_instructions SET row_kind=?, updated_at=? WHERE id=?",
                (KIND_PLACEHOLDER, now, r["id"]))
            n += 1
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    ap.add_argument("--apply", action="store_true", help="实际写入 (默认只报告)")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[backfill] DB 不存在: {db_path}")
        return 2
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(playbook_instructions)").fetchall()}
        if "row_kind" not in cols:
            print("[backfill] row_kind 列不存在 — 先跑 ensure_tables 迁移 "
                  "(app/playbook/db.py ensure_tables) 或重启服务加载迁移")
            return 2
        rows = find_rows(conn)
        if not rows:
            print("[backfill] 无需修复 (0 行)")
            return 0
        by_date = {}
        for r in rows:
            by_date[r["date"]] = by_date.get(r["date"], 0) + 1
        print(f"[backfill] 待标记 {len(rows)} 行 ({KIND_INSTRUCTION} → {KIND_PLACEHOLDER})")
        print(f"  覆盖 {len(by_date)} 个交易日; 首日 {min(by_date)} 末日 {max(by_date)}")
        for d in sorted(by_date)[-5:]:
            print(f"    {d}: {by_date[d]} 行")
        if not args.apply:
            print("[backfill] 未写入 (加 --apply 实际执行)")
            return 0
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit_dir = Path(args.audit_dir)
        audit = audit_dir / f"instruction_row_kind_{ts}.jsonl"
        n = apply_rows(conn, rows, audit)
        print(f"[backfill] 已标记 {n} 行; 审计: {audit}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
