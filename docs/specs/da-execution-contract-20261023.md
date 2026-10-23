---
title: DA 研究意图与 Playbook 执行契约
status: draft
module: cross
related_code:
  - /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py
  - /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py
  - app/playbook/pipeline_parts/auto_execute_flow.py
  - app/playbook/entry_confirm_gate.py
  - app/playbook/exit_engine.py
related_okr: [OKR-2026-10, OKR-2026-11]
related_deb: DEB-20260907-prompt-contract-role-boundary-review
related_rfc: [RFC-123, RFC-133, RFC-141, RFC-144, RFC-154, RFC-156, RFC-158]
tags: [da, execution-contract, provenance, replay, feedback]
updated: 2026-10-23
---

# DA 研究意图与 Playbook 执行契约

> **授权状态：用户要求生成执行契约与推荐方案；尚未批准本契约的代码、Prompt、配置或运行态实施。**
> 本文为待批准目标，不是已上线能力清单。现役行为以代码、配置及已批准裁决为准；本文不改写旧裁决，不新增交易许可。进度只记 `handoff.md`，本文仅定义范围、依赖与验收。

## 1. 推荐方案与取舍

**推荐 B：DA 提出有证据的受约束研究意图，Playbook 负责确定性执行和持仓管理，二者通过不可混用的阶段快照交接。**

| 方案 | 取舍 | 判断 |
|---|---|---|
| A：原样执行 DA 全部文字计划 | 需要多腿执行、条件失效核销与新触发机制；当前回放不够保真，且会触及 C9 等既有红线 | 不推荐 |
| **B：受约束研究意图＋统一执行＋阶段真值** | 保留方向、证据、计划价和既有禁追约束；执行价、数量、实际止损止盈及订单状态由现役执行器决定 | **推荐，最小增量** |
| C：DA 只给方向，删除全部价位和条件 | 会丢失已有 chase-cap 约束、计划归因和失效研究能力；不是简单减字数 | 不推荐 |

本方案不把所有 DA 输出降格成“可忽略建议”：**已被现役规则接入的约束继续有效**。也不复活 7/10 草案中“删掉全部 LLM 数字、缺字段回退市价”的方案。保留 propose–verify–calibrate 的有界建议原则，不新造一套下单系统。

投入价值优先级：**正确价格与事实时点 → 正确反馈口径 → 无歧义裁决 → 阶段化执行证据 → 保真回放**。不以模型更强、报告更长或字段更多作为收益改善证据。

## 2. 当前事实与边界（2026-10-23 复查）

当前链路：

```text
扫描器／UE／事件线索
  → DA 采集数据＋近期经验＋股性档案
  → 分析师＋挑战者
  → signal_gate／adapter
  → playbook_instructions（执行入口）＋bridge（辅助载体）
  → 触发／重定价／确认／VWAP／预算／禁追检查
  → Playbook 下单与退出
  → 交易事实／审计／回放／记忆
```

- Playbook 是唯一下单与持仓管理管线；主管线禁止新仓，但 `scan_loop` 不得整体停止。
- UE 直接写入已停止，intraday 已关闭；两者不因本文恢复。UE 仍有发现、观察与供料职责。
- `playbook.entry_archetype_routing_enabled=false`：DA 的 `confirmation.price_level` 不保证变为一张限价单。`entry_archetype_reanchor_enabled=true` 也不能越过该总开关。
- `playbook.chase_cap_enforce_enabled=true`；`playbook.execution_parameters.tripwire_checker_enforce=false`。
- `playbook.execution_parameters.confidence_sizing_enabled=false`，`model_disagreement_downsize_factor=1.0`：confidence 仍影响分级／预算排序，不能声称当前直接按它加仓。
- 入场确认的 `CONFIRMED` 是现役终态；pending TTL 不等于确认有效期。本文不新增失效 TTL。
- `quality_score` 的数字溯源扣分影响质量记录和提示，当前 tier 决策不直接消费该分；不得称其为有效交易拦截。

| 已证实的问题 | 实际证据 | 不应外推的结论 |
|---|---|---|
| 昨收混用 | 10/22 的 28 份 raw 有 8 份错配；TSCO 32.91 为前两日收盘，真正昨收为 32.69，盘前同时呈现 −5.72%／−6.35% | 未证明实单因此错价成交或错误拒单 |
| 来源时效／独立性缺口 | 20 个标的 100 条研报日期显示为空；新闻／研报有 19 个标的、51 条同稿重复 | 旧报告不等于无价值；重复证据不等于已证明损失 |
| 记忆窗口与收益口径不符 | 全史毛收益分组只按 `last_trade_date` 筛最近活跃组；28 份 raw 均注入 pattern 经验 | 不能把锁盈退出组的高胜率当作退出机制因果收益 |
| 辩论字段冲突 | 5 份 `PARTIAL_AGREE` 同时 `agreed=true`，CAT／TSLA／UNH 获一致度加成并落库 | 不等于直接加仓，也不授权统一降档 |
| 计划与执行阶段不同 | SNDK 指令 RR=2.45，成交 RR≈1.46185，既有 TP 门将固定 TP 关闭；TSLA 计划／实仓 SL 改变审计几何关系 | 不是已修的浮点边界 bug，不授权取消 TP 门 |
| 回放尚不保真 | `data/shadow/exit_t_arm_latest.json` 的 `reason_match_pct=0.604`、`fidelity_ok=false` | 不得读取变体收益作上线依据 |

证据是复查时点的观察，不是新的收益实验。代码锚点及复算约定见 §11。

## 3. 权责与冲突处理

