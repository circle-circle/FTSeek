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
        from app.notifier.dingtalk import DingTalkNotifier
        return bool(DingTalkNotifier().send_text(content))
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

    DEB-20260910-logging §11 (2026-09-11): 双路过渡**收尾单路化** — 旧路径由"无条件并跑"
    改为**只在 PushHub 未受理时兜底**, 让代码与上面那句 docstring 的"PushHub 失败时…仍跑"
    真正一致 (此前 hub.push() 的返回值被丢弃, 条件从未被判定)。
      成因: 旧路径直连 DingTalkNotifier, **不经 hub 的 60s dedup**, 两条路各送一遍 ⇒
        每条关键告警运维端收 2 条钉钉。实测 9/10 夜: 21:31:25 (pre_market 跨开盘阻塞入场)
        与 21:32:28 (playbook_exit_loop 无心跳) 各 2 条, 相隔 10ms / 1ms —
        一条 PushHub 渲染的 🚨 截断版 + 一条旧路径全文版。
      不丢安全网: CRITICAL 在 hub 侧本就是**全通道广播** (钉钉+飞书) + 每通道 send_retries
        + 全败写同一个 data/critical_alerts.flag 与 data/push_failures.jsonl;
        hub.push(sync=True) 还返回真实送达结果供 sync 调用方判重试。
      同源: 649aa535 (2026-07-17) 已把成交/平仓与 sltp 双推收敛到 hub 单路, 并留下
        "推送三层基建统一至 PushHub" 的后补 ADR — 本函数是那轮统一漏掉的最后一处。
    Returns: async_mode=True → None (hub 受理即返回); async_mode=False → 是否送达 (bool)。
    """
    # P-Push: 先入 hub (CRITICAL 走全通道 + flag 兜底)
    hub_ok = False
    try:
        from app.push import get_push_hub, critical_alert as _crit
        # title 取前 80 字符, body 是剩余 (符合钉钉单条限制)
        _title = content.split("\n")[0][:80] if content else "Critical Alert"
        _body = content[len(_title):] if len(content) > len(_title) else ""
        # async 模式要"主线 0 阻塞" ⇒ hub 异步路径 (受理即 True);
        # sync 模式调用方要"真实送达结果" ⇒ hub 同步路径 (绕过异步与 dedup/聚合)。
        hub_ok = bool(get_push_hub().push(_crit(title=_title, body=_body),
                                          sync=not async_mode))
    except Exception as _hub_err:  # noqa: BLE001 — 兜底路径接管, 不让告警链在此断掉
        # exc_info=True: W6 棘轮口径 — 兜底接管时必须带栈, 否则值班人只看到
        # "hub 受理失败" 却无从判因 (hub 单例构造/配置读取/通道可用性均可能)。
        logger.warning(f"[CriticalNotifier] PushHub 受理失败 → 回退旧多通道路径: {_hub_err}",
                       exc_info=True)

    if hub_ok:
        # hub 已受理/已送达 — 旧路径不再并跑 (否则同渠道重复推送)
        return None if async_mode else True

    # PushHub 未受理 (异常 / 全通道失败) → 旧多通道重试路径兜底, "双重保险"仍成立
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
    """ops 检查: 是否有未送达的关键告警

    DEB-20260910-logging §11.9 (2026-09-11): 判据由"文件存在"改为"**文件有内容**"。
    实测 `data/critical_alerts.flag` 是 2026-05-22 遗留的 **1 字节 `\n`** ⇒ 本函数
    连续 141 天恒 True: 这个哨兵既报不出新的未送达告警 (恒真 = 无信息),
    又让文档里"查看 critical_alerts.flag 是否存在"的人工核对恒假阳。
    两条写入方都只写**有内容**的行 —— `send_critical_sync` 的 `atomic_write_text` 写
    "CRITICAL ALERT NOT DELIVERED" + 正文 + last error; `FlagFileChannel.send` 追加
    "--- ts [level] cat ---" + 正文 ⇒ 空白文件不可能是真告警, 判空白即判无。
    """
    import os
    path = _flag_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return bool(f.read().strip())
    except OSError:
        # 读不动 (权限/瞬时锁) → 保守报"有": 宁可让人多看一眼, 不可漏掉未送达的关键告警
        return True
