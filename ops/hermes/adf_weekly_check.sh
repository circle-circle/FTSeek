#!/bin/bash
# DEB-20260808 每周 ADF 平稳性 + 实盘化门槛四判据 (Hermes no_agent, 2026-08-28 迁移)
#
# 前身 = agent-session 定时任务 b460b9c5 (每周六 09:05, permanent): 该机制绑定创建会话进程,
# 进程死则任务照列"已排定"而永不触发 — 创建 (08-09) 至今两个周六均零实跑, 无任何产物。
#
# 槽位刻意用 09:20 而非 09:05: 09:05 已被"周六周检 (四锁+参数冻结+证伪门)"占用, 两者用途
# 不同 (工程状态 vs 统计转正监测), 不合并、不抢跑。
#
# 投递 = 本脚本 stdout (no_agent 语义: 空 stdout 静默)。四判据结论由 _tmp_adf_check.py 自算,
# 不再依赖 agent 代读判据文字。
cd /opt/ftseek/FTSeek-US || exit 1

_err=$(mktemp)
out=$(/opt/ftseek/FTSeek-US/venv/bin/python3.11 scripts/_tmp_adf_check.py 2>"$_err")
rc=$?

if [ $rc -ne 0 ]; then
    echo "❌ ADF 周检失败 (rc=$rc) — 本轮无判据:"
    tail -25 "$_err"
    rm -f "$_err"
    exit $rc
fi
rm -f "$_err"
printf '%s\n' "$out"
