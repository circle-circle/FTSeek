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

## P2.1 PerformanceTracker → 投票权重 🔴🔴 最高杠杆

### 目标
让多模型投票权重（当前硬编码 Qwen 70% / MiniMax 30%）**随真实胜率自适应调整**。这是「让系统能学习」的第一步。

### 关键接入点
- **逻辑已实现但零消费**：`app/agents/voting/performance_tracker.py:108` `get_model_weights_adjustment()`
- **接入点 A（读）**：`app/agents/voting/voting_aggregator.py:28` 附近，`weights = self.config.model_weights.copy()` 之后调用
  ```python
  from .performance_tracker import tracker
  for role, adj in tracker.get_model_weights_adjustment().items():
      if role in weights:
          weights[role] *= adj  # 胜率>0.6 加20%, <0.4 降20%
  ```
- **接入点 B（写）**：`VotingCoordinator._run_full_voting()` 结尾调 `tracker.record_vote_result()`；交易退出时（`order_executor.py` 退出分支）调 `tracker.update_outcome()`

### 预期收益
- 模型权重从「静态配置」→「动态自适应」，长期胜率提升的杠杆点
- 暴露 MiniMax/Qwen 的真实表现差异（当前被硬编码 0.75 偏差校正掩盖）

### 依赖
- 无强依赖；建议 P1.1 模拟盘修复后做（否则 update_outcome 学到的是假数据）

### 待细化项（进入实施时补）
- [ ] 冷启动期（样本 <50）权重不调整，避免过拟合
- [ ] 权重调整的边界（防止某模型权重→0 永久踢出）
- [ ] update_outcome 的"胜"判定标准（T+1 收益 vs 退出收益）

---

## P2.2 教训 → 盘前作战计划 🔴

### 目标
打通「盘后复盘 → 提炼教训 → 注入次日作战计划 prompt」的闭环。让昨天的亏钱经验真的影响今天的决策。

### 关键接入点
- **逻辑已实现但零调用**：`app/review/lesson_injector.py:83` `generate_battle_plan_injection()`
- **闭环已通一半**：复盘→教训入库（`ExperienceAccumulator.accumulate()`）✅；教训→**风控约束**（`pre_trade_check.py:428`）✅；但教训→**盘前计划 prompt** ❌
- **接入点**：`app/plan/pipeline.py`（或 `battle_plan_manager`）生成作战计划的 LLM prompt 处，拼入：
  ```python
  from app.review.lesson_injector import LessonInjector
  lesson_text = LessonInjector().generate_battle_plan_injection()
  prompt = base_prompt + "\n\n## 历史教训\n" + lesson_text
  ```

### 预期收益
- 复盘真正闭环（当前是开环：报告写完就归档）
- 例如教训「NVDA 财报前 1 天易 run-up」「midday 时段胜率低」能进入次日策略

### 依赖
- 无；建议 P1.4 get_plan 修复后做（作战计划链路先正确）

### 待细化项
- [ ] 教训注入是否会污染 prompt（过长？需限制条数/字数）
- [ ] 注入的教训是否需要按 regime/标的筛选（避免无关教训）

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

## P2.4 辩论引擎：接 or 删 🟡

### 目标
处理纯死代码决策。当前 `DebateEngine` 在 `voting_coordinator` 创建后**从未被调用**（已确认）。

### 关键接入点
- **死代码位置**：`app/agents/voting/voting_coordinator.py:355-363`
- **决策 A（删）**：去掉初始化，减复杂度。若团队认为当前投票足够，直接删
- **决策 B（接）**：在 `voting_aggregator.py:218` 的 `auto_debate_triggered=True` 处调用 `debate_engine.run_debate()`
  - ⚠️ **前提**：bear_model 必须换成真正差异化的模型（当前 Bull=Qwen / Bear=DeepSeek，同属中国大模型生态，训练数据高度重叠，辩论会「假分歧」）
  - 可用 `hermes_client` 接 Claude/GPT 作为 Bear，产生真正的对立视角

### 预期收益
- 删：消除维护负担，避免「以为有辩论其实没有」的错觉
- 接：高置信度分歧信号时增加一道独立审计（与 P4.4 Thesis Challenge 互补）

### 依赖
- 无

### 待细化项
- [ ] **决策点（需拍板）**：接还是删？建议：除非能接入差异化模型，否则先删
- [ ] 若接：触发条件（仅在 `auto_debate_triggered`）、结论如何影响最终决策

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
