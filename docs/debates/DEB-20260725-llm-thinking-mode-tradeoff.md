# DEB-20260725: LLM 推理模式 (enable_thinking) 全局关闭 — 质量/时延/成本均衡辩论

> **✅ 用户裁决 2026-08-01: 终裁后再议** (采纳辩论方立场) — 08-29 终裁前不开 thinking 灰度, 保护 8 月样本单一脑子可归因; D 类前置可先做。详见 handoff 变更日志 2026-08-01 首节。
- **状态**: 🔶 待辩论 (作者完成事实链与选项, 等待独立模型辩论后用户裁决)
- **作者**: Qoder (主会话)
- **日期**: 2026-07-25
- **关联**: DEB-20260725-llm-cache-cross-symbol-pollution (§四 审计盲区同源); handoff §2 挑战者 A/B 第2轮 (thinking 状态是 A/B 有效性的混淆变量)
- **宪章合规**: 一事一 DEB; 数据口径七律 (全部数字标注来源与窗口); 变更纪律 (若裁决开启, 属"修尺子"+一次一变量灰度)

---

## 一、议题

用户质询: 目前仓内所有 LLM 调用均未开启 `enable_thinking` (DashScope 推理模式),
输出指令与质量可能无法深入、结论可能不够充分。**耗时与质量如何均衡?**

本 DEB 不预设结论。目标是让辩论方在完整事实链上回答:
开 (哪些特征/多大代价) vs 不开 (质量损失是否可证) vs 怎么验证 (灰度判据)。

---

## 二、证据链 (全部含锚点, 可复核)

### E1. 全局关闭机制 — 仓内无一例外
`app/llm/model_call.py:143-144`:
```python
if "dashscope.aliyuncs.com" in base_url:
    payload.setdefault("enable_thinking", False)
```
模型注册表 (config/unified_config.yaml:622-664): qwen3.7-max / deepseek-v4-pro /
glm-5.2 / kimi-k2.6 全部走 DashScope → **全部被强制 thinking OFF**;
deepseek-flash 走官方 API (api.deepseek.com), 该参数被忽略。
唯一显式用例 `app/agents/debate/debate_engine.py:204` 传 `extra_body={"enable_thinking": False}`
— 与 setdefault 语义重复, 属冗余而非例外。

### E2. 关闭的原始依据 (2026-07-06 P0, model_call.py:135-142 注释实测)
- 故障: DashScope 推理模型 (glm-5.2/deepseek-v4-pro/qwen3.7-max) **默认 thinking=ON**,
  内部 reasoning 与可见输出**共享 max_tokens**。实测 glm-5.2 max_tokens=800 时
  800 tokens 全被 reasoning 吃掉 → 0 可见输出 = `empty_response`。
- 规模: 该故障是 **78/210 次失败 (37%)** 的根因 (投票/评分场景)。
- 关闭收益 (实测): 速度 10x (39s → 4s/调用); reasoning tokens 1297 → 0。
- 原始场景声明: "投票/评分场景不需要 chain-of-thought" — 注意该结论
  **只对 voting 场景有实测支持**, 未覆盖深分析特征。

### E3. 覆盖通道存在但未接线
- `extra_body` 在 `payload.update(extra_body)` (model_call.py:131-133) **先于**
  setdefault 合并 → 调用方传 `extra_body={"enable_thinking": True}` 可干净覆盖
  (setdefault 对已存在键 no-op)。unified_service.py:77/111/138/165 全程透传。
- 但 yaml 路由层 (unified_config.yaml:515-615) **无任何 extra_body/enable_thinking 键**
  → 今天没有 per-feature 配置能力。开启需 ~3 行: `call_llm` 合并
  `route.get("extra_body")` 进 extra_body 参数。
- 死配置证据: unified_config.yaml `llm.strategy_engine.thinking_level: high` 存在,
  但 `app/strategy/llm_strategy_engine.py:165` 只读 `enabled` — **该键从未被消费**,
  说明此前有人想过开 thinking 但没接线。

### E4. 外部 Hermes 盘前 DA 链路 — thinking 状态不对称 (治理盲区)
- 现行盘前 DA 主链 = 外部 Hermes 技能 (scripts/hermes_worker.py → ~/.hermes/config.yaml):
  provider=custom, base_url=DashScope, default_model=glm-5.2 / qwen3.7-max,
  配置中**无 enable_thinking 键** → 客户端不发送该参数 → **服务端默认生效**。
- 按 E2 注释记录, 服务端默认 = ON → **盘前 DA 主链路疑似一直 thinking ON 运行**,
  与仓内全局 OFF 并存, 无文档、无审计、无人声明过此不对称。
