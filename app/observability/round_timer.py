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


def _rss_mb():
    """当前进程 RSS (MB); 读不到返回 None (绝不抛)。"""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def timed_phase(label: str):
    """装饰器: 计时某个同步步骤 + 记录该步 RSS 增量, 超阈值写日志 (不改变返回值/异常语义)。

    为什么加 RSS: 11-02 进程峰值 1.45GB (宿主 3.5GB), 但"谁把堆撑起来的"没有归属 ——
    极可能是某一个批量步骤 (逐标的全量拉取/pandas 合并) 临时膨胀。这里在每步前后读一次
    /proc/self/status 的 VmRSS (两次文件读, 微秒级), 增量 ≥ 阈值就打出来, 直接把
    "哪一步撑大堆" 从猜变成读; 堆大小正是 GC 停顿时长的放大器。

    方向语义 (2026-11-03 复核修正): RSS **上涨** ≥ 阈值 = "台阶" (堆被撑大, 告警);
    RSS **下降** ≥ 阈值 = "释放" (GC / malloc_trim 归还, 非问题) ⇒ 只记 INFO。
    原实现用 abs(delta) 判, 一次堆回落会被误报成"该步长尾, 同轮后续步骤全被它排队" ——
    归因错误且制造噪声。告警文案按"长尾(耗时驱动)"与"台阶(内存驱动)"分列。
    """
    _DEFAULT_RSS_DELTA_MB = 50.0

    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            t0 = time.perf_counter()
            rss_before = _rss_mb()
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    dt = time.perf_counter() - t0
                    rss_after = _rss_mb()
                    info_s = _env_float("FTSEEK_PHASE_INFO_S", _DEFAULT_INFO_S)
                    warn_s = _env_float("FTSEEK_PHASE_WARN_S", _DEFAULT_WARN_S)
                    rss_s = _env_float("FTSEEK_PHASE_RSS_MB", _DEFAULT_RSS_DELTA_MB)
                    delta = None
                    if rss_before is not None and rss_after is not None:
                        delta = rss_after - rss_before
                    grew = delta is not None and rss_s > 0 and delta >= rss_s
                    freed = delta is not None and rss_s > 0 and delta <= -rss_s
                    rss_note = ""
                    if grew or freed:
                        tag = "台阶" if grew else "释放"
                        rss_note = f"; RSS {rss_before:.0f}→{rss_after:.0f}MB ({delta:+.0f}, {tag})"
                    if warn_s > 0 and dt >= warn_s:
                        logger.warning(
                            "[轮次计时] %s %.1fs%s — 单步长尾: 同轮后续步骤 (含入场执行) 全被它排队",
                            label, dt, rss_note,
                        )
                    elif grew:
                        logger.warning(
                            "[轮次计时] %s %.1fs%s — RSS 台阶: 堆被撑大, 全堆 GC 停顿时长的放大器",
                            label, dt, rss_note,
                        )
                    elif freed:
                        logger.info(
                            "[轮次计时] %s %.1fs%s — RSS 释放 (非告警)",
                            label, dt, rss_note,
                        )
                    elif info_s > 0 and dt >= info_s:
                        logger.info("[轮次计时] %s %.1fs", label, dt)
                except Exception as _dt_err:  # 计时不能成为新的故障点, 但失败要留痕
                    logger.warning("[轮次计时] 计时失败 (忽略; 该步耗时未记录): %s", _dt_err, exc_info=True)

        return _wrapped

    return _decorator