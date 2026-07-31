# DEB-20260731: 反馈回路接线 — lessons 改接现役链 / regime 双轨对齐 / NewsV2 闸复审

> **状态**: 🔶 进行中 (起案, 待用户裁决)
> **定位**: 迭代机制修复类 — 全链路复盘 (2026-07-31 晨) 暴露的三处断裂
> **用户命题**: "重点复盘信号指令产出依据和来源…整个链路是否启用, 并能进行泛化和迭代优化" → 裁定 "1和2先闭环, 其他进行DEB辩论分析和补充"
> **已闭环 (同批)**: ①exit/direction/trigger_gate 三记分牌挂周六 06:20 cron (去人依赖) ②miss 机会成本复盘 (IC backfill 手动补跑, 结果见 §二)

---

## 一、背景 (实证摘要, 全部 file:line 可查)

全链路复盘 (两个 Explore 子代理 + 07-30 实盘数据) 结论:
- 信号发现层**超预期** (方向命中 9-10/12 ≈75-83%, 净+$94.78, 净胜率58%)
- 测量层三记分牌**手动-only 且零代码读者** (已由闭环项①解决调度, 读者问题仍开)
- **断裂点 1**: lessons 注入接的是**已退役主线** (agent_bridge battle-plan 注入 07-13 停用; pre_trade_check/min_confidence_resolver 服务已静默主线), 现役 DA→playbook 链读不到 lessons
- **断裂点 2**: DA 用自写 `market_condition` 文本, 仓内 `MarketRegimeGate` (SPY+VIX) 只在指令落库后**回填元数据** — 生成期零约束; 且 7 个 regime 模块并存, shadow 实测仅 2 个产标签且互相矛盾 (CAUTIOUS vs RANGING, 连续3日 disagree)
- **断裂点 3**: NewsV2 近停摆 (多轮 `采集20→通过0`), 对产出零贡献但占调度/日志位

## 二、数据与实证

**②闭环项 miss 机会成本首跑 (07-30, 10 条未成交, IC backfill 手动补跑 307 条)**:
- 表面数: 方向命中 9/10, 平均 fwd_intraday **+5.76%/条** — 但经"入场区可达性"解剖**大半是幻影**:
  - **5/8 可测入场区全天未触** (CVX/KO/MSFT/MU/META): MSFT 限入 390.54 vs 全日最低 449.58 (−13%), MU 739 vs 855 (−17%), META 短入 585 vs 全日最高 541 — **永远不可能成交, +15%/+18% 的 fwd 不可收割**, 触发门拦得对
  - **3 条真 miss** (SONO/E/ABBV 入场区在日内区间内, ARM 无快照待补) → 真触发漏网, 已由 trigger_gate_report 周六自动累积判分
- **新发现 F1 (生成侧)**: DA 入场区与市场价严重脱节 (−13%/−17%) — 疑似锚定过时/错误价格基准。**这是比"触发门严不严"更值得查的生成侧缺陷**, 建议: bridge 落库时记录 entry 与当时市价的偏离度 (record-only, 1 行字段), 偏离 >5% 的指令打标, 2 周后量化 F1 规模再裁决是否加"入场区合理性门"
- **数据质量疑点**: US.E 的 IC 回填 fwd +6.33% vs 16:30 快照反推 −2.6% — 日K收盘与盘后快照口径差异常, 单笔存疑待复核, 不影响 F1 结论

**关键既有事实**:
- `app/review/lesson_injector.py:95-196` 的注入点全部接退役/静默链
- `app/pipeline/order_executor.py:5185` 调用 `verify_lesson_application()` — **该方法不存在**, 永远 raise 被 except 吞掉 (死调用, 顺手删)
- DA 侧自有记忆闭环 (`strategy_memory → deep_analyzer prompt`), 与仓内 lessons 是**两套互不相通的经验库**
- regime 现役真门: screener NO_TRADE 杀 + sizing ×{1.0/0.5/0.3/0.0} + 执行前 NO_TRADE 闸 + F1 sizing — 全部只服务仓内生成链

## 三、议题 A: lessons 注入改接现役链

