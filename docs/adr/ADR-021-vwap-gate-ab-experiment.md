# ADR-021: VWAP 门 wrong_side 拦截 A/B 双开实验 (sim, 50/50 分流)

- 状态: Accepted
- 日期: 2026-08-07
- 关联: DEB-20260806-source-control (修尺子后), ADR-020 (入场侧参数), 审查#1 (再入场 hotfix 链)

## 背景

VWAP 方向一致性硬门 (Tier1, pipeline.py:4240) 自 7/15 起拦截"当日 VWAP 逆侧"入场。
DEB-20260806-source-control 修尺子后的证据: 被拦 n=55 均值 +0.254% (SHORT +0.649%),
门倾向"扼杀"而非"保护" — 影子反事实轨迹 (entry_timing_shadow 持续轮询) 显示被拦标的
若入场本可盈利。

影子反事实口径的局限: 未计费用/滑点/真实退出, 无法回答"真实成交口径下门是否仍有价值"。
决定性检验只能靠 A/B 双开实测 — SIM 无真金/无 PDT, 成本为零。

## 决策

wrong_side 拦截 50/50 分流 (仅 sim 生效):

- **分臂**: sha1(date|symbol|direction) 奇偶 → 'bypass' | 'gated'。纯函数, 日内稳定
  (同标的当日恒定同臂, 不随 300s 轮询漂移, 进程重启不变)。
- **bypass 臂**: 放行成交 (真实口径: 费用/滑点/真实退出)。
- **gated 臂**: 维持拦截 (影子反事实口径, entry_timing_shadow 持续轮询)。
- **台账**: data/vwap_gate_ab.jsonl, 每 symbol-day-direction 一行 (文件级去重,
  防进程重启重复计数 — 审计修复 #4)。双臂首次判定即落账。
- **硬守卫**: 仅 execute_mode=futu_simulate + 开关开生效; 异常 → False (维持拦截,
  保守方向, 即现状)。
- **毕业判定**: 每臂 n≥30, 对照净均值 (pnl_net 口径)。gated 臂反事实不构成成交,
  比较时按影子轨迹模拟退出。
- **config**: `playbook.vwap_gate_ab_enabled: true` (回滚: false + 重启)。
- 审计修复 #3 (bypass 臂不 emit trigger_miss, 防成交单混入 miss 漏斗) 随本 ADR 入库。

## 风险

- 放行臂可能真实开仓 (sim), 将错误的 wrong_side 仓位纳入统计 — 这正是实验目的。
- 分臂非对称影响样本量: 被拦候选供给有限, 毕业需约 2 周 (n≥30/臂)。
- 与再入场生成器 (intraday_reentry) 叠加时, 同标的可能同时走 A/B 放行 + 再入场武装,
  统计切片需按 decision_id 前缀区分。