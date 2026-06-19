# FTSeek-US LLM 机制深度审查：稳定性评估与代码化改进空间

> 审查日期：2026-06-17
> 审查范围：LLM 服务层 / 投票系统 / 辩论引擎 / 深研究管线 / 因子评分 / 作战计划生成

---

## 一、当前 LLM 使用全景图

### 1.1 LLM 调用频次与成本

| 环节 | LLM 调用数 | 模型 | 延迟 | 成本 |
|------|-----------|------|------|------|
| 作战计划生成 (Stage 2 验证) | 8 次 (并行) | qwen-max + deepseek-pro | ~5-15s | ~$0.16 |
| 作战计划叙事 (Stage 5) | 1 次 | qwen-max | ~5s | ~$0.04 |
| 深研究 4 分析师 (Stage 6) | **1 次** (仅基本面) | qwen-max | ~10s | ~$0.06 |
| 深研究辩论 (Stage 7) | 1 次 | deepseek-pro | ~10s | ~$0.02 |
| 深研究综合 (Stage 8) | 1 次 | qwen-max | ~10s | ~$0.06 |
| 深研究风险辩论 (Stage 8.5) | 3 次 (并行) | deepseek-pro | ~10s | ~$0.04 |
| 盘中投票 (Level 0) | 2 次 (并行) | qwen-max + minimax | ~5-15s | ~$0.04 |
| **全流程合计** | **~27 次** | — | **60-90s** | **~$0.74** |

### 1.2 LLM 使用分类：哪些在用、哪些不该用

经过代码深读，系统中 LLM 的使用可以分为三类：