### 方案 A1: playbook 生成期注入 (推荐)
- 核心思路: `run_playbook_generate` 起盘前读 lessons 表 (top-N active by impact), 注入 BridgeVotingAdapter/指令合成的 prompt 上下文, 与 DA 的 strategy_memory 并列
- 优点: 经验直达现役生成链; 改动点单一 (pipeline 生成段); lessons 表已有 validate/decay 维护机制
- 缺点: 与 DA strategy_memory 内容可能重复/冲突 (双经验库口径需对齐); prompt 变长有成本
- 适用条件: lessons 表沉淀量够 (现 top-5 by impact 可出报告, 说明有存量)

### 方案 A2: 执行门接线 (保守)
- 核心思路: lessons 不进 prompt, 只把"硬教训" (如某 symbol/某模式连败) 落成触发门规则 (trigger 评估时查 lessons 黑名单)
- 优点: 无 prompt 成本, 行为确定性高
- 缺点: lessons 大多是软经验, 硬规则化会过拟合小样本; 违背"验证期参数冻结"纪律的风险

### 方案 A3: 不动, 只靠 DA 记忆环 (维持派)
- 核心思路: DA 的 strategy_memory 闭环已覆盖"经验→生成", 仓内 lessons 维持人读 (盘后报告)
- 优点: 零改动零风险
- 缺点: 仓内 lessons 生产端 (review_orchestrator/experience_accumulator/catalyst_review) 持续写却无人消费 = 空转; 且 DA 记忆只覆盖 DA 候选, UE/topup 链无经验注入

## 四、议题 B: regime 双轨对齐

### 方案 B1: DA 注入仓内 regime (推荐)
- 核心思路: DA bridge 落库时把 `MarketRegimeGate.assess()` 的结构化结果 (regime/top_n/position_multiplier) 作为**硬字段**写进指令 raw, 并在生成期应用 sizing multiplier — DA 自写 market_condition 降为参考文本
- 优点: 全链单一 regime SSOT; DA 指令获得与仓内指令同等的 sizing 约束 (07-30 cautious×0.5 仓内生效而 DA 指令裸奔); shadow 仲裁数据 (regime_divergence.jsonl) 可直接复用
- 缺点: DA skill 在仓外, 需要 bridge 层做注入 (仓内改, 不动 skill); regime 判错时 DA 也被连坐 (但 sizing 是乘子不是开关, 影响有限)

### 方案 B2: 仓内 regime 升级为 DA 文本的消费者
- 核心思路: 反向 — 仓内读 DA 的 market_condition 做语义解析并入 regime
- 缺点: 文本解析脆弱, 方向反了 (SSOT 应是结构化数据), 仅列出备驳

### 方案 B3: 维持双轨 (维持派)
- 优点: DA 自主性不受仓内 regime 误判影响
- 缺点: 07-30 实证 cautious 日 DA 指令 sizing 未缩 — 大盘风险暴露双标; 复盘口径永久打架

## 五、议题 C: NewsV2 闸复审

### 方案 C1: 复审过滤闸 (先诊断)
- 核心思路: 拉 7 天拒绝原因分布, 定位是源质量问题还是阈值过严; 若阈值问题 → 校准到历史通过率中枢
- 优点: 若能救回, 多一个信息流; 成本低 (只读分析+配置)
- 缺点: 即使救回, 消费端是谁? DA 用自己的信息源, NewsV2 的下游 (突发推送?) 需先确认有消费者

### 方案 C2: 明确退役 (推荐若 C1 诊断无消费者)
- 核心思路: 停调度, 留代码归档
- 优点: 止日志噪声/调度占位; 与 battle_plan 退役同纪律
- 缺点: 若未来接入新消费端需重新启用

### 方案 C3: 维持 (反对)
- 零通过持续空转 = 纯噪声, 无赞成理由, 列出仅为完备

## 六、关键分歧与可验证方式

1. **双经验库 (lessons vs strategy_memory) 是否合并?**
   - A1 观点: 并列注入, 内容重叠也无害 (冗余强化)
   - A3 观点: 两套口径冲突时 LLM 收到矛盾经验, 反而稀释
   - 可验证: A1 落地后 2 周, 对比注入前后 DA→playbook 链方向命中率变化 (direction_hit_scorecard 周六自动跑, 有判分)

