#!/usr/bin/env python3
"""一次性 · 美股收盘后**有守卫地**重启 `ftseek-us.service` (2026-09-08 用户裁「需要，继续」).

## 为什么要有守卫 (而不是直接 `systemctl restart`)

无人值守的 restart 会把**当时工作区里的任何未提交改动**载进实盘交易进程。而本次重启对
RFC-078 批 A-D 的**净收益为零**: 本批对 `ftseek-us` 的导入图只有 `app/core/db_utils.py` 的
纯新增函数, 且该服务于 20:56:55 启动**晚于**本批最后一次 `app/` 改动 (20:56:02) ⇒ 运行进程
早已载入新代码; 批 B/D 的 `app/web_dashboard/live_ledger.py` 只被看板 (socket-activated, 已重启)
与 cron 脚本 (每次新进程) 导入, **不在** `ftseek-us` 的导入图里。

同时并发会话当时有 `app/playbook/*` 的未提交 WIP ⇒ 无守卫重启 = 替别人把半成品部署到实盘。
故: 守卫不满足就**跳过并记原因**, 宁可这次不重启。

## 守卫 (任一不满足即跳过, 退出码 4)

1. **工作区干净**: `git status --porcelain -- app/ scripts/` 为空 —— 未提交的代码改动
   不得由一个定时任务载进实盘进程 (谁改的谁决定何时上线)。
2. **美股不在盘中**: 盘中重启会留一段持仓管理空窗; 收盘后重启才有意义。
   (本任务的 `at` 时点 = 08:05 CST = 20:05 ET, 已在延长时段收盘之后。)
3. **服务当前是 active**: 若它本来就挂着, 那是另一条故障线, 不该由本任务"顺手重启"掩盖。

重启成功后立刻跑 `scripts/startup_smoke_check.py --wait` (刚重启才满足 `--wait` 的 120s 窗口),
退出码与输出都落日志。

## 退出码 (给 `at` 日志与人工核验用, 不靠 stdout 猜)

  0 = 已重启且冒烟通过
  3 = 已重启但冒烟未过 ⇒ **需人工介入** (服务可能起不来)
  4 = 守卫不满足, 已跳过 (未动服务)
  2 = 脚本自身异常

用法:
  PYTHONPATH=. venv/bin/python scripts/restart_ftseek_us_guarded.py [--dry-run] [--unit NAME]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UNIT = "ftseek-us.service"
SMOKE = ROOT / "scripts" / "startup_smoke_check.py"


def _run(cmd: list, timeout: int = 300) -> tuple:
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def guard_worktree_clean() -> tuple:
    rc, out = _run(["git", "status", "--porcelain", "--", "app/", "scripts/"], timeout=60)
    if rc != 0:
        return False, f"git status 失败 (rc={rc}): {out.strip()[:200]}"
    dirty = [l for l in out.splitlines() if l.strip()]
    if dirty:
        return False, (f"工作区有 {len(dirty)} 项未提交的 app/|scripts/ 改动 ⇒ "
                       f"不由定时任务载进实盘进程 (谁改的谁决定何时上线): "
                       f"{'; '.join(d.strip()[:60] for d in dirty[:5])}")
    return True, "工作区 app/|scripts/ 干净"


def guard_market_closed() -> tuple:
    try:
        from app.utils.timezone import is_market_open, now_et
        if is_market_open():
            return False, f"美股盘中 ({now_et().isoformat(timespec='seconds')}) ⇒ 不在盘中重启"
        return True, f"美股非盘中 ({now_et().isoformat(timespec='seconds')})"
    except Exception as exc:  # noqa: BLE001 — 判不了盘中就不重启 (fail-safe 到"不动服务")
        return False, f"盘中判定不可用 ⇒ 保守跳过: {exc}"


def guard_unit_active(unit: str) -> tuple:
    rc, out = _run(["systemctl", "is-active", unit], timeout=60)
    active = out.strip() == "active"
    return active, f"{unit} is-active={out.strip() or '(空)'}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="有守卫地重启实盘服务 (一次性)")
    ap.add_argument("--dry-run", action="store_true", help="只跑守卫与打印, 不动服务")
    ap.add_argument("--unit", default=UNIT, help=f"systemd 单元 (默认 {UNIT})")
    args = ap.parse_args(argv)

    checks = [guard_worktree_clean(), guard_market_closed(), guard_unit_active(args.unit)]
    print("[restart-guarded] 守卫检查:")
    for (ok, msg) in checks:
        print(f"  {'✓' if ok else '✗'} {msg}")
    failed = [msg for ok, msg in checks if not ok]
    if failed:
        print(f"[restart-guarded] ⏭ 跳过重启 ({len(failed)} 项守卫不满足) — 服务未被动过",
              file=sys.stderr)
        return 4
    if args.dry_run:
        print("[restart-guarded] --dry-run: 守卫全过, 但按要求不执行重启")
        return 0

    print(f"[restart-guarded] 重启 {args.unit} ...")
    rc, out = _run(["systemctl", "restart", args.unit], timeout=180)
    if rc != 0:
        print(f"[restart-guarded] 🚨 systemctl restart 失败 (rc={rc}): {out.strip()[:500]}",
              file=sys.stderr)
        return 3
    rc2, out2 = _run([str(ROOT / "venv" / "bin" / "python"), str(SMOKE), "--wait", "120"], timeout=300)
    print(out2.strip()[-2000:])
    if rc2 != 0:
        print(f"[restart-guarded] 🚨 已重启但冒烟未过 (rc={rc2}) ⇒ 需人工介入", file=sys.stderr)
        return 3
    print(f"[restart-guarded] ✅ {args.unit} 已重启且冒烟通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[restart-guarded] 脚本异常: {exc}", file=sys.stderr)
        sys.exit(2)
