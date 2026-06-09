"""下单后副作用统一钩子 (2026-05-16)

设计目的: 把散在 _execute_long/_execute_stock_short 中的 4 个 try/except
(attribution INSERT / mirror_entry / push.notify / thesis_inferer) 合并为单一 hook 点.

不是为性能 (cooldown 30s 已盖过 50ms 同步) — 是为代码结构清晰 + 单一可观测点.

特性:
- 单 ThreadPool (2 worker) — 不爆线程
- 异步 fire-and-forget — 主线 0 阻塞
- 顺序内部维持 (attribution → mirror → push → thesis_async)
- 单一失败日志格式
- 失败统计可后续加 metric

用法:
    from app.pipeline.post_trade_hook import on_position_opened
    on_position_opened(position, signal, context, scheduler)
    # 主线立即返回, 后台串行执行 4 步
"""
from __future__ import annotations
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 单一线程池 (2 worker 够 — 每笔 hook 串行 50ms, 30s cooldown 内不会堆积)
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_stats = {"submitted": 0, "completed": 0, "failed": 0}
_stats_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is not None:
        return _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="PostTradeHook")
    return _executor


def submit(symbol: str, hooks: List[Tuple[str, Callable[[], None]]]):
    """提交一组下单后副作用. 主线 0 阻塞.

    Args:
        symbol: 用于日志关联
        hooks: [(name, callable), ...] 按顺序执行, 单个失败不影响后续
    """
    def _run_all():
        for name, fn in hooks:
            try:
                fn()
            except Exception as e:
                logger.warning(f"[PostTradeHook·{name}] {symbol} 失败 (跳过, 不影响其他): {e}")
                with _stats_lock:
                    _stats["failed"] += 1
            else:
                with _stats_lock:
                    _stats["completed"] += 1

    try:
        _get_executor().submit(_run_all)
        with _stats_lock:
            _stats["submitted"] += 1
    except Exception as e:
        # ThreadPool 关闭后, 同步降级 (主线阻塞但不丢数据)
        logger.warning(f"[PostTradeHook·submit] {symbol} 异步提交失败, 同步降级: {e}")
        _run_all()


def on_position_opened(position: Any, signal: Any, context: dict,
                       scheduler: Any) -> None:
    """开仓后所有副作用集中钩子 (主线调用入口)

    顺序 (固定, 保证 trace_id 串行落库):
      1. trade_attribution INSERT (factor_* 5 列 + owner + regime + thesis_tags)
      2. ShadowMirror.mirror_entry (4 variant 镜像)
      3. PushHub.push(trade_entry) — 5min 聚合
      4. infer_thesis_async (LLM 异步推断, 本身已是 async, 这里调用即触发)
    """
    symbol = getattr(signal, "symbol", "?") or getattr(position, "symbol", "?")

    hooks: List[Tuple[str, Callable[[], None]]] = []

    # 1. trade_attribution
    def _hook_attribution():
        import sqlite3
        from pathlib import Path
        from datetime import datetime
        from app.pipeline.order_executor import _insert_trade_attribution
        from app.core.db_utils import get_main_db
        attr_db = get_main_db()
        factor_meta = (getattr(signal, "metadata", {}) or {}).get("_factor_detail", {}) or {}
        owner = getattr(position, "owner_strategy", None) or "main"
        regime_str = ""
        try:
            mr = getattr(position, "market_regime", None)
            regime_str = str(mr.value if hasattr(mr, "value") else mr or "")
        except Exception:
            pass
        session_str = (context or {}).get("session") or (context or {}).get("intraday_session") or ""
        vix_entry = float((context or {}).get("vix", 0) or 0)
        sig_tags = (
            (getattr(signal, "metadata", {}) or {}).get("signal_tags")
            or getattr(signal, "strategy_id", "")
            or ""
        )
        direction_str = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)
        conn = sqlite3.connect(str(attr_db))
        try:
            cur = conn.cursor()
            trace_id = getattr(signal, "trace_id", "") or ""
            cur.execute("SELECT trace_id FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            if not cur.fetchone():
                _insert_trade_attribution(
                    cur, trace_id, symbol,
                    int(getattr(signal, "strength", 0) or 0),
                    float(getattr(signal, "confidence", 0) or 0),
                    factor_detail=factor_meta,
                    owner_strategy=owner,
                    direction=direction_str,
                    entry_time=position.entry_time.isoformat() if position.entry_time else "",
                    entry_price=float(position.avg_entry_price),
                    quantity=int(position.size),
                    regime=regime_str,
                    session=session_str,
                    vix_at_entry=vix_entry,
                    signal_tags=sig_tags,
                )
                conn.commit()
        finally:
            conn.close()
    hooks.append(("attribution", _hook_attribution))

    # 2. ShadowMirror
    def _hook_mirror():
        from app.evolution.shadow_mirror import get_shadow_mirror
        get_shadow_mirror().mirror_entry(
            real_trace_id=getattr(signal, "trace_id", "") or getattr(position, "position_id", ""),
            symbol=symbol,
            direction=signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            entry_price=float(position.avg_entry_price),
            quantity=int(position.size),
            signal_strength=int(getattr(signal, "strength", 0) or 0),
            signal_confidence=float(getattr(signal, "confidence", 0) or 0),
            owner_strategy=getattr(position, "owner_strategy", None) or "main",
        )
    hooks.append(("mirror", _hook_mirror))

    # 3. PushHub
    def _hook_push():
        from app.push import get_push_hub, trade_entry as _tx_entry
        # _notify_order 中已有 PushHub.push, 此处避免重复 — 仅当上游 _notify_order 缺失时兜底
        # 实际上 _notify_order 走主线通知, hook 走聚合 — 双路过渡期保留
        get_push_hub().push(_tx_entry(
            symbol=symbol,
            direction=signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            qty=int(position.size),
            price=float(position.avg_entry_price),
            strategy=getattr(position, "owner_strategy", None) or "main",
            trace_id=getattr(signal, "trace_id", ""),
        ))
    hooks.append(("push", _hook_push))

    # 4. thesis_inferer (本身已 async, 这里调用即提交另一个线程)
    def _hook_thesis():
        from app.llm.thesis_inferer import infer_thesis_async
        signal_dict = {
            "trace_id": getattr(signal, "trace_id", ""),
            "direction": signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            "strength": int(getattr(signal, "strength", 0) or 0),
            "confidence": float(getattr(signal, "confidence", 0) or 0),
            "factor_detail": getattr(signal, "_factor_detail", {}) or {},
        }
        infer_thesis_async(symbol, signal_dict, context or {})
    hooks.append(("thesis", _hook_thesis))

    submit(symbol, hooks)


def get_stats() -> dict:
    """运行时统计 — 供 daily_review / CLI 查询"""
    with _stats_lock:
        return dict(_stats)


def shutdown(wait_sec: int = 5):
    """优雅关闭 — main.py shutdown 时调"""
    global _executor
    if _executor is None:
        return
    try:
        _executor.shutdown(wait=True, cancel_futures=False)
        _executor = None
    except Exception:
        pass
