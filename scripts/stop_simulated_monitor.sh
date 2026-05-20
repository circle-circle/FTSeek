#!/bin/bash
# FTSeek-US 模拟盘监控停止脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"

PID_FILE="$LOG_DIR/simulated_monitor.pid"

echo "=============================================="
echo "🇺🇸 FTSeek-US 模拟盘监控停止"
echo "=============================================="
echo ""

if [ ! -f "$PID_FILE" ]; then
    echo "❌ PID 文件不存在，监控可能未启动"
    exit 0
fi

MONITOR_PID=$(cat "$PID_FILE")

if ! ps -p "$MONITOR_PID" > /dev/null 2>&1; then
    echo "⚠️  进程 $MONITOR_PID 已停止"
    rm -f "$PID_FILE"
    exit 0
fi

echo "停止监控进程 (PID: $MONITOR_PID)..."
kill "$MONITOR_PID"

# 等待进程退出
for i in {1..10}; do
    if ! ps -p "$MONITOR_PID" > /dev/null 2>&1; then
        echo "✅ 监控进程已停止"
        rm -f "$PID_FILE"
        exit 0
    fi
    sleep 1
done

# 强制停止
echo "⚠️  进程未响应，强制停止..."
kill -9 "$MONITOR_PID" 2>/dev/null || true
rm -f "$PID_FILE"

echo "✅ 监控进程已强制停止"
echo ""
echo "=============================================="