| 层 | 唯一职责 | 禁止事项 |
|---|---|---|
| 数据组装 | 提供可追溯、带时点和量纲的事实；计算可确定的价格／派生量 | 把缺失记为 0；用采集时间冒充发布时间；把同稿计作独立证据 |
| DA 分析师 | 提出方向、论据、反证、证据缺口和单一研究入场意图；保留计划价位 | 发订单；承诺未启用的限价／第二腿；改变风控阈值 |
| 挑战者 | 核验关键事实、指出实质反证和计划矛盾 | 把阅读分析师答案后的判断称为盲评；把弃权伪装为反向 |
| 确定性适配／规则层 | 方向词表归一、结构检查、既有分级及已批准约束；记录原值和派生值 | 靠散文／字符串子串裁决；把结构补全偷偷变成改方向、改价格策略 |
| Playbook | 执行价、数量、实际 SL/TP、订单与退出状态的权威 | 为迎合报告而放宽门；把原计划覆盖成“模型原话” |
| 复盘／评估 | 重建各阶段真值，区分描述、反事实与因果证据 | 用最新可变 DB 行冒充历史快照；从赢家子集推导盈利规则 |

发生冲突时：现役安全约束优先；研究计划与执行不一致必须可见，但**契约告警本身不新增 BLOCK、撤单、平仓或重确认动作**。改变当前处理结果须独立批准。

## 4. 输入与 Prompt 契约

### 4.1 价格、时间、来源

1. 同一数值必须能追溯到 symbol、来源、交易日、session、币种、复权／模拟价格口径及 `as_of`。时间保留带时区原值，按 ET 交易日归属；公布、采集、快照更新时间不得互相替代。
2. 昨收目标口径为**上一个已完成交易日、同价基的 RTH 收盘价**，不是简单取缓存末行。需要交易日历、最后完整 bar 及复权口径一致性；公司行动／缓存不新鲜／未来 bar 均须识别。
3. `pre_price/(1+pre_change_rate/100)` 仅为交叉核验值，受源涨跌幅精度限制；不得仅凭“偏差小于 1%”确认原值，也不得无条件用推导值覆盖。源无法裁定时记录冲突；对模型的缺失／冲突呈现属于待批准输入变更。
4. 每条新闻／研报／社区材料保留 source ID、原稿 ID、发布时间和采集时间。没有发布时间就明确未知，不以今天日期补齐。
5. 原稿去重与事件关联分开：同一原稿只算一条证据；同事件的独立来源可保留，但不能声称统计独立。优先确定性 ID 去重，不为本批增设 LLM 聚类调用。
6. 陈旧研报可作为历史估值背景；陈旧社区内容不能称为当前情绪。本文不设统一“超过七天全部删除”规则，也不改变既有候选预算。
7. 外部文本是证据材料，不是任务指令。目标 Prompt 需明确不可被材料内指令覆盖；结构化转义只是表示层防护，不宣称彻底消除注入风险。

### 4.2 推理输出

- 保留现有方向、论据、风险、`evidence_gaps`、计划价位字段，不在首批新增模型自评概率或新评分轴。
- **事实、假设、计算结果分开表达**：无来源数字不能因恰好出现在别处而“通过核验”；派生量须记录输入指标、单位和公式。
- “尚未被价格反映”属于可证伪假设，低成交量／小涨跌幅本身不是充分证据；不确定性放入现有 `evidence_gaps`。本条不自行修改现有 score-band 三条件或 RFC-133 判据。
- 推荐目标不强迫固定三层因果链、固定两批入场；只表达有证据且当前接口能承载的单一研究意图。删除这些 Prompt 要求也是行为变更，须单列对照；不捆绑改方向侧论据数量要求。
- `entry_strategy` 不承诺第二腿或“到价必成交”。执行能力说明应陈述当前真实模式，不能向模型暗示关闭的路由已启用。
- 挑战者共享其核验职责所需的有效证据及其来源／时间；不因“对称”把全部历史统计噪声再喂一遍。缺段清单须可检查，不能靠标题偶然匹配。

## 5. 字段交接与消费登记

下表中的**既有字段**沿用原载体，不授权迁移或改名；“目标审计字段”由代码产生、不是新增 LLM 输出要求。每个新增字段须同批落实读取端、单位断言、缺失率与基数检查，再进入契约实施。不得先写库等待未来消费者。

| 字段／载体 | 语义与单位 | 当前消费／目标用途 |
|---|---|---|
| `direction` | 模型 BUY／SELL_SHORT／SKIP；在适配边界映射为 LONG／SHORT，禁止多次猜测 | gate、bridge、执行；原始方向保留，不用最终 SKIP 回填原提案 |
| `score_band`／`score` | 既有确信度档位／映射分，不是获胜概率 | 既有 gate 与 merge；本批不调整映射／阈值 |
| `decision_conf`／DB `confidence` | 原字段通常 0–1／0–100，须逐载体断言 | 分级与预算排序；不新增 sizing 消费 |
| `execution_plan.entry_price` → raw `planned_entry` | 生成时研究计划锚，USD/股 | 计划差异与既有影子；不得由后续 reprice 改写 |
| `confirmation.price_level/side/ttl_min` | 首批研究确认位、方向条件、分钟 | 关闭的限价路由不因此启用；不同于运行态 entry-confirm 的 mid 锚和秒级窗口 |
| `entry_archetype` | 既有枚举，不等于全路由开关 | 已被 RFC-153 及归因消费，不能因为限价路由关闭就视为无用 |
| `chase_cap_price` | BUY 禁追上限／SHORT 禁追下限，USD/股；null 为未提出额外 cap | 现役归一化与禁追门继续生效；保留原 cap、有效 cap 及归一化依据 |
| 原计划 SL／TP | 研究建议价格，USD/股 | 保留原建议；不能称为券商保护单或实际最大损失 |
| `invalidation`／`tripwires`／`invalidation_triggers` | 逻辑失效描述／观测条件；收盘价 USD，量比无量纲，涨跌幅百分点 | 现有审计／影子；不自动撤销、平仓；“收盘跌破”不能等同盘中触价 |
| 可变 `playbook_instructions.entry_price/stop_loss/take_profit` | 当前 DB 投影，不是不可变历史 | 继续服务现役执行；分析原计划必须读生成快照 |
| 实际成交价／数量、初始 SL、动态 SL、TP 门状态 | 券商收据／执行事实；USD/股、股数 | 交易账本、退出与阶段化审计；初始 SL 和移动后 SL 分开 |
| `plan_gap_pct` | `100*(执行锚-计划锚)/计划锚`，百分点，保留正负 | 复用现有计划差异审计；不得默改成方向调整收益 |
| 目标 `stage/as_of/attempt_ref/confirmation_age_sec` | 生成／提交／成交／退出阶段、时点、关联身份、确认龄秒数 | 扩展现有 stop-invalidation／执行审计读取端；缺失标 missing，不默认为 0 |
| 目标输入／运行清单 | 完整 Prompt、模型参数、渲染及规则版本、输入摘要哈希 | 固定输入回放与分版本评估；放代码审计载体，不要求模型抄写 |

