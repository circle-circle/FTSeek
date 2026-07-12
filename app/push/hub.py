"""PushHub — 统一推送入口 (2026-05-16)

调用方法:
    from app.push import get_push_hub, trade_entry, critical_alert
    hub = get_push_hub()
    hub.push(trade_entry("NVDA", "LONG", 100, 145.50))
    hub.push(critical_alert("RISK_OFF 触发", "回撤超 5%"))

设计:
    push(msg) →
        1. dedup 检查 (60s 同 key 跳过)
        2. is_aggregable? → Aggregator (5min 窗口)
        3. 否则 → 立即发: 按优先级遍历 channels, 任一成功即停
        4. 全部失败 → flag 文件兜底 (仅 HIGH/CRITICAL)

向后兼容:
    旧 send_text("xxx") 仍可用 (通过 push_text helper 兼容).
"""
from __future__ import annotations
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from .message import PushMessage, Level, Category
from .channels import Channel, DingTalkChannel, FeishuChannel, FlagFileChannel
from .aggregator import Aggregator
from .dedup import DedupCache
from .health import HealthMonitor

logger = logging.getLogger(__name__)


class PushHub:
    """统一推送中枢 (单例)"""

    def __init__(
        self,
        channels: Optional[List[Channel]] = None,
        dedup_ttl_sec: int = 60,
        aggregate_window_sec: int = 300,
        enable_health_check: bool = True,
        # R2 (2026-05-16): 异步化 — 主线调用 push() 不阻塞, send 在 thread pool 执行
        async_mode: bool = True,
        async_workers: int = 4,
    ):
        # 默认 channel 优先级: DingTalk > Feishu > FlagFile
        self.channels: List[Channel] = channels or [
            DingTalkChannel(),
            FeishuChannel(),
            FlagFileChannel(),
        ]
        # 排序: priority 升序
        self.channels.sort(key=lambda c: c.priority)
        self._dedup = DedupCache(ttl_sec=dedup_ttl_sec)
        self._aggregator = Aggregator(
            window_sec=aggregate_window_sec,
            # R2: aggregator flush 走 _async_send 防阻塞
            flush_callback=lambda m: self._async_send(m),
        )
        self._aggregator.start()
        self._health = HealthMonitor(self.channels) if enable_health_check else None
        if self._health:
            self._health.start()
        self._lock = threading.Lock()
        # R2: ThreadPool 异步 send (4 worker 足够: dingtalk/feishu/flag 顺序串行)
        self._async_mode = async_mode
        self._executor = ThreadPoolExecutor(
            max_workers=async_workers, thread_name_prefix="PushHub"
        ) if async_mode else None
        # 计数器
        self._sent = 0
        self._deduped = 0
        self._aggregated = 0
        self._failed = 0
        logger.info(
            f"[PushHub] 启动 — channels={[c.name for c in self.channels]} "
            f"dedup_ttl={dedup_ttl_sec}s aggregate_window={aggregate_window_sec}s "
            f"async={async_mode} workers={async_workers}"
        )

    def push(self, msg: PushMessage, sync: bool = False) -> bool:
        """主入口. 返回 True = 已处理 (送达或聚合), False = 全部失败.

        - dedup: 60s 内同 key 跳过
        - 聚合: LOW + aggregate_key → 入桶 (返回 True 表示已处理)
        - 立即: 其他级别 → 按 channel 优先级发送
        - sync=True: 绕过异步与去重/聚合, 同步立即送达并返回真实结果
          (用于关键必达推送, 如盘前指令; 调用方据此决定是否重试)
        """
        try:
            dedup_key = msg.auto_dedup_key()
            if not sync and not self._dedup.should_send(dedup_key):
                with self._lock:
                    self._deduped += 1
                logger.debug(f"[PushHub·dedup] 跳过 key={dedup_key} title={msg.title}")
                return True  # 视为已处理

            if not sync and msg.is_aggregable():
                added = self._aggregator.add(msg)
                if added:
                    with self._lock:
                        self._aggregated += 1
                    logger.debug(f"[PushHub·agg] 入桶 key={msg.aggregate_key} title={msg.title}")
                    return True

            # R2: 异步 fire-and-forget (主线 0 阻塞), 同步模式仅测试用
            if self._async_mode and self._executor is not None and not sync:
                self._async_send(msg)
                return True
            return self._immediate_send(msg)
        except Exception as e:
            logger.warning(f"[PushHub·push] 异常: {e}")
            with self._lock:
                self._failed += 1
            return False

    def push_text(self, text: str, level: Level = Level.NORMAL,
                  category: Category = Category.SYSTEM) -> bool:
        """向后兼容: 旧 send_text(content) 等价调用"""
        return self.push(PushMessage(
            title=text[:60], body=text[60:] if len(text) > 60 else "",
            level=level, category=category,
        ))

    def flush_aggregated(self):
        """立即刷出所有聚合桶 (例: shutdown)"""
        self._aggregator.flush_all()

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "sent": self._sent, "deduped": self._deduped,
                "aggregated": self._aggregated, "failed": self._failed,
                "channels": [{"name": c.name, "available": c.is_available()}
                             for c in self.channels],
                "health": self._health.get_status() if self._health else {},
            }

    def stop(self):
        """关闭流程: flush 聚合 → 停 health → 停 aggregator → 关 executor"""
        try:
            self.flush_aggregated()
            if self._health:
                self._health.stop()
            self._aggregator.stop()
            # R2: executor 等 5s 让在飞的 send 完成
            if self._executor is not None:
                self._executor.shutdown(wait=True, cancel_futures=False)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # 内部
    # ─────────────────────────────────────────────────────────────
    def _async_send(self, msg: PushMessage):
        """R2 (2026-05-16): 异步提交 send 到线程池, 主线不阻塞"""
        try:
            self._executor.submit(self._immediate_send, msg)
        except Exception as e:
            # executor 关闭后 submit 异常 — 降级同步
            logger.debug(f"[PushHub·async] submit 失败, 降级同步: {e}")
            try:
                self._immediate_send(msg)
            except Exception:
                pass

    def _immediate_send(self, msg: PushMessage) -> bool:
        """按优先级遍历 channels, 任一成功即停; 全失败时 flag 文件兜底"""
        sent_count = 0
        # CRITICAL 走全通道 (主+备同推), 其他走主 channel 优先
        broadcast_all = msg.level == Level.CRITICAL
        for ch in self.channels:
            try:
                if not ch.is_available():
                    continue
                if isinstance(ch, FlagFileChannel):
                    # flag 文件仅在前面失败且 HIGH/CRITICAL 时才写
                    if sent_count == 0 and msg.is_critical():
                        ch.send(msg)
                    continue
                ok = ch.send(msg)
                if ok:
                    sent_count += 1
                    if not broadcast_all:
                        break
            except Exception as e:
                logger.debug(f"[PushHub] channel {ch.name} 异常: {e}")

        with self._lock:
            if sent_count > 0:
                self._sent += 1
            else:
                self._failed += 1

        if sent_count == 0:
            logger.warning(
                f"[PushHub] 所有 channel 失败! title={msg.title} level={msg.level.value}"
            )
        return sent_count > 0


# ─── 单例 ───
_singleton: Optional[PushHub] = None
_singleton_lock = threading.Lock()


def get_push_hub() -> PushHub:
    """获取全局 PushHub 单例 — yaml 配置可控"""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            try:
                from app.core.config_container import get_config as _gc
                cfg = _gc()
                dedup_ttl = int(cfg.get_dotted("push.dedup_ttl_sec", 60))
                agg_window = int(cfg.get_dotted("push.aggregate_window_sec", 300))
                health = bool(cfg.get_dotted("push.health_check", True))
            except Exception:
                dedup_ttl, agg_window, health = 60, 300, True
            _singleton = PushHub(
                dedup_ttl_sec=dedup_ttl,
                aggregate_window_sec=agg_window,
                enable_health_check=health,
            )
    return _singleton


def reset_push_hub():
    """测试用 — 重置单例"""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            try:
                _singleton.stop()
            except Exception:
                pass
        _singleton = None
