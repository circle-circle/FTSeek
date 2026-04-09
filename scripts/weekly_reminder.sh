#!/bin/bash
# 每周复盘提醒脚本

TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")
DASHBOARD_URL="http://39.97.58.4:8899/simulation/dashboard"
REMINDER_FILE="/opt/ftseek/FTSeek/logs/weekly_reminder.log"

echo "[$TIMESTAMP] 🔔 FTSeek 模拟盘复盘提醒" >> $REMINDER_FILE
echo "[$TIMESTAMP] 📊 Dashboard: $DASHBOARD_URL" >> $REMINDER_FILE
echo "[$TIMESTAMP] ✅ 请查看本周交易记录、分析绩效、调整策略" >> $REMINDER_FILE
echo "[$TIMESTAMP] ========================================" >> $REMINDER_FILE

# 如果配置了钉钉 webhook，发送钉钉通知
if [ -n "$DINGTALK_WEBHOOK_URL" ]; then
    curl -X POST "$DINGTALK_WEBHOOK_URL" \
        -H "Content-Type: application/json" \
        -d "{
            \"msgtype\": \"text\",
            \"text\": {
                \"content\": \"🔔 FTSeek 模拟盘复盘提醒\\n📊 Dashboard: $DASHBOARD_URL\\n✅ 请查看本周交易记录、分析绩效、调整策略\\n⏰ 时间：$(date +'%Y-%m-%d %H:%M')\"
            }
        }" 2>/dev/null
    echo "[$TIMESTAMP] 📱 钉钉通知已发送" >> $REMINDER_FILE
fi
