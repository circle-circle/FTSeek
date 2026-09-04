"""消息聚合器 (2026-05-16)

低优先级 (LOW) 同 aggregate_key 的消息在 5min 窗口内合并为 1 条.
窗口到期后触发 flush, 推送聚合摘要.

设计:
- 按 aggregate_key 分桶 (例: "trade_events" 把开仓+平仓汇总)
- 桶满 N 条 或 窗口到期 触发 flush
- LLM/Plan 等大消息不参与 (NORMAL/HIGH)
"""
from __future__ import annotations
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from .message import PushMessage, Level, Category

logger = logging.getLogger(__name__)


@dataclass
class Bucket:
    """聚合桶"""
    key: str
    messages: List[PushMessage] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)
    attempts: int = 0  # DEB-20260903 #109: flush 重试计数


class Aggregator:
    """5min 窗口聚合器 (线程安全)"""

    def __init__(
        self,
        window_sec: int = 300,
        max_bucket_size: int = 20,
        flush_callback: Callable[[PushMessage], None] = None,
    ):
        self.window_sec = window_sec
        self.max_bucket_size = max_bucket_size
        self._flush_cb = flush_callback or (lambda m: None)
        self._buckets: Dict[str, Bucket] = {}
        self._lock = threading.Lock()
        self._timer_thread: threading.Thread = None
        self._running = False

    def add(self, msg: PushMessage) -> bool:
        """加入聚合. 返回 True = 已入桶 (不立即推送), False = 不聚合直接推送."""
        if not msg.is_aggregable():
            return False
        with self._lock:
            key = msg.aggregate_key or "default"
            bucket = self._buckets.setdefault(key, Bucket(key=key))
            bucket.messages.append(msg)
            # 桶满立即 flush
            if len(bucket.messages) >= self.max_bucket_size:
                self._flush_bucket(key)
        return True

    def start(self):
        """启动后台 flush 线程"""
        if self._running:
            return
        self._running = True
        self._timer_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="PushAggregator"
        )
        self._timer_thread.start()
        logger.info(f"[PushAggregator] 启动 (window={self.window_sec}s)")

    def stop(self):
        self._running = False
        self.flush_all()

    def flush_all(self):
        """立即 flush 所有桶 (例如 shutdown 时)"""
        with self._lock:
            for key in list(self._buckets.keys()):
                self._flush_bucket(key)

    def _flush_loop(self):
        while self._running:
            try:
                time.sleep(min(60, self.window_sec / 5))  # 至少每分钟检查一次
                with self._lock:
                    now = time.time()
                    for key in list(self._buckets.keys()):
                        b = self._buckets[key]
                        if now - b.first_seen >= self.window_sec:
                            self._flush_bucket(key)
            except Exception as e:
                logger.debug(f"[PushAggregator·loop] {e}")

    # DEB-20260903 #109: flush 回调失败重试上限 — 超过则丢弃并 error 留痕
    MAX_FLUSH_RETRY = 3

    def _flush_bucket(self, key: str):
        """合并桶内消息为 1 条聚合消息推送 (必须持有 _lock 调用)"""
        bucket = self._buckets.pop(key, None)
        if not bucket or not bucket.messages:
            return
        try:
            agg_msg = self._build_summary(bucket)
            self._flush_cb(agg_msg)
        except Exception as e:
            # DEB-20260903 #109: 原先 pop 后回调失败仅 warning, 桶内消息永久丢失.
            #   现失败重入桶(保留原消息), 限制重试次数防止无限热循环.
            bucket.attempts += 1
            if bucket.attempts <= self.MAX_FLUSH_RETRY:
                logger.warning(
                    f"[PushAggregator·flush] {key} 失败(第{bucket.attempts}次), 重入桶待重试: {e}")
                # 重置 first_seen 使其下一窗口后才重试, 避免热循环
                bucket.first_seen = time.time()
                self._buckets[key] = bucket
            else:
                logger.error(
                    f"[PushAggregator·flush] {key} 重试 {self.MAX_FLUSH_RETRY} 次仍失败, "
                    f"丢弃 {len(bucket.messages)} 条聚合消息: {e}")

    def _build_summary(self, bucket: Bucket) -> PushMessage:
        """把桶里 N 条交易消息汇总为 1 条 (盈亏 + 标的清单)"""
        msgs = bucket.messages
        entries = [m for m in msgs if m.payload.get("kind") == "entry"]
        exits = [m for m in msgs if m.payload.get("kind") == "exit"]
        total_pnl = sum(m.payload.get("pnl", 0) for m in exits)
        n_entry = len(entries)
        n_exit = len(exits)

        # 标的去重 (最多列前 10)
        symbols = []
        seen = set()
        for m in msgs:
            s = m.payload.get("symbol")
            if s and s not in seen:
                seen.add(s)
                symbols.append(s)
        symbols_str = ", ".join(symbols[:10])
        if len(symbols) > 10:
            symbols_str += f" (+{len(symbols)-10})"

        sign = "+" if total_pnl >= 0 else ""
        title = f"交易聚合 ({n_entry}开 {n_exit}平, {sign}${total_pnl:.0f})"
        body_lines = [
            f"窗口: {self.window_sec}s | 共 {len(msgs)} 笔事件",
            f"标的: {symbols_str}" if symbols else "",
        ]
        # 展开非空平仓明细 (前 5 条)
        if exits:
            body_lines.append("平仓明细:")
            for m in exits[:5]:
                pnl_v = m.payload.get("pnl", 0)
                body_lines.append(
                    f"  • {m.payload.get('symbol','?')} "
                    f"{m.payload.get('direction','?')} "
                    f"{'+' if pnl_v>=0 else ''}${pnl_v:.0f} "
                    f"({m.payload.get('exit_reason','?')})"
                )
            if len(exits) > 5:
                body_lines.append(f"  …还有 {len(exits)-5} 笔")

        return PushMessage(
            title=title,
            body="\n".join(line for line in body_lines if line),
            level=Level.NORMAL,
            category=Category.TRADE_EXIT if n_exit >= n_entry else Category.TRADE_ENTRY,
            dedup_key=f"agg:{bucket.key}:{int(bucket.first_seen)}",
            payload={
                "aggregated_count": len(msgs),
                "total_pnl": total_pnl,
                "n_entry": n_entry,
                "n_exit": n_exit,
                "symbols": symbols,
            },
        )
