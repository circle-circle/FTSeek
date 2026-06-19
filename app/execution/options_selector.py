"""
期权工具函数

ADR-003 (2026-05-21): 期权交易已永久禁用。
本模块仅保留 days_to_expiry 工具函数供 position_recovery.py 和 daily_review.py 使用。
"""
import logging
from datetime import datetime, date

logger = logging.getLogger(__name__)


def days_to_expiry(expiry_date: str) -> int:
    """计算距到期天数
    
    Args:
        expiry_date: 到期日期字符串，格式 YYYY-MM-DD
        
    Returns:
        距到期天数，解析失败返回 999
    """
    try:
        exp = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        return (exp - date.today()).days
    except Exception:
        return 999
