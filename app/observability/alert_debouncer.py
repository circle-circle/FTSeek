"""告警防抖器 (Alert Debouncer) — 观察期 + 升级推送

Karpathy 原则:
- 独立模块, Overlay 注入, 不改热路径
- 内存态, 重启清空, 无持久化依赖
- 只做核心: 观察期计时 + 重复计数 + 升级触发

使用方式:
    debouncer = AlertDebouncer(
        observation_window_sec=1800,   # 30 分钟观察期
        escalate_threshold=3,          # 重复 ≥3 次升级
        dual_push=True,                # 升级后双推 (钉钉+飞书)
    )

    # 每次告警触发时调用
    debouncer.record(
        alert_type="kline_quota",      # 告警类型标识
        title="K线配额告警",
        content="10/10 标的降级为quote_only",
        level="WARN",
        alerter=alert_manager,         # AlertManager 实例
    )

升级逻辑:
    1. 首次出现 → 记录, 不推送 (静默观察)
    2. 观察期内重复 → 计数 +1
    3. 达到阈值 → 升级为 CRITICAL, 推送告警
    4. 观察期超时 → 自动清零 (问题消失)
"""

import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class AlertRecord:
    """单条告警的观察记录"""
    __slots__ = ['alert_type', 'title', 'content', 'level',
                 'first_seen', 'last_seen', 'count', 'escalated']

    def __init__(self, alert_type: str, title: str, content: str, level: str):
        self.alert_type = alert_type
        self.title = title
        self.content = content
        self.level = level
        self.first_seen = time.time()
        self.last_seen = time.time()
        self.count = 1
        self.escalated = False

    def touch(self, content: Optional[str] = None):
        """更新最后出现时间和计数"""
        self.last_seen = time.time()
        self.count += 1
        if content:
            self.content = content

    @property
    def is_expired(self, window_sec: int = 1800) -> bool:
        return (time.time() - self.last_seen) > window_sec

    @property
    def age_sec(self) -> float:
        return time.time() - self.first_seen


class AlertDebouncer:
    """告警防抖器 — 观察期 + 升级推送"""

    def __init__(
        self,
        observation_window_sec: int = 1800,
        escalate_threshold: int = 3,
    ):
        self.observation_window_sec = observation_window_sec
        self.escalate_threshold = escalate_threshold
        self._records: Dict[str, AlertRecord] = {}

    def record(
        self,
        alert_type: str,
        title: str,
        content: str,
        level: str,
        alerter: Any = None,
        **alerter_kwargs,
    ) -> dict:
        """记录一次告警事件, 返回决策结果

        Returns:
            dict: {action: 'observe' | 'count' | 'escalate', record: AlertRecord}
        """
        now = time.time()

        # 清理过期记录
        self._purge_expired()

        if alert_type not in self._records:
            # 首次出现 → 静默观察
            rec = AlertRecord(alert_type, title, content, level)
            self._records[alert_type] = rec
            logger.info(
                f"[告警观察] 首次记录 '{alert_type}' (level={level}), "
                f"进入 {self.observation_window_sec}s 观察期"
            )
            return {"action": "observe", "record": rec}

        rec = self._records[alert_type]
        rec.touch(content)

        if rec.escalated:
            # 已升级过, 不再重复推送 (等待观察期过期后重置)
            logger.debug(
                f"[告警观察] '{alert_type}' 已升级, 第 {rec.count} 次出现 (静默)"
            )
            return {"action": "silence", "record": rec}

        if rec.count >= self.escalate_threshold:
            # 达到阈值 → 升级推送
            rec.escalated = True
            self._do_escalate(rec, alerter, **alerter_kwargs)
            return {"action": "escalate", "record": rec}

        # 观察期内重复, 未达阈值 → 计数
        logger.info(
            f"[告警观察] '{alert_type}' 第 {rec.count}/{self.escalate_threshold} 次出现 "
            f"(已持续 {rec.age_sec:.0f}s)"
        )
        return {"action": "count", "record": rec}

    def _do_escalate(
        self,
        record: AlertRecord,
        alerter: Any = None,
        **alerter_kwargs,
    ):
        """执行升级推送 — 适配不同 notifier 接口"""
        escalated_content = (
            f"【升级告警】{record.content}\n"
            f"观察期内持续出现 {record.count} 次 "
            f"(首次: {time.strftime('%H:%M:%S', time.localtime(record.first_seen))}, "
            f"最近: {time.strftime('%H:%M:%S', time.localtime(record.last_seen))})\n"
            f"原级别: {record.level} → 升级: CRITICAL"
        )

        if alerter:
            try:
                # 适配不同 notifier 接口
                if hasattr(alerter, 'send_critical'):
                    alerter.send_critical(record.title, escalated_content, **alerter_kwargs)
                elif hasattr(alerter, 'send_alert'):
                    alerter.send_alert("CRITICAL", record.title, escalated_content, **alerter_kwargs)
                elif hasattr(alerter, 'send_text'):
                    # DingTalkNotifier 等简单接口
                    alerter.send_text(f"🔴 {record.title}\n{escalated_content}")
                else:
                    logger.warning(f"[告警升级] 未知 alerter 接口: {type(alerter).__name__}")
                    return
                logger.info(f"[告警升级] 已推送 '{record.title}' (级别=CRITICAL)")
            except Exception as e:
                logger.error(f"[告警升级] 推送失败: {e}")

    def _purge_expired(self):
        """清理过期的观察记录"""
        expired = [
            k for k, v in self._records.items()
            if (time.time() - v.last_seen) > self.observation_window_sec
        ]
        for k in expired:
            rec = self._records.pop(k)
            logger.info(
                f"[告警观察] '{k}' 观察期过期 (持续 {rec.age_sec:.0f}s, "
                f"出现 {rec.count} 次), 已清零"
            )

    def get_active_alerts(self) -> list:
        """获取当前活跃的告警列表 (用于盘后汇总)"""
        self._purge_expired()
        return [
            {
                "type": rec.alert_type,
                "title": rec.title,
                "content": rec.content,
                "level": rec.level,
                "count": rec.count,
                "escalated": rec.escalated,
                "first_seen": rec.first_seen,
                "last_seen": rec.last_seen,
                "duration_sec": rec.age_sec,
            }
            for rec in self._records.values()
        ]

    def reset(self, alert_type: Optional[str] = None):
        """手动重置指定或全部告警记录"""
        if alert_type:
            self._records.pop(alert_type, None)
        else:
            self._records.clear()
