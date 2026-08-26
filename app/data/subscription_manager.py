"""
FTSeek-US 分时段订阅管理器 — 协议驱动、低耦合

架构：
  ISubscriptionGateway (协议)    ← 抽象边界
       ↑
  FutuLV3Adapter                 ← 适配器 (包装 FutuLV3Provider)
       ↑
  SubscriptionManager            ← 纯逻辑 (只依赖协议，不依赖具体类)

  共享 config.market_hours.MarketHours  ← 时段判断 (HK + US 共用)
"""

from __future__ import annotations

import logging
import time
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Set, Callable

# ── 共享时段定义 (FTSeek 和 FTSeek-US 共用) ──
# Via symlink: app/shared → ../../FTSeek/app/shared
from app.shared.market_hours import MarketHours, MarketType, get_market_phase

# ── 项目统一 ET 时区工具 (Task #32, 2026-06-16) ──
# 用于 stop_auto_loop 时段保护 / watchdog 时间判断 — 必须使用 ET，
# 避免服务器本地时区或 UTC 误判（参考记忆: 美东时间ET在数据管道中的统一应用）
try:
    from app.utils.timezone import now_et
except Exception:  # pragma: no cover — 极端降级路径
    import pytz as _pytz
    _ET_TZ = _pytz.timezone("America/New_York")
    def now_et() -> datetime:  # type: ignore
        return datetime.now(_ET_TZ)

logger = logging.getLogger("ftseek_us.subscription_manager")

# Task #38 (2026-06-16): 模块级单例 — 供跨模块上报/读取访问
# (避免循环依赖与各模块重复查找 scheduler)
_last_instance: Optional["SubscriptionManager"] = None


def get_active_subscription_manager() -> Optional["SubscriptionManager"]:
    """返回最后创建的 SubscriptionManager 单例 (Task #38)。

    供 ORB 等信号产生模块上报行情质量 / 读取门控。未初始化返 None。
    """
    return _last_instance


# ============================================================
#  协议层 — 抽象边界
# ============================================================

class ISubscriptionGateway(ABC):
    """
    订阅网关协议 — SubscriptionManager 只依赖此接口。
    """

    @abstractmethod
    def subscribe(self, codes: List[str], sub_types: List[str]) -> bool:
        ...

    @abstractmethod
    def unsubscribe_all(self, codes: List[str], sub_types: List[str]) -> bool:
        ...

    # ── 连接健康接口 (Task #7, 2026-06-02) ──
    # 下面几个接口提供默认实现，避免现有适配器被迫修改。
    def is_healthy(self) -> bool:
        """底层连接是否健康。默认总是健康 (兼容旧实现)。"""
        return True

    def force_reconnect(self) -> bool:
        """强制重连。默认 no-op。"""
        return False

    # ── 轻量 ping 接口 (Task #38, 2026-06-16) ──
    # 默认委托给 is_healthy()，适配器可覆盖为更轻量的控制面探测 (e.g. get_global_state)。
    def ping(self) -> bool:
        """轻量存活探测。默认以 is_healthy() 作为 fallback。"""
        try:
            return bool(self.is_healthy())
        except Exception:
            return False


# ============================================================
#  适配器层
# ============================================================

