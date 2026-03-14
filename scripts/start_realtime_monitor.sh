#!/bin/bash
#
# FTSeek 实时行情监控服务
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON:-python3}"

echo "=============================================="
echo "  FTSeek 实时行情监控服务"
echo "=============================================="
echo ""

# 检查 Python
if ! command -v $PYTHON &> /dev/null; then
    echo "❌ 错误：未找到 $PYTHON"
    exit 1
fi
echo "✅ Python: $($PYTHON --version)"

# 检查环境变量
echo ""
echo "🔧 检查配置..."

if [ -z "$API_TOKEN" ]; then
    echo "⚠️  警告：API_TOKEN 未设置"
fi

if [ -n "$FUTU_HOST" ]; then
    echo "✅ 富途主机：$FUTU_HOST"
else
    echo "❌ 错误：FUTU_HOST 未设置"
    exit 1
fi

if [ -n "$FUTU_PORT" ]; then
    echo "✅ 富途端口：$FUTU_PORT"
else
    echo "❌ 错误：FUTU_PORT 未设置"
    exit 1
fi

if [ -n "$FTSEEK_TRADING_MODE" ]; then
    echo "✅ 交易模式：$FTSEEK_TRADING_MODE"
else
    echo "⚠️  交易模式未设置，使用默认 (paper)"
fi

if [ -n "$DINGTALK_WEBHOOK_URL" ]; then
    echo "✅ 钉钉推送：已配置"
else
    echo "❌ 错误：DINGTALK_WEBHOOK_URL 未设置"
    exit 1
fi

# 创建日志目录
mkdir -p "$PROJECT_DIR/logs"

# 启动服务
echo ""
echo "🚀 启动实时行情监控..."
echo "   工作目录：$PROJECT_DIR"
echo "   日志文件：$PROJECT_DIR/logs/realtime_monitor.log"
echo ""

cd "$PROJECT_DIR"
exec $PYTHON scripts/realtime_monitor.py
