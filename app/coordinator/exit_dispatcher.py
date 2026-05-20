"""
ExitDispatcher — 按 position.owner_strategy 路由 ExitPolicy (Phase A 2026-05-14)

Phase A: 仅做注册中心 + 路由查询, 不接管 PositionExitMonitor 的扫描循环.
Phase B: 替代 PositionExitMonitor 的"6 规则一刀切"现状.

Tip: 当 owner_strategy 缺失或未注册 → 兜底用 "main" (最严之外的常规策略).
"""
from __future__ import annotations
import logging
import threading
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


class ExitPolicy(Protocol):
    """每个策略的退出规则接口. 各 adapter 提供具体实现."""
    name: str

    def evaluate(self, position: Any, ctx: Any) -> Dict[str, Any]:
        """
        Returns:
            {
                "action": "NO_ACTION" | "REDUCE" | "CLOSE",
                "qty_pct": 0.0-1.0,        # REDUCE 时 0.5 = 减半, CLOSE 时 1.0
                "reason": "DAYTRADE_TP1",  # 退出原因 (写到 trade_records.exit_reason)
                "priority": int,           # 多规则触发时取高
                "metadata": {...},
            }
        """
        ...


class ExitDispatcher:
    """ExitPolicy 注册中心 + 按 owner_strategy 路由"""

    def __init__(self):
        self._policies: Dict[str, ExitPolicy] = {}
        self._lock = threading.Lock()

    def register(self, strategy_name: str, policy: ExitPolicy):
        with self._lock:
            self._policies[strategy_name] = policy
        logger.info(f"[ExitDispatcher] 注册 {strategy_name} → {getattr(policy, 'name', '?')}")

    def policy_for(self, owner_strategy: Optional[str]) -> Optional[ExitPolicy]:
        """根据 position.owner_strategy 取对应 ExitPolicy"""
        with self._lock:
            if not owner_strategy:
                # 兜底: main
                return self._policies.get("main")
            return self._policies.get(owner_strategy) or self._policies.get("main")

    def all_strategies(self) -> Dict[str, str]:
        with self._lock:
            return {s: getattr(p, "name", "?") for s, p in self._policies.items()}


_singleton: Optional[ExitDispatcher] = None


def get_exit_dispatcher() -> ExitDispatcher:
    global _singleton
    if _singleton is None:
        _singleton = ExitDispatcher()
    return _singleton