`risk_reward` 必须带阶段，不能以仍为 2.45 的计划列解释成交 RR=1.46185。风险金额若来自提交参数，只能称“提交时风险估算”；跳空、滑点、费用会使实际损失不同。

拟增审计字段先复用 `raw_instruction_json`、交易 metadata、既有 audit JSONL 的职责边界；**不预设新增数据库／平行台账**。值理应恒定的枚举版本字段不以“distinct<2”误报，需在既有基数治理中明确字段性质，不能擅自放宽 W2 基线。

## 6. 挑战者裁决契约（推荐目标，尚未生效）

### 6.1 唯一权威

推荐由分析师既有 `direction`、挑战者既有 `my_direction` 与 `verdict` 经确定性一致性检查产生规范状态；`agreed`、展示文字不能反向覆盖规范状态。原始结果完整保留。下面状态由代码派生，**不增加模型必填字段**。

**按表从上到下首个匹配项判定，后行不得覆盖前行。** 规范状态描述合并资格；挑战者技术失败、原始矛盾与模型身份仍独立留痕，不因 ANALYST_SKIP 被擦除。合法方向为 BUY／SELL_SHORT／SKIP，合法 verdict 为 AGREE／PARTIAL_AGREE／DISAGREE。

| 优先级与输入情况 | 目标规范状态 | 语义 |
|---|---|---|
| 1. 分析师 direction=SKIP，不论挑战者方向／可用性 | ANALYST_SKIP | 保持分析师 SKIP；即使挑战者选边，也不产生新交易计划 |
| 2. 挑战者失败／无可解析结果 | UNAVAILABLE | 技术不可用，不是弃权或共识 |
| 3. 原／挑战者方向枚举无效，或 verdict 缺失／无效 | CONFLICT | 契约不可判；禁止将缺失 my_direction 默认成有效弃权 |
| 4. 挑战者有效输出 my_direction=SKIP | ABSTAIN | 原方向此时为有效交易方向；即使 verdict=DISAGREE，也只按弃权，保留原枚举矛盾记录 |
| 5. 两个有效交易方向相同＋verdict=AGREE | AGREE | 方向一致且未报告实质硬伤 |
| 6. 两个有效交易方向相同＋verdict=PARTIAL_AGREE | PARTIAL_AGREE | 同向但有硬伤；`agreed=true` 不能升级它 |
| 7. 两个有效交易方向相反＋verdict=DISAGREE | DISAGREE | 真反向，不等于弃权 |
| 8. 其余：同向却 DISAGREE、反向却 AGREE/PARTIAL | CONFLICT | 不自洽，不允许当作独立一致证明 |

- 分析师原为 SKIP 时，不从挑战者单独拷贝方向拼出缺少完整计划的新指令。
- 模型独立性与裁决状态正交：T2/T3 独立模型 fallback 不等于同模型自辩；T4 `self_debate` 即使 AGREE 也不能算独立共识。merge／gate／adapter 必须采用同一身份口径。
- “存在一条决定性硬伤”可以支持 PARTIAL 或反向论证，不应受“必须凑到两条反证”的修辞约束。具体方向和证据仍须有效。
- **CONFLICT 的推荐行为**：有效分析师方向／score 保持，独立一致加成记为零，不额外施加新惩罚或直接 BLOCK；既有门仍可按其它条件拒单。无效分析师方向沿用现役无效输入处置，不猜方向。哪些现役分级／WARN 路径因此改变，必须在离线对照中逐条列出，待批准后生效。
- 不重写 score、既有真假反向处理、模型选择或思考预算；不将规范状态直接接入新仓位门。

### 6.2 上线前最小真值表

必须覆盖：同向 AGREE；同向 PARTIAL＋bool 真／假；真反向；弃权（含 my_direction=SKIP＋verdict=DISAGREE）；分析师 SKIP×挑战者选边／弃权／失败；技术失败；无效枚举；T1/T2/T3/T4 身份。任一分支输出分级、入库资格、排序有变化都须可解释，不用“只修字符串”淡化行为影响。

首批仅记录新旧规范结果，不消费新结果；后续单独批准一个裁决消费批，不能同时改变数据输入或 score-band 规则。

## 7. 执行阶段契约

### 7.1 必须可区分的事实