```
┌─────────────────────────────────────────────────────────────────────┐
│                    LLM 使用合理性矩阵                                │
│                                                                     │
│  ✅ LLM 有不可替代价值 (应保留)                                       │
│  ├─ 多空辩论综合 (Stage 7) — 需要从 4 份异构报告中提炼逻辑链           │
│  ├─ 投资组合经理裁决 (Stage 8) — 需要跨维度权衡和风险判断              │
│  ├─ 风险三方辩论 (Stage 8.5) — 需要创造性地寻找计划漏洞               │
│  ├─ 基本面分析 web search (Stage 6) — 需要从非结构化信息中提取关键数据  │
│  ├─ 催化剂事件分类 — 需要从新闻标题判断事件类型和影响方向              │
│  └─ 盘中投票的 Qwen/MiniMax 角色 — 第二阶推理、情绪读盘               │
│                                                                     │
│  ⚠️ LLM 在做但代码能做得更好 (应逐步替换)                              │
│  ├─ 候选验证 (Stage 2) — 本质是规则匹配，LLM 只是在做 if/else          │
│  ├─ 叙事生成 (Stage 5) — 模板化文本，不需要推理                        │
│  ├─ 技术分析解读 — 纯数值比较 (RSI>70=超买)，规则引擎更稳定            │
│  ├─ 情绪分析解读 — 阈值判断 (bull_bear_ratio>0.6=偏多)，不需要 LLM     │
│  └─ 新闻影响评估 — 关键词匹配 (earnings/fda/merger)，纯规则             │
│                                                                     │
│  ❌ LLM 不该做 (当前已经是代码实现，确认正确)                           │
│  ├─ 因子评分 (SSS/GDIAN/MFFF) — 纯数学计算                           │
│  ├─ IC 自适应权重 — 统计计算                                         │
│  ├─ ATR 止损计算 — 纯函数                                            │
│  ├─ 市场 regime 识别 — 数值阈值判断                                   │
│  ├─ 信号去重/冷却 — 时间窗口逻辑                                      │
│  └─ 仓位计算 — 分层+水位+波动率调整                                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 二、稳定性评估

### 2.1 LLM 服务层 — ⭐⭐⭐⭐ 稳定且有韧性

**核心架构**：`model_call.call_llm(feature, prompt)` → YAML 路由 → HTTP → 重试 → 降级 → 缓存

| 维度 | 评估 | 说明 |
|------|------|------|
| 路由灵活性 | ✅ 优秀 | YAML 驱动模型选择，切换模型零代码改动 |
| 重试机制 | ✅ 合理 | 每模型 2 次尝试 + 1s 退避，不过度重试 |
| 降级链 | ✅ 优秀 | primary → fallback → 24h 文件缓存 → fail |
| Schema 验证 | ✅ 优秀 | JSON 解析 → 字段校验 → 枚举约束 → 数值裁剪 |
| 审计追踪 | ✅ 完备 | 每次调用记录 SQLite (prompt_hash, cost, latency, success) |
| 成本控制 | ✅ 精细 | per-feature 预算 + 日限额 + token 级成本计算 |

**关键稳定性数据**：
- 单次 LLM 调用最坏情况：4 次 HTTP 请求（primary×2 + fallback×2）→ 缓存 → fail
- 投票系统 3 级降级：多模型(90s) → 单模型(30s) → 纯技术(conf≤0.60)
- 深研究全链路降级：分析师缺 → 标记 missing / 辩论缺 → 规则提取 / 综合缺 → 规则计划

### 2.2 投票系统 — ⭐⭐⭐⭐ 结构合理但有改进空间

| 维度 | 评估 | 说明 |
|------|------|------|
| 输出结构化 | ✅ 好 | 严格 JSON: `{strength, confidence, direction, reasoning}` |
| 偏差校正 | ✅ 实证 | MiniMax ×0.75 校正 + 方向保护防翻转 |
| 降级链 | ✅ 优秀 | 3 级降级 + per-model 技术回退 + 半模型失败 DingTalk 告警 |
| 缓存 | ✅ 合理 | 5min TTL + 0.8% 价格变动 + 信号哈希三重失效 |
| **LLM 必要性** | **⚠️ 部分存疑** | 投票本质是评分，LLM 在做「给定 N 个数值 → 输出 1 个数值」，这是回归模型的活 |

### 2.3 辩论引擎 — ⭐⭐⭐⭐ 设计精巧但有锚定风险

| 维度 | 评估 | 说明 |
|------|------|------|
| R1 并行隔离 | ✅ 好 | 不同线程、不同模型、不同 prompt |
| R2 条件触发 | ✅ 好 | divergence > 0.4 才触发，减少不必要的 LLM 开销 |
| **R2 锚定偏差** | **⚠️ 已识别** | 传递完整 JSON 而非仅 reasoning，导致锚定效应 |
| Arbiter | ✅ 好 | Legacy 路径用确定性规则合并（非 LLM），更可靠 |
| **LLM 必要性** | **✅ 合理** | 综合多维度异构信息、寻找逻辑漏洞是 LLM 的优势 |

### 2.4 深研究管线 — ⭐⭐⭐ 有优化空间

**关键发现：4 个分析师中只有 1 个使用 LLM**

| 分析师 | LLM 调用 | 实际实现 | 评估 |
|--------|----------|----------|------|
| 技术分析 | **0** | Futu K-line → 本地计算 → 规则解读 | ✅ 正确选择 |
| 基本面分析 | **1** | FMP API + LLM web search | ✅ 合理 — 需要从非结构化信息提取 |
| 新闻分析 | **0** | Premarket enrichment + 关键词匹配 | ✅ 正确选择 |
| 情绪分析 | **0** | StockTwits API + Reddit DB + 阈值判断 | ✅ 正确选择 |

**这说明系统已经在践行「能用代码就不用 LLM」的原则。** 4 个分析师中的 3 个已经是纯代码实现，且效果稳定。

### 2.5 候选验证 (Stage 2) — ⭐⭐⭐ LLM 在做规则的活

**当前实现**：8 个候选各 1 次 LLM 调用，prompt 包含 5 因子得分 + 技术指标 + 催化剂评分，要求输出 `{verdict: VALID/INVALID/CONDITIONAL, confidence, reasons[]}`。

**问题**：这个验证的本质是：
```
IF factor_score >= threshold AND catalyst_score >= 3 AND NOT (3+ factors are WEAK):
    verdict = VALID
ELIF 3+ factors are FALSE_POSITIVE:
    verdict = INVALID
ELSE:
    verdict = CONDITIONAL
