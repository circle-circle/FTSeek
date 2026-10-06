"""Channel Health Check — 每小时 ping (2026-05-16)

防 webhook 静默失效. 默认每小时发 1 条隐式 heartbeat (Level.INFO).
连续 3 次失败的 channel 自动降级 (优先级 +50).
"""
from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List

from .message import PushMessage, Level, Category

logger = logging.getLogger(__name__)


class HealthMonitor:
    """通道健康监控 — 后台 ping + 失败计数"""

    # DEB-20260903 #111: 熔断降级参数 — 连续失败达阈值后暂停通道, 冷却后半开恢复.
    DEGRADE_THRESHOLD = 3        # 连续失败次数阈值
    DEGRADE_RECOVER_SEC = 1800   # 熔断冷却 30min 后半开探测

    def __init__(self, channels: List, interval_sec: int = 3600,
                 suppress_window_sec: int = 0):
        self._channels = channels
        self.interval_sec = interval_sec
        # B1 (2026-10-06 推送治理批): 「上次成功外发即活性证据」抑制窗.
        #   为什么需要: webhook 活性探测**本质上必须发一条可见消息** (webhook API 无静默 ping),
        #   所以不能「只探测不打扰」; 但可以「已被证明活着时不重复探测」。
        #   任何一次成功的业务推送本身就证明该通道 webhook 可用 ⇒ 距上次成功 < 本窗口时
        #   跳过本轮 ping。**探测能力不减**: 真失效时业务推送会失败 (不记 note_success)
        #   ⇒ ping 照发 ⇒ 仍走 _degrade 熔断; 且 hub._immediate_send 全通道失败记
        #   logger.error (实测被 error_alert 的 "] ERROR:" 字面量匹配抓到)。
        #   实测上界 (41天窗口按**全局日志时间线**回放): 710 → 224 条 (省 68%, 12 → 5.5 条/天)。
        #   ⚠️ 该上界假设活性证据跨进程共享, 而本实现是**进程内内存态** ⇒ 短命 cron 进程
        #   看不到其他进程的成功记录, 实际收益 ≤ 上界, 部署后按 `ping_suppressed` 计数实测。
        #   长驻服务 (main.py) 是心跳量主体且业务推送同进程 ⇒ 那部分抑制真实生效。
        #   业务推送空档 >8h 有 27 次 (最大 32.8h) ⇒ 空档期无抑制证据, ping 照发, 盲区不放大。
        #   =0 关闭抑制 (回到每小时必发, 即原行为)。
        self.suppress_window_sec = max(0, int(suppress_window_sec))
        self._fail_count: Dict[str, int] = {}
        self._last_ok: Dict[str, float] = {}
        self._last_business_ok: Dict[str, float] = {}  # B1: 最近一次业务消息成功外发
        self._suppressed: Dict[str, int] = {}          # B1: 累计跳过次数 (可观测)
        self._degraded: Dict[str, float] = {}  # DEB-20260903 #111: name → 熔断时刻
        self._thread: threading.Thread = None
        self._running = False

    def note_success(self, channel_name: str) -> None:
        """B1: 由 PushHub 在业务消息发送成功时回调 — 记为通道活性证据.

        只由 hub._immediate_send 调用 (即真实业务/告警消息); ping 自身走
        _ping_all 里的 ch.send() 直达通道, **不经过本方法** ⇒ 不会自我抑制
        (否则第一条 ping 成功后就永远不再 ping = 探测彻底失效)。

        不包 try/except: 方法体只有一次 dict 赋值, 包异常处理属无意义防御;
        且调用方 hub._immediate_send 的 per-channel 循环已有 except 兜底,
        真抛了也会被那层接住 (不会让一次发送失败)。多包一层反而新增
        「except 里 debug 吞异常」的日志卫生违反 (W4 棘轮, 实测撞红过)。
        """
        self._last_business_ok[channel_name] = time.time()


    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._ping_loop, daemon=True, name="PushHealthMonitor"
        )
        self._thread.start()
        logger.info(f"[PushHealth] 启动 (interval={self.interval_sec}s)")

    def stop(self):
        self._running = False

    def _ping_loop(self):
        # 首次启动延迟 60s (避免与初始化日志混)
        time.sleep(60)
        while self._running:
            try:
                self._ping_all()
                time.sleep(self.interval_sec)
            except Exception as e:
                logger.debug(f"[PushHealth·loop] {e}")

    def _ping_all(self):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        msg = PushMessage(
            title="FTSeek-US 通道心跳",
            body=f"系统正常 @ {ts}",
            level=Level.INFO,
            category=Category.HEALTH,
            dedup_key=f"health_ping:{int(time.time()/self.interval_sec)}",
        )
        for ch in self._channels:
            # B1: 每轮必须重置 — 否则多通道时前一个通道的半开恢复会把 True 泄漏给
            #   后续通道, 使它们的抑制静默失效 (测试 test_recovery_probe_flag_does_not_leak 钉)。
            _recovery_probe = False
            # DEB-20260903 #111: 熔断冷却到期 → 半开探测 (恢复可用, 本轮 ping 验证,
            #   失败会重新降级). 置于可用性检查前, 确保探测真正发出.
            if ch.name in self._degraded and (time.time() - self._degraded[ch.name]) >= self.DEGRADE_RECOVER_SEC:
                logger.info(f"[PushHealth] {ch.name} 熔断冷却到期, 半开探测恢复")
                _recovery_probe = True   # B1: 本轮是半开探测, 不得被抑制窗吞掉
                try:
                    ch._available = True
                except Exception as _e:
                    logger.debug(f"[PushHealth] {ch.name} 半开探测标记可用失败: {_e}")
                self._fail_count[ch.name] = 0
                del self._degraded[ch.name]
            try:
                if not ch.is_available():
                    continue
                # B1 (2026-10-06): 距上次业务消息成功外发 < suppress_window_sec ⇒ 跳过本轮 ping。
                #   业务推送成功已是该通道 webhook 可用的直接证据, 此时 ping 纯属冗余噪音。
                #   ⚠️ 半开恢复探测必须豁免 (`_recovery_probe`): 上面的恢复分支已把
                #   _degraded 清掉, 若不豁免则恢复后第一条验证 ping 会被抑制窗吞掉 ——
                #   通道若其实是死的, 就永远发现不了 (本批 test_degrade_recovery_probe_not_suppressed
                #   实跑抓到此序位错误, 不是纸面推演)。
                if self.suppress_window_sec and not _recovery_probe:
                    _last_biz = self._last_business_ok.get(ch.name, 0.0)
                    if _last_biz and (time.time() - _last_biz) < self.suppress_window_sec:
                        self._suppressed[ch.name] = self._suppressed.get(ch.name, 0) + 1
                        # ⚠️ 必须 INFO 不能 debug (2026-10-06 自查修正): 生产 LOG_LEVEL=INFO
                        #   ⇒ debug 根本不落盘 ⇒ 「心跳消失」在日志里与「心跳线程死了」
                        #   **完全同貌**, 运维无从区分, 而抑制是 B1 的核心行为。
                        #   频率安全: 每通道每 interval(3600s) 最多 1 行 ⇒ 单通道 ≤24 行/天,
                        #   比它替代的心跳推送 (24 条/天外发) 更便宜。
                        logger.info(
                            f"[PushHealth] {ch.name} 跳过本轮 ping "
                            f"(业务消息 {(time.time()-_last_biz)/60:.0f}min 前成功外发, "
                            f"< 抑制窗 {self.suppress_window_sec/3600:.1f}h; "
                            f"累计抑制 {self._suppressed[ch.name]} 次)")
                        continue
                ok = ch.send(msg)
                if ok:
                    self._fail_count[ch.name] = 0
                    self._last_ok[ch.name] = time.time()
                else:
                    self._fail_count[ch.name] = self._fail_count.get(ch.name, 0) + 1
                    if self._fail_count[ch.name] >= self.DEGRADE_THRESHOLD:
                        # DEB-20260903 #111: 达阈值执行熔断降级 (原仅 warning 无动作)
                        self._degrade(ch)
            except Exception as e:
                logger.debug(f"[PushHealth] ping {ch.name} 异常: {e}")

    def _degrade(self, ch):
        """熔断降级 (DEB-20260903 #111): 标记通道不可用 + 优先级 +50, 记录熔断时刻供冷却恢复.

        恢复条件: 冷却 DEGRADE_RECOVER_SEC 后半开探测, ping 成功即清零失败计数恢复.
        """
        if ch.name in self._degraded:
            return
        self._degraded[ch.name] = time.time()
        try:
            ch.priority = getattr(ch, "priority", 5) + 50  # 实例属性屏蔽类属性
        except Exception as _e:
            logger.debug(f"[PushHealth] {ch.name} 熔丝调优失败 (priority 只读?): {_e}")
        try:
            ch._available = False  # 熔丝: hub._immediate_send 的 is_available() 将跳过本通道
        except Exception as _e:
            logger.debug(f"[PushHealth] {ch.name} 熔丝标记不可用失败: {_e}")
        logger.error(
            f"[PushHealth] {ch.name} 连续失败 >= {self.DEGRADE_THRESHOLD} 次, "
            f"已熔断降级 (优先级+50, 标记不可用, {self.DEGRADE_RECOVER_SEC}s 后半开探测)")

    def get_status(self) -> Dict:
        return {
            "suppress_window_sec": self.suppress_window_sec,  # B1: 0 = 抑制关闭
            "channels": [
                {
                    "name": ch.name,
                    "priority": ch.priority,
                    "available": ch.is_available(),
                    "fail_count": self._fail_count.get(ch.name, 0),
                    "last_ok": self._last_ok.get(ch.name, 0),
                    "last_business_ok": self._last_business_ok.get(ch.name, 0),  # B1
                    "ping_suppressed": self._suppressed.get(ch.name, 0),          # B1
                    "degraded": ch.name in self._degraded,  # DEB-20260903 #111
                }
                for ch in self._channels
            ],
        }
