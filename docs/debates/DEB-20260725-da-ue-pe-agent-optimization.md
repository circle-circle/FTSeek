# DEB-20260725: DA/UE 链路 PE 工程与 Agent 编排优化 — 起案待辩论

> **状态**: 🔶 待辩论 (作者=Qoder-主工程会话 起案; 方案分级, 修尺子项可先行, 机制项待裁决)
> **数据基础**: 全部绑定当日实证 (信息源复盘 §handoff 07-25 + audit/scorecard 实测), 无凭空断言
> **宪章合规**: 一事一案分级; prompt 变更走 prompt-change-discipline (版本戳+A/B 切片); 零未裁决 live 变更

---

## 一、链路现状与实证痛点 (10 秒版)

| 链路 | 编排 | 实证痛点 (近14天) |
|---|---|---|
| **DA** | Hermes skill: 48标的 → qwen分析师 → deepseek挑战者(双pass) → signal_gate → 13条 | 方向命中 52% (+0.31% 薄弱正edge); **conf 被 gate WARN 罚成常量35 (89/95)**; **debate_outcome 才是真分层器 (分歧BUY 67% > 弃权47%) 但编排把分歧当惩罚**; SHORT 0/6 全烂; 方向偏斜 99:9 |
| **UE** | llm_universe_scan: 30条RSS标题+异动 → qwen 单模型单pass → 直出 | 命中 **29% (-0.67% 负edge)**; **重复推荐无反馈 (ORCL SHORT×5)**; 无对抗无自评校验; 输入=标题级浅材料 |

## 二、PE 工程层建议 (P1-P5)

**P1. 预测性输出与叙事性输出分离 (UE 补可证伪字段)**
现状: UE 输出 thesis/causal_chain(叙事) + direction(预测), 无 invalidation/时间窗 → 只能判"方向对错", 无法判"论点是否兑现"。LLM 叙事能力≫校准能力, 不加可证伪结构 = 给叙事能力发预测的奖。
处方: UE 输出 schema 增 `invalidation`(失效条件) + `horizon_hours`(论点时效) + `expected_move_pct`(幅度预期) — 纯输出字段加法, 落库供判分, 不改任何执行。**[修尺子, 可先行]**

**P2. confidence 的 rubric 化 (从"问自信"改"问证据清单")**
现状: LLM 自报 conf 无校准 (实证 conf≥70 反而 25% 命中); UE self_tier 已是雏形。
处方: 让模型输出可枚举 `evidence_checklist` (catalyst 有无/时效≤24h/量价确认/拥挤度自评 4 项 bool), **代码侧按 checklist 算 rubric_score** — 把校准从模型移到代码, 模型只做它擅长的"枚举证据"。与 P0.2 conf 校准修复合流。**[修尺子, 可先行]**

**P3. UE 负反馈注入 (上下文工程治重复推荐)**
现状: ORCL SHORT×5 根因 = 模型每天看同样新闻自然得同样结论, **它不知道昨天推错了** (无反馈回路; DA 有 strategy_memory, UE 零)。
处方: user prompt 注入近7日"本源已浮现标的 → fwd 结果"小表 (≤10行), 指令: "对连败标的须给出新增量证据才可再浮现"。比代码侧硬冷却更聪明 — 保留"事件持续发酵"的合理重复, 砍掉"无新信息的惯性重复"。**[修尺子级 prompt 加法, 建议先行+版本戳 v2.2]**

**P4. 信息饮食升级 (输入质量锁死输出上限)**
现状: UE 输入 = 30 条 RSS 标题 (标题级, 无正文无时效精度)。29% 命中的一部分是输入太浅。
处方: ①HARD_EVENT 预筛加权排序前置 (基建已有 `_gather_hard_events`) ②注入盘前异动表 (premarket_enrichment 现成) ③每条新闻带发布时点+距今小时数 (时效感知)。**[修尺子, 可先行]**

**P5. SHORT 的 PE 级高门槛 (不禁但提高输出成本)**
现状: SHORT 全烂 (DA 分歧 SHORT 0% 命中 -4.5%; UE SHORT 25%)。LLM 训练分布天然多头叙事强, prompt 喊"多空对等"无法对抗先验。
处方: SHORT 输出强制附加字段: `borrow_feasible`(可借券自评)/`downside_catalyst_expiry`(下跌催化剂时效)/`squeeze_risk`(逼空风险 1-5)。提高 SHORT 的输出成本 = 天然质量门, 保留方向灵活性 (优于硬禁)。**[prompt 变更, 需 DEB 裁决]**