| 阶段 | 应保留的事实 | 权威与约束 |
|---|---|---|
| GENERATED | 原方向／计划价／原 SL/TP／cap／确认研究意图／输入版本 | 生成快照不可被重定价覆盖 |
| EVALUATED | 本次行情时点、执行锚、已生效 gate、预算、确认状态与确认时间 | 多次评估不是多笔独立样本；复用现有评估事件 |
| SUBMITTED | 实际提交价／数量／参数、订单身份及关联评估事件 | 有订单 ACK 不等于成交；提交失败也须能归因 |
| FILLED | 已核验成交量／VWAP、初始 SL、对应 RR、固定 TP 是否有效 | 成交高水位／终态语义沿用现役收据与账本，不另写撮合器 |
| EXITED／SETTLED | 动态保护状态、出场成交、毛／费／净、费用来源、退出版本 | 不用计划退出价替代真实退出；部分成交及费用修订按既有幂等规则留痕 |

这些是审计视图，不是要求新增五套运行态状态机。单笔通过已有 `decision_id`、instruction ID、trade ID／订单身份串联；身份不能唯一关联时标记 ambiguous，不以 symbol＋date 的任意首行补配，禁止 join 扇出。

### 7.2 现役规则如何处理

- **确认后迟成交**：保留当前 CONFIRMED 终态，先记录确认至实际提交／成交的年龄及价差；不把 pending 的 600 秒移植为确认 TTL。是否重确认属于新行为假设，须按 RFC-141／C9 另裁。
- **计划与 VWAP 冲突**：保留当前门；RFC-158 继续原判据。不能根据少数未成交赢家反向放行。
- **固定 TP 被吞**：展示真实成交 RR、当前 `min_tp_rr` 与关闭原因；不降 RR 门，不用计划 RR 强行启用 TP。1e-9 边界修复不重复立项。
- **失效条件与 SL**：按生成／提交／成交分别核对；几何先后、盘中触价、收盘确认是三种不同语义。不得把审计标签自动变为出场动作。
- **快循环**：新增记录或回放不得引入同步网络／SDK 调用阻塞出场线程；读不到审计数据要留缺口，不能拖住持仓管理。
- **失败语义**：只读观测失败不改变订单决策；现役安全门的 fail-close 及现役门的 fail-open 语义分别继承，不新造“所有失败一律放行／一律停止”。

## 8. 反馈与绩效口径

### 8.1 两种时间视图不可共用一个筛选

- **日报／lesson**：按实际退出时间的 ET 日期选已结算交易，包含隔夜出场；未结算／费用待定单列，不以 0 代替。
- **入场 cohort**：按入场交易日／决策版本固定母体，已结算、未结算、右删失分别计数；后来退出只能更新该笔结局，不能静默改变入场母体。
- **模型记忆**：只用模型调用时已可得且已记录的事实。真实滚动 30 天在底层交易筛选；截至时间、窗口时钟（推荐退出事件时间）、来源和版本须明确，不能用分组最后活跃日替代样本窗。
- 重建聚合记忆不等于补齐漏掉的历史日报。lesson 使用稳定事件身份和修订记录实现重跑幂等；事实发生修订时不能静默改写旧 Prompt 中已经出现的内容。

### 8.2 学习内容

1. DA 作为主来源分列，EXT／UE／intraday 的历史背景分列；不混成 DA 的效果。
2. 表现统计复用 `era_clean_where(conn)`／`ERA_CLEAN_PERFORMANCE_WHERE`，执行 A9 含／不含双口径；缺列触发既有 fail-open 时显式披露，不冒充完整合规。账户资金对账及费用标定的既有豁免不变。
3. 毛／费／净同时给出，缺净额不得悄悄用毛额冒名替代；`fees_source=est` 必须明示。
4. “全胜”以该方向完整已结算母体为分母，区分赢／输／平；不从 wins 子集反推全体。
5. 自动 lesson 只描述事实及适用范围，不从单日胜负直接提出方向、仓位或止损调整。退出类型分组是描述性归因，不是策略因果比较。
6. 原始交易和旧 lesson 不因本契约删除。历史回填先 dry-run 列差异，保留来源身份和修订版本，另批批准写库。

### 8.3 净 bps 的名称与公式

设第 i 笔名义额 `N_i=entry_fill_price_i*filled_quantity_i`，净额 `P_i`，则：

- **逐笔等权净 bps**：`mean(10000*P_i/N_i)`。
- **名义额加权净 bps**：`10000*sum(P_i)/sum(N_i)`。
- **逐日等权净 bps**：先计算每日指定口径再等权平均；不能把其 CI 贴给上述另一个估计量。

2026-10-23 复查时，10/06–10/22 DA 已结算 n=80、13 个入场交易日：毛 $370.39、费 $469.68、净 −$99.29；逐笔等权净 −3.22 bps，名义加权净 −2.25 bps。逐笔等权均值的日簇稳健 80% CI 为 [−34.47,+28.03] bps，MDE80 约 69.59 bps（双侧 5%、80% 功效、t 小样本近似）。80 行质量均 real、费用均 est，A9 含／不含相同；退出版本 34c7 为 13 笔、ca44 为 67 笔。此为混版本描述，不是单版本因果证据。

**推荐明确 11 月的主估计量为已有基线 −2.25 对应的“名义额加权净 bps”，逐笔等权并列为诊断，月内不换估计量。** 这是对 OKR 术语歧义的待批准补充，不因本文自动修改 KR1／基线；确认前报双口径，不借择优选口径自动判达标。若用户选择逐笔等权作主指标，必须另行批准同步修改基线和估计量，不能只把 −2.25 改成 −3.22。

## 9. 回放与验收契约

### 9.1 固定输入回放

