#!/bin/bash
# run_premarket_hk.sh - FTSeek 港股盘前预检封装脚本
# 在港股开盘前运行（09:00 CST），检查系统是否准备好交易

cd /opt/ftseek/FTSeek

echo "=============================================="
echo "  FTSeek 港股盘前预检"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="

# 1. 运行盘前预检 v2
echo ""
echo ">>> 运行盘前预检 v2..."
python3.8 scripts/auto_audit/pre_market_check_v2.py 2>&1
PREMARKET_EXIT=$?

# 2. 运行队列健康检查
echo ""
echo ">>> 运行行情队列健康检查..."
python3.8 scripts/auto_audit/check_quote_queue.py 2>&1
QUEUE_EXIT=$?

echo ""
echo "=============================================="
echo "  预检完成"
echo "  盘前预检: $([ $PREMARKET_EXIT -eq 0 ] && echo '✅ 通过' || echo '❌ 失败')"
echo "  队列检查: $([ $QUEUE_EXIT -eq 0 ] && echo '✅ 通过' || echo '❌ 异常')"
echo "=============================================="

# 综合状态
if [ $PREMARKET_EXIT -ne 0 ] || [ $QUEUE_EXIT -ne 0 ]; then
    exit 1
fi
exit 0
