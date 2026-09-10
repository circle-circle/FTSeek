"""
管线 Observer 基础设施 — 横切关注点从主链路抽离

Karpathy 原则: 主管线发事件, Observer 响应 — 互不阻塞。

现役面 (2026-09-10 DEB-20260910 §6 核对后如实登记 — 原表把未接线的 Observer 写成已接线):
    EventBus / PipelineEvent / get_event_bus —— **唯一 live 部分**, 消费方
      app/pipeline/stages/runner.py:134 (`event_bus` property, 由 signal_pipeline.py:1042
      build_default_pipeline 进入)。
    NotificationObserver / LessonRecorder —— 六面零引用 (仓内 import / crontab / systemd /
      Hermes prompt / .git/hooks / _load_script_module 字符串面), 唯一注册入口 setup_observers
      已于本批退役 ⇒ **不可达代码**。本批不删 (超出 §6 预注册范围), 留下一批按减法三步处理;
      在此之前不得据本文件顶部旧表推断"通知/教训链已在跑"。
    AuditLogger (原 :201) —— 本批退役: subscribe("*") 但只写 logger.debug ⇒ LOG_LEVEL=INFO
      下零输出, 且 setup_observers 零调用方 ⇒ 从未注册 (自称"全链路审计"实为零链路)。
    MetricsCollector —— 从未存在于本文件 (旧表误载); 实体在 app/observability/
      metrics_collector.py, 唯一调用方 scripts/archive/run_metrics_collector.py 已归档。

架构:
    Pipeline (emit) ──→ EventBus ──→ [Observer1, Observer2, ...]
                            ↑
                    每个 Observer 独立线程, 异常不影响主管线
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Any, List, Callable, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  EventBus — 轻量级发布/订阅
# ═══════════════════════════════════════════════════════════════════

@dataclass
class PipelineEvent:
    """管线事件"""
    event_type: str                          # "order_submitted" / "position_opened" / etc.
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)


class EventBus:
    """线程安全的轻量级发布/订阅"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable):
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def emit(self, event_type: str, **data):
        event = PipelineEvent(event_type=event_type, data=data)
        subscribers = []
        with self._lock:
            subscribers = list(self._subscribers.get(event_type, []))
            subscribers.extend(self._subscribers.get("*", []))

        for callback in subscribers:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"[EventBus] {event_type} observer 异常: {e}", exc_info=True)


# 全局单例
_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


# ═══════════════════════════════════════════════════════════════════
#  Observer: 通知推送 (钉钉 + 飞书)
# ═══════════════════════════════════════════════════════════════════

class NotificationObserver:
    """
    独立于主管线的通知推送器。

    在事件总线上订阅 order_submitted / order_filled / trade_completed,
    异步推送到钉钉和飞书。推送失败不影响主管线。
    """

    def __init__(self, notifier=None):
        self.notifier = notifier
        self._bus = get_event_bus()
        self._bus.subscribe("order_submitted", self._on_order_submitted)
        self._bus.subscribe("trade_completed", self._on_trade_completed)
        self._bus.subscribe("risk_rejected", self._on_risk_rejected)

    def _on_order_submitted(self, event: PipelineEvent):
        if not self.notifier:
            return
        try:
            data = event.data
            order = data.get("order")
            position = data.get("position")
            voting_result = data.get("voting_result")
            if order and position:
                self.notifier.notify_order_filled(order, position, voting_result=voting_result)
                logger.info(f"[Observer] 下单通知已推送: {data.get('symbol', '?')}")
        except Exception as e:
            logger.error(f"[Observer] 下单通知推送失败: {e}")

    def _on_trade_completed(self, event: PipelineEvent):
        if not self.notifier:
            return
        try:
            data = event.data
            position = data.get("position")
            reason = data.get("exit_reason")
            pnl = data.get("pnl", 0)
            if position and reason:
                self.notifier.notify_exit(position, reason, pnl)
                logger.info(f"[Observer] 平仓通知已推送: {data.get('symbol', '?')} pnl={pnl:.2f}")
        except Exception as e:
            logger.error(f"[Observer] 平仓通知推送失败: {e}")

    def _on_risk_rejected(self, event: PipelineEvent):
        if not self.notifier:
            return
        try:
            data = event.data
            self.notifier.notify_risk_blocked(
                symbol=data.get("symbol", "?"),
                reason=data.get("reason", "unknown"),
            )
        except Exception as e:
            logger.error(f"[Observer] 风控拦截通知失败: {e}")


# ═══════════════════════════════════════════════════════════════════
#  Observer: 教训记录
# ═══════════════════════════════════════════════════════════════════