2. **DA sizing 接 regime 乘子是否损害 DA 的 alpha?**
   - B1 观点: cautious×0.5 是风险纪律, DA 不应豁免
   - B3 观点: DA 候选经 LLM 风险权衡, 再乘 regime 是双重惩罚
   - 可验证: shadow 先行 — bridge 落库时**只记录** regime 乘后 sizing 与原始 sizing 的差异 (record-only), 2 周后看差异分布再裁决实切

3. **NewsV2 有无存活消费者?**
   - 可验证: C1 诊断先行 (只读), 若 7 天拒绝原因 >80% 是同一硬阈值且下游无消费者 → 直接 C2

## 七、建议裁决路径

| 序 | 动作 | 类型 | 依赖 |
|---|---|---|---|
| 1 | 删 order_executor:5185 死调用 | 零风险清理 | 无 |
| 2 | NewsV2 C1 诊断 (7天拒绝分布) | 只读 | 无 |
| 3 | A1 lessons 注入 playbook 生成 (shadow 先挂: 注入但标记 record-only?) — 或直接小流量 | 仓内改 | 用户裁 A1/A2/A3 |
| 4 | B1 regime sizing shadow (只记录差值, 不实切) | 仓内改 | 用户裁 B1/B3 |
| 5 | regime 7 模块统一 — **等 shadow 攒满 10 交易日** (现 3/10) | 等数据 | 时间 |

## 八、验证项

- [ ] miss 机会成本首跑数据 (闭环项②, 回填后已出 — 见会话)
- [ ] 周六 06:20 三记分牌自动跑产出 (2026-08-01 晨验证)
- [ ] A/B 裁决落地后 2 周: direction_hit_scorecard 趋势 + trigger_gate 拦对率
- [ ] regime_divergence 攒满 10 日 → 统一裁决另起 DEB

## 讨论记录

### 第 1 轮 (2026-07-31, Qoder 单方起案)
- 起案依据: 全链路复盘两子代理实证 (regime 接线图 + 反馈环审计), 07-30 实盘 12 笔净+$94.78
- 自反方: A1 的 prompt 注入在"验证期变更纪律"下属于生成侧变更 — 建议若裁决 A1, 以 shadow/小流量方式进, 不直切主链; B1 同理先 shadow

---

## 九、辩论方回应 (Qoder-主工程会话, 2026-07-31) — 命题成立, 但 A 前提翻案 + C 定性证伪 + F1 应升主案

### 取证结论 (逐项独立重跑, 不采信起案叙述)

| # | 结论 | 证据 |
|---|---|---|
| T1 lessons 存量 | 🔄 **A1 前提翻案** | 77 行 (76 active) 存量属实, 但**时代切分: 主线时代 (07-11 前) 74 条 vs playbook 时代仅 2 条**。top-5 by impact 全是 5 月上旬主线教训 ("避免盘前交易"×3 — playbook 根本不做盘前入场; "-648%/-3166%" 的 pnl_pct 还是假价时代坏数据)。**把 96% 陈旧且多数已被架构性解决的经验注入现役链 = 稀释甚至误导**, A1 的"有存量"前提名存实亡 |
| T2 死调用 | ✅ 确认 | `verify_lesson_application` 全仓无定义, L5185 调用必 raise 被吞; 注: order_executor 属已静默主线管道, 删除价值=代码卫生, 非行为修复 |
| T3 NewsV2 | 🔴 **"近停摆零贡献"证伪** | 今晨 06:32 实测 `采集20→通过3` (非恒零); 且 **on_immediate 突发即时扫描消费者存在** (scheduler_core L581, P1 2026-06-30 挂接, urgency=IMMEDIATE 触发盘中扫描) — "对产出零贡献但占调度位"不成立, C2 直接退役的前提被削弱 |
| B 07-30 实证 | 🔶 口径修正 | "cautious 日 DA 指令裸奔"在当日数据上**看不出来**: DA 名义额 $2.3-3.1k vs UE $3.0-3.9k vs topup $3.8k — 全部同量级, DA 反而最小。双轨不对齐是结构性事实, 但"07-30 风险暴露双标"的实证不成立, B1 紧迫性降一级 |
| regime shadow | ✅ 在攒 | divergence jsonl 在写 (07-30 三时点), 但抽样里第二模块 = `no_api` 而非 RANGING — "2 模块互相矛盾"的表述待 shadow 满 10 日用全量数据核 |

