#!/usr/bin/env python3.11
"""元环 findings 生命周期环 (`scripts/meta_audit_loop.py`) 的 Hermes no_agent 载体
(2026-09-20 用户令「按建议执行」= Loop Engineering 试点第 1 个 loop, RFC-091)。

判据零变更: 子进程仍是 `scripts/meta_audit_loop.py` 本体 (只读消费既有探测器产物、
写自己的 STATE.md/findings.json), 载体只负责投递与取证。

stdout 契约 (**静默成功型**, 与门活性探针那种"输出本体即产物"相反):
  · rc == 0 且子进程无输出 → 本载体**也零输出** = 不推送 (状态无变化, 不刷屏)。
  · rc == 0 且有输出       → 原样投递摘要 (状态有变化: 新增/关闭/逾期/探测器停更)。
  · rc != 0                → 先输出一行载体/环级异常 (含尾部证据), 再输出子进程输出, 并以同 rc 退出
                             ⇒ 环自身崩溃必须与"零 finding"可区分 (元环规则 5)。
  · 超时 → 杀子进程, rc 2; 子脚本缺失 → rc 3。
环境变量: META_AUDIT_FORCE=1 透传 `--force-print` (人工查验用; cron 不带)。
取证面: 子进程输出**追加**到 `logs/meta_audit.log`, 另加一行 `[carrier …] rc/dur` 头,
        子进程每跑一轮必落盘 ⇒ "无推送"不等于"没跑"(心跳可外部回读)。
回滚: 删 Hermes job + 删本文件与线上副本 (data/loops/meta_audit/ 可一并删)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "meta_audit_loop.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "meta_audit.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "180"))


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                       # noqa: BLE001
        print(f"[meta_audit_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def main() -> int:
    if not CHILD.exists():
        print(f"[meta_audit_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ findings 归宿环失明")
        return 3
    py = str(PYTHON) if PYTHON.exists() else sys.executable
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    argv = [py, str(CHILD)]
    if os.environ.get("META_AUDIT_FORCE") == "1":
        argv.append("--force-print")
    t0 = time.time()
    try:
        p = subprocess.run(argv, cwd=str(REPO), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=TIMEOUT)
        out, rc = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        rc = 2
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[meta_audit_carrier] 载体级异常: 环超时 >{TIMEOUT}s (已杀) ⇒ 本轮未完成 "
              f"(不得读成'零 finding')")
        return rc

    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                           f"rc={rc} dur={time.time() - t0:.1f}s out_lines={len(out.splitlines())}\n{out}")
    if rc != 0:
        tail = " ¶ ".join(ln.strip() for ln in out.splitlines() if ln.strip())[-400:]
        print(f"[meta_audit_carrier] 载体级异常: 环 rc={rc} ⇒ findings 归宿环自身异常 | 尾部: {tail}")
    if out.strip():
        print(out.rstrip())
    return rc


if __name__ == "__main__":
    sys.exit(main())