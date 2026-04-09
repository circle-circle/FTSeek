#!/bin/bash
# FTSeek v2.1 重启脚本 (systemd 管理)

echo "========================================"
echo "  FTSeek v2.1 重启服务"
echo "========================================"
echo "时间：$(date)"
echo ""

# 使用 systemd 重启服务
if [ -f /etc/systemd/system/ftseek.service ]; then
    echo ">>> 使用 systemd 重启服务..."
    systemctl restart ftseek.service
    sleep 3
    
    # 检查服务状态
    if systemctl is-active --quiet ftseek.service; then
        echo "✅ FTSeek 服务重启成功"
        echo ""
        systemctl status ftseek.service --no-pager | head -10
    else
        echo "❌ FTSeek 服务重启失败"
        journalctl -u ftseek.service -n 20 --no-pager
        exit 1
    fi
else
    echo "⚠️ systemd 服务文件不存在，使用传统方式..."
    
    # 停止旧进程
    echo ">>> 停止旧进程..."
    pkill -9 -f "python.*app/main.py" 2>/dev/null
    sleep 2
    
    # 启动新进程
    echo ">>> 启动新进程..."
    ./start.sh
fi

echo ""
echo "日志：tail -f logs/ftseek.log"