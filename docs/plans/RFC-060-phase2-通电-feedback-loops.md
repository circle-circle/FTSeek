# RFC-060 Phase 2: 通电（接通断裂的反馈闭环）

> **阶段目标**: 把 4 个断裂的学习闭环接回去，让系统「能自己变好」。
> **核心命题**: 本阶段每个 Item 都是「代码已写好，只是没接上」——所以**投入低、收益高**。
> **预计周期**: 2 周
> **总纲**: 见 `RFC-060-TRADING-OPTIMIZATION-PROGRAM.md`
> **深度**: 📋 提纲（待进入实施时按 phase1 模板细化）
> **硬前提**: Gate 1→2 通过（见总纲 §4）

---

## 阶段总览

| ID | Item | 闭环类型 | 关键接入点 | 投入 | 状态 |
|----|------|---------|-----------|------|------|
| P2.1 | PerformanceTracker → 投票权重 | 模型权重学习 | `app/agents/voting/voting_aggregator.py` | 低 | ⬜ |
| P2.2 | 教训 → 盘前作战计划 prompt | 复盘→次日 | `app/plan/pipeline.py` + `LessonInjector` | 低 | ⬜ |
| P2.3 | IC 监控从「报告」改「执行」 | 信号→策略禁用 | `signal_ic_tracker` + 策略注册表 | 中 | ⬜ |
| P2.4 | 辩论引擎：接 or 删 | 分歧观点 | `app/agents/debate/debate_engine.py` | 低 | ⬜ |
| P2.5 | 进化自动转正写 YAML | 影子→实盘 | `app/evolution/shadow_tracker.py:426` | 中 | ⬜ |

> **建议顺序**: P2.4（先清理死代码，决策快）→ P2.1（杠杆最高、最易）→ P2.2 → P2.3（依赖 P1.5）→ P2.5

---

## P2.1 投票准确率闭环通电 ✅ 已实施 (2026-06-17)

> **⚠️ 前提修正（实施时发现）**：原提纲假设要"接 `VotingPerformanceTracker`"。深查后发现**该 tracker 已被更强的 `VotingAccuracyTracker` 取代**，且闭环已建好 90%——真正缺的只是"准确率喂回权重"这一环。

### 真实现状（实施时核查确认）
投票准确率闭环**已通过 `VotingAccuracyTracker` 建好 90%**：
| 环节 | 位置 | 状态 |
|------|------|------|
| 记录预测 | `voting_coordinator.py:891` `record_prediction(...)`（含每个模型方向明细）| ✅ 已闭环 |
| 回填实际结果 | `order_executor.py:4337` `update_outcome(...)`（平仓时按实际方向对错）| ✅ 已闭环 |
| per-model 准确率 | `VotingAccuracyTracker.get_model_benchmark(model_id)` | ✅ 已产出 |
| **准确率→权重** | `voting_aggregator.aggregate()` | ❌ **此前断裂**（权重永远固定 70/30）|

对比：`VotingPerformanceTracker`（内存、零调用、di_container:254 已注明 removed）是**早期冗余实现**。

### 实际落地方案（已实施）
1. **`voting_aggregator.aggregate()`**：VIX 动态权重块之后，插入"准确率→权重系数"块——读 `get_model_benchmark(model_id, 30)`，按方向准确率映射系数（≥0.6→×1.2 / >0.5→×1.0 / >0.4→×0.9 / ≤0.4→×0.8），乘入 `weights[role]`。
2. **冷启动安全**：`total_votes < 20` 或 `accuracy is None` → 不调整（保持默认 70/30）。防小样本噪音。
3. **无需显式归一化**：weights 用于加权平均（sum/total），乘系数即改变相对比例。
4. **标记 `VotingPerformanceTracker` 废弃**：指向 VotingAccuracyTracker，待 P3 清理时删除。

### 验证结果（已通过）
- ✅ py_compile 通过
- ✅ 端到端 `aggregate([qwen60, minimax85])` 成功（LONG, strength=61），闭环块无异常
- ✅ 冷启动安全：当前 qwen/minimax `total_votes=0` → 不调整
- ✅ 真实数据可跑：`get_model_benchmark("qwen"/"minimax", 30)` 返回 accuracy/total_votes/by_direction

