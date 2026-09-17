"""
P0-D3 (2026-05-14): 拆股事件 Guard

最小实现:
- 启动 + 每日 04:00 ET 拉当日 split_events (yaml 静态名单 + 可选 polygon API)
- 命中标的: scoring 跳过 1 日 + ExitMonitor 跳过 FLASH_EXIT 检查
- 监控: daily_review 加 splits_today 字段

Phase B 增强: 接 Polygon /v3/reference/splits API; 拉前一交易日已发生的 split 自动复权历史 K 线.
"""
from __future__ import annotations
import logging
from datetime import date, datetime
from typing import Dict, Optional, Set
from threading import Lock

logger = logging.getLogger(__name__)


def _today_et() -> date:
    """DEB-20260903 #27: 拆股判定的"今天"统一用 ET 交易日口径.

    原 date.today() 取服务器本地日期; 非 ET 时区 (如 CST 比 ET 快 12-13h) 时
    ET 午后本地已次日, mark/is_split_today 落到不同日期键 → 当日拆股标记查不到.
    """
    try:
        from app.utils.timezone import now_et
        return now_et().date()
    except Exception:
        return date.today()


class SplitEventGuard:
    """拆股事件检测器 (单例, 线程安全)"""

    # DEB-20260904 #147: 常见拆股比例 (正拆 2/3/4/5/10/20:1 → prev/current > 1;
    # 反向拆 1:2/1:5/1:10/1:20 → prev/current < 1), 相对容差 ±5%
    _SPLIT_RATIOS = (2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 0.5, 0.2, 0.1, 0.05)
    _SPLIT_RATIO_TOL = 0.05

    def __init__(self):
        self._splits_today: Dict[date, Set[str]] = {}  # {date: {symbols}}
        self._lock = Lock()
        # 静态白名单 — 已知拆股事件可在 yaml 配置后加载
        # 例: 2024-06-10 NVDA 1-for-10
        self._static_known: Dict[str, str] = {}

    def load_static(self, events: Dict[str, str]):
        """从 yaml 加载已知拆股事件 events = {symbol: ratio_or_date}"""
        with self._lock:
            self._static_known.update(events)

    def mark_split(self, symbol: str, event_date: Optional[date] = None):
        """标记某 symbol 在某日发生拆股"""
        with self._lock:
            d = event_date or _today_et()
            self._splits_today.setdefault(d, set()).add(symbol.upper())
        logger.warning(f"[SplitGuard] {symbol} 拆股事件标记 (date={event_date or 'today'})")

    def is_split_today(self, symbol: str) -> bool:
        """该 symbol 今天是否拆股"""
        with self._lock:
            today_set = self._splits_today.get(_today_et(), set())
            return symbol.upper() in today_set

    def get_today_splits(self) -> Set[str]:
        """今天所有拆股 symbols"""
        with self._lock:
            return set(self._splits_today.get(_today_et(), set()))

    def detect_split_anomaly(self, symbol: str, current_price: float, prev_close: float) -> bool:
        """
        启发式拆股检测: 价格突变 > 30% 且静态名单已登记该标的 → 判定拆股；
        仅价格比例吻合只输出疑似观察日志，不得屏蔽信号或风控。

        如果检测到, 自动标记并返回 True.
        Phase A: 仅日志告警, 不阻塞 (因为可能是真实跳空); Phase B 接 Polygon 实时验证.
        """
        if prev_close <= 0 or current_price <= 0:
            return False
        change = abs(current_price - prev_close) / prev_close
        if change <= 0.30:
            return False
        # DEB-20260916 #269: 价格比例由同一组价格推导，不是独立佐证；
        # 只有静态名单/外部事件源确认才可 mark_split。
        with self._lock:
            known = symbol.upper() in self._static_known
        ratio = prev_close / current_price
        ratio_hit = any(
            abs(ratio - r) / r <= self._SPLIT_RATIO_TOL for r in self._SPLIT_RATIOS
        )
        if not known:
            logger.warning(
                f"[SplitGuard·heuristic] {symbol} 价格突变 {change*100:.1f}% "
                f"(prev=${prev_close:.2f}, now=${current_price:.2f}, ratio={ratio:.3f}, "
                f"ratio_hit={ratio_hit}) — 无外部拆股佐证, 仅观察不标记"
            )
            return False
        logger.warning(
            f"[SplitGuard·heuristic] {symbol} 价格突变 {change*100:.1f}% "
            f"(prev=${prev_close:.2f}, now=${current_price:.2f}, ratio={ratio:.3f}) — 疑似拆股, 当日跳过信号"
        )
        self.mark_split(symbol)
        return True


_singleton: Optional[SplitEventGuard] = None


def get_split_guard() -> SplitEventGuard:
    global _singleton
    if _singleton is None:
        _singleton = SplitEventGuard()
        # 启动加载 yaml 已知事件
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            events = cfg.get_dotted("market_events.known_splits", {}) or {}
            if events:
                _singleton.load_static(events)
                logger.info(f"[SplitGuard] 加载 {len(events)} 个已知拆股事件")
        except Exception as e:
            logger.debug(f"[SplitGuard] yaml 加载失败 (可忽略): {e}")
    return _singleton
