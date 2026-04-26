#!/bin/bash
# FTSeek 手动触发收盘推送
# 用法: ./trigger_daily_report.sh [日期]
# 示例: ./trigger_daily_report.sh 2026-04-22
#        ./trigger_daily_report.sh  # 默认今天

cd /opt/ftseek/FTSeek

if [ -n "$1" ]; then
    DATE="$1"
    python3.8 -c "
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')
from app.review.signal_funnel import format_full_daily_report
report = format_full_daily_report('$DATE')
print(report)
"
else
    python3.8 -c "
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')
from app.review.signal_funnel import format_full_daily_report
report = format_full_daily_report()
print(report)
"
fi
