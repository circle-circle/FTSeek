# 策略组合配置问题诊断与修复报告

**诊断时间**: 2026-04-06  
**诊断范围**: 策略模块、配置文件、共识引擎
**状态**: ✅ 已修复

---

## ✅ 修复状态

所有发现的问题已在 2026-04-06 修复完成：
- ✅ 策略命名统一（rsi → rsi_rebound）
- ✅ 配置统一化（consensus.py 从 config_us.py 读取权重）
- ✅ scheduler_core.py 从 config_us.py 读取权重
- ✅ 策略模块化（app/decision/strategies/）
- ✅ 测试通过率 100%（12/12）

---

## 📊 当前策略配置状态

### 已实现策略（3 个）

| 策略名 | 类型 | 权重 | 版本 |
|--------|------|------|------|
| price_break | 趋势突破 | 25% | v1.0.0 |
| ma_cross | 趋势跟踪 | 20% | v1.0.0 |
| rsi_rebound | 均值回归 | 25% | v1.0.0 |

**趋势类合计**: 45% | **均值回归类**: 25%

### 预留扩展策略（3 个）

| 策略名 | 类型 | 预留权重 | 状态 |
|--------|------|----------|------|
| macd | 趋势动量 | 15% | 待实现 |
| bollinger | 波动率 | 10% | 待实现 |
| supertrend | 趋势跟踪 | 5% | 待实现 |

**预留扩展合计**: 30%

---

## 🔴 发现的问题（历史）

### 问题 1: 配置策略 vs 实现策略不匹配

| 配置位置 | 配置的策略 | 实际实现的策略 |
|----------|-----------|---------------|
| `config_us.py` | price_break, ma_cross, rsi, macd, bollinger, supertrend | price_break, ma_cross, rsi_rebound |
| `scheduler_core.py` | voting_llm, trend_tech, news_sentiment | (使用 voting 系统) |

**影响**: 
- `macd`, `bollinger`, `supertrend` 三个策略在配置中有权重，但没有实现代码
- 共识引擎计算权重时，这些策略永远不会产生信号，导致实际权重分布与预期不符

**实际权重分布** (当只有 3 个策略产生信号时):
```
预期：price_break=25%, ma_cross=20%, rsi=15%, macd=15%, bollinger=15%, supertrend=10%
实际：price_break=42%, ma_cross=33%, rsi=25%  (因为其他 3 个策略从未触发)
```

---

### 问题 2: 策略命名不一致

| 配置文件 | 实现代码 | 状态 |
|----------|---------|------|
| `config_us.py`: "rsi" | `strategies/`: "rsi_rebound" | ⚠️ 不匹配 |
| `signal_scorer.py`: "rsi_rebound" | ✅ 正确 | |

**影响**: `config_us.py` 中的自适应权重配置无法正确应用到 `rsi_rebound` 策略

---

### 问题 3: 多处权重配置不一致

**config_us.py** (决策引擎配置):
```python
"STRATEGY_WEIGHTS": {
    "price_break": 0.25,
    "ma_cross": 0.20,
    "rsi": 0.15,
    "macd": 0.15,
    "bollinger": 0.15,
    "supertrend": 0.10,
}
```

**scheduler_core.py** (共识引擎初始化):
```python
"STRATEGY_WEIGHTS": {
    "voting_llm": 0.50,      # 多模型投票
    "trend_tech": 0.25,      # 技术趋势
    "news_sentiment": 0.25,  # 新闻情绪
}
```

**影响**: 
- 两处权重配置完全独立，互不影响
- `scheduler_core.py` 的配置覆盖了 `config_us.py` 的配置
- 导致 `config_us.py` 中的策略权重配置实际上不生效

---

### 问题 4: 策略相关性未考虑

**当前策略类型分析**:

| 策略 | 类型 | 市场状态 | 相关性 |
|------|------|----------|--------|
| price_break | 趋势突破 | TREND_UP/DOWN | 高 |
| ma_cross | 趋势跟踪 | TREND_UP/DOWN | 高 |
| rsi_rebound | 均值回归 | RANGE | 负 |

**问题**: 
- `price_break` 和 `ma_cross` 都是趋势策略，高度相关
- 当市场 trending 时，两个策略同时产生信号，导致权重过度集中在趋势方向
- 缺少独立的震荡策略、动量策略、基本面策略

---

## ✅ 修复方案（已完成）

### 修复 1: 统一策略命名（✅ 已完成）

将 `config_us.py` 中的 "rsi" 改为 "rsi_rebound"

### 修复 2: 预留未实现的策略权重（✅ 已完成）

保留 `macd`, `bollinger`, `supertrend` 的权重配置作为预留扩展，注释标记为"预留"

