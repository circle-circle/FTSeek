"""Finnhub 免费档配额 SSOT — 进程级共享令牌桶（按 API key，而不是按客户端实例）。

为什么需要（2026-10-28 实测）:
    Finnhub 免费档配额按 **API key** 算（60 次/分钟），但进程里有 3 个互不相识的
    客户端各自限速/不限速：
      1. `app/data/finnhub_provider.py`   —— 自带**每实例**令牌桶 ⇒ 每 new 一个 provider 就多 60/min
      2. `app/data/providers/fundamental_provider.py` —— 裸 requests，**完全无限速**
      3. `app/data/premarket_enrichment._get_finnhub_client()` —— finnhub SDK，无限速
    盘前批次（76 只 × 逐只 1-2 次）在 2.5 分钟内打出约 150-190 次请求 ⇒ 收到 96 次 429。

本模块把额度收成**一个进程级桶**：所有客户端在发起 HTTP 前 acquire() 同一份额度；
收到 429 时 on_429() 把桶清空并冻结补 Token 一段时间（全局退避），
而不是像原来那样只有撞 429 的那个线程 sleep(5)、其余线程继续轰。

环境变量:
    FINNHUB_RATE_CAPACITY    每分钟允许的请求数（默认 54，留 10% 余量给其它进程/突发）
    FINNHUB_429_PENALTY_SEC  撞 429 后全局冻结补 Token 的秒数（默认 8）

注意: 这是**安全网**（把浪费变成排队），不是治本。治本是减少请求宽度：
静态字段长 TTL 落盘缓存、可批量接口改批量、净 0 权重的维度砍掉。
"""
import os
import threading
import time
import logging
from typing import Dict

logger = logging.getLogger(__name__)

SUSTAINED_PER_MIN = int(os.getenv("FINNHUB_RATE_PER_MIN", "54") or 54)   # 稳态速率（免费档 60，留 10% 余量）
BURST = int(os.getenv("FINNHUB_BURST", "10") or 10)                        # 突发余量（容量）
REFILL_INTERVAL_SEC = 60.0 / max(1, SUSTAINED_PER_MIN)                     # ≈1.11s/call
PENALTY_SEC = float(os.getenv("FINNHUB_429_PENALTY_SEC", "8") or 8)
# ⚠️ 容量与稳态速率必须分开: 容量=54 时进程启动瞬间会连发 54 个请求, 而服务端按**滚动 60s 窗口**
#   计数（2026-10-28 探针实测: 容量 54 仍打出 429; 容量 10 + 1.11s 间隔 = 0 次 429）。
#   把 60/容量 当补令牌间隔是错的（会把 54/min 误压成 10/min 并造成排队超时）。

# 服务端自报额度的响应头 (Finnhub 免费档实测存在):
#   x-ratelimit-limit: 60   x-ratelimit-remaining: 59   x-ratelimit-reset: <epoch 秒>
# 用它自我校准 —— 跨进程/跨脚本共用同一 key 时, 本地桶看不到别人的消耗, 只有响应头看得见。


