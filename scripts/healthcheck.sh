#!/bin/bash
# FTSeek 健康检查脚本
# 2026-03-15 创建

set -e

LOG_FILE="/opt/ftseek/FTSeek/logs/healthcheck.log"
WEBHOOK_URL="${DINGTALK_WEBHOOK_URL:-}"
SECRET="${DINGTALK_SECRET:-}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

send_alert() {
    local message="$1"
    if [ -n "$WEBHOOK_URL" ]; then
        # 简单文本消息
        curl -s -X POST "$WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "{\"msgtype\": \"text\", \"text\": {\"content\": \"🚨 FTSeek 告警\\n\\n$message\"}}" \
            > /dev/null
        log "告警已发送：$message"
    else
        log "告警未发送 (WEBHOOK_URL 未配置): $message"
    fi
}

# 检查 1: HTTP 健康检查
log "=== 健康检查开始 ==="

HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" http://localhost:8888/health 2>/dev/null || echo "000")

if [ "$HTTP_CODE" != "200" ]; then
    log "❌ HTTP 健康检查失败：$HTTP_CODE"
    send_alert "FTSeek 服务异常\\nHTTP 状态码：$HTTP_CODE\\n时间：$(date '+%Y-%m-%d %H:%M:%S')"
    exit 1
fi

log "✅ HTTP 健康检查通过：$HTTP_CODE"

# 检查 2: 进程检查
FTSEEK_PID=$(pgrep -f "python3.8 -m app.main" | head -1 || echo "")

if [ -z "$FTSEEK_PID" ]; then
    log "❌ 进程检查失败：FTSeek 进程未运行"
    send_alert "FTSeek 进程已停止\\n时间：$(date '+%Y-%m-%d %H:%M:%S')\\n请检查 systemd 服务状态"
    exit 1
fi

log "✅ 进程检查通过：PID $FTSEEK_PID"

# 检查 3: 磁盘空间
DISK_USAGE=$(df -h /opt/ftseek | tail -1 | awk '{print $5}' | sed 's/%//')

if [ "$DISK_USAGE" -gt 85 ]; then
    log "⚠️ 磁盘空间警告：${DISK_USAGE}%"
    if [ "$DISK_USAGE" -gt 90 ]; then
        send_alert "磁盘空间不足\\n使用率：${DISK_USAGE}%\\n路径：/opt/ftseek"
    fi
else
    log "✅ 磁盘空间正常：${DISK_USAGE}%"
fi

# 检查 4: 内存使用
MEM_USAGE=$(ps -p $FTSEEK_PID -o %mem= 2>/dev/null | awk '{print int($1)}' || echo "0")

if [ "$MEM_USAGE" -gt 50 ]; then
    log "⚠️ 内存使用警告：${MEM_USAGE}%"
else
    log "✅ 内存使用正常：${MEM_USAGE}%"
fi

# 检查 5: 日志文件大小
LARGE_LOG=$(find /opt/ftseek/FTSeek/logs -name "*.log" -size +100M 2>/dev/null | head -1 || echo "")

if [ -n "$LARGE_LOG" ]; then
    log "⚠️ 日志文件过大：$LARGE_LOG"
else
    log "✅ 日志文件大小正常"
fi

log "=== 健康检查完成 ==="
exit 0
