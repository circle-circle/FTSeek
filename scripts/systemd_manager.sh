#!/bin/bash
# FTSeek Systemd 服务管理脚本
# 使用方法: ./systemd_manager.sh [status|start|stop|restart|logs]

set -e

SERVICES=("ftseek.service" "ftseek-us.service")

status() {
    echo "=================================="
    echo "FTSeek 服务状态"
    echo "=================================="
    for svc in "${SERVICES[@]}"; do
        systemctl status "$svc" --no-pager | grep -E "Active:|Main PID:"
    done
    echo ""
    echo "端口监听:"
    netstat -tlnp | grep -E "8899|8898|8897" || echo "无监听端口"
}

start() {
    echo "启动所有 FTSeek 服务..."
    for svc in "${SERVICES[@]}"; do
        systemctl start "$svc" && echo "✅ $svc 启动成功"
    done
}

stop() {
    echo "停止所有 FTSeek 服务..."
    for svc in "${SERVICES[@]}"; do
        systemctl stop "$svc" && echo "✅ $svc 已停止"
    done
    rm -f /run/ftseek.lock /run/ftseek-us.lock
}

restart() {
    echo "重启所有 FTSeek 服务..."
    stop
    sleep 2
    start
}

logs() {
    local service="${1:-ftseek}"
    case "$service" in
        ftseek|hk)
            tail -f /opt/ftseek/FTSeek/logs/ftseek.log
            ;;
        us)
            tail -f /opt/ftseek/FTSeek-US/logs/ftseek_us.log
            ;;
        *)
            echo "用法: $0 logs [ftseek|us]"
            ;;
    esac
}

case "$1" in
    status)
        status
        ;;
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    logs)
        logs "$2"
        ;;
    *)
        echo "用法: $0 {status|start|stop|restart|logs [ftseek|us]}"
        exit 1
        ;;
esac
