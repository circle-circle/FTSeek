#!/usr/bin/env python3
"""交易日检查脚本 — 被 cron 任务的 Step0 调用。

用法:
  python scripts/trading_day_check.py           # 检查美股 (默认)
  python scripts/trading_day_check.py --market US  # 显式指定美股
  # 输出 "TRADING_DAY" 或 "NON_TRADING_DAY"，exit code 始终 0
  # 调用方按 stdout 内容判断，不按 exit code
"""
import sys
import os

# 确保能 import app 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    import argparse
    parser = argparse.ArgumentParser(description="US trading day check")
    parser.add_argument("--market", default="US", help="Market (US/HK, default=US)")
    args = parser.parse_args()
    
    if args.market.upper() == "HK":
        # 港股交易日检查 (简化版)
        from datetime import datetime, timezone, timedelta
        # 港股交易时间: 09:30-16:00 HKT (UTC+8)
        hkt = timezone(timedelta(hours=8))
        now_hkt = datetime.now(hkt)
        if now_hkt.weekday() >= 5:
            print("NON_TRADING_DAY")
            return
        print("TRADING_DAY")
        return
    
    # 美股交易日检查
    try:
        from app.utils.timezone import now_et
        from app.utils.holiday_calendar import US_HOLIDAYS
        et_now = now_et()
        if et_now.weekday() >= 5:  # 周六日
            print("NON_TRADING_DAY")
            return
        if et_now.date() in US_HOLIDAYS:
            print("NON_TRADING_DAY")
            return
        print("TRADING_DAY")
    except Exception as e:
        # fallback: 只检查工作日
        from app.utils.timezone import now_et as _now_et
        et_now = _now_et()
        if et_now.weekday() >= 5:
            print("NON_TRADING_DAY")
            return
        print("TRADING_DAY")

if __name__ == "__main__":
    main()
