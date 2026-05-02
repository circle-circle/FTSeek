"""
FTSeek 分时段订阅管理器 — 协议驱动、低耦合

架构：
  ISubscriptionGateway (协议)    ← 抽象边界
       ↑
  MarketSubscriberAdapter        ← 适配器 (包装 MarketSubscriber)
       ↑
  SubscriptionManager            ← 纯逻辑 (只依赖协议，不依赖具体类)

  共享 app.shared.market_hours.MarketHours ← 时段判断 (HK + US 共用, symlink)
"""

from __future__ import annotations

import logging
import time
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Set, Callable

from app.shared.market_hours import MarketHours

logger = logging.getLogger("ftseek.subscription_manager")


# ============================================================
#  协议层 — 抽象边界
# ============================================================

class ISubscriptionGateway(ABC):
    """
    订阅网关协议 — SubscriptionManager 只依赖此接口，不依赖任何具体实现。
    
    实现类负责将通用 subscribe/unsubscribe 调用翻译为具体 SDK 操作。
    """

    @abstractmethod
    def subscribe(self, codes: List[str], sub_types: List[str]) -> bool:
        """
        订阅行情推送
        
        Args:
            codes: Futu 格式代码，如 ["HK.00700"]
            sub_types: 订阅类型，如 ["QUOTE", "K_1M"]
        
        Returns:
            是否成功
        """
        ...

    @abstractmethod
    def unsubscribe_all(self, codes: List[str], sub_types: List[str]) -> bool:
        """
        退订行情推送
        
        Args:
            codes: Futu 格式代码，None 表示退订全部
            sub_types: 订阅类型
        
        Returns:
            是否成功
        """
        ...


# ============================================================
#  适配器层 — 将具体实现包装为协议
# ============================================================

class MarketSubscriberAdapter(ISubscriptionGateway):
    """
    适配器：将 MarketSubscriber 包装为 ISubscriptionGateway。
    
    职责：翻译通用接口调用为 MarketSubscriber 的具体方法。
    """

    def __init__(self, subscriber):
        """
        Args:
            subscriber: MarketSubscriber 实例
        """
        self._subscriber = subscriber

    def subscribe(self, codes: List[str], sub_types: List[str]) -> bool:
        # MarketSubscriber.subscribe() 接受 codes 和从配置读取的 types
        # 这里我们通过覆写配置或直接调用来实现精确类型控制
        from app.core.config_loader import cfg
        old_types = cfg.get("strategy.subscribe_types", ["QUOTE", "K_1M"])
        try:
            # 临时覆写配置以传递 sub_types
            cfg._data.setdefault("strategy", {})["subscribe_types"] = sub_types
            return self._subscriber.subscribe(codes)
        finally:
            # 恢复原始配置
            cfg._data["strategy"]["subscribe_types"] = old_types

    def unsubscribe_all(self, codes: List[str], sub_types: List[str]) -> bool:
        from app.core.config_loader import cfg
        old_types = cfg.get("strategy.subscribe_types", ["QUOTE", "K_1M"])
        try:
            cfg._data.setdefault("strategy", {})["subscribe_types"] = sub_types
        except Exception as e:
            logger.debug(f"[订阅管理] 设置订阅类型失败: {e}")
        except Exception:
            return False
        finally:
            cfg._data["strategy"]["subscribe_types"] = old_types


# ============================================================
#  纯逻辑层 — SubscriptionManager (只依赖协议)
# ============================================================

