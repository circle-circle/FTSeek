"""
关键告警 — 多通道 + 重试 + flag 兜底 (P0-3 2026-05-14)

用法:
    from app.utils.critical_alerts import send_critical
    send_critical("RISK_OFF triggered: drawdown > 3%", level="critical")

设计:
- 重试 5 次, backoff 1/2/4/8/16s
- 每次尝试所有通道 (DingTalk → Feishu → Email)
- 全失败 → 写 data/critical_alerts.flag (Ops 必看)
- 不阻塞主流程 (异步)
"""
from __future__ import annotations
import logging
import threading
import time
from typing import List, Optional

from app.utils.atomic_write import atomic_touch, atomic_write_text

logger = logging.getLogger(__name__)


def _flag_path() -> str:
    try:
        from app.core.paths import get_data_dir
        return str(get_data_dir() / "critical_alerts.flag")
    except Exception:
        return "/tmp/critical_alerts.flag"


def _try_dingtalk(content: str) -> bool:
    try:
        from app.notifier.dingtalk import get_dingtalk_notifier
        notifier = get_dingtalk_notifier()
        if hasattr(notifier, "send_message"):
            return bool(notifier.send_message(content))
    except Exception as e:
        logger.debug(f"[CriticalNotifier] DingTalk fail: {e}")
    return False


def _try_feishu(content: str) -> bool:
    try:
        from app.reports.feishu_push_service import get_feishu_push_service as _gp
        service = _gp()
        if hasattr(service, "send_text"):
            return bool(service.send_text(content))
    except Exception as e:
        logger.debug(f"[CriticalNotifier] Feishu fail: {e}")
    return False


_CHANNELS = [_try_dingtalk, _try_feishu]


def send_critical_sync(content: str, retries: int = 5, backoff_base: float = 1.0) -> bool:
    """
    同步发送 (会阻塞 ~31s 最坏情况). 仅用于真正关键的告警.
    Returns: True 任一通道成功, False 全失败 (此时写 flag 文件)
    """
    last_err = ""
    for attempt in range(retries):
        for ch in _CHANNELS:
            try:
                if ch(content):
                    if attempt > 0:
                        logger.info(f"[CriticalNotifier] 第 {attempt + 1} 次尝试成功 (channel={ch.__name__})")
                    return True
            except Exception as e:
                last_err = str(e)
                continue
        # 全通道失败, 退避
        if attempt < retries - 1:
            time.sleep(backoff_base * (2 ** attempt))

    # 所有重试全失败 → flag 文件 + log ERROR
    try:
        flag = _flag_path()
        atomic_write_text(
            flag,
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] CRITICAL ALERT NOT DELIVERED\n"
            f"Content: {content}\n"
            f"Last error: {last_err}\n",
        )
    except Exception:
        pass
    logger.error(f"[CriticalNotifier] 全 {retries} 次重试失败, 已写 flag 文件. content={content[:200]}")
    return False


def send_critical(content: str, async_mode: bool = True) -> Optional[bool]:
    """
    异步发送 (默认). async_mode=True 立即返回, 后台重试.

    P-Push (2026-05-16): 同步进入 PushHub (含 dedup + flag 兜底), 旧逻辑保留 (双路过渡).
    PushHub 失败时旧 send_critical_sync 仍跑, 双重保险.
    """
    # P-Push: 先入 hub (CRITICAL 走全通道 + flag 兜底)
    try:
        from app.push import get_push_hub, critical_alert as _crit
        # title 取前 80 字符, body 是剩余 (符合钉钉单条限制)
        _title = content.split("\n")[0][:80] if content else "Critical Alert"
        _body = content[len(_title):] if len(content) > len(_title) else ""
        get_push_hub().push(_crit(title=_title, body=_body))
    except Exception:
        pass  # PushHub 失败时旧路径仍工作

    if async_mode:
        threading.Thread(
            target=send_critical_sync,
            args=(content,),
            daemon=True,
            name="CriticalNotifier",
        ).start()
        return None
    return send_critical_sync(content)


def has_pending_critical() -> bool:
    """ops 检查: 是否有未送达的关键告警"""
    import os
    return os.path.exists(_flag_path())
