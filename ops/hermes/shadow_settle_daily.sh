#!/bin/bash
# DEB-20260808 影子组合每日结算 (Hermes no_agent, 2026-08-28 迁移)
#
# 前身 = agent-session 定时任务 e68ae9ad (每日 08:10, permanent)。该机制绑定创建会话进程,
# 进程一死任务照列"已排定"却永不触发: 创建 (08-09) 至今 12 个交易日 30 条入场全部可结算,
# data/shadow_portfolio_outcomes.jsonl 从未存在过一秒 (下游 ic_weekly_report 因此静默空转)。
# 迁到 Hermes = 与 E1 采集/停损监控同一执行体, 有 last_status 可查, 失败进 incidents。
#
# 静默约定: Hermes --no-agent 把 stdout 原样投递, 空 stdout = 不发消息。结算脚本无新行时
# 仍会打表头, 故此处显式吞掉 "新结算 0 笔" 的情形, 只在真有新结算/失败时说话。
cd /opt/ftseek/FTSeek-US || exit 1

# stderr 单独收: futu SDK 的 OpenD 连接/断开日志会刷进 stderr, 与 stdout 合并投递会让
# 每日消息被十几行连接噪音淹没 (结算结论反而看不见)。只在失败时才把 stderr 端出来。
_err=$(mktemp)
out=$(PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python \
      /opt/ftseek/FTSeek-US/venv/bin/python3.11 scripts/settle_shadow_portfolio.py --fetch 2>"$_err")
rc=$?

if [ $rc -ne 0 ]; then
    echo "❌ 影子组合结算失败 (rc=$rc):"
    printf '%s\n' "$out" | tail -10
    echo "--- stderr ---"
    tail -25 "$_err"
    rm -f "$_err"
    exit $rc
fi
rm -f "$_err"

if printf '%s\n' "$out" | grep -q '^新结算 0 笔'; then
    exit 0        # 无新结算 → 静默
fi
printf '%s\n' "$out"
