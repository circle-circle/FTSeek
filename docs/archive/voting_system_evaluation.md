# FTSeek-US 多智能体投票系统评估报告

**评估时间**: 2026-03-26 23:59
**评估人**: 投票系统专家 Subagent

---

## 一、投票系统架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         FTSeek-US 投票决策系统架构                           │
└─────────────────────────────────────────────────────────────────────────────┘

                              ┌──────────────────┐
                              │   Market Data    │
                              │   (行情/新闻)     │
                              └────────┬─────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           VotingCoordinator                                 │
│  (app/agents/voting/voting_coordinator.py)                                  │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                      并行调用 4 个 LLM Agent                          │   │
│  │                                                                       │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │   │
│  │  │  DeepSeek   │  │   MiniMax   │  │    GLM5     │  │   Qwen35    │  │   │
│  │  │  权重: 30%  │  │  权重: 25%  │  │  权重: 25%  │  │  权重: 20%  │  │   │
│  │  │ 趋势/动量   │  │ 新闻情绪    │  │ 技术指标    │  │ 基本面/估值 │  │   │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  │   │
│  │         │                │                │                │         │   │
│  │         ▼                ▼                ▼                ▼         │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │   │
│  │  │ strength    │  │ strength    │  │ strength    │  │ strength    │  │   │
│  │  │ confidence  │  │ confidence  │  │ confidence  │  │ confidence  │  │   │
│  │  │ direction   │  │ direction   │  │ direction   │  │ direction   │  │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘  │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                       │                                      │
│                                       ▼                                      │
│                      ┌─────────────────────────────┐                        │
│                      │     VotingAggregator        │                        │
│                      │ (voting_aggregator.py)      │                        │
│                      │                             │                        │
│                      │ • 加权平均 strength          │                        │
│                      │ • 平均 confidence           │                        │
│                      │ • 计算共识度 consensus       │                        │
│                      │ • 检测中性信号               │                        │
│                      └─────────────┬───────────────┘                        │
└─────────────────────────────────────┼────────────────────────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────┐
                    │         Scheduler 决策层         │
                    │   (app/scheduler.py)            │
                    │                                 │
                    │ 过滤条件:                        │
                    │ • consensus >= 0.50             │
                    │ • effective_strength >= 50      │
                    │ • confidence >= 0.50            │
                    │ • 非中性信号                     │
                    └─────────────┬───────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────────────┐
                    │      PreTradeChecker 风控层     │
                    │   (app/risk/pre_trade_check.py) │
                    │                                 │
                    │ 检查项:                          │
                    │ • effective_strength >= 50      │
                    │ • confidence >= 0.60 ⚠️         │
                    │ • 信号未过期                     │
                    └─────────────┬───────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────────────┐
                    │        ExecutionEngine          │
                    │           (下单执行)             │
                    └─────────────────────────────────┘
```

---

## 二、当前配置总览

### 2.1 模型权重配置
| 模型 | 权重 | 角色 | 实际调用 |
|------|------|------|----------|
| DeepSeek | 30% | 趋势/动量分析 | deepseek-chat |
| MiniMax | 25% | 新闻情绪分析 | bailian/MiniMax-M2.5 |
| GLM5 | 25% | 技术指标分析 | bailian/glm-4.7 |
| Qwen35 | 20% | 基本面/估值分析 | bailian/qwen3.5-plus |

### 2.2 阈值配置（多处不一致！）

| 参数 | voting_config.py | scheduler.py | pre_trade_check.py | strategy_params.json |
|------|------------------|--------------|-------------------|---------------------|
| min_confidence | - | 0.50 | **0.60** | 0.60 |
| consensus_threshold | 0.60 | 0.50 | - | 0.65 |
| min_strength | - | 50 | 50 | - |

⚠️ **关键问题**: `pre_trade_check.py` 中的 `min_confidence=0.60` 是硬编码默认值，而 `config_us.py` 中设置的是 `0.50`，两者不一致。

---

## 三、日志案例分析

### 3.1 最新投票结果 (2026-03-26 23:52-00:00)

| 标的 | 各模型评分 | 最终 strength | 共识度 | 方向 | 结果 |
|------|-----------|--------------|--------|------|------|
| US.META | D:25, M:22, G:28, Q:35 | 26 | 0.55 | SHORT | 预交易检查未通过 |
| US.AMZN | D:42, M:35, G:38, Q:35 | 37 | 0.78 | SHORT | 预交易检查未通过 |
| US.BABA | D:25, M:18, G:25, Q:35 | 25 | 0.43 | SHORT | 意见分歧放弃 |
| US.PDD | D:35, M:22, G:35, Q:35 | 30 | 0.50 | SHORT | 预交易检查未通过 |
| US.JD | D:65(LONG), M:42(SHORT), G:?, Q:55(LONG) | ? | ? | ? | 待观察 |

### 3.2 模型评分分布分析

**SHORT 方向一致性**:
- META: 4个模型全部看空 (22-35区间)，共识度高
- AMZN: 4个模型全部看空 (35-42区间)，共识度高
- BABA: 4个模型全部看空 (18-35区间)，但分歧较大
- PDD: 4个模型全部看空 (22-35区间)，分歧中等

**问题**: 所有模型都看空，但大多数被"预交易检查未通过"拒绝。

---

## 四、问题诊断

### 4.1 为什么大多数投票显示"预交易检查未通过"？

**根本原因**: `min_confidence` 阈值配置不一致

```
数据流:
1. VotingAggregator 计算 final_confidence = avg(各模型confidence)
2. Scheduler 过滤: confidence >= 0.50 ✓
3. PreTradeChecker 检查: confidence >= 0.60 ✗ (硬编码默认值)
```

**模型 confidence 来源分析**:

从 `model_agent.py` 中规则 fallback 逻辑：
```python
if has_core and has_extra:
    confidence = 0.55  # 数据完整