- 使用冻结的候选母体与当时完整输入：system/user、挑战者实际可见材料、动态记忆／卡片、模型实际身份、温度／思考参数、渲染器／gate／执行版本。不能只保存静态 Prompt 哈希。
- 回放不得读取今天的记忆、当前新新闻或未来完整日 K；未知历史输入显式 missing，不能补成“当时已知”。
- 同一输入比较一个变量，包含 SKIP、技术失败、被拦、未成交与成交；不能仅在两臂都有方向的交集判非劣。
- 不靠同方向的 close→close 收益差恒为 0 宣称执行无损。方向评估与交易策略评估分开，entry/open 双基准及是否可成交并报。
- 复用既有载体和测试基础，不复活 KILLED 的 DA 蒸馏／换模型实验；任何新研究假设仍受 C1、注册表和实验上限约束。

### 9.2 执行回放

- 先重现现役 baseline 的真实路径、有效规则和费用，再看变体；使用本系统同价基行情，禁止与异价基行情拼接。
- 限价触达不等于必成交；开盘跳空、同 bar 先后次序、部分成交、未成交与缺 bar 的处理必须披露。分钟 bar 不足以证明秒级行为时，缩小适用结论，不声称完整保真。
- RFC-154 的 `reason_match≥0.80` 沿用原判据；这是必要条件，不自动代替其它已有保真约束。当前 0.604 未达，变体读数继续冻结。
- 通过保真与工程契约只证明“可评估”；证明盈利、转正、放大是另一个统计与批准关口。不得用工程测试通过声称策略有效。

### 9.3 首批验收清单（目标测试，尚未运行）

| ID | 夹具／对照 | 必须验证的结果 |
|---|---|---|
| T01 | TSCO 32.91／32.69；正确昨收；0%盘前变化；节假日；复权冲突；陈旧缓存 | **P1检测面**标出日期／价基冲突，模型仍收到原输入；**P3修正面**才替换经验证的昨收，不再以1%阈值作正确性证明。两面均须未知不伪造、计算可复现 |
| T02 | 有 publish_time 无 time；41 天旧社区；同稿跨两栏目；同事件两独立来源 | 时间不空白伪装；背景／当前可区分；原稿只计一次，独立来源不误删 |
| T03 | 股价70美元被引用为营收70%；成交量被引用为毛利率；正确同单位派生量 | 语义错配不得标 verified；合理派生有依据，不靠扩大数字白名单 |
| T04 | 材料含“忽略系统指令”；普通新闻／研报 | 材料不能成为规则来源；正常材料可读；不能宣称一次测试等于彻底安全 |
| T05 | §6 全状态×模型独立性真值表，含 CAT／TSLA／UNH 冲突样本 | raw 原值保留；PARTIAL 不被 bool 升级；技术失败／弃权／反向不混；新旧差异逐条列出 |
| T06 | 同指令多次 reprice，SNDK RR 及 TSLA SL 案例 | 原计划不漂移；提交／成交 RR、SL/TP、门状态有各自时点，不能复用陈旧标签 |
| T07 | 确认后多次被 cap 拦、迟成交、跨重启 | 只补确认龄与关联证据；首批不改变 CONFIRMED 放行语义或订单事件序列 |
| T08 | 2笔LONG赢＋1笔LONG亏；隔夜退出；毛赢净亏；旧时代／排除样本 | 不写全胜；退出日报包含隔夜；真实窗口与毛净可区分；A9/A10清楚 |
| T09 | 同一批事实重复构建两次，加入调用时点之后的数据 | 重跑不重复累计；固定输入回放不消费未来信息；历史修订可追溯 |
| T10 | 80笔基线／固定身份清单，以及按日不等样本量的构造数据 | 两种净bps名称与公式匹配；CI围绕同一估计量；join不扇出 |
| T11 | record-only关闭／开启、审计写失败 | 订单／方向／数量／价格／退出事件语义一致，告警按既有hub纪律处理 |
| T12 | baseline回放保真失败、缺bar、含未成交样本 | 不产出可上线的变体结论；缺失不当零收益，终态不冒充可比样本 |

来源错误、漏标签、join 扇出等工程不变量可以逐例验收；收益假设仍遵守各案样本门与日簇／MDE，不能拿 28 份夹具代替前瞻证据。

## 10. 推荐实施批次与批准边界

**推荐先批准 P1＋P2 的只读／record-only 实施范围，不先批准新交易机制。** P1 的只读检测面已部分落地（见 §10.1）；P2–P5 尚未实施。

| 批次 | 范围及依赖 | 产出／完成条件 | 不包含 |
|---|---|---|---|
| P1 证据与契约测试 | 固定本轮缺陷夹具；审计原价基、时间／原稿、裁决冲突、确认龄与阶段参数 | T01–T07/T11 的 record-only 面通过；既有行为一致；字段有读取端；交付冻结身份列表、取数时点及结果哈希作为 T10 前置 | 改模型所见输入、改 gate、增加新定时实验 |
| P2 反馈真值 | 在 P1 身份与单位基础上，离线重算窗口、退出日报、毛费净；列出记忆新旧差异 | T08–T10；原始记录不删；对每个回灌载体给出影响清单 | 自动重写生产记忆或让新统计立即进 Prompt |
| P3 输入修正 | 先批准一个单变量：优先昨收标准化；再分别处理时效标签／确定性同稿去重／记忆回灌 | 固定输入对照、全漏斗差异与回滚包；行为类双跑遵守既有≥5交易日纪律 | 把多个输入改变与裁决改变同日打包 |
| P4 裁决契约 | P1 规范状态结果可审计后，单独实施 §6 消费；再单独处理 Prompt 单腿能力说明 | T05/T06，全状态及资格／排序差异获明确批准 | 改 score_band、confidence sizing、换模型、放宽门 |
| P5 保真回放 | 可与 P1/P2 的离线工作并行，执行优化依赖其验收 | T09/T12与既有baseline保真门；逐项解释不匹配 | 保真不足时调退出参数／读取变体收益 |

