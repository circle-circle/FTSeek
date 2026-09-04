"""
时区工具标准化 — P1 2026-05-20

设计原则:
  1. 所有时间相关操作统一使用本模块
  2. 美东时间作为交易时间 SSOT
  3. 提供便捷函数，减少重复代码
  4. 盘前/盘中/盘后时段判断集中管理
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import pytz
    _HAS_PYTZ = True
except ImportError:
    _HAS_PYTZ = False
    logger = logging.getLogger(__name__)
    logger.warning("[TimezoneUtils] pytz 未安装，尝试 zoneinfo fallback")

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  时区常量
# ═══════════════════════════════════════════════════════════════════

# R-13 (2026-08-23 DEB-20260823): fallback 链改 pytz → zoneinfo → 固定偏移.
#   原实现 pytz 缺失时的两个真缺陷:
#     ① ET_OFFSET 在 **import 时** 由 time.localtime().tm_isdst 决定, 而宿主
#        Asia/Shanghai 无 DST 恒为 0 → 永久固定 EST(-5), 夏令时全部慢 1 小时;
#     ② fallback 版 to_et() 直接 `return dt` 不做任何转换 (实测返回 CST 墙钟)。
#   zoneinfo 是 Python 3.9+ 标准库, 无外部依赖且正确处理 DST, 优先于固定偏移。
_HAS_ZONEINFO = False
if not _HAS_PYTZ:
    try:
        from zoneinfo import ZoneInfo
        _HAS_ZONEINFO = True
        logger.warning("[TimezoneUtils] 使用 zoneinfo fallback (DST 正确)")
    except ImportError:
        logger.error(
            "[TimezoneUtils] pytz 与 zoneinfo 均不可用 → 回落固定偏移, "
            "DST 期间 ET 时间将偏 1 小时, 请安装 pytz"
        )

# 美东时区 (交易时间基准)
if _HAS_PYTZ:
    ET = pytz.timezone("America/New_York")
elif _HAS_ZONEINFO:
    ET = ZoneInfo("America/New_York")
else:
    # 最后兜底: 固定偏移 (无 DST 感知, 夏令时会偏 1 小时)
    ET = timezone(timedelta(hours=-5))

# 中国时区 (服务器时间)
if _HAS_PYTZ:
    CST = pytz.timezone("Asia/Shanghai")
elif _HAS_ZONEINFO:
    CST = ZoneInfo("Asia/Shanghai")
else:
    CST = timezone(timedelta(hours=8))


def _localize(dt: datetime, tz) -> datetime:
    """给 naive datetime 附上时区 (统一 pytz/.localize 与 zoneinfo/.replace 的差异)."""
    if dt.tzinfo is not None:
        return dt
    if _HAS_PYTZ and hasattr(tz, "localize"):
        return tz.localize(dt)
    return dt.replace(tzinfo=tz)


# ═══════════════════════════════════════════════════════════════════
#  核心函数
# ═══════════════════════════════════════════════════════════════════

def now_et() -> datetime:
    """获取当前美东时间"""
    return datetime.now(ET)


def now_cst() -> datetime:
    """获取当前北京时间"""
    return datetime.now(CST)


def to_et(dt: datetime) -> datetime:
    """将任意时区时间转换为美东时间. naive 视为北京时间.

    R-13: 原 fallback 分支直接 `return dt` 不做转换 (实测返回 CST 墙钟且丢 tzinfo)。
    现三条 fallback 链行为一致。
    """
    if dt is None:
        return None
    return _localize(dt, CST).astimezone(ET)


def to_cst(dt: datetime) -> datetime:
    """将任意时区时间转换为北京时间. naive 视为美东时间.

    R-13: 同 to_et, 原 fallback 分支不做转换。
    """
    if dt is None:
        return None
    return _localize(dt, ET).astimezone(CST)


def _et_trade_date(entry_time) -> str:
    """Convert entry_time to US Eastern trading date (YYYY-MM-DD).

    P2' 修复 (2026-05-26): trade_date 原用 CST 日期，导致美东下午交易
    (CST 次日凌晨) 被标记到下一天，审计计数偏差。

    2026-07-24 修复: tz-aware 输入 (如 now_et()) 直接按 ET 日历日 — 原一律 -12h
    把 ET 时间当 CST 二次转换, ET 中午前读侧"今天"=昨天 (07-24 实锤: 账户熔断器
    日亏读到前一交易日)。

    2026-08-23 R-8c 修复 (DEB-20260823): naive 输入原用固定 `-timedelta(hours=12)`,
    注释断言"减 12h 对日期边界始终正确"**不成立** —— CST(UTC+8)→EDT(UTC-4) 差 12h,
    但 →EST(UTC-5) 差 **13h**。实测冬令时 naive CST 2026-01-15 12:30 得 2026-01-15,
    真值 2026-01-14。错位窗口 = CST 12:00-12:59 (对应 ET 前一日 23:00-23:59, 美股
    完全闭市, 故历史实际血量≈0), 但本函数是全仓交易日口径 SSOT, 不应留错误偏移。
    现改为按 CST 时区正确换算; pytz 不可用时回落原 -12h 行为 (至少夏令时正确)。
    """
    from datetime import timedelta
    if getattr(entry_time, "tzinfo", None) is not None:
        return entry_time.astimezone(ET).strftime("%Y-%m-%d")
    if _HAS_PYTZ or _HAS_ZONEINFO:
        try:
            return _localize(entry_time, CST).astimezone(ET).strftime("%Y-%m-%d")
        except Exception:
            pass
    return (entry_time - timedelta(hours=12)).strftime("%Y-%m-%d")


def is_market_open(dt: Optional[datetime] = None) -> bool:
    """
    判断美股是否开盘 (美东 9:30 - 16:00, 周一到周五, 含节假日检查)

    P2' 修复 (2026-05-26): 原实现无节假日检查，统一调用 holiday_calendar。

    Args:
        dt: 指定时间 (默认当前时间)

    Returns:
        True=开盘中, False=闭市
    """
    if dt is None:
        dt = now_et()
    else:
        # R-13: 统一走 _localize (naive 视为 CST), 三条 fallback 链行为一致
        dt = _localize(dt, CST).astimezone(ET)

    # 周末
    if dt.weekday() >= 5:
        return False

    # 节假日检查 — 统一入口
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        if get_holiday_calendar().is_us_holiday(dt.date()):
            return False
    except Exception:
        pass

    # ── R-8b 配套 (2026-08-23 DEB-20260823): 半日市动态收盘 ──
    # 原实现固定 `9.5 <= hour_min <= 16.0`。节假日已查, 但**提前收盘日不是节假日**
    # (2026-11-27 感恩节次日 13:00 收), 故半日市 13:00-16:00 仍被判开盘。
    # 另: 原上界用 `<=` 使 16:00 整被判开盘, 改 `<`。
    _close_mins = 16 * 60
    try:
        from app.utils.holiday_calendar import get_us_close_minutes_et
        _close_mins = int(get_us_close_minutes_et(dt.date()))
    except Exception:
        pass

    _mins = dt.hour * 60 + dt.minute
    return 570 <= _mins < _close_mins


def is_pre_market(dt: Optional[datetime] = None) -> bool:
    """判断是否为盘前 (美东 4:00 - 9:30)"""
    if dt is None:
        dt = now_et()
    else:
        # R-13: 统一走 _localize (naive 视为 CST)
        dt = _localize(dt, CST).astimezone(ET)

    if dt.weekday() >= 5:
        return False

    hour_min = dt.hour + dt.minute / 60.0
    return 4.0 <= hour_min < 9.5


def is_after_hours(dt: Optional[datetime] = None) -> bool:
    """判断是否为盘后 (美东 16:00 - 20:00)"""
    if dt is None:
        dt = now_et()
    else:
        # R-13: 统一走 _localize (naive 视为 CST)
        dt = _localize(dt, CST).astimezone(ET)

    if dt.weekday() >= 5:
        return False

    hour_min = dt.hour + dt.minute / 60.0
    return 16.0 <= hour_min <= 20.0


def get_market_session(dt: Optional[datetime] = None) -> str:
    """
    获取当前市场时段

    Returns:
        "pre_market" / "regular" / "after_hours" / "closed"
    """
    if dt is None:
        dt = now_et()
    else:
        # R-13: 统一走 _localize (naive 视为 CST)
        dt = _localize(dt, CST).astimezone(ET)

    if dt.weekday() >= 5:
        return "closed"

    # DEB-20260903 #8: 复用 is_market_open 的动态收盘分钟与 `<` 上界 —
    # 原固定 9.5<=hour_min<=16.0 (16:00 整误判 regular 且无半日市感知)
    _close_mins = 16 * 60
    try:
        from app.utils.holiday_calendar import get_us_close_minutes_et
        _close_mins = int(get_us_close_minutes_et(dt.date()))
    except Exception as _e:
        logger.debug(f"[TimezoneUtils] 动态收盘分钟获取失败, 回退 16:00: {_e}")

    _mins = dt.hour * 60 + dt.minute
    if 240 <= _mins < 570:
        return "pre_market"
    elif 570 <= _mins < _close_mins:
        return "regular"
    elif _close_mins <= _mins < 20 * 60:
        return "after_hours"
    return "closed"


def get_trade_date(dt: Optional[datetime] = None) -> str:
    """
    获取交易日 (美东日期, 格式: YYYY-MM-DD)

    用于数据库存储、PDT 计算等
    """
    if dt is None:
        dt = now_et()
    else:
        # R-13: 统一走 _localize (naive 视为 CST)
        dt = _localize(dt, CST).astimezone(ET)

    return dt.strftime("%Y-%m-%d")


def get_last_trade_date(dt: Optional[datetime] = None) -> str:
    """
    获取最近一个美股交易日 (ET naive, 跳过周末 + 美股节假日).

    周日 → 上周五, 周六 → 上周五, 工作日 → 当日.
    用于盘前预检、盘后审计等 "回顾上一交易日" 场景.
    """
    if dt is None:
        dt = now_et()
    et_naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
    # DEB-20260904 #135: 原实现只跳周末, 节假日 (如感恩节次日盘前) 会被当成
    # 交易日。回退链用离线硬编码表 (is_us_holiday), 日期计算不触网。
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        _cal = get_holiday_calendar()
    except Exception:
        _cal = None
    for _ in range(10):  # 防死循环兜底
        _wd = et_naive.weekday()
        _is_wknd = _wd >= 5
        _is_hol = False
        if not _is_wknd and _cal is not None:
            try:
                _is_hol = _cal.is_us_holiday(et_naive.date())
            except Exception:
                _is_hol = False
        if not _is_wknd and not _is_hol:
            break
        et_naive -= timedelta(days=1)
    return et_naive.strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════
#  便捷函数 (向后兼容)
# ═══════════════════════════════════════════════════════════════════

def is_trading_hours(dt: Optional[datetime] = None) -> bool:
    """向后兼容: 判断是否为交易时段"""
    return is_market_open(dt)


def get_current_session() -> str:
    """向后兼容: 获取当前时段"""
    return get_market_session()


# ═══════════════════════════════════════════════════════════════════
#  UTC 工具 + 安全比较 (原 datetime_utils.py, 2026-07-25 合并)
# ═══════════════════════════════════════════════════════════════════

UTC = timezone.utc


def now_utc() -> datetime:
    """获取当前 UTC 时间 (aware)"""
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """将任意 datetime 转为 UTC aware. naive 视为 UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def compare_safe(a: datetime, b: datetime) -> int:
    """
    安全比较两个 datetime, 自动处理 naive/aware 混用.
    Returns: -1 if a<b, 0 if equal, 1 if a>b. None 视为最大值.
    naive 视为 UTC.
    """
    if a is None and b is None:
        return 0
    if a is None:
        return 1
    if b is None:
        return -1
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
    """解析 ISO 字符串, 支持带/不带时区格式. 失败返回 None."""
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except Exception:
            return None


def market_phase_now() -> str:
    """返回当前 ET 市场 phase"""
    n = now_et()
    if n.weekday() >= 5:
        return "weekend"
    minutes = n.hour * 60 + n.minute
    if minutes < 240:
        return "overnight"
    if minutes < 570:
        return "premarket"
    if minutes < 590:
        return "opening_drive"
    if minutes < 690:
        return "morning"
    if minutes < 840:
        return "midday"
    if minutes < 930:
        return "afternoon"
    if minutes < 960:
        return "closing_drive"
    if minutes < 1200:
        return "after_hours"
    return "overnight"


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
    """phase-aware signal 过期时间, 返回 UTC aware datetime."""
    p = phase or market_phase_now()
    minutes = SIGNAL_EXPIRY_MINUTES_BY_PHASE.get(p, 10)
    return now_utc() + timedelta(minutes=minutes)
