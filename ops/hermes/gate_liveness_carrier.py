#!/usr/bin/env python3.11
"""门活性探针 (`scripts/gate_liveness_probe.py`) 的 Hermes no_agent 载体 (2026-09-19 用户裁「同意」).

为什么换载体: 该探针是"尺子的尺子" (检测死门/空门/陈旧门), 但旧形态
`crontab 15 7 * * 2-6 … >> logs/gate_liveness.log 2>&1` **无读者**: 全仓可执行体零引用,
不在影子注册表, 不写 DB ⇒ 它自己死了没人会察觉 (2026-09-19 普查结论)。

判据零变更: 子进程仍是 `scripts/gate_liveness_probe.py` 本体 (9 道门判据、C2 命中率翻转、
状态语义 ok/empty/stale/dead/off 逐字未改), 只换载体。

stdout 契约 (与告警类载体相反 —— 这里**输出本体就是产物**, 不是"无事静默"):
  · rc == 0  → 原样投递子进程输出 (13 行: 逐门状态 + 汇总行), 含 dead 计数。
  · rc != 0  → 先输出一行载体级异常 (含尾部证据), 再输出子进程输出, 并以同 rc 退出
               ⇒ Hermes 另记 failed (探针自身崩溃必须与"零死门"可区分)。
  · 超时 → 杀子进程, rc 2; 子脚本缺失 → rc 3。
取证面: 子进程输出**追加**到原 `logs/gate_liveness.log` (与 crontab 载体同文件),
另加一行 `[carrier …] rc=… dur=…` 头, 供两载体对账 (crontab 版不写载体头)。
回滚: 删 Hermes job + 删本文件与线上副本 (crontab 行未动)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "gate_liveness_probe.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "gate_liveness.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "240"))


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                   # noqa: BLE001
        print(f"[gate_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def main() -> int:
    if not CHILD.exists():
        print(f"[gate_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ 死门检测失明 (不得读成'零死门')")
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
        print(f"[gate_carrier] 载体级异常: 探针超时 >{TIMEOUT}s (已杀) ⇒ 本轮死门检测未完成")
        return rc

    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                           f"rc={rc} dur={time.time() - t0:.1f}s\n{out}")
    if rc != 0:
        tail = " ¶ ".join(ln.strip() for ln in out.splitlines() if ln.strip())[-400:]
        print(f"[gate_carrier] 载体级异常: 探针 rc={rc} ⇒ 死门检测结果不可信 | 尾部: {tail}")
    if out.strip():
        print(out.rstrip())
    return rc


if __name__ == "__main__":
    sys.exit(main())