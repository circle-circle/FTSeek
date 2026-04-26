"""
富途 OpenD API 限流器 (2026-04-13 新增)

功能:
- 60 秒滑动窗口限流
- 防止超过富途 API 频率限制 (~100 次/分钟)
- 线程安全
- 超时拒绝机制

使用:
    limiter = FutuRateLimiter(max_per_min=100)
    if limiter.acquire(timeout=30):
        ret, data = ctx.get_cur_kline(...)
        limiter.record()
"""

import time
import threading
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger("ftseek.utils.futu_limiter")
logger.propagate = False

# 全局单例限流器 (2026-04-20 修复: 所有 Futu API 调用共享同一个限流器)
_global_limiter: Optional["FutuRateLimiter"] = None
_limiter_lock = threading.Lock()


def get_global_limiter(max_per_min: int = 90) -> "FutuRateLimiter":
    """获取全局共享限流器实例 (单例模式)

    所有 Futu API 调用 (KlinePoller, risk_manager, executor 等)
    必须通过此方法获取同一个限流器，避免各自创建导致配额竞争。
    """
    global _global_limiter
    if _global_limiter is None:
        with _limiter_lock:
            if _global_limiter is None:
                _global_limiter = FutuRateLimiter(max_per_min=max_per_min)
    return _global_limiter


