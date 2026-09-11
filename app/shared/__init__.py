"""
shared 包 — 市场时段/阶段判定 SSOT (MarketHours / MarketType / get_market_phase)。

**2026-09-11 起收回本仓版本控制**, 此前是符号链接 `app/shared -> ../../FTSeek/app/shared`。
符号链接退役理由 (DEB-20260910-logging §11.12/§11.13):
  1. "双市场共享"这一存在理由已过期 —— 港股项目 FTSeek 于 2026-06-14 停用
     (`.DECOMMISSIONED`: 功能全迁 FTSeek-US, DO NOT RESTART; `ftseek.service` 已 masked),
     且 `/opt/ftseek/FTSeek` **没有 .git** ⇒ 一个被实盘导入的 24KB 模块寄居在
     仓外、无版本控制、标着"别删"的目录里 (不可追、不可回滚、清理即无声击穿)。
  2. `pathlib.rglob` **不跟随符号链接目录** ⇒ `scripts/deploy_freshness_check.py`
     的 `newest_code_ts()` 对 `app/shared/**` 完全失明 (实测 466 个 `.py` 命中里 0 个来自本目录),
     即"改了这里 → 服务继续跑旧代码, 而部署断层守卫永不自愈"。
     收回本仓后成为真目录, 守卫自动可见, 无需改守卫代码。
回滚底本: `/opt/ftseek/FTSeek/app/shared/` (全程只读未动; `market_hours.py` 逐字节一致迁入)。
"""

from .market_hours import (
    MarketType,
    MarketPhase,
    get_market_phase,
    is_trading_hours,
    is_trading_day,
    get_session_config,
    get_current_time,
    get_trading_periods,
    MarketHoursCompat,
    MarketHours,  # Alias for MarketHoursCompat (backward compat)
    MarketHoursValidator,
)

__all__ = [
    "MarketType",
    "MarketPhase",
    "get_market_phase",
    "is_trading_hours",
    "is_trading_day",
    "get_session_config",
    "get_current_time",
    "get_trading_periods",
    "MarketHoursCompat",
    "MarketHours",  # Alias for MarketHoursCompat
    "MarketHoursValidator",
]