## 三、Agent 编排层建议 (A1-A4)

**A1. 分歧的奖励反转 (最重要的编排错位)**
实证: 分歧BUY 67%命中+0.9% > 弃权47% > 2模型一致 — **挑战者的价值在"制造有信息量的分歧", 但 gate 把分歧罚成 conf 35 降权执行**。编排激励与实证完全反向。
处方: gate 对"方向分歧但主分析师坚持"不罚 (或轻罚), 对"弃权/无争议"维持罚 — debate_outcome 升为一等公民信号。**[机制变更, 需 DEB + n≥100 样本; scorecard 切片已上线攒数据]**

**A2. UE 加轻量自我对抗 (self-critique pass)**
现状: UE 单模型单 pass 直出 (DA 双 pass 已证明分歧有信息量)。
处方: surfaced 产出后加一个 self-critique pass: "逐条攻击因果链最弱一环, 标记 weakest_link, 可降级/剔除"。同模型二次调用成本 +~$0.05/日, 显式缓存已开 (同前缀命中 98%) 时延增量小。**[机制变更, 需裁决; 建议 shadow 先行 (critique 结果落库不执行)]**

**A3. 裁判-分析师意见分离存储**
现状: gate 直接改写 conf → 分析师原始判断被裁判覆盖 (confidence_semantic 落库已修第一步)。
处方: gate 输出多维标签 (rr_flag/tier/freshness) 独立落库, 永不改写分析师字段 — "谁说的什么"全程可追。**[修尺子, skill 侧改动, 可排周末]**

**A4. 时效衰减管理 (thesis 的保质期)**
现状: DA 19:31 产出 → 次日 10:25 执行, 隔 15h; 昨日 4 笔亏损单全部"入场即水下" — 论点可能已 price-in。
处方: 指令带 `thesis_valid_until` + 盘前 topup 窗对隔夜指令做一次"是否已 price-in"轻复核 (现有 thesis_challenge 基建可复用, 每标的一次 flash 级调用)。**[机制变更, 需 DEB; 与入场分散化议题合流]**

## 四、评估闭环 (E1-E2, 全部修尺子)

**E1. prompt_version 自动 A/B 切片**: scorecard 加 prompt_version 维度 (UE 已落库 v2.1 戳) — 每次 prompt 变更自动可判, 不再靠人工回忆"哪天改的 prompt"。
**E2. 判分纪律**: 所有本案变更 n≥30 方可判; UE tier 切片 (已上线) n≥30 前 THESIS_ONLY 通道不下结论。

## 五、落地分级与顺序

| 级 | 项 | 时机 |
|---|---|---|
| 修尺子·可先行 | P1 UE可证伪字段 / P2 rubric雏形 / P3 负反馈注入(v2.2戳) / P4 信息饮食 / E1 切片 | 周末~下周, 每项独立版本戳 |
| 需 DEB 裁决 | P5 SHORT高门槛 / A1 分歧奖励反转 / A2 UE self-critique / A4 时效复核 | A1 等 n≥100 (约2-3周); 其余用户裁 |
| 终裁后 | 编排大重构 (多角色/动态定序) | 08-29 后新阶段 |

**排序理由**: 修尺子五项共同点 = 只增加可观测性/输入质量, 不改任何执行行为; A1 是数据最强但影响最大的机制变更, 让 scorecard 切片再攒 2 周到 n≥100 再裁, 恰好衔接 08-07 中期裁决点。

## 六、预注册判据 (辩论方/裁决用)

- Q1: P3 负反馈注入后, UE 重复推荐 (同标的同方向 streak≥2) 占比是否下降且不误伤"事件发酵型"正确重复 (判据: streak≥2 子集 fwd 是否改善)?
- Q2: P2 rubric_score 与 fwd_return 的相关性是否 > 现 conf (n≥30)?
- Q3: A1 若实施, 分歧单放量后 67% 命中是否保持 (防"观察效应" — 现在的 67% 是低仓位下的)?
- Q4: P4 输入升级后 UE 整体命中是否脱离 29% 负 edge 区 (判据: 连续 2 周 ≥45%)?

---

*本 DEB 仅方案与判据, 零代码变更。修尺子先行项落地时各自带版本戳+回滚说明入 handoff。*