class FutuLV3Adapter(ISubscriptionGateway):
    """
    适配器：将 FutuLV3Provider 包装为 ISubscriptionGateway。
    """

    def __init__(self, provider):
        """
        Args:
            provider: FutuLV3Provider 实例
        """
        self._provider = provider

    def subscribe(self, codes: List[str], sub_types: List[str]) -> bool:
        try:
            return self._provider.subscribe(codes, sub_types=sub_types)
        except Exception as e:
            logger.error(f"[FutuLV3Adapter] subscribe 失败: {e}")
            return False

    def unsubscribe_all(self, codes: List[str], sub_types: List[str]) -> bool:
        try:
            self._provider.unsubscribe_all(codes, sub_types)
            return True
        except Exception as e:
            logger.error(f"[FutuLV3Adapter] unsubscribe_all 失败: {e}")
            return False

    def is_healthy(self) -> bool:
        """委托给 provider 的 is_healthy() (Task #7)"""
        try:
            fn = getattr(self._provider, "is_healthy", None)
            if callable(fn):
                return bool(fn())
        except Exception as e:
            logger.debug(f"[FutuLV3Adapter] is_healthy 异常: {e}")
        return True  # 未实现 → 默认健康，保持原有行为

    def force_reconnect(self) -> bool:
        """委托给 provider 的 force_reconnect() (Task #7)"""
        try:
            fn = getattr(self._provider, "force_reconnect", None)
            if callable(fn):
                return bool(fn())
        except Exception as e:
            logger.warning(f"[FutuLV3Adapter] force_reconnect 异常: {e}")
        return False

    def ping(self) -> bool:
        """委托给 provider 的 ping() (Task #38, 2026-06-16)。

        provider 未实现 ping() 时降级为 is_healthy()，保持兼容。
        """
        try:
            fn = getattr(self._provider, "ping", None)
            if callable(fn):
                return bool(fn())
        except Exception as e:
            logger.debug(f"[FutuLV3Adapter] ping 异常: {e}")
        # 降级为 is_healthy()
        return self.is_healthy()


# ============================================================
#  纯逻辑层 — SubscriptionManager (只依赖协议)
# ============================================================