class SharedTokenBucket:
    """线程安全令牌桶 + 429 全局退避。"""

    def __init__(self, capacity: int = BURST, refill_interval_sec: float = REFILL_INTERVAL_SEC):
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._refill_interval = refill_interval_sec
        self._last_refill = time.monotonic()
        self._frozen_until = 0.0
        self._lock = threading.Lock()
        self.granted = 0
        self.denied = 0
        self.throttled_429 = 0
        self.waited_sec = 0.0
        self.last_remaining = None   # 服务端自报的剩余额度（响应头校准后）

    # ── 内部 ──
    def _refill_locked(self):
        now = time.monotonic()
        if now < self._frozen_until:      # 429 惩罚期: 不补 Token
            self._last_refill = now
            return
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed / self._refill_interval)
            self._last_refill = now

    # ── 公开 ──
    def acquire(self, n: int = 1, timeout: float = 60.0) -> bool:
        """取 n 个令牌；必要时阻塞排队。返回 False = 排队超时（调用方按失败处理）。"""
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    self.granted += n
                    self.waited_sec += time.monotonic() - t0
                    return True
                wait = self._refill_interval * n
                frozen = max(0.0, self._frozen_until - time.monotonic())
            if time.monotonic() + max(wait, frozen, 0.05) > deadline:
                with self._lock:
                    self.denied += 1
                    self.waited_sec += time.monotonic() - t0
                logger.warning(f"[Finnhub配额] 排队超时 (需 {n} 次, 余 {self._tokens:.1f}) ⇒ 本次按失败返回")
                return False
            time.sleep(min(max(wait, frozen, 0.05), 1.0))

    def on_429(self, endpoint: str = "") -> None:
        """撞 429: 清空令牌并全局冻结补 Token（所有客户端一起退避）。"""
        with self._lock:
            self._tokens = 0.0
            self._frozen_until = time.monotonic() + PENALTY_SEC
            self._last_refill = time.monotonic()
            self.throttled_429 += 1
            n429 = self.throttled_429
        logger.warning(
            f"[Finnhub配额] 429 on {endpoint or '?'} ⇒ 全局退避 {PENALTY_SEC:.0f}s "
            f"(本进程累计 {n429} 次) — 说明请求宽度仍超额度, 查是否有可批量/可缓存项"
        )

    def observe(self, headers=None) -> None:
        """用服务端响应头自我校准额度（跨进程共用同一 key 时唯一可见的真值）。

        `x-ratelimit-remaining` 比本地账本更权威: 另一个进程/脚本(如 21:10 的 UE scan)
        吃掉的额度, 本地桶完全看不见。remaining 见底 ⇒ 冻结到 reset, 否则把本地令牌
        上限压到服务端剩余值。
        """
        if not headers:
            return
        rem_raw = headers.get("x-ratelimit-remaining") or headers.get("X-Ratelimit-Remaining")
        if rem_raw is None:
            return
        try:
            rem = max(0, int(float(rem_raw)))
        except (TypeError, ValueError):
            return                                   # 头部格式变了: 静默跳过校准（不改主流程）
        reset_raw = headers.get("x-ratelimit-reset") or headers.get("X-Ratelimit-Reset")
        reset_in = None
        if reset_raw is not None:
            try:
                reset_in = float(reset_raw) - time.time()
            except (TypeError, ValueError):
                reset_in = None
            if reset_in is not None and (reset_in < 0 or reset_in > 65):
                reset_in = None                      # 时钟漂移/异常值不采信
        with self._lock:
            self._refill_locked()
            if rem < self._tokens:                   # 服务端说剩得少 ⇒ 听服务端的
                self._tokens = float(rem)
            self.last_remaining = rem
            if rem == 0:
                freeze = PENALTY_SEC if reset_in is None else max(1.0, min(reset_in, 65.0))
                self._frozen_until = max(self._frozen_until, time.monotonic() + freeze)

    def stats(self) -> Dict[str, float]:
        with self._lock:
            return {
                "burst": self._capacity,
                "sustained_per_min": round(60.0 / self._refill_interval, 1),
                "tokens_left": round(self._tokens, 1),
                "granted": self.granted,
                "denied": self.denied,
                "throttled_429": self.throttled_429,
                "waited_sec": round(self.waited_sec, 1),
                "srv_remaining": self.last_remaining,
            }


_BUCKET = SharedTokenBucket()
_BUCKET_LOCK = threading.Lock()


def get_limiter() -> SharedTokenBucket:
    """进程级单例（按 key 一份；本部署单 key）。"""
    return _BUCKET


def acquire(n: int = 1, timeout: float = 60.0) -> bool:
    return _BUCKET.acquire(n=n, timeout=timeout)


def on_429(endpoint: str = "") -> None:
    _BUCKET.on_429(endpoint)


def observe(headers=None) -> None:
    """用响应头校准服务端剩余额度（跨进程共 key 时唯一的真值）。"""
    _BUCKET.observe(headers)


def stats() -> Dict[str, float]:
    return _BUCKET.stats()


def _reset_for_tests() -> None:
    """仅供测试: 重置桶状态。"""
    global _BUCKET
    with _BUCKET_LOCK:
        _BUCKET = SharedTokenBucket()