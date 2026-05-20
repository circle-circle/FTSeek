# FTSeek-US LLM 角色重定位设计文档

> 2026-05-13 | 基于 llm.md 审计 + OPTIMIZATION_REPORT 交叉验证
> 核心原则: LLM 做归因+假设生成，规则/回测做决策

---

## 一、当前架构（问题）

```
盘前 LLM → BattlePlan → 交易执行 → 规则提取 lessons → LLM 读 lessons 出新 BattlePlan
                              ↓
                        review_loop → 规则调参（无回测验证）
                              ↓
                        直接改 yaml（无对照组、无 canary）
```

**问题**:
1. LLM 直接做决策（BattlePlan），无法证伪
2. "学习"是硬编码规则提取，LLM 零参与
3. 参数改动无回测验证、无 A/B 对照
4. EOD 归因有 NameError bug，从未跑通
5. enhanced_debate / empirical_verifier 等 5 个子系统全是死代码

---

## 二、新架构（目标）

```
┌──────────────────────────────────────────────────────────────────┐
│                        盘前 (PRE-MARKET)                         │
│                                                                  │
│  策略池（回测验证过的规则）→ BattlePlan（确定性生成，LLM 不参与）    │
│  LLM 角色: 仅注入 lessons/memory 作为风险提示（已实现）             │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                         盘中 (TRADING)                           │
│                                                                  │
│  IntradayScorer(6因子) → VotingCoordinator → RiskGateway → 下单    │
│  LLM 角色: 无（纯规则驱动）                                       │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                        盘后 (POST-MARKET)                         │
│                                                                  │
│  每笔已平仓交易 → LLM 归因（"为什么赚/亏"）                         │
│    ↓                                                             │
│  trade_attribution.llm_insight（结构化: 关键因素/建议/系统性问题）  │
│    ↓                                                             │
│  周末 LLM 周度假设生成（看完所有 attribution + lessons）            │
│    ↓                                                             │
│  ImprovementProposal 列表（1-3 个参数调整建议）                    │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                        验证 (VALIDATION)                          │
│                                                                  │
│  ImprovementPipeline.submit() → proposals.db                     │
│    ↓                                                             │
│  ImprovementPipeline.validate_with_backtest() [依赖 F29]          │
│    ↓                                                             │
│  通过 → canary 部署（5% 资金，5-10 交易日）                        │
│    ↓                                                             │
│  表现优于 baseline → promote_to_full()                           │
│  否则 → reject + 归档原因                                         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 三、异构模型架构

### 3.1 模型选择（基于阿里百炼 API，无需海外直连）

| 模型 | 用途 | 百炼 model_id | 优势 |
|------|------|---------------|------|
| Qwen-Max | 逐笔归因 | `qwen-max` | 综合策略分析、宏观事件理解 |
| DeepSeek-V4-Pro | 周度假设生成 | `deepseek-v4-pro` | 逻辑推理强、假设质量好 |
| MiniMax-M2.5 | 情绪分析（备用） | `MiniMax-M2.5` | 新闻情绪、市场热度 |
| GLM-4.7 | 技术分析（备用） | `glm-4.7` | 技术指标解读 |

### 3.2 调用链路

```
盘后逐笔归因 → Qwen-Max (temperature=0.1, 500 tokens)
    ↓
trade_attribution.llm_insight (SQLite)
    ↓
周五汇总 → DeepSeek-V4-Pro (temperature=0.2, 1000 tokens)
    ↓
ImprovementProposal[] → proposals.db
    ↓