### 10.1 P1 当前实施进度（2026-10-23，直接修复批）

- **只读审计**: `scripts/da_chain_contract_audit.py` + `tests/test_da_chain_contract_audit.py`（14 passed）。覆盖价格基准、来源时间/同稿、裁决规范状态、计划/提交/成交阶段、记忆窗口/毛净/A10；DB 全 `mode=ro`，默认只打印，`--out` 才写报告。
- **输入修正（已生效于下一次 DA 进程）**:
  - **prev_close**: `llm_synth.synthesize` 组装 Prompt 前调用独立 `prev_close_reconcile.py`，默认 `apply`；守卫=premarket/regular、日K早于今日且 ≤4 日、偏差 0.05%–5%、pre_change_rate 可推导且与日K ≤0.5%。10/22 实测 8/28 修正，原始值留 `prev_close_raw`；回滚 `DA_PREV_CLOSE_BAR_RECONCILE=off`。未改 `data_collector.py`（该文件有另一会话未提交 WIP）。
  - **研报时点/同稿**: 研报改读 `publish_time` 渲染 ET 时间，>7 天标 `⚠️N天前`；与新闻共用 `news_id`/URL 的研报不再作为独立证据重复注入。10/22 实测 100 条研报原渲染无日期、51 条跨栏目同稿；修复后 28/28 份 Prompt 数据段变化（16 份出现带日期研报，部分标的全被去重）。回滚 `DA_SOURCE_TRUTH_FIX=0`。
- **裁决语义（已生效）**: `llm_synth._merge_debate_results` 用 `debate_state.classify_debate` 生成规范状态；`PARTIAL_AGREE` 即使 `agreed=true` 也不再获得一致度加成。10/22 五份冲突样本（BWXT/CAT/ORCL/TSLA/UNH）规范状态均为 PARTIAL；规范 AGREE 仅 2 份。原始 bool 留 `challenger_agreed_raw`，新状态留 `challenger_verdict_state`；回滚 `DA_DEBATE_STATE_FIX=0`。未改 `signal_gate.py`/`adapter.py`（它们继续读已归一的 `challenger_agreed`）。
- **记忆真值（下一次 cron 生效）**: `strategy_memory.get_strategy_hint` 直接从窗口内 `trade_records` 聚合，不再用全史 `pattern_memory` 按 `last_trade_date` 伪装 30 天；金额用 `COALESCE(pnl_net, realized_pnl)` 并加 A10 过滤；Prompt 明示 `(近30日净额)`。实测 30 日 36 组/162 笔/净 +$63.55，旧展示 31 组/513 笔（全史毛额）。另加 `LESSON_TRUTH_START=2026-10-23`：修复前按入场日/毛额生成的 lesson 保留在库供审计，但不再注入 Prompt（旧“7胜0负”类假事实不进入下一 DA）。回滚=恢复 `strategy_memory.py`（本批前该文件干净）。
- **复盘真值（下一次 cron 生效）**: `feedback_loop._get_today_trades` 改按 `exit_time` 的 ET 日期归属，包含隔夜出场；`extract_lessons`/日报用净额，且“全胜”以该方向完整母体为分母。实测 10/22 由旧 7 笔/毛 $249 变为 11 笔/净 $622.19（10胜1负），不再写“全胜”。回滚 `DA_FEEDBACK_NET_EXIT=0`（只回滚净额/退出日；“全胜”修复需 git 恢复文件）。
- **Prompt/执行契约对齐（批次 1，单变量）**: 移除“entry_strategy 必须分批(至少2批)”；明确只写 1 个可执行首批、`confirmation` 是研究确认位不是限价单、执行按单一首批/当前锚重算；保留 `chase_cap` 与遗留第2批几何机检（record-only）。旧“限价路由接回/限价单有效分钟数”表述同步修正；模型可见 Prompt 中已无“第2批”教学段（机检代码保留作旧计划兜底）。新 `PROMPT_HASH=c7444d67ce58`；回滚=恢复本批前 `llm_synth.py`（该文件本批前干净）。离线 10/22 文本对照显示：旧模板与新模板差异仅在上述契约段，不额外新增第二腿执行承诺。
- **附带修尺子**: `scripts/tail_attribution_report.py` 补 A10 `data_quality` / 排除 POSITION_SYNC/MANUAL；`tests/test_tail_attribution_caliber.py` 2 passed；重跑仍 n=685 / net −$456.65（本窗全 clean，读数不变）。
- **验证**: 外部 skill 全套 **113 passed**；repo 审计+尾部 **16 passed**；`py_compile` 通过；10/22 真实审计 rc=0。**未运行** T04 注入隔离、T07 确认龄、T09 固定输入回放、T11 开关矩阵、T12 回放保真；未改任何 gate/仓位/止损/止盈/交易频率。
- **部署面**: 外部 DA 脚本按下一次 cron 新进程加载，主服务无需重启；`strategy_memory`/`feedback_loop` 按各自 cron 生效。`data_collector.py`/`pipeline.py`/`signal_gate.py` 的他人 WIP 未触碰。本批已提交。

### 10.2 全链路检查结论与下一阶段方案（2026-10-23）

**当前状态**：输入事实层已完成第一批修正（昨收 8/28、研报日期/同稿、记忆窗口/净额、lesson 真值、复盘退出日/净额、尾部 A10）；输出层已统一 PARTIAL_AGREE 的规范状态。DA 下一次 cron 会看到不同的输入，必须先观察，不能同批再改门。

**仍未解决且会影响判断的冲突**：

