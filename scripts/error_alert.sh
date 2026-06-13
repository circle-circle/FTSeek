#!/bin/bash
# FTSeek-US 错误日志告警
# 检测最近 15 分钟内 ERROR/CRITICAL 数量，超阈值推送钉钉
set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
LOG_FILE="$PROJECT_DIR/logs/ftseek_us.log"
STATE_FILE="$PROJECT_DIR/data/.alert_state"
THRESHOLD=20  # 15分钟内超过20条ERROR触发告警

[ -f "$LOG_FILE" ] || exit 0

# 计算 15 分钟前的时间戳
CUTOFF=$(date -d '15 minutes ago' '+%Y-%m-%d %H:%M' 2>/dev/null)
if [ -z "$CUTOFF" ]; then
    CUTOFF=$(date -v-15M '+%Y-%m-%d %H:%M' 2>/dev/null)
fi

# 统计最近错误
RECENT_ERRORS=$(awk -v cutoff="$CUTOFF" '
    $0 ~ /ERROR|CRITICAL/ && $1 " " $2 >= cutoff { count++ }
    END { print count+0 }
' "$LOG_FILE")

# 冷却检查 (同一类错误 1 小时内只告警一次)
ALERT_KEY="error_spike_$(date '+%Y%m%d%H')"
LAST_ALERT=$(cat "$STATE_FILE" 2>/dev/null || echo "")

if [ "$RECENT_ERRORS" -ge "$THRESHOLD" ] && [ "$LAST_ALERT" != "$ALERT_KEY" ]; then
    echo "$ALERT_KEY" > "$STATE_FILE"
    
    # 提取 Top 3 错误类型
    TOP_ERRORS=$(awk -v cutoff="$CUTOFF" '
        $0 ~ /ERROR|CRITICAL/ && $1 " " $2 >= cutoff {
            # 提取关键错误信息 (去掉时间戳和前缀)
            msg = $0
            gsub(/^[0-9-]+ [0-9:]+[,.][0-9]+ /, "", msg)
            gsub(/^.*?(ERROR|CRITICAL).*?[-—] /, "", msg)
            errors[msg]++
        }
        END {
            n = asorti(errors, sorted, "@val_num_desc")
            for (i=1; i<=3 && i<=n; i++) {
                printf "• [%d次] %s\n", errors[sorted[i]], substr(sorted[i], 1, 80)
            }
        }
    ' "$LOG_FILE")
    
    # 推送钉钉
    MSG="[FTSeek-US] ⚠️ 错误激增告警\n\n最近15分钟: ${RECENT_ERRORS} 条 ERROR/CRITICAL\n\nTop 错误:\n${TOP_ERRORS}\n\n时间: $(date '+%H:%M')"
    
    WEBHOOK=$(grep "DINGTALK_WEBHOOK_URL_US" "$PROJECT_DIR/.env.us" 2>/dev/null | cut -d= -f2-)
    if [ -n "$WEBHOOK" ]; then
        curl -sf -X POST "$WEBHOOK" \
            -H "Content-Type: application/json" \
            -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$MSG\"}}" \
            >/dev/null 2>&1 || true
    fi
fi
