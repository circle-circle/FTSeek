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
    logger.warning("[TimezoneUtils] pytz 未安装，使用 fallback 实现")

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  时区常量
# ═══════════════════════════════════════════════════════════════════

# 美东时区 (交易时间基准)
if _HAS_PYTZ:
    ET = pytz.timezone("America/New_York")
else:
    # Fallback: 固定偏移 (EST=-5, EDT=-4)
    # 美国夏令时: 3 月第二个周日 ~ 11 月第一个周日
    import time
    _is_dst = time.localtime().tm_isdst
    ET_OFFSET = timedelta(hours=-4 if _is_dst else -5)
    ET = timezone(ET_OFFSET)

# 中国时区 (服务器时间)
if _HAS_PYTZ:
    CST = pytz.timezone("Asia/Shanghai")
else:
    CST = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════════
#  核心函数
# ═══════════════════════════════════════════════════════════════════

def now_et() -> datetime:
    """获取当前美东时间"""
    if _HAS_PYTZ:
        return datetime.now(ET)
    return datetime.now(ET)


def now_cst() -> datetime:
    """获取当前北京时间"""
    if _HAS_PYTZ:
        return datetime.now(CST)
    return datetime.now(CST)


def to_et(dt: datetime) -> datetime:
    """将任意时区时间转换为美东时间"""
    if _HAS_PYTZ:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)  # 假设是北京时间
        return dt.astimezone(ET)
    return dt


def to_cst(dt: datetime) -> datetime:
    """将任意时区时间转换为北京时间"""
    if _HAS_PYTZ:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)  # 假设是美东时间
        return dt.astimezone(CST)
    return dt


def is_market_open(dt: Optional[datetime] = None) -> bool:
    """
    判断美股是否开盘 (美东 9:30 - 16:00, 周一到周五)

    Args:
        dt: 指定时间 (默认当前时间)

    Returns:
        True=开盘中, False=闭市
    """
    if dt is None:
        dt = now_et()
    elif _HAS_PYTZ and dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
        dt = dt.astimezone(ET)
    elif _HAS_PYTZ and dt.tzinfo != ET:
        dt = dt.astimezone(ET)

    # 周末
    if dt.weekday() >= 5:
        return False

    # 时间
    hour_min = dt.hour + dt.minute / 60.0
    return 9.5 <= hour_min <= 16.0


def is_pre_market(dt: Optional[datetime] = None) -> bool:
    """判断是否为盘前 (美东 4:00 - 9:30)"""
    if dt is None:
        dt = now_et()
    elif _HAS_PYTZ and dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
        dt = dt.astimezone(ET)

    if dt.weekday() >= 5:
        return False

    hour_min = dt.hour + dt.minute / 60.0
    return 4.0 <= hour_min < 9.5


def is_after_hours(dt: Optional[datetime] = None) -> bool:
    """判断是否为盘后 (美东 16:00 - 20:00)"""
    if dt is None:
        dt = now_et()
    elif _HAS_PYTZ and dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
        dt = dt.astimezone(ET)

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
    elif _HAS_PYTZ and dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
        dt = dt.astimezone(ET)

    if dt.weekday() >= 5:
        return "closed"

    hour_min = dt.hour + dt.minute / 60.0
    if 4.0 <= hour_min < 9.5:
        return "pre_market"
    elif 9.5 <= hour_min <= 16.0:
        return "regular"
    elif 16.0 <= hour_min <= 20.0:
        return "after_hours"
    return "closed"


def get_trade_date(dt: Optional[datetime] = None) -> str:
    """
    获取交易日 (美东日期, 格式: YYYY-MM-DD)

    用于数据库存储、PDT 计算等
    """
    if dt is None:
        dt = now_et()
    elif _HAS_PYTZ and dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
        dt = dt.astimezone(ET)

    return dt.strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════
#  便捷函数 (向后兼容)
# ═══════════════════════════════════════════════════════════════════

def is_trading_hours(dt: Optional[datetime] = None) -> bool:
    """向后兼容: 判断是否为交易时段"""
    return is_market_open(dt)


def get_current_session() -> str:
    """向后兼容: 获取当前时段"""
    return get_market_session()