| 优先级 | 冲突 | 证据／影响 | 推荐处理 |
|---|---|---|---|
| P0（已落地，待观察） | Prompt 强制“至少 2 批入场”，执行端只按单一首批/执行锚；`entry_archetype_routing_enabled=false` | `llm_synth.py:1345` 要求分批；schema 只有单个 `confirmation`；SNDK/TSCO 等计划写第二腿但执行不按第二腿 | 已改为只承诺首批/执行锚、第二腿仅研究背景；保留 legacy 机检；下一 DA 批观察，若供给异常恢复旧 `PROMPT_HASH` |
| P0 | 计划/提交 `risk_reward` 被当作实际 RR | `portfolio_guardrails.py:107` 用 `inst.risk_reward` 做 min_rr；SNDK 计划 2.45、成交 1.462、固定 TP 被门关闭 | 只加阶段标签与审计：plan/submitted/filled/exit RR；暂不改 min_rr 阈值 |
| P1 | “研报有效”按 list 非空计数，全部同稿仍算独立证据 | `_classify_data_sources` 统计 research 非空；10/22 CAT 5 条研报全部同稿 | 覆盖统计改“独立研报数”；全同稿标 degraded，不改 score_band 阈值 |
| P1 | 确认后长期有效（CONFIRMED 终态） | 10/21 ARM/SPCX 确认后 51–59 分钟才成交；期间 5 次 chase-cap 拦截 | 先记录 confirm_age/最终偏离；是否重确认另立 DEB，不直接加 TTL |
| P1 | 旧负向论据/条件黑名单仍可能全时段/跨来源 | `_load_negative_claims_warning_for_prompt` 明确“全时段”；condition blacklist 30/60 日但未分 DA/UE | 加 era/source 标签；先记录，不改注入规则 |
| P2 | `exit_time` 存在 23 笔当前时代无时区行 | 7 月行 `2026-07-20T15:52...`；当前 10 月为 ET-aware | 写方统一 ET-aware；审计加 offset 检查，不追改历史 |
| P2 | 回放保真 0.604 < 0.80 | RFC-154 baseline；此时读退出变体是优化模拟误差 | 先修固定输入/成交/费用回放，过门后才评估变体 |

**最优路径（按依赖，不建议并行改行为）**：
1. **观察批（立即）**：下一 DA 批只读观察；记录 SKIP/方向/score、成交/滑点/费用、输入 truth 计数；不改门、不改参数。目的是确认本批真值修复没有造成供给异常。
2. **P0 契约对齐批**：只改 Prompt 的“分批/第二腿”表述，明确当前执行只按首批与执行锚；保留 chase_cap。先做离线 Prompt 行差异 + ≥5 交易日双跑；滚动回滚旧 PROMPT_HASH。不同时改裁决/记忆/门。
3. **P0 阶段真值批（无行为）**：在审计/报表增加 plan/submitted/filled/exit 的 entry、SL、TP、RR、TP 是否启用、确认龄；给 `portfolio_guardrails` 的 RR 门标注“plan-stage”。不改阈值。
4. **P1 来源覆盖批**：独立研报计数、重复证据不计入覆盖；负向论据/条件黑名单加 era/source 标签。先只影响展示与统计，不改入场门。
5. **P1 反馈收口批**：lesson_log 一次性重算/标注（按退出日+净额），旧 lesson 已不注入；memory rebuild 用 dry-run 列差异后再 cron 应用。仍不自动用 lesson 改方向/仓位。
6. **P2 回放保真批**：固定输入、实际成交/限价/费用/TCA；reason_match ≥0.80 后才允许读任何退出/入场变体。工程测试通过不等于策略有效。
7. **可选确认龄批**：先 record-only 攒 confirm_age 与最终成交偏离；到期再裁是否重确认/TTL，不预设新门。

**范围选择（不做）**：不新增入场门/影子；不启用 archetype 限价路由、多腿、失效自动平仓；不改 SL/TP/VWAP/仓位/频率；不复活 UE 直写/intraday；不重开 C8–C13/RFC-163；不触四锁/实盘/毕业门禁；不在回放保真未过门时用回放读数调策略。

**验收口径**：输入 truth 计数（昨收错配=0、独立研报>0或显式 degraded、重复证据不注入、记忆 30 日精确、lesson 按退出日净额、exit_time offset 全 ET）；输出一致性（verdict 规范状态 100% 一致、plan/submitted/filled RR 全可查、无消费方读 raw bool）；行为只观察（SKIP率/候选/成交/滑点/费用/净 bps 与修复前对照，不作显著性结论）；回放（reason_match≥0.80 且全漏斗）。

### 发布与回滚清单

- 每批批准记录必须列：精确文件／字段、输入或行为差异、固定评估窗、适用既有判据、部署面、回滚动作与所影响版本。缺任一项不进入发布。
- 没有对应收益假设预注册的修复，只验数据／契约正确性与非目标路径不回归；不能临场添加“收益提升X%”门，也不能用收益不显著否决一个已证实的事实修正。
- Prompt／数据渲染／动态记忆改变均可能改变信号分布：记录各自版本，按一个变量一个批次评估。外部 Hermes 文件不受主仓提交自动保护，需记录原文件哈希与备份，不假装已入主仓版本控制。
- 纯 record-only 回滚只撤新增观测读写，不改订单；输入／裁决回滚恢复批准的源版本或既有开关，停止对后续新指令使用该版本。**不得通过恢复旧生产 DB 快照回滚交易**，不得改写已成交仓位／已发生订单。
- 历史记忆修订应可撤销其后续注入版本，不删除原交易或伪造旧 Prompt；对未成交在途指令按已批准生命周期处置，不擅自重新生成／撤销。
- 外部每次新起的 DA 脚本按下一进程生效；主服务加载的 Python 变更按军规核仓位／在途出场／WIP 后用 `systemctl restart ftseek-us`，并跑启动冒烟。纯本文档不需重启。
- 到每批验收时，独立复核实际 diff、固定夹具、真实新产物和非目标路径；状态只在 `handoff.md` 更新，不能把“有文档／有字段／测试过”写成“运行态已验证”。