- 待实测确认 (Q2): 抓一次外部链路实际请求/响应 (或查 DashScope 控制台 usage
  的 reasoning tokens) 即可定论。此为"修尺子"前置, 与 A/B/C 裁决无关都应做。

### E5. 现行 thinking-OFF 基线 (llm_audit.db, 7日窗口 07-19~07-25, n=39)
| feature | model | n | 失败 | 均时延 | 最大时延 | 均tin | 均tout | 成本 |
|---|---|---|---|---|---|---|---|---|
| agent_bridge | deepseek-v4-pro | 28 | 0 | 49.3s | 78.8s | 4514 | 2206 | $0.747 |
| llm_strategy_engine | qwen3.7-max | 6 | 0 | 27.0s | 35.6s | 1140 | 1250 | $0.235 |
| universe_expansion_reasoning | qwen3.7-max | 3 | 0 | 14.8s | 16.3s | 1711 | 1020 | $0.115 |
| news_classifier | qwen3.6-flash | 1 | 0 | 0.6s | — | 24 | 5 | ≈0 |

- 近 24h 总成本 **$0.988** (34 calls); 7日 $1.097。预算 `llm_budget.daily_max_usd: 5.0`
  (子预算 premarket $1.0/day) → 峰值日余量 ~4x。
- **审计盲区**: 6 个 cache_on_fail 特征 (dr_debate / deep_target_analysis /
  premarket_planner / playbook_generator / daily_review / web_search_fundamental)
  7日内**零审计行** — 恰恰是推理最重的一批特征没有时延/质量基线
  (与 cache DEB §四 同源, 先行修复是任何 A/B 判据的前提)。

### E6. max_tokens 现状与 empty_response 回归风险
路由配置 (unified_config.yaml): deep_target_analysis=3000, dr_debate=2500,
playbook_generator=2048, web_search_fundamental=1500, dr_risk_debate=800,
其余默认 (call_llm 默认 max_tokens, 多数 800-1500 区间)。
开启 thinking 而**不同步上调 max_tokens = 直接复现 E2 故障** (reasoning 吃光预算)。
经验法则: reasoning 消耗 ≈ 可见输出的 0.5-3x (深分析场景可能更高), 首批灰度
建议 max_tokens ×2~2.5 起步并监控 empty_response 率。

### E7. 成本数学 (reasoning tokens 按 output 计费)
费率 (per 1k tokens): qwen3.7-max out $0.024 / deepseek-v4-pro out $0.008 / glm-5.2 out $0.02。
以 agent_bridge 类深调用为例 (现 tout≈2206): thinking ON 假设 reasoning +2k~8k tokens
→ deepseek-v4-pro 每次 +$0.016~0.064; qwen3.7-max 每次 +$0.048~0.19。
按日 5-10 次深调用估: **+$0.1~1.9/day**, 仍在 $5/day 预算内但可能顶穿
premarket $1/day 子预算 (若外部 DA 链计入)。**结论: 成本非硬约束, 时延窗口才是。**

### E8. 时延窗口约束
- 盘前批: 所有 DA/计划产出须在 09:30 ET 开盘前就绪 (07-24 已因配额延迟到 10:25 成交,
  链路时延是现存痛点)。E2 实测 thinking ON 单调用 39s (voting 场景, glm-5.2);
  深分析特征 thinking ON 的单调用时延**无实测** — 若 agent_bridge 从 49s 涨到
  2~5min 且批次串行, 盘前窗口可能不可行 (并行度是关键变量, hermes_worker 用
  ThreadPoolExecutor, 仓内 agent_bridge 调用并发度待查)。
- 盘中: pre_entry_gate / thesis_challenge 在交易时段运行, 时延敏感;
  10:30-11:30 topup 窗口为首日验证期, 不宜叠加变量。

### E9. 特征分类 (推理深度需求 vs 时延敏感)
- **推理候选** (输出是指令/论证链, 质量假设值得验证): deep_target_analysis
  (三层因果链), dr_debate (裁决), premarket_planner (计划合成),
  playbook_generator, llm_strategy_engine (二阶推理), thesis_challenge,
  debate_bull/bear/arbiter。
- **速度优先** (分类/打分/盘中门禁, 不建议开): news_classifier, sentiment,
  emergency_interceptor, voting_* (E2 原始场景, 已实测不需要),
  pre_entry_gate (盘中时延敏感)。
- 注意 debate_engine.py:204 已**显式选择 OFF** — 该模块作者有过明确判断,
  灰度时不应默默翻案, 需单独确认意图。

