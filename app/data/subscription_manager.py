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
from app.shared.market_hours import MarketHours

logger = logging.getLogger("ftseek_us.subscription_manager")


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
    # 下面两个接口提供默认实现，避免现有适配器被迫修改。
    def is_healthy(self) -> bool:
        """底层连接是否健康。默认总是健康 (兼容旧实现)。"""
        return True

    def force_reconnect(self) -> bool:
        """强制重连。默认 no-op。"""
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

        logger.info("[SubscriptionManager] 美股分时段订阅管理器已初始化")

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
        """启动自动订阅循环"""
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
        logger.info(f"[SubscriptionManager] 美股自动循环启动 | {interval}s")

    def stop_auto_loop(self):
        self._auto_running = False
        if self._auto_thread:
            self._auto_thread.join(timeout=10)
            self._auto_thread = None
        logger.info("[SubscriptionManager] 美股自动循环停止")

    def _auto_loop(self):
        logger.info("[US] 自动订阅循环启动")
        last_state = None

        while self._auto_running:
            try:
                now = datetime.now()

                # P1 (2026-05-02): 周末完全静默 — 不检查订阅状态
                try:
                    import sys, os
                    _us_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
                    if _us_root not in sys.path:
                        sys.path.insert(0, _us_root)
                    from app.core.scheduler_triggers import get_market_phase, MarketPhase
                    phase = get_market_phase(now)
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
                            last_state = None  # 强制下一轮走“进入订阅”分支
                            logger.info("[US][HEALTH] ✅ 重连成功，下轮将重新同步订阅状态")
                        else:
                            # 退避中或重连失败 — 重置 last_state 以免错过后续重新订阅
                            last_state = None

                if should_sub != last_state:
                    if should_sub and self._codes_callback:
                        codes = self._codes_callback()
                        if codes:
                            logger.info(
                                f"[US] 进入盘前/交易时段，订阅 {len(codes)} 只..."
                            )
                            self.ensure_subscribed(codes)
                    elif not should_sub and self._subscribed_codes:
                        reason = "港股交易" if self.is_hk_trading(now) else "美股收盘"
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
        return {
            "subscribed_codes": list(self._subscribed_codes),
            "subscribed_count": len(self._subscribed_codes),
            "subscribed_types": list(self._subscribed_types),
            "is_market_closed": self.is_market_closed(),
            "is_hk_trading": self.is_hk_trading(),
            "should_be_subscribed": self.should_be_subscribed(),
            "auto_running": self._auto_running,
        }
