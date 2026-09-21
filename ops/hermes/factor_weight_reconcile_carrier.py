#!/usr/bin/env python3.11
"""因子权重对账 (`scripts/factor_weight_reconcile.py`) 的 Hermes no_agent 载体.

为什么设: IC 通道 (data/factor_ic_latest.json → screener 运行时 ±1) / 台账 verdict
(app/playbook/factor_analyzer.classify_factor) / 现金净额 是**三条独立读数**, 而权重由前两条
各自写、**没有一处在对账**——于是「正权重买负证据 / IC 与台账反号 / 罚错了 / 台账分母口径造出的
negative」长期无人知晓 (2026-09-21 审计实测 **7 类**错配)。子脚本是纯只读对账器, 判据见其脚本头与
docs/debates/DEB-20260921-factor-weight-verdict-reconciliation.md。

stdout 契约 (静默类, 与 `pending_adjudication_carrier.py` 同族):
  · 子脚本 rc == 0 且**有输出** → 原样投递 (首次运行/对账面变化时; 这是"有事情"信号)。
  · 子脚本 rc == 0 且**零输出** → 载体也零输出 = 不推送 (对账面无变化)。
  · 子脚本 rc != 0 → 先输出一行载体级异常 (含尾部证据), 再透传子脚本输出, 以同 rc 退出
                       ⇒ Hermes 记 failed (探测失败必须与"无错配"可区分)。
  · 超时 → 杀子进程, rc 2; 子脚本缺失 → rc 3。
取证面: 每轮追加 `logs/factor_weight_reconcile.log` (带 `[carrier …] rc/dur/bytes` 头), 供外部回读
"没推送≠没跑"; 静默轮次在日志里同样留痕 (else 静默与停摆不可区分)。
回滚: 删 Hermes job + 删本文件与线上副本 + 删 state/产物 (零交易行为影响, 子脚本纯只读)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US"))
CHILD = REPO / "scripts" / "factor_weight_reconcile.py"
PYTHON = REPO / "venv" / "bin" / "python"
CHILD_LOG = REPO / "logs" / "factor_weight_reconcile.log"
TIMEOUT = int(os.environ.get("CARRIER_TIMEOUT_S", "240"))


def _append_log(text: str) -> None:
    try:
        CHILD_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CHILD_LOG.open("a", encoding="utf-8", errors="replace") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except Exception as e:                                   # noqa: BLE001
        print(f"[factor_weight_carrier] 取证日志写入失败 ({type(e).__name__}: {e})")


def main() -> int:
    if not CHILD.exists():
        print(f"[factor_weight_carrier] 载体级异常: 子脚本不存在 {CHILD} ⇒ 对账未运行 "
              f"(不得读成『无错配』)")
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
        _append_log(f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] TIMEOUT >{TIMEOUT}s (已杀)\n{out}")
        print(f"[factor_weight_carrier] 载体级异常: 对账超时 >{TIMEOUT}s (已杀) ⇒ 本轮未完成")
        return 2

    dur = time.time() - t0
    _append_log(f"[carrier {datetime.now():%Y-%m-%d %H:%M:%S}] rc={rc} dur={dur:.1f}s "
                f"bytes={len(out)}\n{out}")
    if rc != 0:
        tail = " ¶ ".join(ln.strip() for ln in out.splitlines() if ln.strip())[-400:]
        print(f"[factor_weight_carrier] 载体级异常: 对账 rc={rc} ⇒ 读数不可信 | 尾部: {tail}")
    if out.strip():
        print(out.rstrip())
    return rc


if __name__ == "__main__":
    sys.exit(main())