---

## 三、选项

### 选项 A: 维持全局 OFF (现状冻结)
零风险、零工作量。**代价**: "质量不够深" 的假设永远无法证实或证伪 —
8月终裁框架下 DA/辩论质量是核心不确定项, 不验证就是把假设带进终裁。

### 选项 B: 路由级灰度开启 (推荐候选)
1. 接线: `call_llm` 合并 `route.get("extra_body")` (~3行) + 审计记录 thinking 状态;
2. 首批**单特征** (宪章一次一变量): 建议 dr_debate 或 deep_target_analysis
   (推理最重 + 当前审计盲区, 先补审计再开);
3. 配套: max_tokens ×2~2.5 + timeout 上调 + empty_response 率监控;
4. 判据: 预注册 Q1 质量指标, 2 周 A/B 后裁决推广或回滚 (回滚=删 yaml key)。
**风险**: 单调用时延未知 (需先一次性基准测试 n≥5); 挑战者 A/B 第2轮
(handoff §2) 结论有效性会被 thinking 变更重置 — 两项实验须串行不得并行。

### 选项 C: 全局回滚 thinking ON
**拒绝**。E2 根因 (empty_response 37% 失败率) 直接回归; voting 场景已实测
不需要 CoT; 违反一次一变量。

### 选项 D: 外部 Hermes 链路对齐治理 (无论 A/B 必做)
实测外部 DA 链 thinking 真实状态 (E4) + 纳入统一文档与审计 +
声明仓内/仓外两域的 thinking 策略。属"修尺子", 不构成策略变更。

---

## 四、预注册判据 (辩论方须逐条作答)

- **Q1 质量判据**: "输出更深入" 用什么**可观测指标**裁决? 候选: JSON schema
  一次合格率 / empty_response 率 / conf 校准误差 / DA 评级与 T+5 收益的区分度 /
  挑战者否决率与事后对错的一致性。不接受 "感觉更深" 类主观判据 —
  辩论方若提新指标, 须说明数据源与最小样本 (宪章 n≥30)。
- **Q2 时延红线与基准**: 盘前批的完成时刻硬约束是多少 (相对 09:30 ET)?
  是否必须先跑一次性 thinking-ON 基准测试 (每候选特征 n≥5, 记录时延/
  reasoning tokens/empty 率) 再进入灰度决策? 外部 Hermes 链 thinking 状态
  由谁、何时实测 (E4)?
- **Q3 成本与 max_tokens**: 每个候选特征 max_tokens 提到多少 (现值见 E6)?
  reasoning 计费下的日成本上限接受度 — 维持 $5/day 还是同步调子预算
  (premarket $1/day 可能顶穿, 见 E7)?
- **Q4 灰度范围与回滚**: 首批单特征 (哪个?) 还是成组? 与挑战者 A/B 第2轮
  的串行顺序如何排 (两项实验共享 DA 链, 并行=互相污染)? 回滚动作
  (删 yaml key) 是否满足一键, 是否需要 feature flag 化?
- **Q5 失败语义前置** (§五追加): thinking ON 拉长生成 → 超时/截断概率上升。
  是否应先把 ①`finish_reason` 审计落库 (截断可观测) ②重试按错误类型分流
  (截断 → 降 max_tokens 重发, 对齐 agent_bridge 自救模式) 作为灰度前置?
  还是接受现状 (截断=schema 失败走 fallback, 已有兜底) 直接灰度?

---

## 五、失败语义摘要 (详见专门 DEB)

2026-07-25 用户追加质询"超时/失败/部分失败有无重试或部分接受", 已按一事一 DEB
独立成档: **`DEB-20260725-llm-failure-semantics.md`** (🔶待辩论)。

与本议题的耦合结论 (保留): thinking ON → 生成变长 → 超时/截断概率上升, 而现状
①`finish_reason` 不检查 (截断不可观测) ②重试不分错误类型 (截断原样重发空转)
③截断自救仅 agent_bridge 一处未推广。失败语义 DEB 的修尺子三件套
(finish_reason 落审计 + 重试分流 + hermes 空 verdict 质量门) 是本 DEB
选项 B 灰度的前置候选 (见 Q5)。

## 六、作者倾向 (供辩论方攻击, 不构成结论)

B + D 组合: D 是无条件前置 (修尺子), B 单特征灰度是验证"质量假设"的最低成本路径;
但 B 启动前必须满足三前置 — ①cache DEB 审计盲区修复 (否则无判据数据源)
②thinking-ON 基准测试跑完 (否则时延不可证伪) ③与挑战者 A/B 串行排期。
若辩论方能证明 Q1 无可行质量指标, 则退选项 A (不可证伪的优化不做)。

