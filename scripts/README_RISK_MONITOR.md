# FTSeek 风险监控定时任务配置
# 推送频率：交易日每 2 小时 (10:00/12:00/14:00/16:00)

# 方式 1: 使用 cron (推荐)
# 编辑 crontab: crontab -e
# 添加以下行:

0 10,12,14,16 * * 1-5 cd /opt/ftseek/FTSeek && /usr/bin/python3 /opt/ftseek/FTSeek/scripts/risk_monitor.py >> /opt/ftseek/FTSeek/logs/risk_monitor.log 2>&1

# 方式 2: 使用 systemd timer
# 1. 复制服务文件到 systemd 目录:
#    sudo cp /opt/ftseek/FTSeek/scripts/ftseek-risk-monitor.service /etc/systemd/system/
#
# 2. 创建 timer 文件 /etc/systemd/system/ftseek-risk-monitor.timer:
#    [Unit]
#    Description=FTSeek Risk Monitor Timer
#    Documentation=file:///opt/ftseek/FTSeek/docs/plans/2026-03-01-ftseek-v3-refactor-plan.md
#
#    [Timer]
#    OnCalendar=*-*-* 10,12,14,16:00:00
#    Weekday=Mon,Tue,Wed,Thu,Fri
#    Persistent=true
#    Unit=ftseek-risk-monitor.service
#
#    [Install]
#    WantedBy=timers.target
#
# 3. 启用并启动 timer:
#    sudo systemctl daemon-reload
#    sudo systemctl enable ftseek-risk-monitor.timer
#    sudo systemctl start ftseek-risk-monitor.timer
#
# 4. 查看状态:
#    systemctl list-timers | grep ftseek
#    systemctl status ftseek-risk-monitor.timer

# 验证 crontab:
# crontab -l
# 应该看到:
# 0 10,12,14,16 * * 1-5 cd /opt/ftseek/FTSeek && /usr/bin/python3 /opt/ftseek/FTSeek/scripts/risk_monitor.py >> /opt/ftseek/FTSeek/logs/risk_monitor.log 2>&1

# 查看日志:
# tail -f /opt/ftseek/FTSeek/logs/risk_monitor.log

# 手动测试:
# cd /opt/ftseek/FTSeek && python3 scripts/risk_monitor.py --test
# cd /opt/ftseek/FTSeek && python3 scripts/risk_monitor.py --force
