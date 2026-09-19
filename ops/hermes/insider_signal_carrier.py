#!/usr/bin/env python3.11
"""内部人信号日报 (`scripts/insider_signal_monitor.py`) 的 Hermes no_agent 载体
(2026-09-19 用户裁「按建议执行」= 方案 A: 入账 + 修 buyers/amount + 每交易日投一行).

为什么换载体 (2026-09-19 普查): 该脚本写 `data/reports/insider_daily_<date>.json`, 而那是
skill 里"缺档=内部人链静默失败"诊断程序的**唯一活体证据** —— 但它自己: 无推送、无读者扫描、
不在台账 (存量基线债务) ⇒ 每交易日的数字**从不到达人眼**, 且缺档只能靠人想起来去 ls。

判据零变更: 子进程仍是 `scripts/insider_signal_monitor.py` 本体 (信号判定/去重/统计口径逐字未改;
本批只修了 buyers/amount 回填 —— 旧实现在 symbol 行写死 0, 现从 provider 行取真实值)。
stdout 契约 (报表类, 输出本体即产物):
  · rc == 0 → 原样投递日报 (11 行: 信号数/标的/buyers/金额/分标的计数)。
  · rc != 0 → 先插一行载体级异常 + 尾部证据, 再投递输出, 并以同 rc 退出。
  · 超时 → rc 2; 子脚本缺失 → rc 3。
取证面: 输出**追加**到原 `logs/insider_monitor.log` (与 crontab 载体同文件) + `[carrier …]` 头行。
并行期: crontab 行 `0 21 * * 1-5` 保留不动。
回滚: 删 Hermes job + 删本文件与线上副本 (crontab 行未动 ⇒ 零中断)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "insider_signal_monitor.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "insider_monitor.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "240"))


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                   # noqa: BLE001
        print(f"[insider_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def main() -> int:
    if not CHILD.exists():
        print(f"[insider_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ 内部人日报缺失 (不得读成'零信号')")
        return 3
    py = str(PYTHON) if PYTHON.exists() else sys.executable
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONUNBUFFERED="1")
    t0 = time.time()
    try:
        p = subprocess.run([py, str(CHILD)], cwd=str(REPO), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=TIMEOUT)
        out, rc = p.stdout or "", p.returncode
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        rc = 2
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[insider_carrier] 载体级异常: 日报超时 >{TIMEOUT}s (已杀) ⇒ 本交易日内幕数字缺失")
        return rc

    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                           f"rc={rc} dur={time.time() - t0:.1f}s\n{out}")
    if rc != 0:
        tail = " ¶ ".join(ln.strip() for ln in out.splitlines() if ln.strip())[-400:]
        print(f"[insider_carrier] 载体级异常: 日报 rc={rc} ⇒ 数字不可信 | 尾部: {tail}")
    if out.strip():
        print(out.rstrip())
    return rc


if __name__ == "__main__":
    sys.exit(main())