#!/bin/bash
# FTSeek-US 盘前预检 - 包装脚本
# 只在美东 08:30-09:15 (北京 20:30-21:15) 执行
# 周一到周五

cd /opt/ftseek/FTSeek-US
source venv/bin/activate

# 检查是否为工作日 (1-5)
DAY=$(date +%u)
if [ "$DAY" -gt 5 ]; then
    echo "周末跳过"
    exit 0
fi

# 检查时间 (北京时间 20:00-22:00)
HOUR=$(date +%H)
if [ "$HOUR" -ge 20 ] && [ "$HOUR" -le 21 ]; then
    echo "=== FTSeek-US 盘前预检 $(date) ==="
    python scripts/pre_market_check.py
else
    echo "非盘前时间跳过 (当前北京时间 $(date +%H:%M))"
    exit 0
fi