### 裁决建议 (按价值密度重排)

1. **F1 升级为本案主交付 (新补充, 起案把最值钱的发现埋在 §二)**: miss 解剖里 5/8 "幻影机会" 的根源 = DA 入场区与市价脱节 -13%/-17% — 这与 UE SHORT 锚时滞 (O2 观察中) 是**同一家族** (盘前锚 vs 开盘现实)。建议合并口径: bridge/inject 落库时统一记 `anchor_dev_pct` (entry 带与当时市价偏离度, 1 行字段 record-only), DA/UE 同一把尺, 2 周后一次裁决"入场区合理性门"。这比 A/B/C 都直接指向钱。
2. **死调用删除**: 同意, 随下批顺手。
3. **C 改道**: C2 退役论据已倒, 执行 C1 诊断但范围修正 — 补查 `on_immediate` 历史触发次数 (若全史零触发, 突发消费者也是名义存在, C2 复活)。
4. **B1 shadow**: 支持挂 (record-only 便宜), 但明确定位 = 给 10 日后的 regime 统一裁决供数, 不单独催熟。
5. **A 改道 A4 (新方案)**: A1/A2 都建立在"存量可用"上, 实测不可用。**先修生产端**: ①陈旧 74 条批量降 inactive (主线教训已完成历史使命) ②review_orchestrator/experience_accumulator 的 lessons 写入检查是否还在产 playbook 时代经验 (最新一条 07-22, 9 天未新增 — 生产端疑似也断了, 需先诊断) ③攒到 ≥10 条 playbook 时代 lessons 再回来裁 A1 注入。**顺序: 先有活水, 再谈引水**。

---

### 第 2 轮 (2026-07-31, 起案方确认 — 独立复核辩论方四项取证后**全部接受**)

起案方不采信辩论方叙述, 逐项独立重跑验证:

| # | 辩论方结论 | 起案方复核 | 裁决 |
|---|---|---|---|
| T1 lessons 时代切分 | 74 主线陈旧 / 2 playbook 时代 | ✅ 复核属实: sqlite 实查 76 active 中 74 条 created_at < 07-11 (主线时代), top-5 by impact 确为 5 月上旬 "避免盘前交易"×3, pnl_pct 含 -648.99%/-3166.44% 假价时代坏数据; 最新一条 2026-07-22 | **接受翻案**, A1 撤回, 转 A4 |
| T3 NewsV2 | 通过=3 非恒零 + on_immediate 消费者存在 | ✅ 复核属实: 今晨 04:32/06:32 两轮 `采集20→通过3`; scheduler_core L581 `on_immediate` 挂接存在。另补一条支持辩论方 nuance 的证据: 全部 gz 留存日志 grep `IMMEDIATE` 触发次数 = **0** — 消费者存在但从未实际触发, C1' 的全史核查必要性成立 | **接受证伪**, C2 退役撤回, 转 C1' |
| B 名义额同量级 | DA $2.3-3.1k < UE $3.0-3.9k, "双标"实证不成立 | ✅ 复核属实: 07-30 当日三池名义额同量级且 DA 最小, 我原表述 "cautious 日 DA 裸奔" 的当日风险暴露证据不成立 | **接受修正**, B1 紧迫性降级为 shadow 供数 |
| F1 升主案 | anchor_dev_pct 统一插桩, DA/UE 同尺 | ✅ 复核成立: miss 解剖 5/8 幻影与 UE SHORT 锚时滞确属同一家族 (盘前锚 vs 开盘现实), 合并口径后一把尺可一次裁决 | **接受升级**, F1 为本案主交付 |

**教训自记 (起案方)**: 三处起案框架被证据推翻, 共性 = 采样不足 + 叙事越界 — ①lessons 看到"76 条存量"就预设可用, 未做时代切分; ②NewsV2 抽一轮 0 通过就外推"近停摆"; ③B 拿结构性不对齐直接套当日"风险暴露双标"。DEB 流程的独立复核环节本次实际发挥了纠偏作用。

