"""GC 风暴护栏 + 停顿取证 (2026-11-02 开盘冻结批, [hotfix]).

## 为什么有这个文件

2026-11-02 09:30 ET (EST 首日) 开盘后进程整体僵死约 3 分钟:
  - 22:30:53 出场快循环报 `批量取价超时 (>6s)` — 首个症状;
  - 22:31:00–22:34:38 `logs/ftseek_us.log` **0 行** (全线程静默), 宿主仍活
    (同期 journald 每分钟都有 cron/systemd 记录) ⇒ 冻结是**进程级**, 不是整机;
  - 22:34:01 freeze_sentinel 快速层 (心跳停滞 101s) 取到 py-spy 栈:
    除 futu 网络线程 (`protobuf/internal/python_message.py` 纯 Python 解析, active+gil)
    外全线程 idle;
  - 同刻 SIGUSR1 faulthandler 进程内快照 (logs/ftseek_us.log)
    `Current thread ... Garbage-collecting` @ `scheduler_core.run_forever`。

结论: 冻结 = 单线程长时独占 GIL, 抓到的状态是**正在执行 GC**。
本仓库此前**没有任何 GC 配置或观测** (`grep 'import gc' app/` 为空):
1.45GB 常驻堆 + 默认阈值 (700/10/10 ⇒ 每约 7 万次净分配一次 gen2 全堆扫描)
+ 开盘数据洪峰 (纯 Python protobuf 解析 = 巨量对象churn) ⇒ gen2 风暴。
本模块只做两件事: **削掉风暴的结构性条件** + **让下一次能自我归因**。

## 做了什么 (全部进程级, 零交易语义变更)

1. `install_gc_guard()` — 调阈值 (gen0 700→20000, gen1/gen2 10→25;
   gen2 频率降约 180 倍) + 注册 `gc.callbacks` 计停顿。
2. `freeze_long_lived()` — 启动完成后 `gc.freeze()` 一次: 把长命对象 (imports/配置/
   loader 缓存) 移入永久代, 后续 gen2 不再扫描它们 (CPython 3.7+ 官方手段)。
   **只冻结一次**: 运行时再冻结会把"此刻活着、之后成环的垃圾"变成永不回收 (内存泄漏)。
3. 停顿取证: gen2 开始打一行 INFO (调阈值后 gen2 已很稀疏, 开销可忽略) —
   若再冻结, 冻结前最后一行就是它, 直接给出归因; 单次停顿 > 阈值 (默认 800ms) 打 WARNING。
4. 每 10 分钟一行 `[GC]` 汇总 (各代次数/已回收数/窗口内最长停顿/RSS) —
   本仓库此前无任何堆/RSS 观测, 内存问题只能靠猜。

## 回滚

- 只停观测: `FTSEEK_GC_MONITOR=0`
- 只回阈值与 freeze: `FTSEEK_GC_TUNE=0`
- 全关 (等价改动前): 两者都置 0, 或删 main.py 的两处调用 (本文件无其它依赖方)。
"""
from __future__ import annotations

import gc
import logging
import os
import threading
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_GEN0 = 20000
_DEFAULT_GEN1 = 25
_DEFAULT_GEN2 = 25
_DEFAULT_WARN_MS = 800.0
_DEFAULT_REPORT_S = 600.0
_DEFAULT_RSS_WARN_MB = 2200
_DEFAULT_PROBE_S = 5.0
_DEFAULT_GAP_WARN_S = 15.0
_DEFAULT_TRIM_RSS_MB = 600

_installed = False
_frozen = False
_lock = threading.Lock()

# 嵌套 GC (gen0 内触发 gen1/gen2) 时按栈配对 start/stop
_stack: List[Tuple[int, float]] = []
_window_max_ms = 0.0
_window_max_gen = -1
_window_pauses = 0
_total_pauses = 0
_since_start_max_ms = 0.0