### 触发条件（通电生效后）
当某模型累积 ≥20 个已回填预测且方向准确率显著偏离 0.5 时，权重会自动调整。**目标验证（Gate 2→3）**：部署后 7 天内至少发生 1 次权重自适应变化。

### 回滚
`git revert`（voting_aggregator.py 改动）；权重恢复固定 70/30。

### 风险
- ⚠️ 当前信号 IC 为负（噪音），模型准确率数据少且可能不准 → 冷启动 20 样本门槛 + P1.5 IC 门禁先过滤噪音信号，降低过拟合风险
- ⚠️ 权重系数（1.2/0.8）较温和，避免某模型被永久踢出

---

## P2.2 教训 → 盘前作战计划 🔴

### 目标
打通「盘后复盘 → 提炼教训 → 注入次日作战计划 prompt」的闭环。让昨天的亏钱经验真的影响今天的决策。

### 关键接入点
- **逻辑已实现但零调用**：`app/review/lesson_injector.py:95` `generate_battle_plan_injection()` ✅ 已接通
- **闭环已通一半**：复盘→教训入库（`ExperienceAccumulator.accumulate()`）✅；教训→**风控约束**（`pre_trade_check.py:433` / `signal_pipeline.py:227` / `min_confidence_resolver.py:74`）✅；教训→**盘前计划 prompt** ✅ 已接通

### 实际落地（已实施）
在 `app/llm/agent_bridge.py::generate_battle_plan()` 接通（仿 `session_exit_quality` 注入模式，P2 G3）：
1. **构建**：`lessons_context = LessonInjector().generate_battle_plan_injection()`（无活跃教训时安全返回"今日正常执行"默认文案）
2. **进 context dict**：`"lessons": lessons_context`（供 LLM worker 也可读）
3. **渲染进 prompt**：在【历史记忆与教训】后新增【历史教训(复盘提炼)】section，`{lessons_context}` 直接渲染——保证 LLM 真的看到

### 验证结果（已通过）
- ✅ py_compile 通过；`generate_battle_plan_injection` 现已有消费者（agent_bridge:381）
- ✅ 三处接入就位（构建/context/prompt 渲染）；无活跃教训时安全降级（默认文案）

### 预期收益
- 复盘真正闭环：昨日亏钱教训进入今日作战计划 prompt，影响次日策略

### 回滚
`git revert`（agent_bridge.py 改动）；恢复开环

### 待续
- 教训注入长度管控（条数/字数上限）——若 lessons 过多污染 prompt，后续加 cap（当前无活跃教训时仅默认文案，暂不急）

---

## P2.3 IC 监控从「报告」改「执行」🔴🔴

### 目标
IC 低/负时**自动禁用对应策略/方向**（7 天），而非只输出报告。这是 PM 审计 Task 3 的核心诉求，也是 Gate 4「IC>0」能自动维持的关键。

### 关键接入点
- **报告已有**：`app/analysis/signal_ic_tracker.py` + `get_ic_summary()`
- **门禁已有（P1.5 强化后）**：`order_executor.py:3517`
- **缺失的一环**：IC 监控 → **策略注册表禁用**。需要：
  - 新增 `ICGatekeeper`：每日盘后计算各 `(strategy_id, direction)` 的 7 日滚动 IC
  - IC<-0.05 且 n>30 → 自动调 `strategy_registry.disable(strategy_id, days=7)`
  - 触发钉钉告警 + 写 audit log
- **复用资产**：`StrategyCircuitBreaker`（`app/risk/circuit_breaker.py:36`）已有「连续亏损→禁用」逻辑，可扩展为「IC→禁用」

### 预期收益
- 系统对策略失效的**自动响应**（当前完全靠人）
- 是 P4.1（因子权重学习）的统计基础

### 依赖
- **P1.5**（IC 门禁多维度拆分 + p 值）——本 Item 在其上构建「禁用」动作

### 待细化项
- [ ] 自动禁用的回滚机制（误禁用如何恢复？人工 override）
- [ ] 禁用粒度（策略级 vs 策略×方向 vs 策略×方向×regime）
- [ ] 与现有 `StrategyCircuitBreaker` 的职责边界（避免重复禁用）

