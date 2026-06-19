"""VIX Guard - VIX阈值防抖和状态管理

Task #2 (2026-06-17): VIX>28硬拒新仓 + Hysteresis防抖

封锁阈值: VIX > 28
解封阈值: VIX < 25（需持续15分钟）
防止VIX在28附近频繁开关
"""
import time
import logging
import threading

logger = logging.getLogger(__name__)


class VIXGuard:
    """
    VIX硬拒守卫 - 含Hysteresis防抖

    封锁阈值: VIX > 28
    解封阈值: VIX < 25（需持续15分钟）
    防止VIX在28附近频繁开关
    """

    BLOCK_THRESHOLD = 28.0
    UNBLOCK_THRESHOLD = 25.0
    UNBLOCK_HOLD_SECONDS = 900  # 15分钟

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self._is_blocked = False
        self._unblock_candidate_since = None  # 开始满足解封条件的时间
        self._last_vix = None
        self._state_lock = threading.Lock()

    def should_block(self, current_vix: float) -> tuple:
        """
        判断是否应该阻止新仓

        Returns:
            (should_block: bool, reason: str)
        """
        if current_vix is None:
            return True, "vix_data_unavailable"

        with self._state_lock:
            self._last_vix = current_vix

            if not self._is_blocked:
                # 当前未封锁：检查是否需要封锁
                if current_vix > self.BLOCK_THRESHOLD:
                    self._is_blocked = True
                    self._unblock_candidate_since = None
                    logger.warning(f"[VIXGuard] BLOCKED: VIX={current_vix:.2f} > {self.BLOCK_THRESHOLD}")
                    return True, f"vix={current_vix:.1f}>threshold"
                return False, "ok"
            else:
                # 当前已封锁：检查是否满足解封条件
                if current_vix < self.UNBLOCK_THRESHOLD:
                    if self._unblock_candidate_since is None:
                        self._unblock_candidate_since = time.time()
                        return True, f"vix={current_vix:.1f}<{self.UNBLOCK_THRESHOLD} (waiting {self.UNBLOCK_HOLD_SECONDS}s)"

                    elapsed = time.time() - self._unblock_candidate_since
                    if elapsed >= self.UNBLOCK_HOLD_SECONDS:
                        # 持续15分钟低于25，解封
                        self._is_blocked = False
                        self._unblock_candidate_since = None
                        logger.info(f"[VIXGuard] UNBLOCKED: VIX={current_vix:.2f} < {self.UNBLOCK_THRESHOLD} for {elapsed:.0f}s")
                        return False, "ok"
                    else:
                        return True, f"vix={current_vix:.1f}<{self.UNBLOCK_THRESHOLD} (hold {elapsed:.0f}/{self.UNBLOCK_HOLD_SECONDS}s)"
                else:
                    # VIX回到25以上但低于28，重置解封计时
                    self._unblock_candidate_since = None
                    return True, f"vix={current_vix:.1f} (blocked, above unblock threshold)"

    def get_status(self) -> dict:
        """获取当前状态"""
        with self._state_lock:
            return {
                "is_blocked": self._is_blocked,
                "last_vix": self._last_vix,
                "unblock_candidate_since": self._unblock_candidate_since,
                "block_threshold": self.BLOCK_THRESHOLD,
                "unblock_threshold": self.UNBLOCK_THRESHOLD,
            }

    def force_unblock(self, reason: str = ""):
        """手动解封"""
        with self._state_lock:
            self._is_blocked = False
            self._unblock_candidate_since = None
            logger.warning(f"[VIXGuard] Force unblocked: {reason}")
