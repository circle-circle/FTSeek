#!/bin/bash
# 复盘报告生成脚本
# 用法: ./run_review.sh [daily|weekly|monthly]

cd /opt/ftseek/FTSeek

case "$1" in
    daily)
        python3 scripts/daily_review_v2.py
        ;;
    weekly)
        python3 scripts/weekly_review.py 2>/dev/null || echo "周报脚本不存在"
        ;;
    monthly)
        python3 scripts/monthly_review.py 2>/dev/null || echo "月报脚本不存在"
        ;;
    *)
        python3 scripts/daily_review_v2.py
        ;;
esac