def _env_num(name: str, default, cast=float):
    try:
        raw = os.environ.get(name, "")
        if raw == "":
            return default
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _env_on(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def _rss_mb() -> Optional[float]:
    """当前进程 RSS (MB); 读 /proc/self/status, 失败返回 None (不抛)。"""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def release_free_memory(reason: str = "periodic") -> Optional[Tuple[float, float]]:
    """把 glibc 已 free 的堆页还给 OS (malloc_trim), 返回 (前MB, 后MB)。

    为什么重要 (11-02 冻结的物理环节): 本进程峰值 ~1.4GB, 宿主仅 3.5GB 且 swappiness=0
    (已在最保守档) ⇒ 内核仍把本进程的冷堆页换出; 之后任何一次**全堆 GC** 触碰这些页
    就要从 swap 回读。实测 05:01 一次 gen1 停顿 7259ms 只回收 288 个对象 —— 时间花在
    缺页而不是回收。glibc 默认不把 arena 还给 OS, RSS 峰值过后仍虚高, 放大换出概率。
    定期 trim 把"已释放但未归还"的内存交回, 直接缩小可被换出的冷页集。

    安全: 纯内存归还, 不动任何对象; 失败/非 glibc 平台直接跳过; env 可关。
    """
    if not _env_on("FTSEEK_GC_TRIM", True):
        return None
    before = _rss_mb()
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)          # 0 = 归还所有可归还的空闲页
    except Exception as exc:
        logger.warning("[GC] malloc_trim 不可用 (跳过; 空闲堆无法归还 OS): %s", exc, exc_info=True)
        return None
    after = _rss_mb()
    if before is not None and after is not None and (before - after) >= 20:
        logger.info("[GC] 归还空闲堆 (%s): RSS %.0fMB → %.0fMB (-%.0fMB)",
                    reason, before, after, before - after)
    return (before, after) if before is not None and after is not None else None


def _on_gc(phase: str, info: dict) -> None:
    """gc.callbacks 钩子 — 绝不能抛异常, 也不能在 GC 中做重活。"""
    global _window_max_ms, _window_max_gen, _window_pauses, _total_pauses
    global _since_start_max_ms
    try:
        gen = int(info.get("generation", -1))
        if phase == "start":
            _stack.append((gen, time.perf_counter()))
            # gen2 开始行 = 冻结前的最后一行日志 ⇒ 下一次事故的归因锚点。
            # 调阈值后 gen2 稀疏 (每约 1250 万次净分配), 这行开销可忽略。
            if gen == 2:
                logger.info(
                    "[GC] gen2 全堆扫描开始 (本进程累计停顿 %d 次, 窗口最长 %.0fms, RSS=%.0fMB)",
                    _total_pauses, _window_max_ms, _rss_mb() or -1,
                )
            return
        # phase == "stop"
        if not _stack:
            return
        start_gen, t0 = _stack.pop()
        if start_gen != gen:  # 栈失配 (理论上不会), 不猜配对
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        _total_pauses += 1
        _window_pauses += 1
        if elapsed_ms > _window_max_ms:
            _window_max_ms = elapsed_ms
            _window_max_gen = gen
        if elapsed_ms > _since_start_max_ms:
            _since_start_max_ms = elapsed_ms
        warn_ms = _env_num("FTSEEK_GC_PAUSE_WARN_MS", _DEFAULT_WARN_MS)
        if elapsed_ms >= warn_ms:
            logger.warning(
                "[GC] 停顿 %.0fms ≥ %.0fms (gen=%d, collected=%s, RSS=%.0fMB) — "
                "该线程此间独占 GIL, 其它循环与心跳全部静默",
                elapsed_ms, warn_ms, gen, info.get("collected"), _rss_mb() or -1,
            )
    except Exception as _gc_cb_err:  # 钩子内异常会污染 GC 状态, 但失败要留痕
        logger.warning("[GC] 停顿回调异常 (忽略; GC 停顿取证失灵): %s", _gc_cb_err, exc_info=True)


def _build_stall_message(elapsed: float, interval_s: float,
                         d_collections: List[int], stack_snapshot: list) -> str:
    """停摆 WARNING 文案 (纯函数, 便于契约测试)。

    stack_snapshot = 采样瞬间 gc.callbacks 配对栈 (_stack) 的快照。非空 ⇒ 有 GC 正在进行:
    get_stats() 只计**已完成**回收, 在途 GC 不计入增量, 此时「gen 全 0 ⇒ 非 GC」会说反
    (2026-11-16 23:31 实证: 探针报 gen+0/非 GC, 260ms 后 GC WARNING 实证 20972ms gen1)。
    故在途情形直接归因 GC, 空栈才保留原「非 GC」提示。_stack 存 perf_counter, 在途时长
    亦用 perf_counter 算 (探针主体用 monotonic, 两钟不混)。
    """
    base = (
        "[冻结取证] 采样间隔 %.1fs (预期 %.0fs) — 进程整体停摆约 %.0fs; "
        "期间 GC 次数 gen0=+%d gen1=+%d gen2=+%d, 窗口最长停顿 %.0fms, RSS=%.0fMB; "
        % (elapsed, interval_s, max(0.0, elapsed - interval_s),
           d_collections[0] if len(d_collections) > 0 else -1,
           d_collections[1] if len(d_collections) > 1 else -1,
           d_collections[2] if len(d_collections) > 2 else -1,
           _window_max_ms, _rss_mb() or -1)
    )
    if stack_snapshot:
        gen, start_t = stack_snapshot[-1]
        in_flight_ms = (time.perf_counter() - start_t) * 1000.0
        return base + (
            "停摆期间有 GC 正在进行 (gen=%d, 已 %.0fms) — get_stats() 只计已完成回收, "
            "在途 GC 不计入上面增量, 本次停摆归因 GC (全堆扫描/缺页)" % (gen, in_flight_ms)
        )
    return base + "若 gen 全为 0 则停摆非 GC 所致 (查 C 扩展/长解析/死锁)"


