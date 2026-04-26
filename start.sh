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
    
    # 设置环境变量 - 从 .env 统一加载，避免硬编码导致与 config.yaml 不一致
    export PYTHONPATH=/opt/ftseek/FTSeek:$PYTHONPATH
    
    # 加载 .env 配置 (敏感信息和运行时参数统一由 .env + config.yaml 管理)
    ENV_FILE="/opt/ftseek/FTSeek/.env"
    if [ -f "$ENV_FILE" ]; then
        echo ">>> 从 $ENV_FILE 加载环境变量..."
        set -a
        source "$ENV_FILE"
        set +a
    else
        echo "⚠️ .env 文件不存在: $ENV_FILE，使用最小默认配置"
        export TRADE_ENV=1
        export OPEND_HOST=127.0.0.1
        export OPEND_PORT=11111
    fi
    
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