```

这些判断规则**已经在 prompt 中用自然语言写清楚了**。LLM 只是在执行 if/else 逻辑，没有真正的「推理」发生。

---

## 三、代码化改进空间：具体方案

### 3.1 高优先级：候选验证 (Stage 2) 代码化

**当前**：8 次 LLM 调用 × 300s timeout × ~$0.16

**改进方案**：确定性规则引擎 + LLM 仅处理边界案例

```python
def validate_candidate(candidate: dict) -> ValidationResult:
    """确定性候选验证 — 替代 LLM 调用"""
    factors = candidate["factor_scores"]
    catalyst = candidate.get("catalyst_score", 0)

    # 规则 1: 3+ 因子 WEAK/FALSE_POSITIVE → INVALID
    weak_count = sum(1 for f in factors.values() if f["signal"] in ("WEAK", "FALSE_POSITIVE"))
    if weak_count >= 3:
        return ValidationResult("INVALID", confidence=85,
                                reasons=[f"{weak_count} factors weak/false_positive"])

    # 规则 2: 3+ 因子 CONFIRMED + catalyst ≥ 3 → VALID
    confirmed = sum(1 for f in factors.values() if f["signal"] == "CONFIRMED")
    if confirmed >= 3 and catalyst >= 3:
        return ValidationResult("VALID", confidence=min(95, 60 + confirmed * 10),
                                reasons=[f"{confirmed} confirmed + catalyst {catalyst}"])

    # 规则 3: composite_score 排名 top 3 + catalyst ≥ 5 → VALID
    if candidate.get("rank_position", 99) <= 3 and catalyst >= 5:
        return ValidationResult("VALID", confidence=75,
                                reasons=["top rank + strong catalyst"])

    # 边界案例: 2 confirmed + catalyst 1-2 → CONDITIONAL
    if confirmed >= 2:
        return ValidationResult("CONDITIONAL", confidence=50,
                                reasons=[f"borderline: {confirmed} confirmed, catalyst {catalyst}"])

    # 默认: INVALID
    return ValidationResult("INVALID", confidence=70, reasons=["insufficient confirmation"])
```

**收益**：
- 延迟：从 ~15s (8 并行 LLM) → **<10ms** (纯代码)
- 成本：从 ~$0.16 → **$0**
- 稳定性：从依赖 LLM 可用性 → **100% 可用**
- 可解释性：从 LLM 黑箱 → **每条规则可追溯**

**保留 LLM 的场景**：仅对 CONDITIONAL 候选（边界案例）调用 LLM 做深度判断。通常 8 个候选中只有 2-3 个 CONDITIONAL，LLM 调用从 8 次降到 2-3 次。

### 3.2 高优先级：叙事生成 (Stage 5) 模板化

**当前**：1 次 LLM 调用，让 LLM 根据 regime + 目标列表 + 限制列表生成一段叙述性文字。

**改进方案**：Jinja2 模板引擎

```python
NARRATIVE_TEMPLATE = """
## 市场判断
{{ regime_reasoning }}
{% if regime == 'RISK_ON' %}
当前市场处于风险偏好模式，适合趋势跟随策略。
{% elif regime == 'RISK_OFF' %}
市场避险情绪浓厚，建议缩小仓位、加宽止损。
{% endif %}

## 今日核心目标
{% for t in primary_targets %}
{{ loop.index }}. **{{ t.symbol }}** ({{ t.direction }})
   - 入场区间: {{ t.entry_zone[0] }}–{{ t.entry_zone[1] }}
   - 止损: {{ t.stop_loss }} | 目标: {{ t.take_profit }}
   - 信心: {{ t.confidence }}%
{% endfor %}

## 风险警告
{% for w in risk_warnings %}
- {{ w }}
{% endfor %}
"""
```

**收益**：零 LLM 调用、零延迟、100% 一致格式、无幻觉风险。

### 3.3 中优先级：盘中投票部分代码化

**当前**：2 个 LLM 模型（Qwen 70% + MiniMax 30%）并行评分 → 加权聚合。

**问题**：LLM 在做的是「给定 N 个数值指标 → 输出 1 个评分」，本质是**回归模型**。

**改进方案**：引入轻量级梯度提升模型 (GBM) 替代一个 LLM 角色

```
方案 A: 完全替代 (激进)
  Qwen (70%) → 训练好的 XGBoost/LightGBM 模型
  MiniMax (30%) → 保留 (情绪读盘是 LLM 的优势)

