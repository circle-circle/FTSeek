#!/bin/bash
# FTSeek 开盘前检查脚本
# 每日开盘前运行，确保系统正常

echo "================================================"
echo "FTSeek 开盘前检查 - $(date '+%Y-%m-%d %H:%M')"
echo "================================================"

cd /opt/ftseek/FTSeek

# 1. 验证模拟交易模式
echo ""
echo "📋 1. 验证模拟交易模式..."
python3 scripts/verify_simulate_mode.py

# 2. 检查 OpenD 连接
echo ""
echo "📋 2. 检查富途 OpenD 连接..."
timeout 10 python3 -c "
from futu import OpenQuoteContext
try:
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    ret, data = ctx.get_global_state()
    if ret == 0:
        print('  ✅ OpenD 连接正常')
    else:
        print(f'  ❌ OpenD 状态异常: {data}')
    ctx.close()
except Exception as e:
    print(f'  ❌ 连接失败: {e}')
"

# 3. 检查监控股票
echo ""
echo "📋 3. 监控股票列表..."
cat config/watchlist.json | python3 -c "
import json, sys
data = json.load(sys.stdin)
print('  港股监控:')
for stock in data.get('hk_stocks', []):
    status = '✅' if stock.get('enabled') else '⏸️'
    print(f'    {status} {stock[\"code\"]} {stock[\"name\"]}')
print('  美股监控:')
if data.get('us_stocks'):
    for stock in data['us_stocks']:
        status = '✅' if stock.get('enabled') else '⏸️'
        print(f'    {status} {stock[\"code\"]} {stock[\"name\"]}')
else:
    print('    无')
"

# 4. 检查昨日信号
echo ""
echo "📋 4. 检查近期信号..."
YESTERDAY=$(date -d "yesterday" '+%Y-%m-%d')
TODAY=$(date '+%Y-%m-%d')
for d in $YESTERDAY $TODAY; do
    SIGNAL_FILE="data/signal_records/signals_${d}.json"
    if [ -f "$SIGNAL_FILE" ]; then
        COUNT=$(python3 -c "import json; print(len(json.load(open('$SIGNAL_FILE'))))")
        echo "  📊 $d: $COUNT 个信号"
    else
        echo "  📊 $d: 无数据"
    fi
done

echo ""
echo "================================================"
echo "✅ 开盘前检查完成"
echo "================================================"
echo ""
echo "📌 今日提醒:"
echo "   - 交易模式: 模拟盘"
echo "   - 监控标的: 泡泡玛特、中芯国际"
echo "   - 系统就绪，等待开市"
echo ""