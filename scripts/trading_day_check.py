#!/usr/bin/env python3
"""交易日检查脚本 — 被 cron 任务的 Step0 调用。

用法:
  python scripts/trading_day_check.py           # 检查美股 (默认)
  python scripts/trading_day_check.py --market US  # 显式指定美股
  echo $?  # 0=交易日, 1=非交易日
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
        if now_hkt.weekday() >= 6 or now_hkt.weekday() == 5:
            print("NON_TRADING_DAY")
            sys.exit(1)
        # 周六不交易
        if now_hkt.weekday() == 5:
            print("NON_TRADING_DAY")
            sys.exit(1)
        print("TRADING_DAY")
        sys.exit(0)
    
    # 美股交易日检查
    try:
        from app.utils.timezone import now_et
        from app.utils.holiday_calendar import US_HOLIDAYS
        et_now = now_et()
        if et_now.weekday() >= 5:  # 周六日
            print("NON_TRADING_DAY")
            sys.exit(1)
        if et_now.date() in US_HOLIDAYS:
            print("NON_TRADING_DAY")
            sys.exit(1)
        print("TRADING_DAY")
        sys.exit(0)
    except Exception as e:
        # fallback: 只检查工作日
        from datetime import datetime, timezone, timedelta
        et = timezone(timedelta(hours=-4))  # EDT
        now_et = datetime.now(et)
        if now_et.weekday() >= 5:
            print("NON_TRADING_DAY")
            sys.exit(1)
        print("TRADING_DAY")
        sys.exit(0)

if __name__ == "__main__":
    main()