方案 B: 渐进替代 (推荐)
  新增 Level 0.5: GBM 模型评分 (instant, conf capped at 0.75)
  与 LLM 评分做 cross-check:
    - 两者方向一致 → 取均值 (confidence +5%)
    - 两者方向矛盾 → 降级到纯技术 (Level 2)
  LLM 调用从 2 次降到 0-1 次

方案 C: 不替代，但加校准 (保守)
  保留 LLM 投票，但添加 Platt Scaling 校准:
    收集 30 天 LLM confidence vs 实际胜率
    用逻辑回归拟合 calibration curve
    投票输出 confidence = calibrated_confidence
```

**收益（方案 B）**：
- 延迟：从 ~10s → **<50ms** (GBM 推理)
- 成本：从 ~$0.04/次 → **$0**
- 稳定性：不再受 LLM API 波动影响
- 风险：需要 30+ 天训练数据，初期可能不如 LLM

### 3.4 中优先级：深研究辩论 prompt 优化

**当前问题**：
1. **Prompt truncation 过于激进**：辩论结果被截断到 2000 字符传给综合阶段，可能丢失关键论证
2. **JSON Schema 未正式验证**：依赖 `_safe_json_loads()` + `setdefault()` 填充缺失字段，而非 `call_llm` 的 `schema_required` 参数
3. **R2 锚定偏差**（已在 P0 修复清单中标记）

**改进方案**：

```python
# 1. 启用 call_llm 的内置 schema 验证 (替代手动 _safe_json_loads)
result = call_llm(
    feature="dr_debate",
    prompt=debate_prompt,
    json_mode=True,
    schema_required=["bull_case", "bear_case", "verdict"],
    schema_optional=["causal_chain", "historical_reference", "max_disagreement"],
    schema_enums={"verdict.direction": ["LONG", "SHORT", "NEUTRAL"]},
)

# 2. 放宽 truncation 限制 (当前 token 成本很低)
# debate → synthesis: 2000 chars → 5000 chars
# build_plan → risk_debate: 1500 chars → 3000 chars

# 3. R2 只传递 reasoning，不传递具体交易参数
r2_context = {
    "opponent_reasoning": opponent["reasoning"],
    "opponent_data_cited": opponent["data_cited"],
    # 不传递: entry_zone, stop_loss, take_profit, confidence
}
```

### 3.5 低优先级：Agent Skills vs 直接代码

**当前状态**：系统**不使用 agent skills**，所有 LLM 调用通过 `model_call.call_llm()` 直接发起 HTTP 请求。没有 LangChain/LlamaIndex/CrewAI 等框架。

**评估**：这是一个**正确的选择**。原因：

| 维度 | 直接代码 (当前) | Agent 框架 |
|------|----------------|-----------|
| 延迟 | 低 (直接 HTTP) | 高 (框架开销 + 多轮工具调用) |
| 成本 | 低 (精确控制 token) | 高 (agent 自主决策可能过度调用) |
| 可控性 | 高 (确定性流程) | 低 (agent 可能走错路径) |
| 可审计性 | 高 (每次调用记录) | 中 (框架内部逻辑不透明) |
| 适用场景 | 结构化、可预测的任务 | 开放性、探索性任务 |

**唯一适合引入 agent 的场景**：深度研究中的基本面分析。当前通过 `web_search_fundamental` feature 让 LLM 从 web 搜索中提取财务数据。如果改为 agent + tools (SEC EDGAR API, FMP API, Yahoo Finance API)，agent 可以自主决定查哪些数据源、做几轮搜索。但这增加了复杂度和不可预测性，目前单次 LLM 调用已经足够。

---

## 四、综合评估与路线图

### 4.1 当前系统 LLM 使用合理性评分

| 子系统 | LLM 合理性 | 稳定性 | 改进空间 |
|--------|-----------|--------|---------|
| LLM 服务层 (model_call) | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 清理 legacy 调用路径 |
| 盘中投票 (VotingCoordinator) | ⭐⭐⭐ | ⭐⭐⭐⭐ | 引入 GBM 替代 Qwen 角色 |
| 辩论引擎 (DebateEngine) | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 修复 R2 锚定 + 启用 schema |
| 深研究 4 分析师 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 已经 3/4 是代码，设计正确 |
| 候选验证 (Stage 2) | ⭐⭐ | ⭐⭐⭐ | **代码化优先级最高** |
| 叙事生成 (Stage 5) | ⭐⭐ | ⭐⭐⭐⭐ | 模板化优先级高 |
| 因子评分 / regime / 止损 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 无需改动 (已全代码) |

### 4.2 改进路线图

```
Phase 1 (1-2 周, 投入产出比最高):
├─ 候选验证 (Stage 2) 代码化
│   → 省 8 次 LLM 调用 / ~$0.16 / ~15s
│   → 仅 CONDITIONAL 案例走 LLM (2-3 次)
├─ 叙事生成 (Stage 5) 模板化
│   → 省 1 次 LLM 调用 / 零幻觉风险
└─ 深研究启用 schema_required 验证
    → 减少 _safe_json_loads fallback 频率