### 明确不做

不复开 C8–C13 已终裁轴；不依据纸面错失收益改 entry；不启用 archetype 限价路由、多腿、失效自动平仓或确认 TTL；不调 SL／TP／VWAP／仓位／频率；不复活 UE 直接写入或 intraday；不重开 RFC-163；不触四锁／实盘／毕业门禁；不自动删除字段、脚本、cron 或影子。

未来如果批准删 Prompt 字段或退役载体，仍须先执行六面零引用审计；本文的精简建议不代表已通过该审计。11 月实验上限与现有实验判据保持不变，优先复用已有审计，不新增平行注册表。

## 11. 证据锚点与复现约定

以下行号为 2026-10-23 复查时定位，后续以函数／字段为准：

| 主题 | 代码／产物 |
|---|---|
| 昨收阈值修正 / 日K核对 | `/root/.hermes/skills/deep-stock-analysis/scripts/data_collector.py:366`（现有 1% 推导）；独立核对模块 `scripts/prev_close_reconcile.py`（未接线） |
| 研报／社区日期、数字核验 | `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:609`、`:2971` |
| 只读契约审计 | `scripts/da_chain_contract_audit.py`；`tests/test_da_chain_contract_audit.py` |
| prev_close 影子测试 | `/root/.hermes/skills/deep-stock-analysis/tests/test_prev_close_reconcile.py`；独立模块 `scripts/prev_close_reconcile.py` |
| Prompt、挑战者视图与裁决 | 同文件 `:1144`、`:2284`、`:2415`、`:2661` |
| 分级／适配 | `/root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py:377`、`:405`；`adapter.py:217` |
| 记忆与日报 | `/root/.hermes/skills/deep-stock-analysis/scripts/strategy_memory.py:192`、`:371`；`feedback_loop.py:62`、`:137` |
| 实际注入入口 | `/root/.hermes/skills/deep-stock-analysis/scripts/deep_analyzer.py:544` |
| 桥接／原计划保存 | `/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py:2011`；raw `planned_entry` |
| 预算排序／重定价 | `app/playbook/pipeline_parts/auto_execute_flow.py:136`、`:463` |
| 确认终态、TP门 | `app/playbook/entry_confirm_gate.py:406`；`app/playbook/exit_engine.py:716` |
| 阶段审计 | `scripts/stop_invalidation_audit.py:145`；既有 `exec_gate_audit`／`quality_shadow`／`vwap_gate_ab` 载体 |
| 原始样本 | `data/da_checkpoint/2026-10-22/`：28份 `.raw.json` 均含 `user_prompt`；CAT/TSLA/UNH、TSCO、SNDK 对应 `.json` |
| 成交样本 | `data/ftseek_us.db`：TSLA instruction4569/trade1718；SNDK instruction4572/trade1712；10/21 ARM instruction4506、SPCX instruction4514 |
| 回放保真 | `data/shadow/exit_t_arm_latest.json:121`；`docs/decisions/RFC-154-exit-conditional-shadow.md` |

收益复算最小母体（只读 SQLite `mode=ro`；正式脚本复用 A10 SSOT，不另造边界）：

```sql
SELECT id, trade_date, entry_price, quantity, realized_pnl, pnl_net,
       fees_source, json_extract(metadata_json,'$.config_version') AS exit_version
FROM trade_records
WHERE source_type='PLAYBOOK_SIM'
  AND decision_id LIKE 'DA%'
  AND trade_date BETWEEN '2026-10-06' AND '2026-10-22'
  AND exit_price>0 AND pnl_net IS NOT NULL
  AND COALESCE(data_quality,'') IN ('clean','real')
  AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')='';
```

A9 对照去掉最后一项单独复算；按 source_type 分层且本窗始于 10/06，满足现役日期及来源过滤。真实账本继续变化，**上述 SQL 固定入场窗但不冻结未来结算**；不能要求以后重跑仍恰为 80 笔。实施 P1 时须冻结身份列表、原始结果与取数时点／哈希，形成不包含未来结局的评估夹具，本文未创建该夹具。

逐笔均值的聚类计算：令 `y_i=10000*P_i/N_i`、`mu=mean(y)`、日簇数 G、笔数 n；`SE=sqrt(G/(G-1)*sum_d(sum_i∈d(y_i-mu))²)/n`。80% CI 使用 `mu±t(0.90,G-1)*SE`，MDE80 小样本近似为 `(t(0.975,G-1)+z(0.80))*SE`。名义加权口径须对自己的估计量计算日簇不确定性，不能复用该 CI。

## 12. 审查与决策记录

- 2026-10-23：按用户要求生成本契约；推荐 B，推荐先做 P1/P2，再逐变量批准 P3/P4，P5 先修尺子。没有把文档请求解释为生产执行授权。
- 前序只读复核的两项分歧经主审复核：现役记忆查询谓词确实可纳入 9 笔 6/24–6/26 行，不应宣称已有 A10 等价隔离；数字核验虽扣 `quality_score`，但当前 tier 不消费该分，不应宣称有 live 软门拦截。两处均按实际代码／SQL处理，不按审查方多数意见表决。
- 本契约的独立审查与修订结论写回关联 DEB §十五；批准、实施和运行态验收尚待后续批次，不能将 §9 的目标测试误报为已通过。
