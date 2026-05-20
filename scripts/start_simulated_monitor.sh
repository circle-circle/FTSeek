#!/bin/bash
# FTSeek-US 模拟盘监控启动脚本
# 2026-03-27 版本：使用方案 B（修复后策略）

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"

# 创建日志目录
mkdir -p "$LOG_DIR"

echo "=============================================="
echo "🇺🇸 FTSeek-US 模拟盘监控启动"
echo "=============================================="
echo ""
echo "启动时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "策略版本：方案 B (修复后策略)"
echo "交易环境：模拟盘"
echo ""

# 检查环境变量
echo "检查配置..."
source "$PROJECT_DIR/.env.us"

if [ -z "$FEISHU_WEBHOOK_URL_US" ]; then
    echo "❌ FEISHU_WEBHOOK_URL_US 未配置，无法推送告警"
    exit 1
fi

if [ "$TRADE_ENV" != "1" ]; then
    echo "⚠️  警告：TRADE_ENV=$TRADE_ENV (应为 1=模拟盘)"
    echo "   建议修改 .env.us: TRADE_ENV=1"
fi

echo "✅ 配置检查通过"
echo ""

# 检查 OpenD 服务
echo "检查 OpenD 服务..."
if ! pgrep -f "opend" > /dev/null 2>&1; then
    echo "⚠️  警告：OpenD 服务未运行"
    echo "   如需使用富途行情，请先启动 OpenD:"
    echo "   cd /opt/futu && ./start_opend.sh"
    echo ""
else
    echo "✅ OpenD 服务运行中"
fi
echo ""

# 启动监控进程
echo "启动监控进程..."
cd "$PROJECT_DIR"

# 使用 nohup 后台运行
nohup python3 -u scripts/us_stock_monitor.py > "$LOG_DIR/simulated_monitor.log" 2>&1 &
MONITOR_PID=$!

echo "✅ 监控进程已启动 (PID: $MONITOR_PID)"
echo ""

# 保存 PID
echo "$MONITOR_PID" > "$LOG_DIR/simulated_monitor.pid"

# 显示监控状态
echo "=============================================="
echo "📊 监控状态"
echo "=============================================="
echo ""
echo "进程 PID: $MONITOR_PID"
echo "日志文件：$LOG_DIR/simulated_monitor.log"
echo "PID 文件：$LOG_DIR/simulated_monitor.pid"
echo ""
echo "查看日志：tail -f $LOG_DIR/simulated_monitor.log"
echo "停止监控：bash $SCRIPT_DIR/stop_simulated_monitor.sh"
echo ""
echo "=============================================="
echo "✅ 模拟盘监控已启动，祝交易顺利！🇺🇸📈"
echo "=============================================="
