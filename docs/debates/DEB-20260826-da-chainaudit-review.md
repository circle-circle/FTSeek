# DEB-20260826-da-chainaudit-review

状态: 复核通过并归档 (2026-08-26)
裁决方: 独立复核 (Hermes), 另一 agent (chainaudit) 产出修复
关联: FTSeek 侧 fbfe7037 系列 (bridge重载/IC去污染); skill 侧 v0.70 (本记录)

## 缘起

另一 agent 对 DA 全链复查, 发现生产端字段错配等 7 类问题并修复 (v0.70)。
修复当时**未提交** (skill 仓仅跟踪 6 文件, 其余改动在版本控制外, 回滚靠 .bak cp)。
用户要求独立复核 + 按 ponytail 判最优性 + 补提交留痕。

## 复核结论: 修复方向正确, 实现判最优

### P0 字段错配 — 实弹复现成立
prompt 模板读幻键 (pe_ttm/market_cap/change_pct/eps/dividend_yield/high_52w/low_52w),
data_collector.collect_snapshot 真键是 pe_ttm_ratio/total_market_val/highest52weeks_price/
lowest52weeks_price, 且不产 change_pct/eps/dividend_yield。
AAPL 实弹: 旧读法得 涨跌幅0%/PE=0/市值$0.0B/52周$0, 真值 −0.14%/35.5/$4522.7B。
叠加 USER_PROMPT L712 "PE为负或0必须在bear_case提及" = 每只股票被注入虚假亏损论据。

修法判最优: _fundamental_view 单一归一助手 (渲染+回填同源), 就地归一化不动
collect_snapshot 契约 (market_scanner 用真键消费, 改契约会波及)。缺失维度显式
"数据未采集(N/A)" 防 0 假象。

### 机械批次 — 全部合理最小
DST×6 月份近似→zoneinfo (两解释器实测可用); WAL×4 移除 (SKILL.md L137 纪律);
原子写×2; alpha_backfill 无条件 os._exit(0)→条件透传退出码 (原把失败伪装成功)。

### 验证电池 (全实跑)
7 文件编译过 | PROMPT_HASH 新旧实证一致 15c13f7cb3f3 | 契约测试 35/35 |
端到端渲染冒烟 (AAPL 真值) | zoneinfo 双解释器可用

### 诚实标注
1. 归因切分锚点是日期非 hash: _snapshot 回填值恒0→真值但 PROMPT_HASH 不变,
   未来 IC/回测若消费 _snapshot 字段必须按 8/26 切分 (现在无消费方)
2. eps/dividend_yield 数据源不采集恒 N/A — 已知天花板
3. 治理欠账: 修复时未提交 + 无 DEB 留痕 (本记录补) — 已由复核方补提交

## 反思防再发 (复核方自问"为什么没主动发现")

漏检根因: 35 个契约测试只测**输出契约** (方向/字段/违约), 输入保真
(渲染文本↔数据源真键) 不在测试面。模型看到 0 不是异常值, 输出契约照常通过。

落地: tests/test_da_fundamental_contract.py + fixtures/da_real_snapshot_20260826.json
— 真实 OpenD 快照钉住"渲染路径↔真键"契约; 变异测试验证: 视图改回幻键读法 →
2 测试立红, 恢复后 39/39 绿。
