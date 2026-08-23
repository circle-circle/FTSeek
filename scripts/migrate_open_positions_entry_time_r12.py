#!/usr/bin/env python
"""R-12 存量迁移: open_positions.entry_time 从 CST 归一到 ET (2026-08-23, DEB-20260823).

背景
----
`upsert_position_simple`（trade_store.py:949）原用裸 `datetime.now()` = 宿主 Asia/Shanghai，
而同一笔仓写进 `trade_records.entry_time` 的是 ET（playbook_service 用 `now_et()`）。
实证 US.WEAV: open_positions=2026-08-19T21:36:54 vs trade_records=2026-08-19T09:36:54，
恰差 12h。写入侧已在 R-12 修好，本脚本处理存量行。

策略（保守，优先用真源而非盲算）
--------------------------------
1. 对每个 open_positions 行，优先从 `trade_records` 同 symbol 的未平仓行取 ET entry_time
   作为真值直接写入 —— 这是最可靠的，不依赖时区推算。
2. 找不到对应 trade_records 行时，只在能确认该值"像 CST"的情况下才换算：
   判据 = 转成 ET 后落在美股可交易时段（ET 04:00-20:00）。否则跳过并报告，不猜。
3. 已经是 ET 的行（换算后会落到非交易时段）自动跳过 → **幂等**，可重复执行。

用法
----
    ./venv/bin/python scripts/migrate_open_positions_entry_time_r12.py          # 预演 (默认)
    ./venv/bin/python scripts/migrate_open_positions_entry_time_r12.py --apply  # 实际写入
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db_utils import get_main_db  # noqa: E402
from app.utils.timezone import CST, ET  # noqa: E402

# 美股可交易时段 (ET 小时): 盘前 4:00 起, 盘后至 20:00
_TRADEABLE_ET_HOURS = set(range(4, 20))


def _parse(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _looks_like_cst(dt: datetime) -> bool:
    """naive 值按 CST 解释后落在 ET 可交易时段 → 判定它原本是 CST."""
    if dt.tzinfo is not None:
        return False
    try:
        et = CST.localize(dt).astimezone(ET)
    except Exception:
        return False
    return et.hour in _TRADEABLE_ET_HOURS


def _looks_like_et(dt: datetime) -> bool:
    """naive 值直接当 ET 解释就落在可交易时段 → 已经是 ET, 无需迁移."""
    if dt.tzinfo is not None:
        return True
    return dt.hour in _TRADEABLE_ET_HOURS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入 (缺省仅预演)")
    args = ap.parse_args()

    db = get_main_db()
    if not db.exists():
        print(f"❌ 主库不存在: {db}")
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT symbol, entry_time, size FROM open_positions WHERE size > 0"
        ).fetchall()
        if not rows:
            print("open_positions 无在仓行, 无需迁移")
            return 0

        print(f"{'symbol':<12} {'当前 entry_time':<32} {'判定':<26} {'新值':<32}")
        print("-" * 106)

        plan, skipped = [], []
        for r in rows:
            sym, cur = r["symbol"], r["entry_time"]
            dt = _parse(cur) if cur else None
            if dt is None:
                skipped.append((sym, cur, "无法解析, 跳过"))
                continue

            # 真源优先: trade_records 同 symbol 未平仓行的 ET entry_time
            tr = conn.execute(
                "SELECT entry_time FROM trade_records WHERE symbol=? "
                "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                "AND (exit_time IS NULL OR exit_time='') "
                "ORDER BY id DESC LIMIT 1",
                (sym,),
            ).fetchone()

            if tr and tr["entry_time"]:
                new = str(tr["entry_time"])
                if new == str(cur):
                    skipped.append((sym, cur, "已与 trade_records 一致 (幂等)"))
                    continue
                plan.append((sym, cur, new, "取 trade_records 真源"))
                continue

            # 无真源 → 只在确认"像 CST 且不像 ET"时才换算
            if _looks_like_cst(dt) and not _looks_like_et(dt):
                new = CST.localize(dt).astimezone(ET).replace(tzinfo=None).isoformat()
                plan.append((sym, cur, new, "CST→ET 换算 (无真源)"))
            else:
                skipped.append((sym, cur, "已是 ET 或无法判定, 跳过"))

        for sym, cur, new, why in plan:
            print(f"{sym:<12} {str(cur):<32} {why:<26} {new:<32}")
        for sym, cur, why in skipped:
            print(f"{sym:<12} {str(cur):<32} {why:<26} {'—':<32}")

        print("-" * 106)
        print(f"待迁移 {len(plan)} 行 / 跳过 {len(skipped)} 行")

        if not plan:
            print("✅ 无需改动 (已全部为 ET 口径)")
            return 0

        if not args.apply:
            print("\n预演模式 — 加 --apply 实际写入")
            return 0

        for sym, _cur, new, _why in plan:
            conn.execute(
                "UPDATE open_positions SET entry_time=? WHERE symbol=?", (new, sym)
            )
        conn.commit()
        print(f"\n✅ 已写入 {len(plan)} 行")

        print("\n迁移后复核:")
        for r in conn.execute(
            "SELECT symbol, entry_time FROM open_positions WHERE size > 0"
        ):
            tr = conn.execute(
                "SELECT entry_time FROM trade_records WHERE symbol=? "
                "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') "
                "AND (exit_time IS NULL OR exit_time='') ORDER BY id DESC LIMIT 1",
                (r["symbol"],),
            ).fetchone()
            mark = "✓" if (tr and str(tr["entry_time"]) == str(r["entry_time"])) else "·"
            print(f"  {mark} {r['symbol']:<12} open_positions={r['entry_time']}"
                  f"  trade_records={tr['entry_time'] if tr else '(无)'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
