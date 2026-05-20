#!/bin/bash
# FTSeek-US 盘后审计 - 包装脚本
# 只在美东 16:15-17:00 (北京 04:15-05:00) 执行
# 周一到周五

cd /opt/ftseek/FTSeek-US
source venv/bin/activate

# 检查是否为工作日 (1-5)
# 注意: 北京时间凌晨对应美东前一天下午，所以周一凌晨对应美东周五
DAY=$(date +%u)
if [ "$DAY" -gt 5 ]; then
    echo "周末跳过"
    exit 0
fi

# 检查时间 (北京时间 04:00-06:00)
HOUR=$(date +%H)
if [ "$HOUR" -ge 4 ] && [ "$HOUR" -le 5 ]; then
    echo "=== FTSeek-US 盘后审计 $(date) ==="
    python scripts/post_market_audit.py
else
    echo "非盘后时间跳过 (当前北京时间 $(date +%H:%M))"
    exit 0
fi
