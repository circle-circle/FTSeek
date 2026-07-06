"""
盘后脚本节假日保护 — 统一入口

用法 (在脚本 main() 开头):
  from scripts.utils.market_guard import exit_if_not_trading_day
  exit_if_not_trading_day()

原理:
  cron 周期 2-6 (CST Tue-Sat) 对应 US Mon-Fri ET,
  但美股节假日 (Independence Day, Thanksgiving 等) cron 照常触发.
  此函数检查昨日是否为美股交易日, 非交易日则跳过 (sys.exit(0)).

注: 盘后脚本在 CST 凌晨运行, 处理的是"昨日 ET"的交易数据.
    所以检查 yesterday (ET) 是否为交易日.
"""
import sys
import os
from datetime import date, timedelta

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_DIR)


def exit_if_not_trading_day(days_back: int = 1) -> bool:
    """
    检查 N 天前 (默认1=昨日) 是否为美股交易日.
    非交易日 → 打印日志 + sys.exit(0) (cron 不报警).
    交易日 → return True, 继续执行.

    Args:
        days_back: 往前看几天 (1=昨日, 0=今天)

    Returns:
        True if trading day (继续执行)
    """
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        cal = get_holiday_calendar()
        # 盘后脚本在 CST 凌晨运行, 需要检查昨日 ET
        # ET 比 CST 慢 12-13 小时, 所以 CST 凌晨 = ET 前一天下午
        target_date = date.today() - timedelta(days=days_back)
        if not cal.is_trading_day("US", target_date):
            print(f"[market_guard] {target_date} 非美股交易日, 跳过盘后脚本")
            sys.exit(0)
        return True
    except Exception as e:
        # 节假日检查异常 → 保守放行 (不阻断)
        print(f"[market_guard] 节假日检查异常 (放行): {e}")
        return True
