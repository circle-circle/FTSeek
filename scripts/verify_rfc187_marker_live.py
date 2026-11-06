#!/usr/bin/env python3
"""RFC-187 上线后核对: 周报跑完后, 标记是否落在新表、当日环境行是否还在。

背景: 周报在周五 16:00 ET 后触发 (scheduler_triggers.py)。旧写法会把标记写到
      playbook_regime_log 的 date=周报周五, 覆盖当日环境行。RFC-187 后应改为写
      playbook_weekly_markers, 环境行必须存活。

判据 (任一不满足 => exit 1):
  ① playbook_weekly_markers 有 week_end=<日期> 一行
  ② playbook_regime_log 在该日期仍有 1 行且 regime != 'weekly_report'
  ③ 该行 vix > 0

注: 对**历史**日期跑会 fail c2/c4 (那些周五的环境行当年就被旧写法吃掉, 不可恢复)。
    本脚本的用途是核对**上线之后**新发生的周报日, 不是回溯修复。

用法:
  venv/bin/python3.11 scripts/verify_rfc187_marker_live.py                # 核对今天(ET)
  venv/bin/python3.11 scripts/verify_rfc187_marker_live.py --date 2026-11-06
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD, 默认今天(ET)")
    args = ap.parse_args(argv)
    day = args.date or datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    marker = con.execute(
        "SELECT week_end, created_at FROM playbook_weekly_markers WHERE week_end = ?", (day,)
    ).fetchone()
    env = con.execute(
        "SELECT date, regime, vix FROM playbook_regime_log WHERE date = ?", (day,)
    ).fetchall()
    legacy = con.execute(
        "SELECT COUNT(*) FROM playbook_regime_log WHERE regime LIKE '%weekly%'").fetchone()[0]

    checks = {
        "c1_marker_in_new_table": marker is not None,
        "c2_env_row_present": len(env) == 1,
        "c3_env_regime_not_marker": bool(env) and "weekly" not in str(env[0]["regime"]),
        "c4_env_vix_positive": bool(env) and (env[0]["vix"] or 0) > 0,
        "c5_no_legacy_marker_rows": legacy == 0,
    }
    out = {
        "date_et": day,
        "checks": checks,
        "passed": all(checks.values()),
        "marker": dict(marker) if marker else None,
        "env_row": [dict(r) for r in env],
        "legacy_marker_rows": legacy,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())