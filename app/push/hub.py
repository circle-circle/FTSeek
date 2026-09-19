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

from app.core.trade_identity import is_test_symbol

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
        # 3a (2026-07-17): 单通道发送失败后的额外重试次数 (间隔 1s, 在 async worker 内 sleep 不阻塞主线)
        send_retries: int = 1,
        # 2026-09-18 (用户裁「交易类事件双发钉钉+飞书」): 这些 category 的消息**不走"主通道
        #   成功即停"**, 而是广播到全部可用通道 (消除"成交在飞书、平仓在钉钉"的平台盲区)。
        #   只对"钱动"的交易类事件生效, 日报/告警等仍走主通道优先, 避免噪音翻倍。
        #   传 None = 用 yaml push.broadcast_trade_events 的实际值 (默认 True)。
        broadcast_trade_events: Optional[bool] = None,
    ):
        # 默认 channel 优先级: DingTalk > Feishu > FlagFile
        self.channels: List[Channel] = channels or [
            DingTalkChannel(),
            FeishuChannel(),
            FlagFileChannel(),
        ]
        # 排序: priority 升序
        self.channels.sort(key=lambda c: c.priority)
        # 2026-09-18: 交易类双发开关 (默认 True — 用户裁「成交+平仓双发钉钉+飞书」)
        if broadcast_trade_events is None:
            _bte = True
            try:
                from app.core.config_container import get_config as _gc
                _bte = bool(_gc().get_dotted("push.broadcast_trade_events", True))
            except Exception:
                pass
            broadcast_trade_events = _bte
        self._broadcast_trade_events = bool(broadcast_trade_events)
        # 上线可验证: 重启后 grep 本行即可确认双发开关的实际取值 (2026-09-18)
        logger.info(
            f"[PushHub·路由] 交易类(成交/平仓)双发={'on' if self._broadcast_trade_events else 'off'}"
            f" · 其余类别主通道优先: {[c.name for c in self.channels]}"
        )
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
        self._send_retries = max(0, int(send_retries))
        # 计数器
        self._sent = 0
        self._deduped = 0
        self._aggregated = 0
        self._failed = 0
        logger.info(
            f"[PushHub] 启动 — channels={[c.name for c in self.channels]} "
            f"dedup_ttl={dedup_ttl_sec}s aggregate_window={aggregate_window_sec}s "
            f"async={async_mode} workers={async_workers} send_retries={self._send_retries}"
        )

    def push(self, msg: PushMessage, sync: bool = False) -> bool:
        """主入口. 返回 True = 已处理 (送达或聚合), False = 全部失败.

        - dedup: 60s 内同 key 跳过
        - 聚合: LOW + aggregate_key → 入桶 (返回 True 表示已处理)
        - 立即: 其他级别 → 按 channel 优先级发送
        - sync=True: 绕过异步与去重/聚合, 同步立即送达并返回真实结果
          (用于关键必达推送, 如盘前指令; 调用方据此决定是否重试)
        """
        if is_test_symbol((msg.payload or {}).get("symbol")):
            logger.warning(
                f"[PushHub·test-symbol] 拦截测试标的推送: "
                f"{(msg.payload or {}).get('symbol')} title={msg.title}"
            )
            return True
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
                    # DEB-20260903 #110: 已入桶=已处理, 确认占窗防止同事件重复入桶
                    self._dedup.mark_sent(dedup_key)
                    logger.debug(f"[PushHub·agg] 入桶 key={msg.aggregate_key} title={msg.title}")
                    return True

            # R2: 异步 fire-and-forget (主线 0 阻塞), 同步模式仅测试用
            if self._async_mode and self._executor is not None and not sync:
                self._async_send(msg, dedup_key)
                return True
            # DEB-20260903 #110: 同步路径发送成功后才确认占窗 (sync 模式本不走 dedup)
            return self._send_and_mark(msg, dedup_key if not sync else None)
        except Exception as e:
            logger.warning(f"[PushHub·push] 异常: {e}")
            with self._lock:
                self._failed += 1
            return False

    def push_text(self, text: str, level: Level = Level.NORMAL,
                  category: Category = Category.SYSTEM,
                  title: Optional[str] = None) -> bool:
        """向后兼容: 旧 send_text(content) 等价调用

        2026-09-18: 新增 title=(...) 参数供具名标题的多行告警使用; 不传时保持
        旧行为 (前 60 字符作 title, 其余作 body — render_text() 会拼回完整原文,
        内容不丢)。
        """
        if title:
            return self.push(PushMessage(
                title=title, body=text, level=level, category=category,
            ))
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
    def _async_send(self, msg: PushMessage, dedup_key=None):
        """R2 (2026-05-16): 异步提交 send 到线程池, 主线不阻塞"""
        try:
            self._executor.submit(self._send_and_mark, msg, dedup_key)
        except Exception as e:
            # executor 关闭后 submit 异常 — 降级同步
            logger.debug(f"[PushHub·async] submit 失败, 降级同步: {e}")
            try:
                self._send_and_mark(msg, dedup_key)
            except Exception:
                pass

    def _send_and_mark(self, msg: PushMessage, dedup_key=None) -> bool:
        """发送并在成功后确认 dedup 占窗 (DEB-20260903 #110).

        失败时不 mark, 使同 key 重试不会被误判为重复.
        """
        ok = self._immediate_send(msg)
        if ok and dedup_key:
            self._dedup.mark_sent(dedup_key)
        return ok

    def _immediate_send(self, msg: PushMessage) -> bool:
        """按优先级遍历 channels, 任一成功即停; 全失败时 flag 文件兜底"""
        sent_count = 0
        # CRITICAL 走全通道 (主+备同推); 交易类 (成交/平仓) 按 2026-09-18 用户裁决同样全通道
        #   (消除"成交在飞书、平仓在钉钉"的平台盲区); 其余走主 channel 优先.
        broadcast_all = msg.level == Level.CRITICAL or (
            self._broadcast_trade_events
            and msg.category in (Category.TRADE_ENTRY, Category.TRADE_EXIT))
        for ch in self.channels:
            try:
                if not ch.is_available():
                    continue
                if isinstance(ch, FlagFileChannel):
                    # flag 文件仅在前面失败且 HIGH/CRITICAL 时才写
                    if sent_count == 0 and msg.is_critical():
                        ch.send(msg)
                    continue
                ok = self._send_once_with_retry(ch, msg)
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
            logger.error(
                f"[PushHub] 所有 channel 失败! title={msg.title} level={msg.level.value}"
            )
            # NORMAL 及以上失败 durable 落盘 (成交/平仓 async fire-and-forget 不再静默丢失)
            if msg.level != Level.INFO:
                self._record_failure(msg)
        return sent_count > 0

    def _send_once_with_retry(self, ch, msg: PushMessage) -> bool:
        """单通道发送 + 轻量重试 (失败/异常均重试, 间隔 1s). 在 async worker 内执行, 不阻塞主线."""
        import time as _t
        attempts = 1 + self._send_retries
        for i in range(attempts):
            try:
                if ch.send(msg):
                    return True
            except Exception as e:
                logger.debug(f"[PushHub] channel {ch.name} send 异常 (第{i+1}/{attempts}次): {e}")
            if i < attempts - 1:
                _t.sleep(1)
        return False

    def _record_failure(self, msg: PushMessage):
        """全通道失败时追加一条到 data/push_failures.jsonl (ops 可回溯). 绝不抛异常."""
        try:
            import json
            import os
            import time as _t
            try:
                from app.core.paths import get_data_dir
                data_dir = str(get_data_dir())
            except Exception:
                data_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data"
                )
            path = os.path.join(data_dir, "push_failures.jsonl")
            rec = {
                "ts": _t.strftime("%Y-%m-%d %H:%M:%S"),
                "title": msg.title,
                "level": msg.level.value,
                "category": msg.category.value,
                "body": (msg.body or "")[:500],
                "trace_id": msg.trace_id,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[PushHub·_record_failure] 落盘失败 (忽略): {e}")


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
                send_retries = int(cfg.get_dotted("push.send_retries", 1))
            except Exception:
                dedup_ttl, agg_window, health, send_retries = 60, 300, True, 1
            _singleton = PushHub(
                dedup_ttl_sec=dedup_ttl,
                aggregate_window_sec=agg_window,
                enable_health_check=health,
                send_retries=send_retries,
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


class HubTextAdapter:
    """统一文本出口适配器 (2026-09-18 用户裁「统一推送渠道」)。

    用途: 代码里把通知器当对象传递/持有的地方 (`self.notifier = DingTalkNotifier()`
    然后 `self.notifier.send_text(...)`), 保留原调用形状但实际走 PushHub ——
    单点替换即可回收, 不必逐个改散落的 send_text 调用点 (漏改一处就是隐患)。

    默认 level/category 由构造参数决定; 不传则用 PushHub 的 NORMAL/SYSTEM。
    """

    def __init__(self, level: Optional[Level] = None, category: Optional[Category] = None):
        self._level = level
        self._category = category

    def send_text(self, text: str, *_a, **_kw) -> bool:
        return push_text_safe(text, level=self._level, category=self._category)

    def send_markdown(self, title: str, text: str = "", *_a, **_kw) -> bool:
        body = f"{text}" if text else ""
        return push_text_safe(body or title, title=title,
                              level=self._level, category=self._category)

    def notify_system_alert(self, title: str = "", message: str = "",
                            level: str = "WARN", *_a, **_kw) -> bool:
        """兼容 `DingTalkNotifier.notify_system_alert(title, message, level)` 形状。

        level 是旧接口的字符串档位 (INFO/WARN/ERROR/CRITICAL) → 映射到 PushHub Level;
        **逐次调用的档位优先于构造默认** (调用点比对象更清楚本次事件的严重度),
        字符串无法识别时才用构造默认, 都没有则 NORMAL。
        """
        lv = {
            "INFO": Level.INFO, "WARN": Level.HIGH, "WARNING": Level.HIGH,
            "ERROR": Level.CRITICAL, "CRITICAL": Level.CRITICAL,
        }.get(str(level or "").strip().upper())
        if lv is None:
            lv = self._level or Level.NORMAL
        return push_text_safe(f"{message}".strip() or title, title=title or None,
                              level=lv, category=self._category)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<HubTextAdapter level={self._level} category={self._category}>"


def push_text_safe(
    text: str,
    level: Optional[Level] = None,
    category: Optional[Category] = None,
    title: Optional[str] = None,
) -> bool:
    """安全文本推送 — 全库**唯一**的非交易类文本出口 (SSOT)。

    2026-09-18 (用户裁「统一推送渠道」): 回收 14 处绕过 PushHub、直连
    `DingTalkNotifier().send_text()` / `FeishuNotifier().send_text()` 的调用点。
    其中 3 处飞书直连全在延长时段链 ⇒ 同一事件在"盘中→钉钉、盘前/盘后→飞书"
    分流的根因; 且直连调用丢掉了 Hub 的去重/聚合/健康检查/失败兜底。

    语义:
      - 统一走 PushHub 路由 (交易类双发, 其余主通道优先 + 失败降级);
      - **异常永不上抛**: 通知失败绝不允许影响交易/风控主流程 (原直连调用点
        多处用 try/except 裸吞, 这里统一收口)。
    """
    try:
        from .message import Level as _L, Category as _C
        return bool(
            get_push_hub().push_text(
                text,
                level=level or _L.NORMAL,
                category=category or _C.SYSTEM,
                title=title,
            )
        )
    except Exception as e:  # pragma: no cover - 兜底路径
        logger.warning(f"[PushHub·push_text_safe] 推送失败 (不影响主流程): {e}")
        return False
