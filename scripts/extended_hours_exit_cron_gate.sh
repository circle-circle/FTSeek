#!/bin/bash
# 2026-10-18 空跑治理: 延长时段退出 cron 的 ET 窗口闸。
#
# 原 crontab 为 `* * * * *` 直接起 Python: 实测 1,465 次/交易日中 895 次
# outside_extended_hours, 周末 1,440 次几乎全是空跑, 单次 Python 启动 ~0.76s。
# 本闸只做时间判定 (不替代脚本内 session/假日守卫):
#   - ET 周一-周五 04:00-20:05: 每分钟 (覆盖盘前/盘中/盘后守卫窗口)
#   - 其余时段 + 周末: 每 5 分钟 (孤儿单/close_key 守卫仍持续)
# 窗口外每 5 分钟一次, 保留 INCIDENT-001/002 的守卫覆盖; 不改变脚本内任何判据。
#
# 回滚: crontab 行改回直接 `venv/bin/python scripts/extended_hours_exit_cron.py`.
set -u

ET_NOW="${FTSEEK_ET_NOW_OVERRIDE:-$(TZ=America/New_York date +%H%M)}"
ET_DOW="${FTSEEK_ET_DOW_OVERRIDE:-$(TZ=America/New_York date +%u)}"

hhmm=$((10#$ET_NOW))
minute=$((hhmm % 100))

run=0
if [ "$ET_DOW" -ge 6 ]; then
    # 周末: 只保留 5 分钟守卫节拍
    [ $((minute % 5)) -eq 0 ] && run=1
elif [ "$hhmm" -ge 400 ] && [ "$hhmm" -le 2005 ]; then
    # 工作日 04:00-20:05 ET: 每分钟 (含 RTH 守卫)
    run=1
else
    # 工作日隔夜: 5 分钟守卫节拍
    [ $((minute % 5)) -eq 0 ] && run=1
fi

if [ "${FTSEEK_GATE_DRYRUN:-0}" = "1" ]; then
    if [ "$run" -eq 1 ]; then echo RUN; else echo SKIP; fi
    exit 0
fi

if [ "$run" -eq 1 ]; then
    cd /opt/ftseek/FTSeek-US || exit 1
    exec /opt/ftseek/FTSeek-US/venv/bin/python \
        /opt/ftseek/FTSeek-US/scripts/extended_hours_exit_cron.py "$@"
fi
exit 0