---

## P2.4 辩论引擎：删 ✅ 已实施 (2026-06-17)

### 决策与现状
**决定：删**（用户拍板）。`DebateEngine` 在 `voting_coordinator.py` 创建后**从未被调用**——debate_engine.py:479 自述"辩论降级/结论持久化/中性降级全部不可达"。且 Bear=DeepSeek 与 Bull=Qwen 同源（中国大模型生态），接通也只是假分歧。

### 实际落地（已实施）
- 删除 `voting_coordinator.py:357-363` 的 `DebateEngine` 实例化（死代码对象）
- 删除 2 个未用 import：`DebateEngine` / `DebateSummary`（line 13-14）
- **保留**：
  - `enable_debate` 参数（默认 True）——兼容调用方，不破坏签名
  - `auto_debate_triggered` 标志——`voting_aggregator` 仍计算分歧度，且被 `decision_recorder.py:214` 持久化到决策 DB（诊断用，非未用）

### 验证结果（已通过）
- ✅ py_compile 通过；VotingCoordinator 可 import，`enable_debate` 参数保留
- ✅ `self.debate_engine` 赋值已清除（死代码移除）
- ✅ `auto_debate_triggered` 仍保留（有下游消费）
- ✅ debate 模块（debate_engine.py / debate_models.py）保留，供 `catalyst/detector.py`、`review/review_orchestrator.py` 等其他路径使用，不受影响

### 回滚
`git revert`（voting_coordinator.py 改动）

### 未来重引入条件
当能通过 hermes 接入真正差异化模型（Claude/GPT）作 Bear 时，可重新引入——届时在 `auto_debate_triggered=True` 处调 `run_debate()`，产生真分歧审计（与 P4.4 Thesis Challenge 互补）。

---

## P2.5 进化自动转正写 YAML 🟡🟡

### 目标
让进化系统（`ImprovementPipeline` + `ShadowTracker`）的「自动转正」**真正修改配置并生效**，而非只改 DB 状态（当前注释写「模拟」）。

### 关键接入点
- **断点**：`app/evolution/shadow_tracker.py:426-443` `_execute_promotion()` 只 `_update_proposal_status`，不写 YAML
- **需新增**：
  - `ConfigWriter.write_param(target_param, new_value)`：原子更新 `unified_config.yaml` 指定键
  - `_execute_promotion` 调 ConfigWriter 后，触发 `ConfigHotReloader` 的 `_reload()`（复用现有热重载回调）
  - 回滚影子（`_create_rollback_shadow`）走同一机制自动回滚
- **复用资产**：`PromotionEngine`（统计纪律严格，MIN_SAMPLES=20 / Bonferroni 校正）已是正确的「是否转正」判定器，无需改

### 预期收益
- 进化系统从「架构完整但闭环断裂」→「真正落地驱动迭代」
- 参数优化的自动化最后一公里

### 依赖
- **P3.3**（ConfigContainer 读写锁 + 原子 reload）——写 YAML 必须有原子热重载，否则半初始化状态危险
- 因此 P2.5 实际可能需与 P3.3 并行或在其后

### 待细化项
- [ ] ConfigWriter 的原子写策略（临时文件 + rename）
- [ ] promotion audit log（谁/何时/基于什么数据/改了什么）
- [ ] 回滚影子的自动触发条件

---

## 阶段 2 实施状态表（迭代用）

| ID | Owner | PR/Commit | 部署日期 | 验收结果 | 状态 |
|----|-------|-----------|---------|---------|------|
| P2.1 | | | | | ⬜ |
| P2.2 | | | | | ⬜ |
| P2.3 | | | | | ⬜ |
| P2.4 | | | | | ⬜ |
| P2.5 | | | | | ⬜ |

---

## 阶段 2 完成定义（Definition of Done）
- [ ] P2.1~P2.5 全部 `✅`
- [ ] **投票权重在过去 7 天至少发生过 1 次自适应变化**（证明闭环通电）
- [ ] 至少 1 个策略被 IC 监控自动禁用，或 1 条教训进入次日计划（证明反馈真的回流）
- [ ] 进化系统如有待转正 proposal，验证其 YAML 写入 + 热重载生效
