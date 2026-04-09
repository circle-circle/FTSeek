"""
冷却管理模块 (Cooldown Manager)
负责信号推送冷却、止损冷却、开仓去重等时间控制逻辑
"""
import logging
import time
import threading
from typing import Dict, Optional
from collections import defaultdict

logger = logging.getLogger("ftseek.data.cooldown")
logger.propagate = False


class CooldownManager:
    """冷却时间管理器"""

    def __init__(self, default_cooldown: int = 120):
        """
        初始化冷却管理器

        Args:
            default_cooldown: 默认冷却时间 (秒)，默认 120 秒
        """
        self.default_cooldown = default_cooldown

        # 推送冷却：code -> last_push_time
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()

        # 止损冷却：code -> last_stop_loss_time
        self._stop_loss_cooldown: Dict[str, float] = {}

        # 止损失败计数：code -> int（用于递增冷却）
        self._stop_loss_fail_count: Dict[str, int] = {}

        # 开仓时间戳：code -> last open timestamp（防止重复开仓）
        self._open_position_ts: Dict[str, float] = {}
        self._open_lock = threading.Lock()

        # 追踪止盈触发时间：code -> timestamp（用于重开仓冷却）
        self._trailing_exit_time: Dict[str, float] = {}

        # 软止损预警冷却：code -> last_warning_timestamp
        self._soft_stop_warnings: Dict[str, float] = {}

        logger.info(f"冷却管理器已初始化 (默认冷却={default_cooldown}s)")

    def set_cooldown(self, cooldown: int) -> None:
        """动态更新冷却时间"""
        self.default_cooldown = cooldown
        logger.info(f"冷却时间已更新为 {cooldown}秒")

    def get_cooldown(self) -> int:
        """获取当前冷却时间"""
        return self.default_cooldown

    def is_cooled_down(self, code: str, cooldown: Optional[int] = None) -> bool:
        """
        检查是否冷却完成

        Args:
            code: 股票代码
            cooldown: 冷却时间 (可选，覆盖默认值)

        Returns:
            True: 冷却完成，可以推送
            False: 仍在冷却中
        """
        now = time.time()
        cooldown_time = cooldown if cooldown is not None else self.default_cooldown

        with self._cooldown_lock:
            last_time = self._cooldown.get(code, 0)
            return (now - last_time) >= cooldown_time

    def update_cooldown(self, code: str) -> None:
        """
        更新冷却时间（推送成功后调用）

        Args:
            code: 股票代码
        """
        now = time.time()
        with self._cooldown_lock:
            self._cooldown[code] = now
            logger.debug(f"[{code}] 冷却时间已更新")

    def cleanup_expired(self) -> None:
        """清理过期的冷却记录"""
        now = time.time()
        with self._cooldown_lock:
            expired = [
                k for k, t in self._cooldown.items()
                if now - t > self.default_cooldown * 2
            ]
            for k in expired:
                del self._cooldown[k]

    # ========== 止损冷却相关 ==========

    def can_trigger_stop_loss(self, code: str) -> bool:
        """
        检查是否可以触发止损

        Args:
            code: 股票代码

        Returns:
            True: 可以触发止损
            False: 仍在止损冷却中
        """
        now = time.time()
        fail_count = self._stop_loss_fail_count.get(code, 0)

        # 递增冷却时间：5s → 15s → 30s → 60s → 60s
        cooldown_schedule = [5, 15, 30, 60, 60]
        cooldown_seconds = cooldown_schedule[min(fail_count, len(cooldown_schedule) - 1)]

        last_stop_time = self._stop_loss_cooldown.get(code, 0)
        return (now - last_stop_time) >= cooldown_seconds

    def record_stop_loss(self, code: str, success: bool) -> None:
        """
        记录止损执行结果

        Args:
            code: 股票代码
            success: 是否成功
        """
        now = time.time()
        self._stop_loss_cooldown[code] = now

        if success:
            # 成功后重置失败计数
            self._stop_loss_fail_count.pop(code, None)
        else:
            # 失败时递增计数
            self._stop_loss_fail_count[code] = self._stop_loss_fail_count.get(code, 0) + 1
            fail_count = self._stop_loss_fail_count[code]

            if fail_count >= 5:
                logger.error(
                    f"[止损严重警告] {code} 连续{fail_count}次止损失败，"
                    f"请立即检查！"
                )

    def get_stop_loss_fail_count(self, code: str) -> int:
        """获取止损失败次数"""
        return self._stop_loss_fail_count.get(code, 0)

    # ========== 开仓去重相关 ==========

    def try_acquire_open_lock(self, code: str, cooldown_sec: float = 5.0) -> bool:
        """
        尝试获取开仓锁（防止重复开仓）

        Args:
            code: 股票代码
            cooldown_sec: 冷却时间 (秒)

        Returns:
            True: 获取成功，可以开仓
            False: 在冷却期内，禁止开仓
        """
        with self._open_lock:
            now = time.time()
            last_open_ts = self._open_position_ts.get(code, 0)

            if now - last_open_ts < cooldown_sec:
                logger.debug(
                    f"[开仓去重] {code} {cooldown_sec:.0f}s 内已执行过开仓 "
                    f"(距上次 {now - last_open_ts:.1f}s)"
                )
                return False

            # 预占时间戳
            self._open_position_ts[code] = now
            return True

    def clear_open_lock(self, code: str) -> None:
        """清除开仓锁"""
        with self._open_lock:
            self._open_position_ts.pop(code, None)

    # ========== 追踪止盈冷却相关 ==========

    def is_trailing_exit_cooldown(self, code: str, cooldown_sec: float = 900.0) -> bool:
        """
        检查是否在追踪止盈冷却期内

        Args:
            code: 股票代码
            cooldown_sec: 冷却时间 (秒)，默认 15 分钟

        Returns:
            True: 在冷却期内
            False: 可以开仓
        """
        now = time.time()
        trailing_exit_ts = self._trailing_exit_time.get(code, 0)
        return (now - trailing_exit_ts) < cooldown_sec

    def record_trailing_exit(self, code: str) -> None:
        """
        记录追踪止盈触发时间

        Args:
            code: 股票代码
        """
        self._trailing_exit_time[code] = time.time()
        logger.debug(f"[{code}] 追踪止盈触发时间已记录")

    def clear_trailing_exit(self, code: str) -> None:
        """清除追踪止盈触发时间"""
        self._trailing_exit_time.pop(code, None)

    # ========== 软止损预警相关 ==========

    def can_warn_soft_stop(self, code: str, cooldown_sec: float = 300.0) -> bool:
        """
        检查是否可以发送软止损预警

        Args:
            code: 股票代码
            cooldown_sec: 冷却时间 (秒)，默认 5 分钟

        Returns:
            True: 可以发送预警
            False: 在预警冷却期内
        """
        now = time.time()
        last_warning = self._soft_stop_warnings.get(code, 0)
        return (now - last_warning) >= cooldown_sec

    def record_soft_stop_warning(self, code: str) -> None:
        """
        记录软止损预警时间

        Args:
            code: 股票代码
        """
        self._soft_stop_warnings[code] = time.time()

    # ========== 清理缓存 ==========

    def clear_code_cache(self, code: str) -> None:
        """
        清除指定代码的所有缓存

        Args:
            code: 股票代码
        """
        self._cooldown.pop(code, None)
        self._stop_loss_cooldown.pop(code, None)
        self._stop_loss_fail_count.pop(code, None)
        self._open_position_ts.pop(code, None)
        self._trailing_exit_time.pop(code, None)
        self._soft_stop_warnings.pop(code, None)
        logger.debug(f"[{code}] 冷却缓存已清理")


# 全局单例
_cooldown_manager: Optional[CooldownManager] = None


def get_cooldown_manager(cooldown: int = 120) -> CooldownManager:
    """
    获取冷却管理器单例

    Args:
        cooldown: 默认冷却时间 (秒)

    Returns:
        CooldownManager 实例
    """
    global _cooldown_manager
    if _cooldown_manager is None:
        _cooldown_manager = CooldownManager(cooldown)
    return _cooldown_manager
