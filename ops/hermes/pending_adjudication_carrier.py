#!/usr/bin/env python3.11
"""待裁决触发通知 (`scripts/pending_adjudication_report.py`) 的 Hermes no_agent 载体 (2026-09-21).

为什么设: 影子注册表有 37 项 record-only 事项, 但裁决面**无读者**——`READY_TO_EVALUATE`
只是"样本量够了"的机械标记, 没人被提醒该裁 (2026-09-21 实测: 19 项挂 READY, 其中多项判据
早已实测不过仍在攒样, 39MB/日的资金流产物无人裁)。判据字典与裁决器
(`app/review/criteria_status.py::pending_adjudications`) 都已实现, 缺的只是**触发人**。

stdout 契约 (静默类, 与报表类载体不同 —— 无变化即不推送):
  · rc == 0 且 stdout 为空 → 静默 (待裁面无变化), 不产生任何推送
  · rc == 0 且有 stdout → 原样投递 (裸告警面里的"有人要看这段")
  · rc != 0  → 先输出一行载体级异常 (含尾部证据), 再投递子进程输出, 并以同 rc 退出
               ⇒ Hermes 另记 failed (通知链自己坏了必须与"没有待裁项"可区分)
  · 超时 → 杀子进程, rc 2; 子脚本缺失 → rc 3 (与告警类载体逐字同契约)
取证面: 子进程输出**追加**到 `logs/pending_adjudication.log`, 另加一行
`[carrier …] rc=… dur=…` 头 (与其它 Hermes 载体同格式, 便于两载体对账)。
回滚: 删 Hermes job + 删本文件与线上副本 (无 crontab 行依赖, 删除即静默)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "pending_adjudication_report.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "pending_adjudication.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "300"))


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                          # noqa: BLE001
        print(f"[pending_adj_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def main() -> int:
    if not CHILD.exists():
        print(f"[pending_adj_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ 待裁面失明 (不得读成'无待裁项')")
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
        _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[pending_adj_carrier] 载体级异常: 通知生成超时 >{TIMEOUT}s (已杀) ⇒ 本期待裁面未知")
        return 2

    _append_log(CHILD_LOG, f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] "
                           f"rc={rc} dur={time.time() - t0:.1f}s lines={len(out.splitlines())}\n{out}")
    if rc != 0:
        tail = " ¶ ".join(ln.strip() for ln in out.splitlines() if ln.strip())[-400:]
        print(f"[pending_adj_carrier] 载体级异常: 通知生成 rc={rc} ⇒ 待裁面不可信 | 尾部: {tail}")
    if out.strip():
        print(out.rstrip())
    return rc


if __name__ == "__main__":
    sys.exit(main())