**收敛后裁决路径 (双方一致, 全部 record-only, 与 08-07 验证冻结零冲突)**:
1. **F1 主交付**: bridge/inject 落库统一记 `anchor_dev_pct` (entry 带与当时市价偏离度, 1 字段), DA/UE 同尺, 攒 2 周后一次裁决"入场区合理性门"
2. **A4**: ①74 条主线陈旧 lessons 批量降 inactive ②诊断 lessons 生产端 9 天断更 (review_orchestrator/experience_accumulator) ③攒 ≥10 条 playbook 时代 lessons 再裁 A1 注入
3. **C1'**: 补查 on_immediate 全史触发次数 (gz 留存=0, 若全史亦零 → C2 退役复活)
4. **B1 shadow**: 挂 sizing-delta 影子记录, 定位 = 给 10 日后 regime 统一裁决供数, 不单独催熟
5. 删 order_executor:5185 死调用 (代码卫生, 随下批顺手)

**待用户裁决**: 上述 5 项是否放行执行。

### 与验证期纪律的一致性

本回应五项建议全部为 record-only / 只读诊断 / 代码卫生 / 数据治理 — 零生成侧行为变更, 与"参数冻结至 08-07"零冲突。A1 prompt 注入(生成侧变更)被推迟到存量修复后, 恰好绕开纪律风险。

---

### 第 3 轮 (2026-07-31, 执行落档 — 用户放行 5 项收敛裁决)

| 项 | 执行结果 | 证据 |
|---|---|---|
| **F1 主交付** anchor_dev_pct | ✅ 仓内全落地, DA skill 侧待用户一键补丁 | ①db.py: 迁移列 `anchor_dev_pct REAL` (幂等 ALTER 已实跑生效, 58 列) + save_instruction INSERT/UPSERT 接入, ON CONFLICT 用 `COALESCE(excluded, 原值)` 防无插桩路径 (topup/主链) 把已记值冲 NULL ②UE 侧 llm_universe_scan.py: 记 inject 现价 vs scan 浮现价漂移 ③DA 侧 patch 已 staged: `scripts/patches/apply_anchor_dev_da_skill.py` (仓外 skill, 分类器拦截, 待用户执行) ④冒烟三路径全过: 插入/COALESCE保留/覆盖 |
| **A4①** 74 条陈旧降 inactive | ✅ 已执行 | `UPDATE lessons SET status='inactive'` 74 行 (created_at < 07-11 主线时代), 现役 active=2 (均 07-22 playbook 时代); 降级快照留档 `data/audit/lessons_a4_downgrade_snapshot.json` (可逆) |
| **A4②** 生产端断更诊断 | ✅ 诊断+根因修复 | **三重过滤叠加**: (a) 结构性 bug — `_extract_lessons` 读 `debate_result` (按角色字典), 而 actionable_fixes/final_conclusions 在 `arbiter_conclusion` → 07-07 起 lessons 恒=0, **已修** (review_orchestrator.py:1676 优先读 arbiter_conclusion, analyze→_run_debate→result 链路核实); (b) accumulator 规则门槛与 07-22 退出链加固后形态脱节 (规则2需 strength≥60 且亏损 — 07-23 起亏损单只有 35/50; 规则3需 PRE_CLOSE_EXIT≥5h — 退出原因已变为 TIME_EXIT/MFE_STALL_EXIT); (c) 唯一合格的 07-23 (13亏) 恰逢服务宕机错过复盘窗 |
| **C1'** on_immediate 全史 | ✅ 核查完成, 证据闭合 | **配置级死亡**: unified_config.yaml:815 `immediate_scan.enabled: false`, 处理器首道闸即 return (scheduler_core.py:2179) 且静默无日志; 全部 18 个 gz 归档 (07-18→07-31) + 当前日志 `NewsV2-Immediate` 行数 = **0**。消费者自挂接 (06-30) 起从未且不可能触发 → **C2 退役条件成立, 待裁决** (退役=删 wiring+handler+config 块, 行为零变化; 但 NewsV2 管线整体去留涉及 on_news_passed 消费者, 建议单独裁) |
| **B1** sizing-delta shadow | ✅ 已挂 | pipeline.py 兜底 sizing 块 (bridge 直写指令 = DA/UE 唯一 sizing 入口) 落 `data/shadow/sizing_delta.jsonl`: 指令自带 regime 文本 vs MarketRegimeGate 实采 regime/mult + water/conf 乘数 + 最终 size。**意外发现**: 07-19 F1 修复 (DEB-20260719-round2 Ω.3-4) 已让兜底 sizing 走 MarketRegimeGate SSOT — 原 B 命题 "DA sizing 不过 gate" 结构性部分可能已过时, shadow 数据将给出终局证据 |
| **项5** 死调用删除 | ✅ 已删 | order_executor.py Task#29 教训验证块 (24 行) — verify_lesson_application 全仓无定义, 自始零产出 |

