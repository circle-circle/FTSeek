"""主循环**进度**心跳 (2026-11-03, 冻结长轮次豁免).

为什么不复用 `/run/ftseek-heartbeat`:
  那个文件只有一个写入者 —— **出场快循环线程** (每 10s touch)。它回答的是
  "出场监控还活着吗"; freeze_sentinel 的 90s 快层就靠它判冻结。
  11-02 首轮 122s (GC/GIL 形态) 时, 主线程连续持 GIL ⇒ 快循环拿不到 CPU ⇒ 心跳停
  ⇒ SIGKILL。被判死的是"慢", 不是"死"。

本模块写**第二个**文件 (`/run/ftseek-progress-main`, env 可覆盖), 只由**主线程**在
逐条指令 / 逐标的的循环里调用 —— 语义是"有进展", 而不是"活着":
  · 心跳停滞 + 进度新鲜 + 停滞 ≤ 豁免上限  ⇒ 长轮次, 不杀 (告警留痕)
  · 心跳停滞 + 进度也停                   ⇒ 真冻结, 照旧 SIGKILL
  · 心跳停滞 + 进度新鲜但超过豁免上限        ⇒ 照旧杀 (出场线程可能独死, 不能被掩盖)

纪律 (改这个模块前先读):
  1. 绝不在**后台线程**里调 progress_ping —— 那等于把"活着"伪装成"进展", 会掩盖真冻结。
  2. 绝不在可能长时间阻塞的单次调用外面只 ping 一次 —— 只在循环内按迭代 ping。
  3. 一切失败静默降级 (写不出文件不能让主循环挂), 但 must not raise。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认 5s 一次: 哨兵判"进度新鲜"用 45s 窗口, 5s 节流留 9x 余量, 且写文件是 ~µs 级。
_DEFAULT_THROTTLE_S = 5.0

_progress_path = Path(os.environ.get("FTSEEK_PROGRESS_FILE", "/run/ftseek-progress-main"))
_last_ping_ts = 0.0


def _throttle_s() -> float:
    try:
        return float(os.environ.get("FTSEEK_PROGRESS_THROTTLE_S", "") or _DEFAULT_THROTTLE_S)
    except (TypeError, ValueError):
        return _DEFAULT_THROTTLE_S


def progress_ping(force: bool = False) -> bool:
    """主线程推进一格 (节流后 touch 进度文件)。返回是否真的写了。

    绝不抛异常: 进度打点失败只能降级, 不能成为新的故障点 (它处于交易主路径上)。
    """
    global _last_ping_ts
    try:
        now = time.monotonic()
        if not force and (now - _last_ping_ts) < _throttle_s():
            return False
        _last_ping_ts = now
        _progress_path.parent.mkdir(parents=True, exist_ok=True)
        _progress_path.touch()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("[progress] 进度打点失败 (忽略): %s", exc)
        return False