def _stall_probe_loop(interval_s: float, gap_warn_s: float) -> None:
    """整体停摆探针: 自己每 interval_s 唤醒一次, 若实际间隔远大于预期 ⇒ 进程被整体卡住。

    为什么需要它: 11-02 冻结时只有**单点**证据 (py-spy + 一次 SIGUSR1 快照), 无法回答
    "到底是谁独占 GIL 三分钟"。本探针在恢复后立刻能给出 (停顿秒数 + 期间各代 GC 次数增量):
      - 期间 gen2 次数 +N (N>0) ⇒ GC 风暴成立, 归因闭合;
      - 期间 GC 次数 +0        ⇒ 不是 GC, 去查 C 扩展/正则/protobuf 长解析
        (例外: 采样时若有在途 GC, get_stats 尚未计入增量, 由 _build_stall_message 按 _stack 判别);
      两者都是可直接落到代码的结论, 不再是猜测。
    """
    last = time.monotonic()
    last_stats = gc.get_stats()
    while True:
        try:
            time.sleep(interval_s)
            now = time.monotonic()
            elapsed = now - last
            stats = gc.get_stats()
            d_collections = []
            for i, (a, b) in enumerate(zip(stats, last_stats)):
                d_collections.append(int(a.get("collections", 0)) - int(b.get("collections", 0)))
            last, last_stats = now, stats
            if gap_warn_s > 0 and elapsed >= gap_warn_s:
                logger.warning(
                    _build_stall_message(elapsed, interval_s, d_collections, list(_stack))
                )
        except Exception as exc:  # 观测失败不影响交易
            logger.warning("[冻结取证] 探针异常 (忽略; 停摆探针失明): %s", exc, exc_info=True)


def _reset_window() -> None:
    """清零窗口统计 (供 reporter 每个窗口末尾调用, 报告后清零避免"窗口值"其实是启动以来值)。"""
    global _window_max_ms, _window_max_gen, _window_pauses
    _window_max_ms = 0.0
    _window_max_gen = -1
    _window_pauses = 0


