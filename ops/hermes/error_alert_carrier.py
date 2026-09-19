#!/usr/bin/env python3.11
"""FTSeek ERROR/CRITICAL 告警链的 **Hermes no_agent 载体**旁路 (RFC-087 §15 试点).

背景 (为什么存在): `scripts/error_alert.py` 的旧载体是系统 crontab 四行 `*/15 … >> logs/error_alert.log 2>&1`。
crontab 载体有三处结构性缺口, 均在本项目有实证前科:
  1. **无执行账本** — 跑没跑、几点跑的, 只有日志文件里有没有新行可推;
  2. **失败不可见** — 脚本自述 `exit 1 = 检测器/推送异常`, 但 crontab 只把它写进日志尾部,
     无人读 ⇒ "守卫自己坏了"只能靠人事后发现 (同类: INCIDENT-001 连崩 7 轮零告警);
  3. **无运行上下文** — 每轮"看到几个文件、几个 ERROR、阈值多少"只在日志正文里, 不可检索。
Hermes no_agent 载体补的正是这三件 (executions.db 对账 / 非零退出→告警投递 / 每次跑一条会话)。

本旁路**只换载体, 不换判据** (一次一个变量): 子进程仍是 `scripts/error_alert.py` 本身,
阈值、基线、冷却键、状态文件 `data/.alert_state`、告警正文与推送路径 (app/push hub, Level.HIGH)
**逐字未改**; 告警本体仍由 error_alert.py 自己投递 ⇒ 本脚本在正常时**零输出**,
避免与 hub 推送形成双推。

stdout 契约 (Hermes no_agent: 空 stdout = 静默成功; 非零退出 = 失败告警):
  · rc == 0  → 无输出 (静默)。告警若已触发, 已由 error_alert.py 经 push hub 送达。
  · rc != 0  → 输出**一行**载体级异常 (含子进程尾部证据), 并以同一 rc 退出
               ⇒ Hermes 记 failed 并投递该行 (crontab 载体做不到的那一半)。
  · 超时      → 杀子进程, 输出 `timeout` 行, rc 2。子进程缺失 → rc 3。

取证面: 子进程 stdout+stderr 合并后**追加**到 repo `logs/error_alert.log` (与 crontab 载体同文件、
同语义), 另加一行带时间戳/rc 的载体头供对账。该文件被 error_alert.py 自身排除在扫描面外,
不会形成"告警制造告警"反馈环。

用法 (Hermes job: script=error_alert_carrier.py, no_agent):
  python3 error_alert_carrier.py [--timeout 180] [--log PATH] [-- <error_alert.py 参数透传>]
手工核验: `-- --dry-run` 走 error_alert.py 的"算但不推不写状态"路径。
回滚: 删 Hermes job + 恢复 crontab 四行 (作业本 §15 记完整回滚命令)。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "error_alert.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "error_alert.log"
DEFAULT_TIMEOUT = 180
TAIL_CHARS = 400


def _append_log(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                   # noqa: BLE001 — 日志失败不得吞掉判定
        print(f"[error_alert_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def _tail(text: str, n: int = TAIL_CHARS) -> str:
    """压成单行尾部证据: 换行折平, 超长只留末尾 (异常栈的结论在尾部)。"""
    flat = " ¶ ".join(ln.strip() for ln in (text or "").splitlines() if ln.strip())
    return flat[-n:] if len(flat) > n else flat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="error_alert.py 的 Hermes no_agent 载体旁路")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--log", default=str(CHILD_LOG))
    ap.add_argument("child_args", nargs="*", help="透传给 error_alert.py 的参数")
    args = ap.parse_args(argv)

    if not CHILD.exists():
        print(f"[error_alert_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ ERROR 覆盖失明 (不得读成'零错误')")
        return 3
    py = str(PYTHON) if PYTHON.exists() else sys.executable

    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env.setdefault("PYTHONUNBUFFERED", "1")
    t0 = time.time()
    try:
        p = subprocess.run([py, str(CHILD), *args.child_args], cwd=str(REPO), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=args.timeout)
        out, rc = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        rc = 2
        _append_log(Path(args.log), f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                                    f"TIMEOUT >{args.timeout}s (已杀子进程)\n{out}")
        print(f"[error_alert_carrier] 载体级异常: 子进程超时 >{args.timeout}s (已杀) "
              f"⇒ 本轮 ERROR 覆盖未完成 | 尾部: {_tail(out)}")
        return rc

    dur = time.time() - t0
    stamp = f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] rc={rc} dur={dur:.1f}s args={args.child_args or '-'}"
    _append_log(Path(args.log), f"{stamp}\n{out}")

    if rc != 0:
        # Hermes: 非零退出 = 失败告警 (会投递下面这行) —— 正是 crontab 载体缺的那一半可见性
        print(f"[error_alert_carrier] 载体级异常: error_alert.py rc={rc} ({dur:.1f}s) "
              f"⇒ 本轮 ERROR 覆盖可能失明 | 尾部: {_tail(out)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())