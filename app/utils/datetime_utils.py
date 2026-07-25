"""
时区安全 datetime 工具 — 已合并至 app.utils.timezone (2026-07-25)

本模块保留为向后兼容 shim, 所有实现已迁移到 app.utils.timezone.
新代码请直接 from app.utils.timezone import ...
"""
from app.utils.timezone import (  # noqa: F401
    ET,
    UTC,
    now_et,
    now_utc,
    now_cst,
    to_et,
    to_utc,
    to_cst,
    compare_safe,
    is_expired_safe,
    parse_iso_safe,
    market_phase_now,
    signal_expires_at,
    SIGNAL_EXPIRY_MINUTES_BY_PHASE,
    get_trade_date,
    get_market_session,
    is_market_open,
    _et_trade_date,
)