Phase 2 (2-4 周, 需要训练数据):
├─ 盘中投票 GBM 替代方案 POC
│   → 收集 30 天 LLM 评分 vs 实际涨跌训练数据
│   → 训练 XGBoost 模型 + cross-check 架构
└─ LLM confidence 校准 (Platt Scaling)
    → 30 天 confidence vs 胜率回归
    → 消除 LLM 过度自信偏差

Phase 3 (1-2 月, 架构优化):
├─ 清理 legacy LLM 调用路径
│   → 删除 MultiModelClient (1 个调用方)
│   → 删除 file-system IPC (agent_bridge)
│   → 统一到 model_call.call_llm
├─ 合并 3 套 JSON 解析实现
│   → json_schema_guard (唯一入口)
└─ 辩论 R2 只传 reasoning
    → 消除锚定偏差
```

### 4.3 核心结论

> **系统当前的 LLM 使用整体合理且稳定，但存在「过度 LLM 化」的问题 — 部分环节（候选验证、叙事生成）用 LLM 在做 if/else 和模板填充的活，增加了不必要的延迟、成本和故障点。**
>
> **最大的改进空间不是引入 agent 框架（这会增加复杂度和不可预测性），而是将已在 prompt 中用自然语言写清楚的规则逻辑「编译」为确定性代码，让 LLM 专注于它真正擅长的事：跨维度综合判断、寻找逻辑漏洞、从非结构化信息中提取洞察。**

---

## 五、数据支撑

### 5.1 LLM 调用统计 (当前 vs 改进后)

| 环节 | 当前 LLM 调用 | 改进后 | 减少 |
|------|-------------|--------|------|
| 候选验证 | 8 次 | 2-3 次 | ~65% |
| 叙事生成 | 1 次 | 0 次 | 100% |
| 深研究 schema | 6 次 (手动解析) | 6 次 (自动验证) | 0% (质量提升) |
| **全流程合计** | ~27 次 | **~21 次** | **~22%** |

### 5.2 延迟对比

| 环节 | 当前 | 改进后 |
|------|------|--------|
| 候选验证 | ~15s (8 并行) | **<10ms** (代码) |
| 叙事生成 | ~5s | **<5ms** (模板) |
| **全流程** | 60-90s | **~45-70s** |

### 5.3 成本对比

| 指标 | 当前 | 改进后 |
|------|------|--------|
| 每次计划生成 | ~$0.74 | ~$0.58 |
| 月度 (22 交易日) | ~$16.28 | ~$12.76 |
| **年度节省** | — | **~$42** |

成本节省金额不大，但**稳定性和可预测性的提升远大于成本节省的价值**。

---

*本报告基于对 LLM 服务层、投票系统、辩论引擎、深研究管线的全量代码审查。所有结论均可追溯到具体文件和实现逻辑。*
