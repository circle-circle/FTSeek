"""进程内故障取证开关 (DEB-20261023 freeze-fast-lane).

目的: 进程整体僵死时, 外部哨兵除了 py-spy 之外还能拿到一份**进程内**全线程栈。

- faulthandler.enable(): 致命信号 / 崩溃时把全线程栈写 stderr
  (systemd StandardError=append 到 logs/ftseek_us.log)。
- faulthandler.register(SIGUSR1): 外部哨兵可在不杀进程的前提下要求一次全线程栈快照。

只做取证, 不改变任何交易行为。重复调用幂等。
"""
from __future__ import annotations

import faulthandler
import logging
import signal

logger = logging.getLogger(__name__)

_enabled = False


def enable_fault_diagnostics() -> bool:
    """启用 faulthandler + SIGUSR1 栈快照; 失败只告警, 不影响启动。"""
    global _enabled
    if _enabled:
        return True
    try:
        faulthandler.enable(all_threads=True)
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
        _enabled = True
        logger.info("[Liveness] faulthandler 已启用 (SIGUSR1=全线程栈快照)")
        return True
    except Exception as exc:
        logger.warning("[Liveness] faulthandler 启用失败 (不影响启动): %s", exc, exc_info=True)
        return False