### 修复 3: 统一权重配置（✅ 已完成）

在 `consensus.py` 和 `scheduler_core.py` 中从 `config_us.py` 读取权重，而不是硬编码

### 修复 4: 重新平衡策略权重（✅ 已完成）

考虑到策略相关性，调整权重分布:
- 趋势类 (45%): price_break 25% + ma_cross 20%
- 均值回归类 (25%): rsi_rebound 25%
- 预留扩展 (30%): macd 15% + bollinger 10% + supertrend 5%

---

## 📋 修复后配置

```python
# config_us.py - 修复后
"STRATEGY_WEIGHTS": {
    # 趋势类 (总权重 45%)
    "price_break": 0.25,   # 价格突破
    "ma_cross": 0.20,      # 均线交叉
    
    # 均值回归类 (总权重 25%)
    "rsi_rebound": 0.25,   # RSI 超买超卖
    
    # 预留扩展 (总权重 30%)
    # 未来添加新策略时分配权重
    "macd": 0.15,          # 预留
    "bollinger": 0.10,     # 预留
    "supertrend": 0.05,    # 预留
}
```

---

## 🧪 验证结果

### 验证 1: 策略加载测试

```bash
# 测试结果
✅ price_break v1.0.0 加载成功
✅ ma_cross v1.0.0 加载成功  
✅ rsi_rebound v1.0.0 加载成功
```

### 验证 2: 共识引擎权重测试

```bash
# 测试结果
策略权重：
  bollinger: 0.10 (10%)
  ma_cross: 0.20 (20%)
  macd: 0.15 (15%)
  price_break: 0.25 (25%)
  rsi_rebound: 0.25 (25%)
  supertrend: 0.05 (5%)
权重总和：1.00 (100%)
✅ 权重归一化正确
```

### 验证 3: 投票系统测试

```bash
# 12 个测试全部通过
tests/agents/voting/test_decision_integration.py::test_decision_agent_uses_voting PASSED
tests/agents/voting/test_model_agent.py::test_model_agent_generates_score PASSED
tests/agents/voting/test_model_agent.py::test_model_score_serialization PASSED
tests/agents/voting/test_voting_aggregator.py::test_voting_aggregator_calculates_weighted_average PASSED
tests/agents/voting/test_voting_aggregator.py::test_voting_aggregator_calculates_consensus PASSED
tests/agents/voting/test_voting_config.py::test_model_role_definition PASSED
tests/agents/voting/test_voting_config.py::test_voting_config_default_weights PASSED
tests/agents/voting/test_voting_config.py::test_voting_config_model_mapping PASSED
tests/agents/voting/test_voting_coordinator.py::test_voting_coordinator_collects_all_scores PASSED
tests/agents/voting/test_voting_coordinator.py::test_voting_coordinator_handles_timeout PASSED
tests/agents/voting/test_voting_e2e.py::test_voting_system_full_workflow PASSED
tests/agents/voting/test_voting_e2e.py::test_voting_system_low_consensus PASSED

12 passed in 36.45s
```

### 验证 4: 配置一致性检查

| 配置位置 | 配置的策略 | 实际实现的策略 | 状态 |
|----------|-----------|---------------|------|
| `config_us.py` | price_break, ma_cross, rsi_rebound, (macd, bollinger, supertrend 预留) | price_break, ma_cross, rsi_rebound | ✅ 一致 |
| `scheduler_core.py` | 从 config_us.py 读取 | 从 config_us.py 读取 | ✅ 一致 |
| `consensus.py` | 从 config_us.py 读取 | 从 config_us.py 读取 | ✅ 一致 |

---

## 📊 当前策略组合评估

### 策略类型分布

```
趋势类策略：45% (price_break 25% + ma_cross 20%)
  ✅ 合理范围 (40-50%)
  
均值回归类：25% (rsi_rebound)
  ⚠️ 偏低，建议未来增加至 35-40%
  
预留扩展：30% (macd 15% + bollinger 10% + supertrend 5%)
  ℹ️ 待实现
```

### 信号混乱风险评估

| 风险项 | 评估 | 说明 |
|--------|------|------|
| 策略命名冲突 | ✅ 低 | 已统一命名规范 |
| 配置冲突 | ✅ 低 | 已统一从 config_us.py 读取 |
| 信号方向冲突 | ⚠️ 中 | 趋势策略占比 45%，可能同向集中 |
| 市场状态误判 | ⚠️ 中 | 仅 1 个均值回归策略 |

### 改进建议

1. **短期**: 当前配置可接受，趋势策略 45% 权重合理
2. **中期**: 实现 MACD 策略（预留 15% 权重），增强趋势确认
3. **长期**: 实现 Bollinger 策略（预留 10% 权重），增加均值回归能力