elif has_core:
    confidence = 0.45  # 核心数据齐
else:
    confidence = 0.35  # 数据不足
```

问题链:
1. 规则 fallback 最高只给 0.55 confidence
2. LLM 返回的 confidence 可能也在 0.4-0.6 区间
3. 平均后约 0.45-0.55
4. PreTradeChecker 要求 >= 0.60 → 几乎不可能通过

### 4.2 共识阈值是否合理？

**当前配置**:
- `voting_config.py`: `min_consensus_threshold = 0.60`
- `scheduler.py`: `MIN_CONSENSUS_LEVEL = 0.50`
- `strategy_params.json`: `consensus_threshold = 0.65`

**建议**: 统一为 **0.55**，理由：
- 0.50 太低，容易接受分歧信号
- 0.65 太高，会拒绝大多数共识信号
- 0.55 是合理的中等偏严格阈值

### 4.3 模型权重是否需要调整？

**当前权重**: DeepSeek 30%, MiniMax 25%, GLM5 25%, Qwen35 20%

**评估**:
1. 权重分配合理，DeepSeek 作为主力趋势分析模型权重最高
2. 但各模型的"角色差异化"需要验证：
   - 是否真的在分析不同维度？
   - 还是在做相似的分析（同质化）？

**日志观察**:
- META: D=25, M=22, G=28, Q=35 → 方向一致，但 Qwen 明显偏多
- BABA: D=25, M=18, G=25, Q=35 → MiniMax 最悲观，Qwen 最乐观

**建议**: 权重暂不调整，但需要持续监控各模型的"预测准确性"

---

## 五、优化建议

### 5.1 配置统一化 (高优先级)

**问题**: 多处配置不一致，导致行为不可预测

**修复方案**:

```python
# config/config_us.py 中统一配置
RISK_CONFIG = {
    ...
    "MIN_CONFIDENCE": 0.55,  # 统一降低到 0.55
    "MIN_CONSENSUS": 0.55,   # 新增共识阈值
    ...
}
```

同时修改:
1. `pre_trade_check.py` 的默认值改为读取配置
2. `scheduler.py` 的常量改为从配置读取
3. `voting_config.py` 的阈值统一

### 5.2 预交易检查逻辑优化 (高优先级)

**当前问题**: 检查过于严格，几乎不可能通过

**建议修改**:

```python
# app/risk/pre_trade_check.py
class PreTradeChecker:
    def __init__(
        self,
        min_confidence: float = 0.50,  # 降低默认值
        min_signal_strength: int = 50,
    ):
        self.min_confidence = min_confidence
        self.min_strength = min_signal_strength
```

### 5.3 增加调试日志 (中优先级)

在 scheduler.py 中增加 confidence 输出：

```python
logger.info(
    f"[投票] {symbol} strength={vote_result['final_strength']} "
    f"confidence={vote_result['final_confidence']:.2f} "  # 新增
    f"consensus={vote_result['consensus_level']:.2f} "
    f"direction={vote_result['direction']}"
)
```

### 5.4 模型评分归因 (中优先级)

建议在投票结果中记录每个模型的 confidence：

```python
# 投票结果增加模型详情
{
    "final_strength": 37,
    "final_confidence": 0.52,
    "consensus_level": 0.78,
    "model_scores": [
        {"model": "deepseek", "strength": 42, "confidence": 0.6},
        {"model": "minimax", "strength": 35, "confidence": 0.5},
        ...
    ]
}
```

### 5.5 动态阈值调整 (低优先级)

根据市场波动率动态调整阈值：
- VIX > 25 时：提高阈值到 0.60
- VIX < 20 时：降低阈值到 0.50

---

## 六、快速修复步骤

### Step 1: 修改 pre_trade_check.py 默认值

```python
# app/risk/pre_trade_check.py 第 19 行
min_confidence: float = 0.50,  # 从 0.60 改为 0.50
```

### Step 2: 确认 RiskEngine 读取配置

```python
# app/risk/engine.py 第 56 行已正确读取配置
min_confidence=risk_config.get("MIN_CONFIDENCE", 0.50),
```

### Step 3: 重启服务

```bash
systemctl restart ftseek-us
# 或
pm2 restart ftseek-us
```

---

## 七、监控指标建议

建议增加以下监控指标：

1. **投票通过率**: 共识通过的信号数 / 总信号数
2. **风控拒绝率**: 预交易拒绝数 / 共识通过数
3. **模型一致性**: 各模型 direction 一致的比例
4. **平均 confidence**: 模型返回的平均置信度
5. **阈值敏感度**: 不同阈值下的通过率变化

---

## 八、总结

| 问题 | 严重程度 | 状态 |
|------|----------|------|
| min_confidence 配置不一致 | 🔴 高 | 需修复 |
| pre_trade_check.py 硬编码 0.60 | 🔴 高 | 需修复 |
| 日志缺少 confidence 信息 | 🟡 中 | 建议优化 |
| 共识阈值分散在多处 | 🟡 中 | 建议统一 |
| 模型权重分配 | 🟢 低 | 暂不调整 |

**核心问题**: `pre_trade_check.py` 中 `min_confidence=0.60` 是导致大多数信号被拒绝的直接原因。将其降至 0.50 或从配置读取后，问题应可解决。

---

*报告生成时间: 2026-03-26 23:59*
*评估完成*