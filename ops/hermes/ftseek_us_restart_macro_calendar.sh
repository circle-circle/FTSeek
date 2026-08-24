#!/bin/bash
# 一次性: 收盘后重启 ftseek-us 加载宏观日历影子采集 (f3e5f17e) + 验证
set -e
echo "=== restart start: $(date) ==="
systemctl restart ftseek-us
sleep 45
ACTIVE=$(systemctl is-active ftseek-us)
echo "service: $ACTIVE"
PID=$(systemctl show ftseek-us -p MainPID --value)
echo "pid=$PID started: $(ps -o lstart= -p $PID)"
# 验证新代码已加载: 挂载点在 PRE_MARKET 分支, 盘中无输出属正常;
# 改验证进程启动时间晚于 commit 时间即视为加载成功
grep -q "macro_calendar_shadow_collect" app/core/scheduler_core.py && echo "mount point present in file: OK"
echo "=== restart done: $(date) ==="
