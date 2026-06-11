"""数值安全转换工具 (SSOT)

统一 safe_float 实现, 消除散落在 data_service / manual_order_service / position_monitor 中的重复定义。
SQLite NULL 字段 → float(None) 会抛 TypeError, 此函数统一兜底。
"""
from typing import Any


def safe_float(v: Any, default: float = 0.0) -> float:
    """安全转 float: None / 'N/A' / 异常值 → default
    
    Usage:
        from app.utils.numeric import safe_float
        price = safe_float(row.get("cur_price"))
    """
    if v is None:
        return default
    try:
        result = float(v)
        return result
    except (ValueError, TypeError):
        return default
