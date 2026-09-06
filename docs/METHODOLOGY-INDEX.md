# 方法论总索引 (METHODOLOGY-INDEX)

> 2026-08-30 建。目的: 方法论不被发现就等于不存在。本索引回答一个问题——
> **"我现在要做 X，该用哪套已沉淀的方法？"** 全部指向可执行载体 (skill/脚本/文档)，不写散文。
> 维护规则: 新增方法论必须在此登记一行 (同控制环路台账的入账纪律); 废弃方法论改状态不删行。

---

## 一、按场景查表 (主入口)

| 我要做的事 | 用哪个 | 载体 | 一句话触发条件 |
|---|---|---|---|
| 验证一条交易规则/因子是否成立 | **backtest-rule-replay** | skill | 四关门+安慰剂防假阳; 任何"这个规则有效吗" |
| 回放历史标的做全量复盘 | **ftseek-trade-history-review** | skill | 聚合 trade_records+instructions+signals 逐笔审 |
| 复现/核查别人报的数字 | **data-claim-verification** | skill | SQL NULL 陷阱/照抄完整 WHERE/跨列代理同基准 |
| 复核另一 agent 的结论/提交 | **ftseek-verification-audit** / **ftseek-independent-review** | skill | 不信任描述, 逐行读码+查 DB; ~25% 子代理误报需亲验 |
| 对代码库做主动审计 (找盲区) | **proactive-codebase-audit** | skill | 静态扫描+并行子代理分域+逐条验证防误报, 三档分级 |
| 架构/设计层面的盲区扫描 | **architecture-hotspot-sweep** | skill | git 热点定位摩擦区, 改动密集处深挖 |
| 怀疑遥测/台账数字被放大 | **telemetry-caliber-audit** | skill | 轮询放大/重复计数 → 按唯一键去重验证 |
| 怀疑统计被轮询放大 (同上, 数据侧) | **data-replication-audit** | skill | 复现照抄查询; 代理减法忌量纲混合 |
| 集成/审计外部数据源 | **external-data-source-audit** | skill | key 验证/死源影响面/接源先活体探针 |
| 评估外部框架是否值得借鉴 | **framework-integration-evaluation** | skill | 读源码不读 README; 先 grep 自家有无等价物 |
| 给系统做任何变更 (优化/修复/新功能) | **engineering-change-protocol** | skill | 6 阶段带强制门; 一切改动的生命周期协议 |
| 给长批处理加断点续跑 | **pipeline-checkpoint-resume** | skill | 先判缓存再提交重活, 命中跳副作用 |
| 修断裂前防下游重复 | **Guard-First 原则** | 记忆铁律 | 先 grep 下游 dedup 守卫, 有则只注入一处 |
| 多模型协作/辩论/裁决 | **multi-model-debate** / **agent-debate** | skill | 双模型对抗; 共享文档多轮收敛 |
| 写实施计划 | **writing-plans** / **plan** | skill | bite-sized 任务+路径+代码 |
| 执行计划 | **subagent-driven-development** | skill | 子代理执行+两阶段审查 |
| 调试一个具体 bug | **systematic-debugging** | skill | 4 阶段根因调试, 先理解再修 |
| 修完要验证 | **fix-verification** | skill | 声称必核; 修复后独立复现 |
| 交易决策执行 (生产闭环) | **deep-stock-analysis** | skill | 盘前扫描→LLM 分析→门禁→执行→复盘 |
| 数据查询 (行情/财务/资金) | **financial-data-query** / **futuapi** | skill | A/港/美股全市场数据接口 |
| 跨仓位变更比较绩效 (美元/笔不可比时) | **尺寸归一口径 (bps of notional)** | 脚本 `scripts/exit_scorecard.py::size_normalized()` + `scripts/okr_status.py` trade_stats 四列 | 任何 sizing/仓位参数改动前后, $/笔混入"注下大了" → 必须并列 bps; 且按**日等权**做 t (pooled 会被成交多的日子主导, 实测 pooled +1.25bps 而日均 −1.32bps) |
| 单行状态表被误当时序用 (算峰谷/回撤/趋势) | **事件日记账重建 + 权威锚点倒推** | 脚本 `scripts/exit_scorecard.py::reconstruct_max_drawdown()` | 带 UNIQUE 约束的状态表 (如 `capital_state`) 窗口内恒 1 行 → 峰谷恒 0.0 = 判据免检假绿; 改从事件流 (`trade_records` 出场日记账) 重建, 锚定 live 权威值倒推窗口初值, 不可测一律 `None` 不得当 0 |
| 滚动窗口的 history 被当成时序 | **冻结同口径锚点块 + 观测点按日去重 + 到点推送** | 脚本 `scripts/trigger_gate_report.py::ANCHOR_SINCE` / `_fixed_anchor_streak()` / `push_anchor_verdict()` (2026-09-06 已接周六 cron `--push`) | `--days N` 滚动使每行 `since` 不同 → 跨行趋势是窗口在动不是指标在动; 额外落一份锚点块 (滚动头条不动), "持续≥N周"类判据按**日历日**折叠计数 (同日重跑不得凑数); **判据会在文件里静默成熟** ⇒ 观测点凑满即推送呈裁 (推送闸与判据同一按日口径) |

## 二、元规则层 (高于一切场景)

| 载体 | 管什么 |
|---|---|
| `docs/specs/multi-agent-collaboration-charter.md` | 多代理协作宪章 (对抗性审查/四象限) |
| `docs/ADAPTATION-CONTROL-LAW.md` | 适应律: 改参数转移函数的单一真源 |
| `docs/CONTROL-LOOP-LEDGER.md` | 控制环路台账: 新环路必须入账 |
| 记忆铁律 (MEMORY.md) | 声称必核/口径钉死/横跳禁令/简洁强制 |

## 三、决策知识层

| 载体 | 内容 |
|---|---|
| `docs/debates/` (122 份) | 每个重大改动的对抗辩论+裁决留痕 |
| `docs/decisions/` + `docs/adr/` | 已终裁决策与架构决策记录 |
| `docs/ADJUDICATED-CRITERIA.md` | **判据字典** (已终裁判据速查, 防重复辩论) |
| `obsidian_vault/lessons/` | 交易教训库 (运行时自动生成) |

---

## 维护纪律

1. 新沉淀方法论 → 本索引加一行 + 载体必须可执行 (skill/脚本), 纯文档不算方法论
2. 本索引自身是"方法论的方法论"——发现某场景无对应方法 = 暴露能力缺口, 立项补
3. 禁止大而全白皮书: 每个条目一句话触发条件, 细节进载体不进本表