class SubscriptionManager:
    """
    分时段订阅管理器 — 纯逻辑，不依赖任何具体 SDK 或实现类。
    
    只通过 ISubscriptionGateway 协议与外部交互。
    """

    def __init__(self, gateway: ISubscriptionGateway, market: str = "hk"):
        """
        Args:
            gateway: 实现 ISubscriptionGateway 的适配器
            market: "hk" 或 "us"
        """
        self._gateway = gateway
        self._market = market.lower()

        # 状态跟踪
        self._subscribed_codes: Set[str] = set()
        self._subscribed_types: Set[str] = set()

        # 自动循环
        self._auto_thread: Optional[threading.Thread] = None
        self._auto_running = False
        self._codes_callback: Optional[Callable] = None
        self._check_interval = 60

        # 防抖
        self._last_action_time = 0.0
        self._min_action_interval = 30  # 秒

        logger.info(f"[SubscriptionManager] 初始化 | market={self._market}")

    # ===== 时段判断 (委托给共享 MarketHours) =====

    def should_be_subscribed(self, now: Optional[datetime] = None) -> bool:
        """判断当前是否应该保持订阅"""
        if self._market == "hk":
            return MarketHours.is_hk_active(now)
        return MarketHours.is_us_active(now)

    def is_market_closed(self, now: Optional[datetime] = None) -> bool:
        """当前市场是否已收盘"""
        if self._market == "hk":
            return MarketHours.is_hk_closed(now)
        return MarketHours.is_us_closed(now)

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

        new_codes = [c for c in codes if c not in self._subscribed_codes]
        existing = [c for c in codes if c in self._subscribed_codes]

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

            prefix = self._market.upper()
            logger.info(
                f"[{prefix}订阅] ✅ +{result['new_subs']} | "
                f"类型={sub_types} | 累计={len(self._subscribed_codes)}"
            )
        else:
            result["success"] = False

        return result

    # ===== 退订 =====

    def ensure_unsubscribed(self, codes: List[str] = None,
                            sub_types: List[str] = None) -> Dict:
        """退订已订阅的标的"""
        if not self._check_cooldown():
            return {"success": False, "unsubscribed": 0}

        target = list(self._subscribed_codes) if codes is None else \
                 [c for c in codes if c in self._subscribed_codes]
        if not target:
            return {"success": True, "unsubscribed": 0}

        types = list(self._subscribed_types) if sub_types is None else sub_types

        ok = self._gateway.unsubscribe_all(target, types)
        if ok:
            self._subscribed_codes.clear()
            self._last_action_time = time.time()
            prefix = self._market.upper()
            logger.info(f"[{prefix}退订] ✅ -{len(target)} | 剩余=0")

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
            name=f"subscription-{self._market}-auto",
        )
        self._auto_thread.start()
        logger.info(f"[SubscriptionManager] 自动循环启动 | {self._market} | {interval}s")

    def stop_auto_loop(self):
        self._auto_running = False
        if self._auto_thread:
            self._auto_thread.join(timeout=10)
            self._auto_thread = None
        logger.info(f"[SubscriptionManager] 自动循环停止 | {self._market}")

    def _auto_loop(self):
        """自动循环"""
        prefix = self._market.upper()
        logger.info(f"[{prefix}] 自动订阅循环启动")
        last_state = None

        while self._auto_running:
            try:
                now = datetime.now()
                should_sub = self.should_be_subscribed(now)

                if should_sub != last_state:
                    if should_sub and self._codes_callback:
                        codes = self._codes_callback()
                        if codes:
                            logger.info(
                                f"[{prefix}] 进入交易/盘前时段，"
                                f"订阅 {len(codes)} 只标的..."
                            )
                            self.ensure_subscribed(codes)
                    elif not should_sub and self._subscribed_codes:
                        logger.info(
                            f"[{prefix}] 收盘，退订 {len(self._subscribed_codes)} 只释放槽位..."
                        )
                        self.unsubscribe_all()
                    last_state = should_sub

                # 周期性确认
                if should_sub and self._codes_callback:
                    self.ensure_subscribed(self._codes_callback())

            except Exception as e:
                logger.error(f"[{prefix}] 自动循环异常: {e}")

            time.sleep(self._check_interval)

        logger.info(f"[{prefix}] 自动订阅循环结束")

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

    @property
    def market(self) -> str:
        return self._market

    def get_status(self) -> Dict:
        return {
            "market": self._market,
            "subscribed_codes": list(self._subscribed_codes),
            "subscribed_count": len(self._subscribed_codes),
            "subscribed_types": list(self._subscribed_types),
            "is_market_closed": self.is_market_closed(),
            "should_be_subscribed": self.should_be_subscribed(),
            "auto_running": self._auto_running,
        }
