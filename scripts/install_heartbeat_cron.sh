#!/bin/bash
# 业务心跳 crontab 安装 (2026-08-24 用户已批准) — 幂等, 自动备份, 可重跑
set -e
crontab -l > /tmp/crontab_backup_20260824.txt

if crontab -l | grep -q business_heartbeat.sh; then
    echo "(已安装过, 跳过) 当前相关条目:"
    crontab -l | grep business_heartbeat
    exit 0
fi

(crontab -l
 echo ''
 echo '# 业务心跳: 盘中指令数=0告警 (每30min宽窗, ET窗口脚本自判DST免改 — 复查A6②)'
 echo '*/30 21-23 * * 1-5 /bin/bash /opt/ftseek/FTSeek-US/scripts/business_heartbeat.sh 2>/dev/null'
 echo '*/30 0-5  * * 2-6 /bin/bash /opt/ftseek/FTSeek-US/scripts/business_heartbeat.sh 2>/dev/null') | crontab -

echo "✅ 已安装 (备份在 /tmp/crontab_backup_20260824.txt):"
crontab -l | tail -3
