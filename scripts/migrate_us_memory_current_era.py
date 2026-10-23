#!/usr/bin/env python3
"""把 ~/.hermes/ftseek/memory/us.json 从 2026-05 旧时代迁移到现役口径 (2026-07-01+, A10).

安全策略:
- 先备份 us.json.bak.docs-sync-<ts>;
- 旧字段整体移到 legacy_2026_05, 不删除;
- 原子写 (tmp + replace);
- 不重启服务, 不改交易行为; 运行中的 MemoryManager 需下次重启才加载新文件.

用法:
  PYTHONPATH=. ./venv/bin/python scripts/migrate_us_memory_current_era.py --dry-run
  PYTHONPATH=. ./venv/bin/python scripts/migrate_us_memory_current_era.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_MEMORY = Path.home() / ".hermes" / "ftseek" / "memory" / "us.json"
DEFAULT_DB = Path("data/strategy_memory.db")
LEGACY_KEY = "legacy_2026_05"

CORE_PRINCIPLE = (
    "稳定赚钱 > 激进收益；现役只走 Playbook；费用约 10bps/笔；"
    "先证明正期望，再谈放大。"
)

MARKET_RULES = [
    "1. 现役下单唯一路径 = Playbook；主管线冻结、ORB 停用。",
    "2. UE 直接产出自 2026-10-05 停止；intraday 自 2026-09-17 关闭。",
    "3. 费用约 10bps/笔；净 bps 是跨 sizing 的唯一可比口径。",
    "4. 全窗死单率 23.8% vs 盈亏平衡 23.0%；关闭侧通道后 15.6%。",
    "5. 隔夜持夜反事实优于 EOD 平仓（+$2.5k/55 笔）；不要一刀切平仓。",
    "6. 15/30min 死单即砍反事实 -$1.4k；不要砍。",
    "7. 极端跳空单特征阻断误杀>40%；不新增同轴门。",
    "8. 行为变更先影子 + 预注册 + 用户裁；效应<MDE 只写不可分辨。",
    "9. 不因短期亏损放宽 SL/补仓（C11）；不因未触发错失改 entry 定价（C9）。",
    "10. 四锁：sim / no real / futu_simulate / graduation fail-close。",
]

PERF_SUMMARY = (
    "2026-07-01→10-22 A9 clean: 685 trades, gross +8.74bps, fee -10.12bps, "
    "net -1.38bps; post-close 10-06→10-22: 96 trades, net +5.88bps "
    "(DA -2.25bps, EXT +53.19bps/16 trades); equity ~$99.6k sim."
)

CUMULATIVE = {
    "total_trades": 685,
    "SHORT": {"trades": 138, "pnl": 490.97, "wr": 62.3},
    "LONG": {"trades": 547, "pnl": -947.62, "wr": 61.6},
    "SHORT_PROXY_OPTION": {"trades": 0, "pnl": 0, "wr": 0.0},
}

ACTIVE_FIXES = {
    "live_pipeline": "playbook_only",
    "ue_direct_output": "off since 2026-10-05",
    "intraday_reentry": "false since 2026-09-17",
    "four_locks": "sim/no_real/futu_simulate/graduation_fail_close",
    "okr": "OKR-2026-11 approved, effective 2026-11-01",
    "last_config_fix": "2026-10-23 docs/OKR sync (no trading behavior change)",
}

LEGACY_FIELDS = (
    "core_principles",
    "us_performance_summary",
    "cumulative_stats",
    "active_fixes",
    "seven_day_snapshot_20260525",
    "historical_lessons",
    "tagged_lessons",
    "last_eod",
    "last_restart",
    "updated_at",
)


def _recent_lessons(db: Path, limit: int = 20) -> list[str]:
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select date, lesson_type, content from lesson_log order by date desc, id desc limit ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [f"{d} {t}: {c}" for d, t, c in reversed(rows)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", default=str(DEFAULT_MEMORY))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    memory = Path(args.memory)
    if not memory.exists():
        raise SystemExit(f"memory file not found: {memory}")
    data = json.loads(memory.read_text(encoding="utf-8"))
    now = datetime.now().isoformat(timespec="seconds")

    legacy = data.setdefault(LEGACY_KEY, {})
    for key in LEGACY_FIELDS:
        if key in data and key not in legacy:
            legacy[key] = data[key]

    lessons = _recent_lessons(Path(args.db))
    data.update(
        {
            "market": "美股",
            "core_principle": CORE_PRINCIPLE,
            "market_specific_rules": MARKET_RULES,
            "historical_lessons": lessons,
            "tagged_lessons": [],
            "performance_tracking": {},
            "us_performance_summary": PERF_SUMMARY,
            "cumulative_stats": CUMULATIVE,
            "active_fixes": ACTIVE_FIXES,
            "last_eod": "2026-10-22",
            "last_restart": "2026-10-23 08:39:20 CST",
            "updated_at": now,
            "last_updated": now,
            "memory_era": "2026-07-01+ (A10)",
            "memory_note": (
                "2026-05 fields preserved under legacy_2026_05; "
                "current era facts are the top-level fields above."
            ),
        }
    )

    if args.dry_run:
        print(json.dumps({
            "would_write": str(memory),
            "core_principle": data["core_principle"],
            "rules_n": len(data["market_specific_rules"]),
            "lessons_n": len(data["historical_lessons"]),
            "legacy_keys": sorted(legacy),
        }, ensure_ascii=False, indent=2))
        return

    backup = memory.with_name(memory.name + f".bak.docs-sync-{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(memory, backup)
    tmp = memory.with_name(memory.name + ".tmp_migrate")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, memory)
    print(f"backup: {backup}")
    print(f"wrote:  {memory}")
    print("note: running MemoryManager reloads this file only on next service restart")


if __name__ == "__main__":
    main()
