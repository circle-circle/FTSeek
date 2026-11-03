"""轮次单步计时 (2026-11-03 开盘卡顿归因批).

## 为什么需要

11-02 09:30 ET 那轮 `pre_market` 跑了 **122s**, 但日志里 22:28:14→22:30:01 **一行都没有** —
`_llm_strategy_loop` 里各步骤是顺序同步调用, 慢在哪一步只能靠猜, 而猜测已经导致过一次
错归因 (把整轮 122s 说成"重数据采集", 实际未证)。

本模块只做一件事: 给可疑的同步步骤套一层计时, 超过阈值就把**步骤名+耗时**打进日志,
让"开盘卡在谁身上"从猜变成读。零返回值/零执行路径改动 (只包一层计时 + 日志)。

阈值 (env 可调):
  FTSEEK_PHASE_INFO_S   默认 5s   — ≥ 此值打 INFO
  FTSEEK_PHASE_WARN_S   默认 30s  — ≥ 此值打 WARNING (同轮后续步骤, 含入场执行, 都在这后面排队)

读法: 一轮里所有 `[轮次计时]` 行相加 ≈ 该轮总耗时; 最长的那条就是长尾步骤。
"""
from __future__ import annotations

import functools
import logging
import os
import time

logger = logging.getLogger(__name__)

_DEFAULT_INFO_S = 5.0
_DEFAULT_WARN_S = 30.0


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "")
        return float(raw) if raw != "" else default
    except (TypeError, ValueError):
        return default


def timed_phase(label: str):
    """装饰器: 计时某个同步步骤, 超阈值写日志 (不改变返回值/异常语义)。"""

    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    dt = time.perf_counter() - t0
                    info_s = _env_float("FTSEEK_PHASE_INFO_S", _DEFAULT_INFO_S)
                    warn_s = _env_float("FTSEEK_PHASE_WARN_S", _DEFAULT_WARN_S)
                    if warn_s > 0 and dt >= warn_s:
                        logger.warning(
                            "[轮次计时] %s %.1fs ≥ %.0fs — 单步长尾: 同轮后续步骤 (含入场执行) 全被它排队",
                            label, dt, warn_s,
                        )
                    elif info_s > 0 and dt >= info_s:
                        logger.info("[轮次计时] %s %.1fs", label, dt)
                except Exception:  # 计时不能成为新的故障点
                    pass

        return _wrapped

    return _decorator