"""
时区安全 datetime 工具 (P1-3 2026-05-14)

解决: 项目中混用 datetime.now() (naive) 与 datetime.now(ET) (aware) 导致比较抛 TypeError.

约定:
- 系统内部时间一律用 ET-aware (美股市场基准)
- 持久化到 DB 时用 ISO 8601 字符串
- 比较两个 datetime 时必须用 compare_safe()
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import pytz
    ET = pytz.timezone("America/New_York")
    UTC = pytz.UTC
except ImportError:
    # fallback: 用 datetime.timezone
    UTC = timezone.utc
    ET = timezone(timedelta(hours=-4))  # 简化, 不处理夏令时


def now_et() -> datetime:
    """ET 时区的当前时间 (aware)"""
    return datetime.now(ET)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_et(dt: datetime) -> datetime:
    """任意 datetime 转 ET aware. naive 当 UTC 处理."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def to_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def compare_safe(a: datetime, b: datetime) -> int:
    """
    安全比较两个 datetime, 自动处理 naive/aware 混用.
    Returns: -1 if a<b, 0 if equal, 1 if a>b. None 视为最大值.
    """
    if a is None and b is None:
        return 0
    if a is None:
        return 1   # None 最大 (永远在未来)
    if b is None:
        return -1
    # 统一为 UTC aware
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    if b.tzinfo is None:
        b = b.replace(tzinfo=UTC)
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def is_expired_safe(expires_at: Optional[datetime]) -> bool:
    """检查是否已过期, 时区安全."""
    if expires_at is None:
        return False
    return compare_safe(now_utc(), expires_at) > 0


def parse_iso_safe(s: Optional[str]) -> Optional[datetime]:
    """解析 ISO 字符串, 支持各种带不带时区格式. 失败返回 None."""
    if not s:
        return None
    try:
        # 处理 Z 后缀
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except Exception:
            return None


def market_phase_now() -> str:
    """返回当前 ET 市场 phase 简易版 (供需要的代码用)"""
    n = now_et()
    if n.weekday() >= 5:
        return "weekend"
    h, m = n.hour, n.minute
    minutes = h * 60 + m
    if minutes < 240:                # 0:00-4:00
        return "overnight"
    if minutes < 570:                # 4:00-9:30
        return "premarket"
    if minutes < 590:                # 9:30-9:50
        return "opening_drive"
    if minutes < 690:                # 9:50-11:30
        return "morning"
    if minutes < 840:                # 11:30-14:00
        return "midday"
    if minutes < 930:                # 14:00-15:30
        return "afternoon"
    if minutes < 960:                # 15:30-16:00
        return "closing_drive"
    if minutes < 1200:               # 16:00-20:00
        return "after_hours"
    return "overnight"


# 信号 expires 持续时间 (P1-1)
SIGNAL_EXPIRY_MINUTES_BY_PHASE = {
    "opening_drive":  5,
    "morning":       10,
    "midday":        15,
    "afternoon":     10,
    "closing_drive":  3,
    "premarket":     30,
    "after_hours":   30,
    "overnight":     30,
    "weekend":       30,
}


def signal_expires_at(phase: Optional[str] = None) -> datetime:
    """
    P1-1 (2026-05-14): phase-aware signal 过期时间.
    返回 UTC aware datetime.
    """
    p = phase or market_phase_now()
    minutes = SIGNAL_EXPIRY_MINUTES_BY_PHASE.get(p, 10)
    return now_utc() + timedelta(minutes=minutes)