class SubscriptionManager:
    """
    美股分时段订阅管理器 — 纯逻辑，不依赖任何具体 SDK。
    """

    def __init__(self, gateway: ISubscriptionGateway):
        """
        Args:
            gateway: 实现 ISubscriptionGateway 的适配器
        """
        self._gateway = gateway

        # 状态
        self._subscribed_codes: Set[str] = set()
        self._subscribed_types: Set[str] = set()

        # 自动循环
        self._auto_thread: Optional[threading.Thread] = None
        self._auto_running = False
        self._codes_callback: Optional[Callable] = None
        self._check_interval = 60

        # 防抖
        self._last_action_time = 0.0
        self._min_action_interval = 30

        # ── 连接健康跟踪 (Task #7, 2026-06-02) ──
        self._last_gateway_healthy: Optional[bool] = None
        self._unhealthy_since: Optional[float] = None

        # ── Watchdog & 降级标记 (Task #32, 2026-06-16) ──
        # 防止主循环意外退出导致盘中无订阅 / 信号扫描中断
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_running: bool = False
        self._watchdog_interval: int = 60  # 每分钟检查一次
        self._reconnect_failures: int = 0
        self._degraded: bool = False
        self._degraded_reason: Optional[str] = None

        # ── stop_auto_loop 拒绝计数 (Task #10, 2026-06-16) ──
        # 盘中 stop 被拒绝是正常保护行为，不应标记 degraded。
        # 用独立计数 + 时间戳供 Dashboard 展示，避免误报"系统降级"。
        self._stop_rejected_count: int = 0
        self._stop_rejected_last_at: Optional[datetime] = None

        # ── OpenD ping 健康检查 (Task #38, 2026-06-16) ──
        # ping 与 force_reconnect 独立计数:
        #   - ping 连续失败 ≥ _ping_fail_reconnect_threshold → 触发 force_reconnect
        #   - ping 连续失败 ≥ _ping_fail_degraded_threshold → 标记 degraded
        #   - reconnect_failures ≥ _reconnect_fail_recommend_threshold → recommend_opend_restart
        # 参数选型: 60s 检查周期下，2 次失败 = 2 分钟压力后才重连，
        # 5 次 = 5 分钟后标 degraded，与 Dashboard "degraded 持续超 5min 提示" 需求一致。
        self._ping_consecutive_fails: int = 0
        self._ping_fail_reconnect_threshold: int = 2
        self._ping_fail_degraded_threshold: int = 5
        self._reconnect_fail_recommend_threshold: int = 5
        self._degraded_recommend_after_sec: float = 300.0  # 5min
        self._degraded_since: Optional[float] = None
        self._last_ping_ok: Optional[bool] = None

        # ── 行情质量门控 (Task #38, 2026-06-16) ──
        # 行情质量评分 < quality_threshold 时：
        #   - is_data_quality_acceptable() 返 False (供信号管道门控)
        #   - 记录 stale 告警日志
        #   - last_known_good_quote 供 Dashboard 贯穿显示 (标记 stale=True)
        self._quality_threshold: float = 0.5
        self._last_known_good_quote: Optional[Dict] = None
        self._last_quality_score: Optional[float] = None
        self._last_quality_stale: bool = False
        self._stale_alert_count: int = 0

        logger.info("[SubscriptionManager] 美股分时段订阅管理器已初始化")

        # Task #38 (2026-06-16): 注册到模块级单例供跨模块读取
        # (e.g. ORB 引擎上报质量, Dashboard 读取状态)
        global _last_instance
        _last_instance = self

    # ===== 时段判断 (委托给共享 MarketHours) =====

    def should_be_subscribed(self, now: Optional[datetime] = None) -> bool:
        """
        美股是否应该订阅：
        1. 港股已停止自动交易 (2026-06-11) → 不再退订让槽位
        2. 美股盘前/交易 → 订阅
        3. 美股收盘 → 退订
        """
        # 港股已停用，不再因港股时段退订
        return MarketHours.is_us_active(now)

    def is_market_closed(self, now: Optional[datetime] = None) -> bool:
        return MarketHours.is_us_closed(now)

    def is_hk_trading(self, now: Optional[datetime] = None) -> bool:
        # 港股已停止自动交易 (2026-06-11) — 始终返回 False
        return False

    # ===== 增量订阅 =====

    def ensure_subscribed(self, codes: List[str],
                          sub_types: List[str] = None) -> Dict:
        """增量订阅 — 只订阅尚未订阅的部分"""
        if not codes:
            return {"success": False, "new_subs": 0, "already_subscribed": 0}

        if sub_types is None:
            # [ADR-013] 扫描池精简集 (QUOTE+K_1M); 2026-08-10 起动态活跃集 (本方法全部
            #   调用方: 启动订阅 + auto_loop 重订阅) 默认追加 ORDER_BOOK+TICKER —
            #   消费者已就位且性能/方向映射缺陷已修复, 配置键可回滚.
            try:
                from app.data.futu_lv3_provider import _configured_active_set_sub_types
                sub_types = _configured_active_set_sub_types()
            except Exception:
                sub_types = ["QUOTE", "K_1M"]

        if not self._check_cooldown():
            return {"success": False, "new_subs": 0, "already_subscribed": 0}

        futu_codes = [c if c.startswith("US.") else f"US.{c}" for c in codes]
        new_codes = [c for c in futu_codes if c not in self._subscribed_codes]
        existing = [c for c in futu_codes if c in self._subscribed_codes]

        result = {
            "success": True,
            "new_subs": 0,
            "already_subscribed": len(existing),
        }

        if not new_codes:
            return result

        ok = self._gateway.subscribe(new_codes, sub_types)
        if ok:
            result["new_subs"] = len(new_codes)
            self._subscribed_codes.update(new_codes)
            self._subscribed_types.update(set(sub_types))
            self._last_action_time = time.time()
            logger.info(
                f"[US订阅] ✅ +{result['new_subs']} | "
                f"类型={sub_types} | 累计={len(self._subscribed_codes)}"
            )
        else:
            result["success"] = False

        return result

    # ===== 退订 =====

    def ensure_unsubscribed(self, codes: List[str] = None) -> Dict:
        """退订已订阅的标的"""
        if not self._check_cooldown():
            return {"success": False, "unsubscribed": 0}

        target = list(self._subscribed_codes) if codes is None else \
                 [c for c in codes if c in self._subscribed_codes]
        if not target:
            return {"success": True, "unsubscribed": 0}

        types = list(self._subscribed_types) if self._subscribed_types else ["QUOTE", "K_1M"]

        ok = self._gateway.unsubscribe_all(target, types)
        if ok:
            self._subscribed_codes.clear()
            self._last_action_time = time.time()
            logger.info(f"[US退订] ✅ -{len(target)}")

        return {"success": ok, "unsubscribed": len(target) if ok else 0}

    def unsubscribe_all(self) -> Dict:
        return self.ensure_unsubscribed()

    # ===== 自动循环 =====

    def start_auto_loop(self, codes_callback: Callable = None,
                        interval: int = 60):
        """启动自动订阅循环 (并连带启动 watchdog 协程)"""
        if self._auto_running:
            return

        self._codes_callback = codes_callback
        self._check_interval = interval
        self._auto_running = True

        self._auto_thread = threading.Thread(
            target=self._auto_loop, daemon=True,
            name="subscription-us-auto",
        )
        self._auto_thread.start()

        # ── Watchdog 协程随主循环一并启动 (Task #32) ──
        if not self._watchdog_running:
            self._watchdog_running = True
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True,
                name="subscription-us-watchdog",
            )
            self._watchdog_thread.start()
            logger.info(
                f"[SubscriptionManager] 美股 watchdog 已注册 | {self._watchdog_interval}s"
            )

        logger.info(f"[SubscriptionManager] 美股自动循环启动 | {interval}s")

    def stop_auto_loop(self, force: bool = False):
        """停止自动订阅循环。

        Task #32 时段保护 (2026-06-16):
          - 默认 (force=False): 交易时段 09:30-16:00 ET 拒绝执行 stop，
            避免昨日 14:28 ET 盘中循环退出导致 5 个 plan 0% 执行率的故障复现。
          - force=True: 用于运维强制下线，跳过时段保护。
          - 盘后 / 盘前 / 周末: 正常停止 (兼容现有 scheduler.stop() 调用路径)。

        Returns:
            bool: True=已停止 / False=被时段保护拒绝
        """
        # ── 时段保护 ──
        if not force:
            try:
                _now = now_et()
                if MarketHours.is_us_trading_hours(_now):
                    # Task #10 (2026-06-16): 盘中 stop 被拒绝是正常保护行为，
                    # 不应标记 degraded（会导致 Dashboard 误报"系统降级"）。
                    # 改用独立计数 + 时间戳供运维参考。
                    self._stop_rejected_count += 1
                    self._stop_rejected_last_at = _now
                    logger.warning(
                        "[SubscriptionManager] ⛔ 交易时段拒绝 stop_auto_loop "
                        f"| ET={_now.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                        "| 如需强制停止请显式传入 force=True "
                        f"| 累计拒绝 {self._stop_rejected_count} 次"
                    )
                    return False
            except Exception as e:
                # 时段判断异常时降级允许停止，避免反向阻塞下线流程
                logger.error(
                    f"[SubscriptionManager] stop 时段判断异常 (降级允许停止): {e}"
                )

        # ── 优雅关闭 ──
        self._auto_running = False
        self._watchdog_running = False

        if self._auto_thread:
            self._auto_thread.join(timeout=10)
            self._auto_thread = None
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=5)
            self._watchdog_thread = None

        try:
            et_str = now_et().strftime('%Y-%m-%d %H:%M:%S %Z')
        except Exception:
            et_str = "unknown"
        reason = "force=True (运维强制下线)" if force else "盘后正常下线"
        logger.info(
            f"[SubscriptionManager] 美股自动循环停止 | ET={et_str} | reason={reason}"
        )
        return True

    # ===== Watchdog (Task #32, 2026-06-16) =====

    def _watchdog_loop(self):
        """每分钟检查订阅状态：
          1) 主循环线程存活监控 — 已死亡则盘中自动重启
          2) OpenD ping 轻量健康检查 (Task #38, 每 60s) —
             连续 2 次失败 → force_reconnect；连续 5 次 → degraded
          3) 重连失败计数 — 连续失败 ≥ _reconnect_fail_recommend_threshold →
             recommend_opend_restart=True
          4) 底层连接健康 — is_healthy() 作为 ping 补充检查

        与 _auto_loop 内置健康检查互为冗余：主循环负责常规重订阅，
        watchdog 负责"主循环本身挂掉"的极端场景，兼容已有网络异常处理。
        """
        logger.info("[Watchdog] 美股订阅 watchdog 启动")
        # 2026-08-07 启动竞态修复: 启动 90s 宽限 — OpenD/网关连接尚未建立时
        #   健康判定必假阳性 (degraded ERROR → 60s 后自愈的噪音告警, 08-06/08-07 两连发).
        _watchdog_started = time.time()
        while self._watchdog_running:
            try:
                _now = now_et()
                should_sub = self.should_be_subscribed(_now)

                # 1) 主循环线程存活监控 — 仅在应订阅时段重启，避免盘后误启动
                if should_sub and self._auto_running:
                    alive = bool(self._auto_thread and self._auto_thread.is_alive())
                    if not alive:
                        logger.error(
                            "[Watchdog] ⚠️ 主自动循环线程已死亡且仍在交易时段 "
                            f"| ET={_now.strftime('%H:%M')} — 自动重启"
                        )
                        self._auto_thread = threading.Thread(
                            target=self._auto_loop, daemon=True,
                            name="subscription-us-auto",
                        )
                        self._auto_thread.start()

                # 2) OpenD ping 检查已移除 (2026-06-17): 不再实时探测 OpenD 健康状态

                # 3) 连接健康 + 重连失败计数 (原 Task #32 逻辑)
                #   启动 90s 宽限内跳过 (见循环头注释) — 宽限内由主循环负责初始连接.
                if should_sub and (time.time() - _watchdog_started) >= 90:
                    try:
                        healthy = self._gateway.is_healthy()
                    except Exception as e:
                        logger.debug(f"[Watchdog] is_healthy 异常 (忽略): {e}")
                        healthy = True  # 兼容旧 gateway，异常不阻塞

                    if healthy:
                        if self._reconnect_failures > 0:
                            logger.info(
                                f"[Watchdog] ✅ 连接已恢复，清零失败计数 "
                                f"(此前 {self._reconnect_failures} 次)"
                            )
                        self._reconnect_failures = 0
                        # 健康恢复后清除 degraded 标记 (仅自动设置的)
                        self._maybe_clear_degraded()
                    else:
                        # 由 ping 路径 或主循环负责重连, watchdog 仅计数 + 告警
                        try:
                            ok = self._gateway.force_reconnect()
                        except Exception as e:
                            logger.error(f"[Watchdog] force_reconnect 异常: {e}")
                            ok = False

                        if ok:
                            self._reconnect_failures = 0
                            # 重连成功 — 清空本地订阅以便下轮主循环全量同步
                            self._subscribed_codes.clear()
                            self._subscribed_types.clear()
                            logger.info("[Watchdog] ✅ 重连成功，下轮主循环将全量同步订阅")
                            self._maybe_clear_degraded()
                        else:
                            self._reconnect_failures += 1
                            self._maybe_mark_degraded(
                                threshold=3,
                                reason=f"reconnect_failed_x{self._reconnect_failures}",
                                level="reconnect",
                                current=self._reconnect_failures,
                            )
            except Exception as e:
                logger.error(f"[Watchdog] 循环异常 (吞掉避免协程退出): {e}")

            time.sleep(self._watchdog_interval)

        logger.info("[Watchdog] 美股订阅 watchdog 结束")

    # ===== Task #38 (2026-06-16): OpenD ping 健康检查已移除 (2026-06-17) =====
    # 不再实时探测 OpenD 健康状态，由调用方 catch 异常处理

    def _maybe_mark_degraded(self, threshold: int, reason: str, level: str,
                             current: Optional[int] = None) -> None:
        """设置 degraded=True 并记录起始时间。

        Args:
            threshold: 连续失败达到该次数才真正标记 (2026-08-07 修复: 原仅日志装饰,
                第一次失败 x1 即标记 degraded 推告警, 60s 后自愈 — 纯噪音).
            reason: 设置的原因标签 (degraded_reason)。
            level: "ping" / "reconnect" / "manual" — 仅仅为了日志表达。
            current: 当前连续失败计数; 提供且 < threshold 时只告警不标记。
        """
        if current is not None and current < threshold:
            logger.warning(
                f"[Watchdog] {level} 连续失败 {current}/{threshold} ({reason}) — 未达阈值, 暂不标记 degraded"
            )
            return
        # 首次标记 → 记录 _degraded_since
        if not self._degraded:
            self._degraded_since = time.time()
        was_degraded = self._degraded
        self._degraded = True
        self._degraded_reason = reason
        if not was_degraded:
            logger.error(
                f"[Watchdog] ⛔ 标记 degraded=True | level={level} | reason={reason} | "
                f"推送 Dashboard 告警，请人工介入"
            )

    def _maybe_clear_degraded(self) -> None:
        """连接/ping 恢复后清除自动设置的 degraded 标记。

        Task #10 (2026-06-16): stop_rejected_intraday_* 已不再设置 degraded
        (改用 _stop_rejected_count 独立计数)，此处仅处理 reconnect/ping 类。
        """
        if not self._degraded or not self._degraded_reason:
            return
        auto_prefixes = ("reconnect_failed", "opend_ping_failed")
        if self._degraded_reason.startswith(auto_prefixes):
            self._degraded = False
            self._degraded_reason = None
            self._degraded_since = None

    def _auto_loop(self):
        logger.info("[US] 自动订阅循环启动")
        last_state = None

        while self._auto_running:
            try:
                # [2026-08-26 修复·用户批准] 原 datetime.now() 返回服务器本地 CST naive
                #   时间, 被 MarketHours 契约按 ET 解释 → 北京挂钟对 ET 边界, 有效订阅
                #   窗口反转为 CST[04:00,20:00], 每晚美股整个常规盘中(21:30-04:00CST)
                #   被误退订且拒绝重订 (LV3推送消费者盘中全盲, 仅REST轮询兜底)。
                #   2026-08-10 已修 MarketHours 默认路径但本调用方显式传参绕过了修复。
                #   改用本模块统一 now_et() (Task #32, 与 watchdog/stop 保护同源)。
                now = now_et()

                # P1 (2026-05-02): 周末完全静默 — 不检查订阅状态
                # [2026-08-26 修复] 原实现 get_market_phase(now) 对零参函数传参
                #   (scheduler_triggers 版自带正确ET钟+假日历) → 每tick TypeError
                #   被 except 吞掉, 特性静默死亡多时。改无参调用; 别名防遮蔽本模块
                #   顶部 market_hours 同名共享导入。复活安全性: 周五盘后释放发生在
                #   ET 周五 (weekday仍=4), 不落入周末静默窗。
                try:
                    import sys, os
                    _us_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
                    if _us_root not in sys.path:
                        sys.path.insert(0, _us_root)
                    from app.core.scheduler_triggers import (
                        get_market_phase as _st_phase, MarketPhase)
                    phase = _st_phase()
                    if phase == MarketPhase.WEEKEND:
                        time.sleep(self._check_interval * 5)  # 周末延长检查间隔到 5min
                        continue
                except Exception:
                    pass  # 降级: 如果 phase 检测失败，继续正常流程

                should_sub = self.should_be_subscribed(now)

                # ── 健康检查 (Task #7, 2026-06-02): 仅在应订阅时段下检查底层连接 ──
                if should_sub:
                    try:
                        healthy = self._gateway.is_healthy()
                    except Exception as e:
                        logger.error(f"[US] 健康检查异常: {e}")
                        healthy = True  # 异常不阻塞主流程

                    if healthy:
                        if self._last_gateway_healthy is False:
                            duration = (
                                time.time() - self._unhealthy_since
                                if self._unhealthy_since else 0.0
                            )
                            logger.info(
                                f"[US][HEALTH] ✅ 底层连接恢复健康 — 不健康持续 {duration:.1f}s"
                            )
                            self._unhealthy_since = None
                        self._last_gateway_healthy = True
                    else:
                        if self._last_gateway_healthy is not False:
                            self._unhealthy_since = time.time()
                            logger.warning(
                                "[US][HEALTH] ⚠️ 底层 OpenD/Futu 连接不健康 — 触发重连与重订阅"
                            )
                        self._last_gateway_healthy = False

                        # 尝试强制重连（退避保护由 gateway 内部处理）
                        try:
                            reconnected = self._gateway.force_reconnect()
                        except Exception as e:
                            logger.error(f"[US] force_reconnect 异常: {e}")
                            reconnected = False

                        if reconnected:
                            # 重连后 gateway 会自行恢复订阅，清空本地订阅记录以便下轮 ensure_subscribed 全量同步
                            self._subscribed_codes.clear()
                            self._subscribed_types.clear()
                            last_state = None  # 强制下一轮走"进入订阅"分支
                            logger.info("[US][HEALTH] ✅ 重连成功，下轮将重新同步订阅状态")
                        # P2a (2026-06-16): 重连失败时不重置 last_state
                        # 根因: 重连失败 → last_state=None → should_sub≠None → 每分钟重复日志
                        # 修法: 失败时保持 last_state, 仅成功重连时才重置

                if should_sub != last_state:
                    if should_sub and self._codes_callback:
                        codes = self._codes_callback()
                        if codes:
                            logger.info(
                                f"[US] 进入盘前/交易时段，订阅 {len(codes)} 只..."
                            )
                            self.ensure_subscribed(codes)
                    elif not should_sub and self._subscribed_codes:
                        # [2026-08-26 修复·用户批准] 文案诚实化: 原恒称"美股收盘"
                        #   (盘前退订也打"收盘"); 改输出真实 ET phase。now 已是 aware ET。
                        _ph = get_market_phase(MarketType.US, now).value
                        reason = ("港股交易" if self.is_hk_trading(now)
                                  else f"美股非订阅时段(phase={_ph})")
                        logger.info(
                            f"[US] {reason}，退订 {len(self._subscribed_codes)} 只释放..."
                        )
                        self.unsubscribe_all()
                    last_state = should_sub

                # 周期性确认
                if should_sub and self._codes_callback:
                    self.ensure_subscribed(self._codes_callback())

            except Exception as e:
                logger.error(f"[US] 自动循环异常: {e}")

            time.sleep(self._check_interval)

        logger.info("[US] 自动订阅循环结束")

    # ===== 防抖 =====

    def _check_cooldown(self) -> bool:
        return (time.time() - self._last_action_time) >= self._min_action_interval

    # ===== 状态 =====

    @property
    def subscribed_codes(self) -> Set[str]:
        return set(self._subscribed_codes)

    @property
    def subscribed_count(self) -> int:
        return len(self._subscribed_codes)

    def get_status(self) -> Dict:
        # Task #38: 计算推荐重启标记
        # 触发条件:
        #   1) degraded=True 且 持续超 _degraded_recommend_after_sec (默认 5min)
        #   2) reconnect_failures ≥ _reconnect_fail_recommend_threshold (默认 5)
        recommend_restart = False
        degraded_for_sec: Optional[float] = None
        if self._degraded and self._degraded_since:
            degraded_for_sec = max(0.0, time.time() - self._degraded_since)
            if degraded_for_sec >= self._degraded_recommend_after_sec:
                recommend_restart = True
        if self._reconnect_failures >= self._reconnect_fail_recommend_threshold:
            recommend_restart = True

        return {
            "subscribed_codes": list(self._subscribed_codes),
            "subscribed_count": len(self._subscribed_codes),
            "subscribed_types": list(self._subscribed_types),
            "is_market_closed": self.is_market_closed(),
            "is_hk_trading": self.is_hk_trading(),
            "should_be_subscribed": self.should_be_subscribed(),
            "auto_running": self._auto_running,
            # Task #32: Dashboard 据此渲染告警条
            "watchdog_running": self._watchdog_running,
            "degraded": self._degraded,
            "degraded_reason": self._degraded_reason,
            "degraded_since": self._degraded_since,
            "degraded_for_sec": (
                round(degraded_for_sec, 1) if degraded_for_sec is not None else None
            ),
            "reconnect_failures": self._reconnect_failures,
            # Task #10 (2026-06-16): stop_auto_loop 拒绝计数 (非 degraded)
            "stop_rejected_count": self._stop_rejected_count,
            "stop_rejected_last_at": (
                self._stop_rejected_last_at.isoformat()
                if self._stop_rejected_last_at else None
            ),
            # Task #38 (2026-06-16): OpenD ping + 重启推荐 + 行情质量
            "ping_consecutive_fails": self._ping_consecutive_fails,
            "ping_fail_reconnect_threshold": self._ping_fail_reconnect_threshold,
            "ping_fail_degraded_threshold": self._ping_fail_degraded_threshold,
            "last_ping_ok": self._last_ping_ok,
            "recommend_opend_restart": recommend_restart,
            "data_quality_score": self._last_quality_score,
            "data_quality_stale": self._last_quality_stale,
            "data_quality_threshold": self._quality_threshold,
            "stale_alert_count": self._stale_alert_count,
            "last_known_good_quote": (
                dict(self._last_known_good_quote)
                if self._last_known_good_quote else None
            ),
        }

    # ===== Task #38 (2026-06-16): 行情质量门控 =====

    def update_data_quality(
        self,
        score: float,
        snapshot: Optional[Dict] = None,
        symbol: Optional[str] = None,
    ) -> None:
        """上报一次行情质量评分。

        Args:
            score: 质量评分 [0, 1]。
            snapshot: 本次评分对应的行情快照 (K 线 / quote)，可选。
                      质量达阈时会被存为 last_known_good 供 Dashboard 贯穿。
            symbol: 标的代码，仅用于日志。

        质量低于阈值时会记录 stale 告警。供信号管道入口
        is_data_quality_acceptable() 门控。
        """
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            score_f = 0.0
        self._last_quality_score = score_f

        if score_f < self._quality_threshold:
            # P0 修复 (2026-06-18): 只在状态变化时告警，避免每60秒重复告警
            was_stale = self._last_quality_stale
            self._last_quality_stale = True
            self._stale_alert_count += 1
            
            # 只在首次进入 stale 状态时告警，后续静默
            if not was_stale:
                logger.warning(
                    f"[DataQuality] ⚠️ STALE 行情质量低于阈值 "
                    f"| symbol={symbol or '-'} | score={score_f:.2f} "
                    f"< {self._quality_threshold:.2f} | 跳过信号管道 "
                    f"| 累计 stale 告警 {self._stale_alert_count} 次"
                )
            else:
                # 已处于 stale 状态，仅 debug 级别记录
                logger.debug(
                    f"[DataQuality] STALE 持续 | symbol={symbol or '-'} | score={score_f:.2f}"
                )
            # 低质量不覆盖 last_known_good；仅作 stale 补充标记
            return

        # 质量达阈 → 更新 last_known_good 快照
        self._last_quality_stale = False
        if snapshot is not None:
            try:
                snap = dict(snapshot)
                snap.setdefault("_captured_at", time.time())
                snap.setdefault("_quality_score", score_f)
                if symbol:
                    snap.setdefault("_symbol", symbol)
                self._last_known_good_quote = snap
            except Exception:
                pass

    def is_data_quality_acceptable(self, score: Optional[float] = None) -> bool:
        """信号管道入口门控—低于 _quality_threshold 返 False。

        Args:
            score: 本次传入的评分；传 None 时用最近一次上报。

        Returns:
            True: 质量足够，可进入信号管道。
            False: 质量不足，调用方应跳过。
        """
        s = score if score is not None else self._last_quality_score
        if s is None:
            return True  # 未上报过 → 不阻塞 (默认信任上游)
        try:
            return float(s) >= self._quality_threshold
        except (TypeError, ValueError):
            return True

    def get_last_known_good_quote(self) -> Optional[Dict]:
        """返回最后一份质量达阈的行情快照 (供 Dashboard 贯穿)。

        调用方可读取 status["data_quality_stale"] 判断是否需在 UI 上打 stale 标。
        """
        return dict(self._last_known_good_quote) if self._last_known_good_quote else None