人工审批 → ConfigWriter 部署
```

### 3.3 预算控制

| 参数 | 值 | 说明 |
|------|------|------|
| `llm_budget.daily_max_usd` | 5.0 USD | 每日总上限 |
| `attribution` | 2.0 USD | 逐笔归因上限 |
| `weekly_proposal` | 2.0 USD | 周度假设上限 |
| `premarket` | 1.0 USD | 盘前上限 |
| `warn_pct` | 70% | 告警阈值 |
| `halt_pct` | 100% | 触顶停用 |

### 3.4 成本估算（按日均 10 笔交易）

| 模块 | 调用次数 | 平均 token | 单次成本 | 日成本 |
|------|---------|-----------|---------|--------|
| 逐笔归因 (Qwen) | 10 | 500 output | $0.012 | $0.12 |
| 周度假设 (DeepSeek) | 1/周 | 1000 output | $0.020 | $0.003 |
| 合计 | — | — | — | **$0.12/天** |

远低于 $5.0 日预算。

---

## 四、LLM 在新架构中的角色

### 4.1 核心职责: 归因 + 假设生成

| 职责 | 输入 | 输出 | 频率 |
|------|------|------|------|
| 逐笔归因 | 交易数据 + 市场上下文 | llm_insight JSON | 每笔平仓后 |
| 周度假设 | 本周所有 attribution + lessons | ImprovementProposal[] | 每周末 |
| 风险提示 | lessons + memory | prompt injection | 每日盘前 |

### 4.2 不做: 直接决策

- LLM **不生成** BattlePlan（由回测验证过的策略池生成）
- LLM **不直接修改** 配置（由 ImprovementPipeline 管控）
- LLM **不执行交易**（纯规则驱动）

### 4.3 辅助: 非结构化信息解读

- 新闻情绪 → 注入归因过程
- SEC 文件 → 注入假设生成
- Twitter 情绪 → 辅助归因

---

## 五、代码改动清单

### P0 — 立即修（Bug 修复）

| 改动 | 文件 | 说明 |
|------|------|------|
| B1 | agent_bridge.py:583 | 定义 `_et_date` |
| B3 | review_loop.py:832 | 修复 endswith bug |
| B6 | decision_recorder.py | 删除 weight_adjustments 死代码 |
| B4 | 多处 | 路径统一（已在 F18 完成） |

### P1 — 本周修（LLM 角色重定位）

| 改动 | 文件 | 说明 |
|------|------|------|
| 新增 | `app/llm/multi_model_client.py` | 异构多模型客户端 (Qwen/DeepSeek/MiniMax/GLM)，通过百炼 API，含预算控制 |
| 新增 | `app/llm/attribution_engine.py` | LLM 逐笔归因引擎 |
| 新增 | `app/llm/weekly_proposer.py` | 周度假设生成器 |
| 新增 | `app/evolution/improvement_pipeline.py` | 提议→验证→部署流水线骨架 |
| 修改 | `scheduler_triggers.py` | 盘后触发 LLM 归因 + 周五触发周度假设 |
| 修改 | `config/unified_config.yaml` | 新增 `llm_budget` + `llm_models` 配置 |
| 修改 | `config/unified_config.yaml` | 新增 `paths` 段 (F18) |
| 修改 | `config/unified_config.yaml` | 新增 `risk.per_trade_risk_pct` + `risk.portfolio` (F10/F17) |

### P1 — 本周修（死代码清理）

| 改动 | 文件 | 说明 |
|------|------|------|
| 移到 .trash/ | app/us_pipeline/eod_attribution.py | 死代码，无调用方 |
| 移到 .trash/ | app/llm/empirical_verifier.py | mock，无真回测 |
| 移到 .trash/ | app/llm/disagreement_registry.py | 默认关闭 |
| 移到 .trash/ | app/llm/production_monitor.py | 零功能 |
| 修复 | premarket_planner.py:291 | 修复 import 路径 |

### P2 — 本月修

| 改动 | 说明 |
|------|------|
| Canay 框架 | 资金分桶执行 |
| 回测接入 | `ImprovementPipeline.validate_with_backtest()` 依赖 F29 |

---

## 六、LLM 归因 Prompt 设计

```
你是 FTSeek-US 美股交易归因分析师。

【交易数据】
标的: {symbol}
方向: {direction}
入场: {entry_price} at {entry_time}
出场: {exit_price} at {exit_time}
PnL: {pnl_pct}
退出原因: {exit_reason}

【入场信号因子】
{factor_detail}

【市场上下文】
VIX: {vix} | SPY 涨跌: {spy_chg} | 时段: {session} | 状态: {regime}

【回答格式（JSON）】
{
  "key_decision_factor": "入场时机/退出时机/仓位大小/市场环境",
  "should_have_done_differently": true/false,
  "specific_suggestion": "可量化的具体建议（eg: 'stop_loss 应改为 1.5x ATR'）",
  "is_systemic_issue": true/false,
  "systemic_pattern": "如果是系统性问题，描述模式（eg: 'RSI<25 入场的 NVDA 全部亏损'）",
  "confidence": 0.0-1.0
}
```

---

## 七、LLM 周度假设 Prompt 设计

```
你是 FTSeek-US 策略优化师。

【本周交易汇总】
共 {n} 笔交易，胜率 {win_rate}，总 PnL {total_pnl}

【逐笔 LLM 归因汇总】
{trade_attributions_with_llm_insights}

【当前关键参数】
day_trade_mode.tier_1.profit_pct = 3.0
chandelier_trail.activation_profit_atr = 1.2
...

【任务】
提议 1-3 个参数调整，每个含:
1. 调整前后值
2. 理由（引用 trade_id 作证据）
3. 预期改善（量化）
4. 失败假设（什么情况下这个调整会让结果变糟）

【输出格式】
JSON list of ImprovementProposal
```