def _report_loop(interval_s: float) -> None:
    """周期汇总: 各代次数/已回收/窗口最长停顿/RSS。

    口径: `gc.get_stats()` 的代计数是**进程累计值** (只能靠与上次快照做差得到窗口值),
    故日志里显式标"启动以来"; 只有停顿计数 (_window_*) 是本窗口值 (报告后清零)。
    """
    while True:
        try:
            time.sleep(interval_s)
            stats = gc.get_stats()  # [{'collections','collected','uncollectable'}, ...]
            rss = _rss_mb()
            parts = []
            for i, st in enumerate(stats):
                parts.append(
                    f"gen{i}={st.get('collections', 0)}次/{st.get('collected', 0)}回收"
                )
            logger.info(
                "[GC] %dmin 汇总: 启动以来 %s | 本窗口停顿 %d 次, 最长 %.0fms (gen=%d); "
                "进程启动以来最长 %.0fms | RSS=%.0fMB",
                int(interval_s // 60), " ".join(parts), _window_pauses,
                _window_max_ms, _window_max_gen, _since_start_max_ms,
                rss if rss is not None else -1,
            )
            _reset_window()
            rss_warn = _env_num("FTSEEK_GC_RSS_WARN_MB", _DEFAULT_RSS_WARN_MB, int)
            if rss is not None and rss_warn > 0 and rss >= rss_warn:
                logger.warning(
                    "[GC] RSS %.0fMB ≥ 告警线 %dMB — 堆已过大, gen2 停顿时长随堆线性上升, "
                    "先查缓存/持引用泄漏再谈调参",
                    rss, rss_warn,
                )
            # 峰值过后把空闲堆归还 OS: 缩小被内核换出的冷页集 (换出 → 全堆 GC 时缺页 → 长停顿)
            trim_mb = _env_num("FTSEEK_GC_TRIM_RSS_MB", _DEFAULT_TRIM_RSS_MB, int)
            if rss is not None and trim_mb > 0 and rss >= trim_mb:
                release_free_memory("periodic")
        except Exception as exc:  # 观测失败不影响交易
            logger.warning("[GC] 汇总异常 (忽略; GC 观测汇总失灵): %s", exc, exc_info=True)


def install_gc_guard() -> bool:
    """装 GC 护栏 (幂等): 调阈值 (可选) + 停顿回调 + 周期汇总。

    返回 True 表示至少一项生效。任何异常只告警, 不影响启动。
    """
    global _installed
    with _lock:
        if _installed:
            return True
        ok = False
        try:
            if _env_on("FTSEEK_GC_TUNE", True):
                gen0 = _env_num("FTSEEK_GC_GEN0", _DEFAULT_GEN0, int)
                gen1 = _env_num("FTSEEK_GC_GEN1", _DEFAULT_GEN1, int)
                gen2 = _env_num("FTSEEK_GC_GEN2", _DEFAULT_GEN2, int)
                before = gc.get_threshold()
                gc.set_threshold(gen0, gen1, gen2)
                logger.info(
                    "[GC] 阈值 %s → %s (gen2 全堆扫描频率约降 %dx)",
                    before, gc.get_threshold(),
                    max(1, int((gen0 * gen1 * gen2) /
                               max(1, before[0] * before[1] * before[2]))),
                )
                ok = True
            else:
                logger.info("[GC] 阈值调整已禁用 (FTSEEK_GC_TUNE=0), 保持 %s", gc.get_threshold())

            if _env_on("FTSEEK_GC_MONITOR", True):
                if _on_gc not in gc.callbacks:
                    gc.callbacks.append(_on_gc)
                interval = _env_num("FTSEEK_GC_REPORT_S", _DEFAULT_REPORT_S)
                if interval > 0:
                    t = threading.Thread(
                        target=_report_loop, args=(interval,),
                        daemon=True, name="GcReporter",
                    )
                    t.start()
                # 整体停摆探针 (归因用): 默认 5s 采样, 实际间隔 ≥15s 判停摆
                probe_s = _env_num("FTSEEK_STALL_PROBE_S", _DEFAULT_PROBE_S)
                gap_s = _env_num("FTSEEK_STALL_GAP_WARN_S", _DEFAULT_GAP_WARN_S)
                if probe_s > 0:
                    threading.Thread(
                        target=_stall_probe_loop, args=(probe_s, gap_s),
                        daemon=True, name="StallProbe",
                    ).start()
                ok = True
            else:
                logger.info("[GC] 停顿观测已禁用 (FTSEEK_GC_MONITOR=0)")
            _installed = ok
        except Exception as exc:
            logger.warning("[GC] 护栏安装失败 (不影响启动): %s", exc, exc_info=True)
        return ok


def freeze_long_lived(reason: str = "startup") -> bool:
    """启动完成后冻结长命对象 (只做一次; 重复调用为空操作)。

    必须在所有模块 import / 配置装载 / 缓存预热**之后**、进入主循环**之前**调用。
    """
    global _frozen
    if _frozen:
        return True
    if not _env_on("FTSEEK_GC_TUNE", True):
        return False
    try:
        gc.collect()            # 先收掉启动期的短命垃圾, 不进永久代
        gc.freeze()             # 再把现存对象移入永久代 (不再被扫描/回收)
        _frozen = True
        logger.info(
            "[GC] 长命对象已冻结 (%s): 后续 gen2 不再扫描它 — 1.4GB 级堆的主要停顿来源",
            reason,
        )
        return True
    except Exception as exc:
        logger.warning("[GC] freeze 失败 (不影响运行): %s", exc, exc_info=True)
        return False


def get_status() -> dict:
    """只读状态 (供 dashboard/脚本/测试)。"""
    return {
        "installed": _installed,
        "frozen": _frozen,
        "thresholds": gc.get_threshold(),
        "window_pauses": _window_pauses,
        "window_max_ms": round(_window_max_ms, 1),
        "since_start_max_ms": round(_since_start_max_ms, 1),
        "window_max_gen": _window_max_gen,
        "total_pauses": _total_pauses,
        "rss_mb": _rss_mb(),
    }
