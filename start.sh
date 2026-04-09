#!/bin/bash
# FTSeek v2.1 启动脚本 (systemd 管理)
# 更新日期: 2026-03-31 - 使用 systemd 确保进程唯一

echo "========================================"
echo "  FTSeek v2.1 启动 (systemd 管理)"
echo "========================================"
echo "时间：$(date)"
echo "Python: $(python3.8 --version)"
echo "环境：模拟盘"
echo "监控标的: 泡泡玛特 + 中芯国际 + 美团"
echo "========================================"
echo ""

# 检查 systemd 服务是否存在
if [ -f /etc/systemd/system/ftseek.service ]; then
    echo ">>> 使用 systemd 管理服务..."
    
    # 检查服务状态
    if systemctl is-active --quiet ftseek.service; then
        echo "⚠️ FTSeek 服务已在运行"
        systemctl status ftseek.service --no-pager | head -10
        echo ""
        echo "如需重启，请使用: systemctl restart ftseek.service"
        exit 0
    fi
    
    # 启动服务
    echo ">>> 启动 FTSeek 服务..."
    systemctl start ftseek.service
    sleep 3
    
    # 检查启动结果
    if systemctl is-active --quiet ftseek.service; then
        echo "✅ FTSeek 服务启动成功"
        systemctl status ftseek.service --no-pager | head -10
    else
        echo "❌ FTSeek 服务启动失败"
        journalctl -u ftseek.service -n 20 --no-pager
        exit 1
    fi
else
    echo "⚠️ systemd 服务文件不存在，使用传统方式启动..."
    
    # 清理旧进程
    echo ">>> 检查并清理旧进程..."
    OLD_PIDS=$(pgrep -f "python.*app/main.py" 2>/dev/null)
    if [ -n "$OLD_PIDS" ]; then
        echo ">>> 发现旧进程，正在清理: $OLD_PIDS"
        pkill -9 -f "python.*app/main.py" 2>/dev/null
        sleep 2
        echo "✅ 旧进程已清理"
    fi
    
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
    export WATCH_LIST_HK='["HK.09992", "HK.00981", "HK.03690"]'
    export CONSENSUS_THRESHOLD=0.4
    export SIGNAL_COOLDOWN=60
    export DEFAULT_ORDER_QTY_HK=100
    
    # 启动主程序
    nohup python3.8 app/main.py > logs/ftseek.log 2>&1 &
    PID=$!
    echo "✅ FTSeek 主程序已启动 (PID: $PID)"
fi

echo ""
echo "日志：tail -f logs/ftseek.log"
echo "停止：systemctl stop ftseek.service"
echo "重启：systemctl restart ftseek.service"
echo ""
echo "=== 最近日志 ==="
tail -30 logs/ftseek.log 2>&1 | head -20
