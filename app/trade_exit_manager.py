"""
交易冷却期管理器 (Trade Cooldown Manager)

2026-04-26 收敛改造：
- 原文件提供大量 stub 方法（check_and_execute_exit / register_protection / _place_mandatory_server_stop 等），
  均为 mock 实现或注释为 "由 data_subscriber 内联处理"。
- 实际离场逻辑已由 DynamicExitManager.check_exit_priority() 统一承担（data_subscriber.py 调用）。
- 实际服务端止损由 TradeExecutor 写入 positions.stop_loss 字段后，由 DynamicExitManager 轮询比较触发。
- 本模块现仅保留"离场后冷却期"功能，避免震荡反复打脸。

保留接口:
- TradeExitManager()                 单例获取
- is_in_cooldown(symbol: str) -> bool
- register_exit_cooldown(symbol: str, market: str, reason: str) -> None
"""

import time
import logging
from typing import Dict

logger = logging.getLogger("ftseek.trade_exit_manager")


class TradeExitManager:
    """交易冷却期管理器（单例）

    仅负责离场后的冷却窗口（禁止短时间内再次开仓同标的），
    不再承担保护单挂单、离场优先级判定等职责。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TradeExitManager, cls).__new__(cls)
            cls._instance._initialized = False
            cls._instance.cooldowns: Dict[str, float] = {}  # symbol -> timestamp
        return cls._instance

    def initialize(self, executor=None, market_data_router=None):
        """兼容旧接口签名，当前仅作幂等初始化标记使用"""
        if self._initialized:
            return
        self._initialized = True
        logger.info("🛡️ TradeExitManager (Cooldown Only) 已初始化")

    # =========================================================================
    #  离场冷却 (Cooldown)
    # =========================================================================

    def register_exit_cooldown(self, symbol: str, market: str, reason: str) -> None:
        """触发离场后，记录冷却时间，防止震荡反复打脸"""
        duration_minutes = 10  # Default
        if market == "HK" and ("CRASH" in reason or "PLUNGE" in reason):
            duration_minutes = 30
        elif "STOP" in reason:
            duration_minutes = 15

        cooldown_until = time.time() + (duration_minutes * 60)
        self.cooldowns[symbol] = cooldown_until
        logger.warning(
            f"⏳ [冷却期] {symbol} 在 {duration_minutes} 分钟内禁止开仓 (原因: {reason})"
        )

    def is_in_cooldown(self, symbol: str) -> bool:
        """检查标的是否处于开仓冷却期"""
        if symbol in self.cooldowns:
            if time.time() < self.cooldowns[symbol]:
                return True
            # 过期清理
            del self.cooldowns[symbol]
        return False
