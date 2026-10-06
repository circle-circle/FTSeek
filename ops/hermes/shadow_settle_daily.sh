#!/bin/bash
# DEB-20260808 影子组合每日结算 (Hermes no_agent, 2026-08-28 迁移)
#
# 前身 = agent-session 定时任务 e68ae9ad (每日 08:10, permanent)。该机制绑定创建会话进程,
# 进程一死任务照列"已排定"却永不触发: 创建 (08-09) 至今 12 个交易日 30 条入场全部可结算,
# data/shadow_portfolio_outcomes.jsonl 从未存在过一秒 (下游 ic_weekly_report 因此静默空转)。
# 迁到 Hermes = 与 E1 采集/停损监控同一执行体, 有 last_status 可查, 失败进 incidents。
#
# 静默约定: Hermes --no-agent 把 stdout 原样投递, 空 stdout = 不发消息。结算脚本无有效行时
# 仍会打表头+固定护栏, 故此处判"有效新结算 eff_new" (= 新结算 N 笔 − 剔除延迟成交 M 笔):
# M == N (含 N=0 无剔除段) ⇒ summarize() 不打门表格 ⇒ 消息零信息量 ⇒ 静默。
# 2026-10-06 修正: 旧守卫只判字面 "^新结算 0 笔", 当日实弹 7 进 7 剔 (全部延迟成交) 漏网,
# 用户收到只剩护栏样板、无任何数字的空推送。首行格式由 tests/test_shadow_settle_honesty.py
# 钉死 (新结算 N 笔 / 剔除延迟成交 M 笔), 改格式必须同步本守卫。
# 头行不可解析 (如 "无影子入场台账") ⇒ fail-open 照发, 不静默吞异常态。
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

# eff_new 守卫 (2026-10-06): 从头行取两个同源计数, N==M ⇒ 有效新结算为 0 ⇒ 静默。
# 不做全文正则、不猜表格 — 头行两计数就是 summarize() 决定有无门表格的那个量。
_head=$(printf '%s\n' "$out" | head -1)
_n_new=$(printf '%s' "$_head" | sed -n 's/^新结算 \([0-9][0-9]*\) 笔.*/\1/p')
_n_del=$(printf '%s' "$_head" | sed -n 's/.*剔除延迟成交 \([0-9][0-9]*\) 笔.*/\1/p')
_n_del=${_n_del:-0}
if [ -n "$_n_new" ] && [ "$_n_del" -eq "$_n_new" ]; then
    exit 0        # eff_new = 0 (N=0, 或全部剔除延迟成交) → 静默
fi
printf '%s\n' "$out"