class FutuRateLimiter:
    """富途 API 限流器 (60 秒滑动窗口)"""

    def __init__(self, max_per_min: int = 90):
        """
        Args:
            max_per_min: 每分钟最大 API 调用次数 (默认 100，Futu 实际限额)
        """
        self.max_per_min = max_per_min
        self._call_times: deque = deque(maxlen=max_per_min * 2)  # 存两倍余量
        self._lock = threading.Lock()
        self._total_calls = 0
        self._total_rejected = 0

    def acquire(self, timeout: float = 30) -> bool:
        """获取 API 调用许可，超时则拒绝

        Args:
            timeout: 等待超时时间（秒）

        Returns:
            True: 获取成功，可以调用 API
            False: 超时拒绝
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                now = time.time()
                # 清理 60 秒外的记录
                while self._call_times and self._call_times[0] < now - 60:
                    self._call_times.popleft()
                if len(self._call_times) < self.max_per_min:
                    return True
            time.sleep(0.3)  # 等待一小段时间后重试

        # 超时拒绝
        with self._lock:
            self._total_rejected += 1
        logger.warning(
            f"[限流器] API 调用被拒绝 (当前窗口: {len(self._call_times)}/{self.max_per_min}, "
            f"累计拒绝: {self._total_rejected})"
        )
        return False

    def record(self):
        """记录一次 API 调用"""
        with self._lock:
            self._call_times.append(time.time())
            self._total_calls += 1

    def call(self, func, *args, timeout: float = 30, **kwargs):
        """限流保护下调用函数

        自动 acquire + 调用 + record

        Args:
            func: 要调用的函数
            *args: 函数参数
            timeout: 等待超时时间
            **kwargs: 函数关键字参数

        Returns:
            函数返回值，如果被拒绝则返回 None
        """
        if not self.acquire(timeout=timeout):
            return None
        try:
            result = func(*args, **kwargs)
            self.record()
            return result
        except Exception as e:
            logger.error(f"[限流器] 被保护函数调用失败: {e}")
            return None

    def headroom(self) -> int:
        """当前 60s 窗口剩余可用次数"""
        with self._lock:
            now = time.time()
            while self._call_times and self._call_times[0] < now - 60:
                self._call_times.popleft()
            return max(0, self.max_per_min - len(self._call_times))

    def get_stats(self) -> dict:
        """获取限流统计"""
        with self._lock:
            now = time.time()
            # 清理 60 秒外的记录
            while self._call_times and self._call_times[0] < now - 60:
                self._call_times.popleft()
            current_window = len(self._call_times)

        return {
            "max_per_min": self.max_per_min,
            "current_window": current_window,
            "headroom": self.max_per_min - current_window,
            "total_calls": self._total_calls,
            "total_rejected": self._total_rejected,
            "rejected_rate": (
                self._total_rejected / max(1, self._total_calls + self._total_rejected)
            ),
        }


class KlinePoller:
    """长周期 K 线定时轮询器 (2026-04-13 新增)

    不占用实时订阅槽位，通过定时批量拉取 K_15M/K_30M/K_60M/K_DAY 数据
    注入到 DataSubscriber 的缓冲区中。
    """

    # K 线类型到 futu K 线的映射
    KTYPE_MAP = {
        "K_15M": None,   # 运行时从 futu.SubType 获取
        "K_60M": None,
        "K_DAY": None,
        # K_30M 已移除 (2026-04-14): 策略层无实际使用，纯消耗 API 配额
    }

    # 轮询间隔 (秒)
    POLL_INTERVALS = {
        "K_15M": 120,   # 每 120 秒拉一次 15M (原 60s, 优化：15M 无需秒级刷新)
        # K_30M 已移除 (2026-04-14): 策略层无实际使用
        "K_60M": 300,   # 每 300 秒拉一次 60M
        "K_DAY": 600,   # 每 600 秒拉一次日线
    }

    BATCH_SIZE = 10     # 每次拉 10 只，避免单次 API 超时
    KLINE_COUNT = 100   # 每次拉取最近 100 根 K 线

    def __init__(self, quote_ctx, kline_buffers_by_period: dict,
                 limiter: FutuRateLimiter, codes: list):
        """
        Args:
            quote_ctx: futu OpenQuoteContext 实例
            kline_buffers_by_period: {code: {period: IndicatorBuffer}}
            limiter: FutuRateLimiter 实例
            codes: 股票代码列表
        """
        self._ctx = quote_ctx
        self._buffers = kline_buffers_by_period
        self._limiter = limiter
        self._codes = codes
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.logger = logging.getLogger("ftseek.subscriber.kline_poller")

        # 延迟导入 futu SubType
        try:
            from futu import SubType
            self.KTYPE_MAP = {
                "K_15M": SubType.K_15M,
                "K_60M": SubType.K_60M,
                "K_DAY": SubType.K_DAY,
            }
        except ImportError:
            self.logger.warning("futu 模块不可用，KlinePoller 将无法工作")

    def start(self):
        """启动轮询后台线程"""
        if self._running:
            self.logger.warning("KlinePoller 已在运行")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.logger.info(
            f"KlinePoller 已启动 | {len(self._codes)} 只股票 | "
            f"周期: {list(self.POLL_INTERVALS.keys())}"
        )

    def stop(self):
        """停止轮询"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self.logger.info("KlinePoller 已停止")

    def _run_loop(self):
        """轮询主循环"""
        last_poll_time = {k: 0.0 for k in self.POLL_INTERVALS}

        # 2026-04-20: 启动后短暂等待，让订阅完成
        self.logger.info("KlinePoller 等待 30s 启动缓冲期...")
        time.sleep(30)
        # 重置计时器
        now = time.time()
        for k in last_poll_time:
            last_poll_time[k] = now - self.POLL_INTERVALS[k] + 30

        while self._running:
            now = time.time()
            polled_any = False

            for k_type, interval in self.POLL_INTERVALS.items():
                if now - last_poll_time[k_type] >= interval:
                    self._poll_klines(k_type)
                    last_poll_time[k_type] = now
                    polled_any = True

            # 如果没有轮询任何数据，等待较短时间
            if not polled_any:
                time.sleep(10)
            else:
                time.sleep(5)  # 轮询后稍等

    def _poll_klines(self, k_type: str):
        """批量拉取指定周期的 K 线 (2026-04-20 修复: 配额感知，跨窗口续传)"""
        sub_type = self.KTYPE_MAP.get(k_type)
        if sub_type is None:
            self.logger.warning(f"不支持的 K 线类型: {k_type}")
            return

        codes = self._codes
        if not codes:
            return

        # 2026-04-20: 检查剩余配额，不足则跳过本轮 (等待下一分钟窗口重置)
        headroom = self._limiter.headroom()
        # 需要至少 20 次空闲才启动轮询 (避免中途被其他模块截胡)
        if headroom < 20:
            self.logger.debug(f"[{k_type}] 配额不足 (headroom={headroom}<20)，跳过本轮，等待下一窗口")
            return

        success_count = 0
        fail_count = 0
        calls_this_cycle = 0
        # KlinePoller 最多占 1/3 配额 (30次/分钟)，给其他模块留 60 次
        max_calls_this_cycle = min(30, max(5, headroom - 20))

        for i in range(0, len(codes), self.BATCH_SIZE):
            if calls_this_cycle >= max_calls_this_cycle:
                self.logger.debug(
                    f"[{k_type}] 本轮配额已用完 ({calls_this_cycle}/{max_calls_this_cycle})，"
                    f"剩余 {len(codes) - i * self.BATCH_SIZE} 只下轮续传"
                )
                break

            batch = codes[i:i + self.BATCH_SIZE]

            for code in batch:
                if calls_this_cycle >= max_calls_this_cycle:
                    break
                # 2026-04-20 修复: 快速跳过配额耗尽的批次，避免 15s 阻塞
                if self._limiter.headroom() <= 0:
                    self.logger.debug(f"[{k_type}] 配额耗尽，跳过剩余股票")
                    break
                if not self._limiter.acquire(timeout=2):
                    self.logger.debug(f"[{k_type}] 限流跳过 {code}")
                    fail_count += 1
                    continue
                try:
                    ret, data = self._ctx.get_cur_kline(
                        code=code,
                        ktype=sub_type,
                        num=self.KLINE_COUNT,
                        autype="QFQ",
                    )
                    self._limiter.record()
                    calls_this_cycle += 1

                    if ret == 0 and data is not None and not data.empty:
                        if code in self._buffers:
                            self._inject_kline(code, k_type, data)
                            success_count += 1
                    else:
                        fail_count += 1
                except Exception as _ce:
                    fail_count += 1
                    self.logger.debug(f"[{k_type}] {code} 拉取失败: {_ce}")
                time.sleep(0.05)

            time.sleep(0.3)

        if success_count > 0:
            self.logger.info(
                f"[{k_type}] 轮询完成: 成功={success_count}, 失败={fail_count}, "
                f"本周期调用={calls_this_cycle}"
            )

    def _inject_kline(self, code: str, k_type: str, data):
        """将 K 线数据注入缓冲区"""
        try:
            buf_dict = self._buffers.get(code, {})
            buf = buf_dict.get(k_type)
            if buf is None:
                return

            # 取最新一根 K 线
            latest = data.iloc[-1]
            kline = {
                "time_key": latest.get("time_key", ""),
                "open": float(latest.get("open", 0)),
                "close": float(latest.get("close", 0)),
                "high": float(latest.get("high", 0)),
                "low": float(latest.get("low", 0)),
                "volume": float(latest.get("volume", 0)),
            }
            buf.push_kline(kline)

        except Exception as e:
            self.logger.debug(f"[{k_type}] 注入 K 线失败 {code}: {e}")
