#!/bin/bash
# FTSeek-US 健康检查
# 检查: 1) 进程存活 2) Dashboard 可达 3) DB 完整性 4) OpenD 连通性
set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
STATE_FILE="$PROJECT_DIR/data/.health_state"
LOG_FILE="$PROJECT_DIR/logs/healthcheck.log"
ISSUES=""

# 1. 主进程检查
if ! pgrep -f "main.py --start" >/dev/null 2>&1; then
    ISSUES="${ISSUES}• 🔴 main.py 进程不存在\n"
fi

# 2. Dashboard 检查 (port 8901)
# 2026-07-31: dashboard 改按需激活 (socket unit 持有监听, 闲置进程自退)。
# 不能再 curl HTTP (会把服务唤醒, 按需激活失效) — 改为被动检查端口在听。
if ! ss -tln 2>/dev/null | grep -q ':8901 '; then
    ISSUES="${ISSUES}• 🟡 Dashboard 端口未监听 (8901, socket unit 异常)\n"
fi

# 3. OpenD 连通性检查 (port 11111)
if command -v nc >/dev/null 2>&1; then
    if ! nc -z 127.0.0.1 11111 -w 3 2>/dev/null; then
        ISSUES="${ISSUES}• 🔴 Futu OpenD 不可达 (port 11111)\n"
    fi
fi

# 4. DB 完整性 (快速检查主 DB)
for db in "$PROJECT_DIR/data/ftseek_us_live.db" "$PROJECT_DIR/data/ftseek_us.db"; do
    if [ -f "$db" ] && [ -s "$db" ]; then
        RESULT=$(sqlite3 "$db" "PRAGMA integrity_check;" 2>/dev/null || echo "FAILED")
        if [ "$RESULT" != "ok" ]; then
            ISSUES="${ISSUES}• 🔴 DB 损坏: $(basename $db) → $RESULT\n"
        fi
    fi
done

# 5. 磁盘空间
DISK_PCT=$(df /opt/ftseek | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK_PCT" -ge 85 ]; then
    ISSUES="${ISSUES}• 🟡 磁盘 ${DISK_PCT}% 使用\n"
fi

# 6. 内存水位 (2026-07-31: 07-30 内核 OOM 杀 hermes 零预警的教训)
AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
if [ -n "$AVAIL_MB" ] && [ "$AVAIL_MB" -lt 300 ]; then
    TOP3=$(ps aux --sort=-rss | awk 'NR>1 && NR<=4 {printf "%s(%.0fMB) ", $11, $6/1024}')
    ISSUES="${ISSUES}• 🔴 可用内存 ${AVAIL_MB}MB < 300MB | 大户: ${TOP3}\n"
fi

# 记录结果到日志
NOW_STR=$(date '+%Y-%m-%d %H:%M:%S')
if [ -n "$ISSUES" ]; then
    echo "[$NOW_STR] ⚠️ 健康检查异常: $(echo -e "$ISSUES" | tr '\n' ' ')" >> "$LOG_FILE"

    # 冷却: 30分钟内同类告警只推送一次
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
else
    echo "[$NOW_STR] ✅ 健康检查通过" >> "$LOG_FILE"
fi
