#!/bin/bash
# FTSeek v2.1 启动脚本 (Python 3.8+)

cd /opt/ftseek/FTSeek

# 设置环境变量
export PYTHONPATH=/opt/ftseek/FTSeek:$PYTHONPATH
export API_TOKEN=test123
export FTSEEK_DEV_MODE=1
export DINGTALK_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=cddea8919223640d4f3bd59e507373cae0df476df5f541fcca5c8921eab73121'
export DINGTALK_SECRET='SEC177f5321e91e689a1a2134e4414ae49e7b7f2cddc63775613effbf371f388f78'
export CALLBACK_EXTERNAL_URL=http://39.97.58.4:8899
export TRADE_ENV=1
export OPEND_HOST=127.0.0.1
export OPEND_PORT=11111

echo "========================================"
echo "  FTSeek v2.1 启动 (Python 3.8)"
echo "========================================"
echo "时间：$(date)"
echo "Python: $(python3.8 --version)"
echo "环境：模拟盘"
echo "========================================"
echo ""

# 启动主程序 (使用 Python 3.8)
nohup python3.8 app/main.py > logs/ftseek.log 2>&1 &
PID=$!

echo "✅ FTSeek 主程序已启动 (PID: $PID)"
echo ""
echo "日志：tail -f logs/ftseek.log"
echo "停止：kill $PID"
echo ""

sleep 5
echo "=== 最近日志 ==="
tail -30 logs/ftseek.log 2>&1 | head -20