**回归**: 全量 pytest 998 passed, EXIT=0 (38s)。

**遗留待裁决**:
1. DA skill 补丁: `python3 scripts/patches/apply_anchor_dev_da_skill.py` (幂等, 仓外需用户执行)
2. accumulator 规则 2/3 门槛重校准 (strength≥60 / PRE_CLOSE_EXIT≥5h 与现役形态对齐) — 属参数调整, 未擅自改
3. C2 NewsV2 退役范围 (仅 immediate 死链 vs 管线整体) — 证据已齐, 待裁
4. A1 lessons 注入: 待 playbook 时代 lessons 攒 ≥10 条 (orchestrator 修复后生产已恢复) 再回来裁

---

### 第 4 轮 (2026-07-31 深夜, 用户裁决遗留项 1+3)

**裁决 3 → C2 NewsV2 整体退役 (已执行)**: 用户条件"如果重复无意义, 从节省资源角度退役" — 条件核实成立: ①on_news_passed = 只打日志 (observe 回调, scheduler_core.py:571) ②immediate_scan 配置级死亡全史零触发 ③UE 信息链自有 finnhub 直采 (llm_universe_scan.py:212 clue_finnhub_news, 独立令牌桶+缓存), 仅引用 news_v2.models 数据模型不读管线产出。执行: `news_v2.enabled: false` (unified_config.yaml) + scheduler_core 构建门 (退役走既有降级路径, 与真故障分级日志); 保留 models 包, 改 true 可完整复活。下次服务重启生效。节省: NewsV2Loop 线程 + RSSHub/SEC 轮询 + dedup sqlite 读写 + 日志噪音。

**裁决 1 → lessons 保留生产线, 两周试用期**: 机制定位 = 每日复盘辩论的副产品 (边际成本≈0, 辩论反正要跑)。现役 2 条即"共性候选"样板 (高强度信号仍亏损→信号可靠性 / 6.2h 不触止损→止损过宽), 但均单样本孤证。**不拦截机会**: 当前 record-only 交易链零消费; 未来 A1 注入仅为 prompt 软建议 (A2 执行硬门已被 DEB 否决), 且前置 ≥10 条 + 质量审计 + 可回滚。试用期截止 ~08-14, 审计标准: 是否重复出现 (共性)、证据强度 (n≥2)、可操作 (指向具体旋钮)。审计不过 → 连生产线一起关。
**门槛重校准 (遗留项2) 并入试用期审计**: orchestrator 修复后若两周仍产出 ≈0, 即证明门槛脱节, 届时与"是否保留"一并裁。

### 第 5 轮 (2026-08-01 凌晨盘中, F1 收尾执行 — 用户裁"根据建议执行")

盘中现场检查发现 AMZN 指令 (DA-20260731180745) `anchor_dev_pct` 为 NULL — F1 DA 链双断点:
- **仓内半 (已修, record-only)**: BridgeVotingAdapter.vote 不透传 → Instruction dataclass 无字段 → _instruction_to_db_dict 无落库键。三处+构建点共 4 touch 已补齐; 冒烟 4/4 + DB 落库 2/2 + 定向 44 passed。下次重启生效。
- **skill 半 (遗留, 交另一模型)**: 今日 opportunity_scan 16 候选 0 只带 anchor_dev_pct — 补丁插入点疑似不在 opportunity_scan 产出路径 (或补丁在 18:07 扫描后才应用)。明日文件验证; 若仍缺, 补丁锚点需移到 opportunity_scan 候选发射路径。

**现场意外收获**: AMZN entry $236 vs 市价 $271 (-12.9%) 被极端 gap 门 ×11 正确拦截 — F1 幻影"入场区永不触达"在盘中被门拦下的首个活案例, 佐证 F1 量化后"合理性门"裁决的必要性。