---

## 七、辩论方作答 (Qoder-主工程会话, 2026-07-25, 锁点独立复核)

**事实链复核**: E1 ✓ (L143-144 setdefault 实在); E3 ✓ (L131-133 `payload.update(extra_body)` 先于 setdefault → 覆盖通道有效; yaml:686 `thinking_level: high` 存在且 llm_strategy_engine 确无消费 = 死配置实锤); E4 ✓ (`~/.hermes/config.yaml` grep thinking 键 = **0** — 客户端确实不传参, "服务端默认生效"推论成立, 但 ON/OFF 仍待实测)。

- **Q1 (质量判据)**: 可行指标存在, 但要防"样本不够装有效"。推荐主判据 = **DA 方向命中率 (fwd_return 前向台账, direction_hit_scorecard 现成)** — 它直接衡量"分析更深→方向更准"的商业假设; 副判据 = JSON 一次合格率 + empty_response 率 (守底线)。但诚实提醒: 命中率差异要 n≥100/臂才有区分度 (52% vs 56% 在 n=30 下不可判) — 单特征 2 周只有 ~10-20 样本, **真实判据周期是 4-6 周, 与 08-29 终裁撞期**。这是 B 的最大隐藏成本: 终裁前引入新变量会污染终裁数据可比性。
- **Q2 (时延红线)**: 盘前硬约束 = 指令在 auto_execute 首轮前就绪 (现状 bridge 19:31 CST 写库, 余量充足); 基准测试 (n≥5/特征) **必须先行** — 同意作者。E4 外部链实测方法: hermes_worker 加一次性诊断打印 usage.reasoning_tokens (或查 DashScope 控制台), 周末可做, 属 D。
- **Q3 (成本)**: 同意"成本非硬约束"。若 B 启动, premarket 子预算 $1→$2/day 预批免得灰度期误报。
- **Q4 (灰度范围)**: 若做, 首选 **dr_debate** (推理最重+当前零审计=增量可对照) 非 deep_target_analysis (dashboard 人在环, 质量判据主观)。与挑战者 A/B 串行: A/B 第2轮先结 (两日合并裁决已在 handoff §2), thinking 基准测试后排。回滚一键性: 删 yaml key 满足。
- **Q5 (失败语义前置)**: **硬前置** — 见 failure DEB 辩论节 Q1, 不重复。
- **攻击点 (核心)**: 选项 B 的真实价值窗口存疑 — Q1 判据需 4-6 周样本, 而 08-29 终裁冻结新机制且禁临近改参; 灰度若 8 月中启动则样本不够即被终裁打断, 若终裁后启动则今天裁决无紧迫性。**辩论方建议: D 立即做 (修尺子无条件); B 的裁决延至 08-29 终裁后与新阶段规划合并** — 避免在终裁冲刺期引入不可完成的实验。若用户仍要提前, 则严格按作者三前置串行。
- **结论**: D 支持立即; B 建议推迟至终裁后 (与作者分歧点, 交用户裁); C 同拒; A 作为 B 推迟期间的默认态。

(待用户裁决)

**2026-07-25 用户裁决 (部分) + D 实测完成**:
- 用户定调: 信号产生/筛选优化属"修尺子", 不涉宪法原则; 担忧 = 全标的开 thinking 的耗时。
- **D 实测实锤 (探针 A/B 对照, glm-5.2 同 hermes 配置)**: 不传参 → **reasoning_tokens=117, 2.8s**; 显式 False → reasoning=0, 0.8s。**服务端默认 thinking=ON 实锤 → E4 推论成立: 外部 Hermes 盘前 DA 链 (48 标的深分析) 一直在 thinking ON 下运行**, 且系统正常 (18:05→19:31 完成, 离开盘余量 2h)。用户担忧的"全标的耗时"已是现状且可承受 — 两域不对称的真相是"仓内该开的没开"而非"外部开多了"。
- **基建接线完成** (E3 缺口): `_resolve_feature_route` 透传 + `call_llm` 合并 `route.extra_body` (调用方显式传入优先); yaml 无配置时行为不变 (回归冒烟 ✓)。至此 per-feature 灰度 = yaml 2 行。
- 分层方案 (待用户确认首灰特征) 见 handoff 当日节。死配置 `strategy_engine.thinking_level` 待减法批清理。

---

*本 DEB 仅含事实与选项, 不含任何代码变更。裁决前不动 model_call.py / yaml 路由。*
