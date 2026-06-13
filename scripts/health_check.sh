#!/bin/bash
# FTSeek-US 健康检查
# 检查: 1) 进程存活 2) Dashboard 可达 3) DB 完整性
set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
STATE_FILE="$PROJECT_DIR/data/.health_state"
ISSUES=""

# 1. 主进程检查
if ! pgrep -f "main.py --start" >/dev/null 2>&1; then
    ISSUES="${ISSUES}• 🔴 main.py 进程不存在\n"
fi

# 2. Dashboard 检查 (port 8901)
HTTP_CODE=$(curl -sf -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8901/ 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "302" ]; then
    ISSUES="${ISSUES}• 🟡 Dashboard 不可达 (HTTP $HTTP_CODE)\n"
fi

# 3. DB 完整性 (快速检查主 DB)
for db in "$PROJECT_DIR/data/ftseek_us_live.db" "$PROJECT_DIR/data/ftseek_us.db"; do
    if [ -f "$db" ] && [ -s "$db" ]; then
        RESULT=$(sqlite3 "$db" "PRAGMA integrity_check;" 2>/dev/null || echo "FAILED")
        if [ "$RESULT" != "ok" ]; then
            ISSUES="${ISSUES}• 🔴 DB 损坏: $(basename $db) → $RESULT\n"
        fi
    fi
done

# 4. 磁盘空间
DISK_PCT=$(df /opt/ftseek | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK_PCT" -ge 85 ]; then
    ISSUES="${ISSUES}• 🟡 磁盘 ${DISK_PCT}% 使用\n"
fi

# 如果有问题且冷却已过 (30分钟)
if [ -n "$ISSUES" ]; then
    LAST_ALERT=$(cat "$STATE_FILE" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    DIFF=$((NOW - LAST_ALERT))
    
    if [ "$DIFF" -ge 1800 ]; then
        echo "$NOW" > "$STATE_FILE"
        
        MSG="[FTSeek-US] 🏥 健康检查异常\n\n${ISSUES}\n时间: $(date '+%Y-%m-%d %H:%M')"
        
        WEBHOOK=$(grep "DINGTALK_WEBHOOK_URL_US" "$PROJECT_DIR/.env.us" 2>/dev/null | cut -d= -f2-)
        if [ -n "$WEBHOOK" ]; then
            curl -sf -X POST "$WEBHOOK" \
                -H "Content-Type: application/json" \
                -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$MSG\"}}" \
                >/dev/null 2>&1 || true
        fi
    fi
fi