class LessonRecorder:
    """
    交易完成后自动记录到 lessons DB。

    订阅 trade_completed 事件, 提取:
      - 盈亏 / 持仓时长 / 信号强度
      - 自动分类: premarket_loss / high_signal_loss / preclose_loss
      然后写入 lessons 表供后续盘前注入使用。
    """

    def __init__(self, lesson_injector=None):
        self.lesson_injector = lesson_injector
        self._bus = get_event_bus()
        self._bus.subscribe("trade_completed", self._on_trade_completed)

    def _on_trade_completed(self, event: PipelineEvent):
        data = event.data
        symbol = data.get("symbol", "")
        pnl = data.get("pnl", 0)
        pnl_pct = data.get("pnl_pct", 0)
        holding_hours = data.get("holding_hours", 0)
        signal_strength = data.get("signal_strength", 0)
        direction = data.get("direction", "")

        if not self.lesson_injector:
            return

        try:
            # 分类
            if holding_hours > 5 and pnl < 0:
                self.lesson_injector.save_lesson(
                    category="exit",
                    title=f"{symbol} 持仓 {holding_hours:.1f}h 全程未触发止损/止盈 — 止损参数过宽",
                    description=f"持仓 {holding_hours:.1f}h, 亏损 ${abs(pnl):.2f} ({pnl_pct:.2%})",
                    action_required="收紧止损参数, 启用时间止损",
                    impact_score=abs(pnl),
                    expiry_days=5,
                )
                logger.info(f"[Lesson] {symbol} preclose_loss 已记录")

            if signal_strength >= 60 and pnl < 0:
                self.lesson_injector.save_lesson(
                    category="signal",
                    title=f"高信号强度 (strength={signal_strength}) {symbol} 仍亏损 ${abs(pnl):.2f} — 信号可靠性需验证",
                    description=f"signal_strength={signal_strength}, pnl={pnl:.2f}, pnl_pct={pnl_pct:.2%}",
                    action_required="验证信号管线是否有静默失败",
                    impact_score=abs(pnl),
                    expiry_days=5,
                )
                logger.info(f"[Lesson] {symbol} high_signal_loss 已记录")

        except Exception as e:
            logger.error(f"[Lesson] {symbol} 教训记录失败: {e}")


# ═══════════════════════════════════════════════════════════════════
#  退役区 (2026-09-10, DEB-20260910 §6 议题六 — 两个死审计组件)
# ═══════════════════════════════════════════════════════════════════
# 已删: class AuditLogger (原 :201) + def setup_observers (原 :217)。
# 证据 (六面零引用, 军规 AGENT.md:25 口径 + 本批新增第六面):
#   ① 仓内 import: `rg -n setup_observers --type py` 只有定义处; AuditLogger 只被
#      setup_observers 自己构造 ⇒ 二者互为唯一引用 (闭环死代码)。
#   ② crontab -l / ③ /etc/systemd + /etc/crontab + /etc/cron.d / ④ Hermes prompt
#      (/root/.hermes/cron/jobs.json) / ⑤ .git/hooks / ⑥ scheduler `_load_script_module`
#      字符串面: 全部 0 命中。
# 为何"从未运行": AuditLogger 唯一输出是 logger.debug 且 LOG_LEVEL=INFO ⇒ 即便被注册也零输出;
#   而 setup_observers 零调用方 ⇒ 连注册都没发生过 (自称"全链路审计"实为零链路)。
# 同批退役: app/observability/audit_logger.py (同名不同类, 零消费者; 若被启用会**双写** —
#   FileHandler("logs/audit.log") + propagate 未关 ⇒ 同一 record 再进 root→stdout→主日志;
#   且 retention_days=90 的 _cleanup_expired 只过滤**内存 list**, 不删盘上文件 = 虚假的有界
#   保留承诺) + 其唯一消费者 tests/observability/test_audit_logger.py (只断言内存 list)。
# 减法三步的第②步"配置停"在本例是**空操作**: 两个组件都没有开关, 也从未被构造 ⇒ "停"的状态
#   自始成立且不可变 (无任何代码路径能改变它), 观察一周不会产生新信息 ⇒ 直接进第③步代码退役。
# 注: logs/audit/ 目录与被删的两个类**无关** — 该目录由 scripts/post_market_audit.py:48 与
#   scripts/pre_market_check.py:379 的 FileHandler 写, ④批已补 logrotate 覆盖。
# 回滚: git revert 本批 (或 `git show <本 commit>^:app/pipeline/observers.py`)。
