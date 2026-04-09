#!/bin/bash
# FTSeek v2.1 停止脚本 (systemd 管理)

echo "========================================"
echo "  FTSeek v2.1 停止服务"
echo "========================================"
echo "时间：$(date)"
echo ""

# 使用 systemd 停止服务
if [ -f /etc/systemd/system/ftseek.service ]; then
    echo ">>> 使用 systemd 停止服务..."
    systemctl stop ftseek.service
    
    # 检查是否还有残留进程
    REMAINING=$(pgrep -f "python.*app/main.py" 2>/dev/null)
    if [ -n "$REMAINING" ]; then
        echo "⚠️ 发现残留进程: $REMAINING"
        echo ">>> 强制清理..."
        pkill -9 -f "python.*app/main.py" 2>/dev/null
        sleep 1
    fi
    
    # 清理进程锁
    rm -f /run/ftseek.lock 2>/dev/null
    
    echo "✅ FTSeek 服务已停止"
else
    echo "⚠️ systemd 服务文件不存在，手动停止..."
    pkill -9 -f "python.*app/main.py" 2>/dev/null
    echo "✅ 已清理所有进程"
fi

# 显示当前状态
echo ""
echo "=== 当前进程 ==="
ps aux | grep -E "python.*main\.py" | grep -v grep || echo "无运行进程"