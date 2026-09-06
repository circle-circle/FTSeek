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
| 判分面板沿用**已废 OKR** 的 KR 集 (目标换了、尺子没换) | **新旧判据并列不替代 + 拆纯函数模块才可钉 + 不给百分比总分** | 脚本 `scripts/okr_2026_09_scoring.py` + `scripts/okr_status.py` 6b 段 | OKR-2026-08 已于 8/23 SUPERSEDED 而 `calc_okr_score` 仍按 win_rate/weekly_pnl/IC 判分 ⇒ 头条 60% 与现行判据**不同源** (KR2/KR4 恒 0 是废目标算出来的 = 量错了东西而非没做到); 旧块键名有消费方 (`okr_query.py`) + history 时序连续性 ⇒ **并列**并在两处头条标口径, 不改键; 判分逻辑必须拆出**模块级执行体** (import 即跑 SQL 的脚本契约测试钉不到); 二元/待裁项取算术平均 = 假精度 ⇒ 只出 verdict 计数; 待裁项 (KR2 (a)/(b)) **恒 PENDING 不自评**; **不新增第四把尺子** — 已有两处测量实现时只读其产物 (再加一份 SQL 即口径漂移) |
| 判据要求的**事实已不可逆**（窗内已发生 N 次变更，判据要 N=0） | **判分单位改 cohort + 变更预算归零 + 多版本期如实留痕** | 脚本 `scripts/daily_invariant_check.py::_FREEZE_COHORT_START`（`config_freeze.cohort` 块）+ `scripts/okr_2026_09_scoring.py::kr2_freeze` | 按全窗判 ⇒ 判据当天即永久 BROKEN，会把「从此冻结」本可创造的样本（≈215 笔单版本窗）一起判作废 = **自毁判据自身的价值陈述**；只改判分不冻未来 ⇒ cohort 永远攒不满 = 僵尸尺子（B17）。合流后判据重新**可达成且可证伪**：窗内出现第 2 个哈希即当天告警 `W1_kr2_cohort_broken` + 判 BROKEN；豁免通道对新维**关闭**（前 4 版全走豁免 ⇒ 再开一次口子 = 判据名存实亡）；**两口径并列不得互相冒充**（守卫 `exit_status='ok'` 是豁免后口径 ≠ cohort 判据达成）；窗未闭合只报 `ACCUMULATING`，不提前自评 MET |
| 预注册判据的**判分代码**该不该提前写 | **不写 —— 裁决批现写现验** | `docs/decisions/RFC-072-kr3-direction-runup-gate-preregistration.md` §六.3 + 注册表 `kr3_direction_runup_gate` notes + `scripts/okr_2026_09_scoring.py::kr3_prereg` | 「先写判分再凑数据」= 选择压力（会不自觉挑对声称有利的口径）⇒ 产物里缺「按日聚类 t / MDE / OOS」是**设计不是缺口**，把它误记成尺子病灶会诱导下一批去写判分代码（本仓 2026-09-06 批③ 曾误归因，批④ 更正）。可机械判的只有**假设数**（只许一个）与**样本门**（would-block 子集 n≥30）—— 数数不是判分；样本门未达 ⇒ `ACCUMULATING`（不裁决、延长观察），与日历无关 |
| 门禁「在」但**没在判**（判据坏掉刚好返回 false） | **用修好的尺子重定义判据 + 六道 AND + 不可测 fail-close + 三重闸** | `docs/decisions/RFC-077-graduation-gate-redefinition-preregistration.md`（**草案，未生效**）+ `scripts/okr_2026_09_scoring.py::kr4_hard_gate` | 现毕业门禁判据 `confidence IC>0.05` 测的是常量（confidence 自 8/14 恒 70）⇒ `graduated=False` 是**巧合安全**：门在，但门没在判，一旦有人「修好」confidence 就可能假放行。重定义只准用修好的尺子作输入（净 bps 日等权 t / 成本覆盖 / 重建回撤 / 触发门冻结锚点缺口 / cohort 样本+OOS / 方向腿），任一不可测 ⇒ `UNKNOWN_FAIL_CLOSE`，**不得以其余五道过而放行**；草案 ≠ 生效（预注册 + 用户批准 + 用户签字三重闸），且草案必须**明码标价今天六道全不过** —— 提案的作用是让「离实盘多远」可见，不是为放行铺路 |

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
