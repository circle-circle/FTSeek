#!/usr/bin/env python
"""R-10 修复: hermes 硬止损监控 cron 的 DoW 错配 (2026-08-23, DEB-20260823).

问题
----
job `DEB-20260815 硬止损监控` 的 expr = `*/30 21-23,0-4 * * 2-6`。
宿主时区 Asia/Shanghai (CST)，两个小时段对应的 ET 交易日**不同**：

    CST 21-23 段  → ET 同日 09:00-11:59  ⇒ DoW 应为 1-5 (周一至周五)
    CST 0-4  段  → ET 前日 12:00-16:59  ⇒ DoW 应为 2-6 (周二至周六)

两段合用一个 DoW=2-6，后果：
  * **ET 周一 09:00-11:59 完全无监控**（每周开盘首 3 小时裸奔）
  * ET 周六 09:00-11:59 空跑（市场休市）

该脚本负责在账户触及硬止损线时把 `playbook.auto_execute` 置 false，
故周一失守 = 硬止损闸门每周固定缺一段。

修法
----
照抄仓内已有的**正确范式** —— `ftseek-intraday-diagnose` 与
`ftseek-intraday-diagnose-late` 就是拆成两个 job、各带正确 DoW：

    ftseek-intraday-diagnose       */30 21-23 * * 1-5
    ftseek-intraday-diagnose-late  */30 0-4  * * 2-6

本脚本把硬止损监控同样拆成「上午段」+「下午段」两个 job。

用法
----
    ./venv/bin/python scripts/fix_hermes_stoploss_cron_r10.py           # 预演
    ./venv/bin/python scripts/fix_hermes_stoploss_cron_r10.py --apply   # 实际写入

幂等：已拆分过则检测到后直接退出，可重复执行。
写入前自动备份到 data/backups_r9_r13/。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

HERMES_JOBS = Path("/root/.hermes/cron/jobs.json")
BACKUP_DIR = Path(__file__).resolve().parent.parent / "data" / "backups_r9_r13"

OLD_EXPR = "*/30 21-23,0-4 * * 2-6"
MORNING_EXPR = "*/30 21-23 * * 1-5"   # ET 周一-周五 09:00-11:59
AFTERNOON_EXPR = "*/30 0-4 * * 2-6"   # ET 周一-周五 12:00-16:59


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入 (缺省仅预演)")
    args = ap.parse_args()

    if not HERMES_JOBS.exists():
        print(f"❌ 未找到 {HERMES_JOBS}")
        return 1

    d = json.loads(HERMES_JOBS.read_text(encoding="utf-8"))
    jobs = d.get("jobs")
    if not isinstance(jobs, list):
        print("❌ jobs.json 结构异常 (期望顶层 'jobs' 为 list)")
        return 1

    hits = [(i, j) for i, j in enumerate(jobs) if "硬止损" in str(j.get("name", ""))]
    if not hits:
        print("❌ 未找到硬止损监控 job")
        return 1

    exprs = {j["schedule"].get("expr") for _, j in hits}
    if exprs == {MORNING_EXPR, AFTERNOON_EXPR}:
        print("✅ 已是拆分后状态 (幂等), 无需改动:")
        for _, j in hits:
            print(f"   {j['schedule']['expr']:24s} | {j['name']}")
        return 0

    if len(hits) != 1 or hits[0][1]["schedule"].get("expr") != OLD_EXPR:
        print("⚠️ 当前状态与预期不符, 拒绝自动改动。请人工检查:")
        for _, j in hits:
            print(f"   expr={j['schedule'].get('expr')!r} | {j['name']}")
        return 1

    idx, orig_ref = hits[0]
    orig = copy.deepcopy(orig_ref)

    print("当前:")
    print(f"  {OLD_EXPR:24s} | {orig['name']}")
    print("     ⚠️ CST 21-23 段挂 DoW=2-6 → ET 周一 09:00-11:59 无监控")
    print("\n将改为:")
    print(f"  {MORNING_EXPR:24s} | 上午段 → ET 周一-周五 09:00-11:59")
    print(f"  {AFTERNOON_EXPR:24s} | 下午段 → ET 周一-周五 12:00-16:59  (新增 job)")

    if not args.apply:
        print("\n预演模式 — 加 --apply 实际写入")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"hermes_jobs.pre_r10.{stamp}.json"
    shutil.copy2(HERMES_JOBS, backup)
    print(f"\n已备份 → {backup}")

    # 上午段: 原地改 DoW
    a = jobs[idx]
    a["name"] = "DEB-20260815 硬止损监控·上午段 (-4840.06)"
    a["schedule"] = {"kind": "cron", "expr": MORNING_EXPR, "display": MORNING_EXPR}
    a["schedule_display"] = MORNING_EXPR
    a["next_run_at"] = None

    # 下午段: 克隆一份, 重置运行态
    b = copy.deepcopy(orig)
    b["id"] = uuid.uuid4().hex[:12]
    b["name"] = "DEB-20260815 硬止损监控·下午段 (-4840.06)"
    b["schedule"] = {"kind": "cron", "expr": AFTERNOON_EXPR, "display": AFTERNOON_EXPR}
    b["schedule_display"] = AFTERNOON_EXPR
    b["repeat"] = {"times": None, "completed": 0}
    for k in ("next_run_at", "last_run_at", "last_status", "last_error", "last_delivery_error"):
        b[k] = None
    b["failure_streak"] = 0
    jobs.insert(idx + 1, b)

    d["updated_at"] = datetime.now().astimezone().isoformat()

    fd, tmp = tempfile.mkstemp(dir=str(HERMES_JOBS.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HERMES_JOBS)

    print("✅ 已写入:")
    print(f"   A id={a['id']} {MORNING_EXPR:24s} → ET 周一-周五 09:00-11:59")
    print(f"   B id={b['id']} {AFTERNOON_EXPR:24s} → ET 周一-周五 12:00-16:59")
    print("\n⚠️ hermes 需重载调度才生效, 请执行:")
    print("   XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway")
    print(f"\n回滚: cp {backup} {HERMES_JOBS} && 重启 hermes-gateway")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
