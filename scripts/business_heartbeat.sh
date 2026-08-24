#!/bin/bash
# FTSeek-US 业务心跳推送壳 (2026-08-24, 复查A6②)
# 调 scripts/business_heartbeat.py 判"交易日盘中指令数=0", 非空输出=异常 →
# 记日志 + 30min 冷却 + 钉钉推送。镜像 health_check.sh 同款模式。
# crontab (宽 CST 窗口, ET 窗口由 py 脚本自判, DST 免改):
#   */30 21-23 * * 1-5 /bin/bash /opt/ftseek/FTSeek-US/scripts/business_heartbeat.sh 2>/dev/null
#   */30 0-5  * * 2-6 /bin/bash /opt/ftseek/FTSeek-US/scripts/business_heartbeat.sh 2>/dev/null

PROJECT_DIR="/opt/ftseek/FTSeek-US"
STATE_FILE="$PROJECT_DIR/data/.heartbeat_state"
LOG_FILE="$PROJECT_DIR/logs/healthcheck.log"

OUT=$("$PROJECT_DIR/venv/bin/python3" "$PROJECT_DIR/scripts/business_heartbeat.py" 2>&1)
RC=$?

if [ "$RC" -ne 0 ] && [ -n "$OUT" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 业务心跳异常: $(echo "$OUT" | head -1)" >> "$LOG_FILE"

    # 冷却: 30 分钟内只推送一次 (与 health_check 共用节奏)
    LAST_ALERT=$(cat "$STATE_FILE" 2>/dev/null || echo "0")
    NOW=$(date +%s)
    DIFF=$((NOW - LAST_ALERT))

    if [ "$DIFF" -ge 1800 ]; then
        echo "$NOW" > "$STATE_FILE"
        MSG="[FTSeek-US] 💓 业务心跳告警\n\n$OUT\n时间: $(date '+%Y-%m-%d %H:%M')"

        WEBHOOK=$(grep "DINGTALK_WEBHOOK_URL_US" "$PROJECT_DIR/.env.us" 2>/dev/null | cut -d= -f2-)
        if [ -n "$WEBHOOK" ]; then
            curl -sf -X POST "$WEBHOOK" \
                -H "Content-Type: application/json" \
                -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$MSG\"}}" \
                >/dev/null 2>&1 || true
        fi
    fi
fi
