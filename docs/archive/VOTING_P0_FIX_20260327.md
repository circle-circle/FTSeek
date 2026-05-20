# FTSeek-US Voting 系统 P0 紧急修复报告

**修复日期**: 2026-03-27 21:48 (Asia/Shanghai)  
**执行人**: FTSeek-US Subagent  
**备份位置**: `/opt/ftseek/FTSeek-US/backups/voting_p0_20260327_214829/`

---

## 修复概述

本次紧急修复针对 Voting 系统的 4 项 P0 级别问题，旨在提升投票系统稳定性和信号质量。

---

## 修复清单

### P0-1: 统一评分标准 ✅

**文件**: `app/agents/voting/voting_config.py`

**变更内容**:
- 在 `VotingConfig` 类中添加统一的评分区间常量定义：
  ```python
  # 评分区间定义 (统一标准)
  STRONG_SHORT_MAX = 30      # 0-30: 强烈看空
  SHORT_MAX = 45             # 31-45: 看空
  NEUTRAL_MAX = 55           # 46-55: 中性
  LONG_MAX = 70              # 56-70: 看多
  # 71-100: 强烈看多
  ```

**验证结果**:
```
✓ voting_config.py: syntax OK
✓ STRONG_SHORT_MAX=30, SHORT_MAX=45, NEUTRAL_MAX=55, LONG_MAX=70
```

---

### P0-2: 收紧中性区间 ✅

**文件**: `app/agents/voting/voting_aggregator.py`

**变更内容**:
```python
class VotingAggregator:
    # 修改前 → 修改后
    NEUTRAL_LOW   = 40  →  45
    NEUTRAL_HIGH  = 60  →  55
    NEUTRAL_MAX_STD = 8.0  →  10.0  # 从 8.0 放宽至 10.0
```

**说明**: 
- 中性区间从 [40, 60] 收紧至 [45, 55]，与统一评分标准一致
- 中性判定标准差阈值从 8.0 放宽至 10.0，减少误判

**验证结果**:
```
✓ voting_aggregator.py: syntax OK
✓ NEUTRAL_LOW=45, NEUTRAL_HIGH=55, NEUTRAL_MAX_STD=10.0
```

---

### P0-3: 降低 Fallback 置信度 ✅

**文件**: `app/agents/voting/model_agent.py`

**变更内容**:

1. **降低规则评分置信度**:
   ```python
   # 修改前 → 修改后
   if has_core and has_extra:
       confidence = 0.55 → 0.40  # 数据完整
   elif has_core:
       confidence = 0.45 → 0.30  # 核心数据齐
   else:
       confidence = 0.35 → 0.20  # 数据不足
   ```

2. **降低置信度上限**:
   ```python
   # 修改前 → 修改后
   confidence=min(0.8, confidence)  →  min(0.5, confidence)
   ```

**说明**: Fallback 规则评分的置信度应显著低于 LLM 评分，避免在数据不足时过度自信。

**验证结果**:
```
✓ model_agent.py: syntax OK
✓ model_agent imports OK
```

---

### P0-4: 强化 JSON 格式要求 ✅

**文件 1**: `app/agents/voting/voting_coordinator.py`

**变更内容**: 在所有角色提示词中添加统一的 JSON 格式要求：
```
【重要】返回纯 JSON，不要使用 ```json 代码块包裹，不要有任何额外文本。
返回示例：{"strength": 65, "confidence": 0.72, "direction": "LONG", "reasoning": "均线多头排列"}
```

**更新的角色提示词**:
- `deepseek`: 趋势/动量分析
- `minimax`: 新闻情绪分析
- `glm5`: 技术指标分析
- `qwen35`: 基本面/估值分析
- 通用 fallback 提示词

**文件 2**: `app/agents/voting/model_agent.py`

**变更内容**: 在 `_parse_response` 方法中添加 markdown 代码块移除逻辑：
```python
def _parse_response(self, response: str) -> Optional[ModelScore]:
    # 移除可能的 markdown 代码块标记
    response = response.strip()
    if response.startswith("```json"):
        response = response[7:]
    if response.startswith("```"):
        response = response[3:]
    if response.endswith("```"):
        response = response[:-3]
    response = response.strip()
    # ... 继续解析 ...
```

**说明**: 双重保障确保 JSON 解析成功率：
1. 提示词明确要求纯 JSON
2. 解析器兼容处理可能的 markdown 包裹

**验证结果**:
```
✓ voting_coordinator.py: syntax OK
✓ voting_coordinator imports OK
```

---

## 测试验证结果

### 1. Python 语法检查
| 文件 | 状态 |
|------|------|
| voting_config.py | ✅ PASS |
| voting_aggregator.py | ✅ PASS |
| model_agent.py | ✅ PASS |
| voting_coordinator.py | ✅ PASS |

### 2. 模块导入测试
| 模块 | 状态 |
|------|------|
| voting_config | ✅ PASS |
| voting_aggregator | ✅ PASS |
| model_agent | ✅ PASS |
| voting_coordinator | ✅ PASS |

### 3. 常量验证
| 配置项 | 预期值 | 实际值 | 状态 |
|--------|--------|--------|------|
| STRONG_SHORT_MAX | 30 | 30 | ✅ |
| SHORT_MAX | 45 | 45 | ✅ |
| NEUTRAL_MAX | 55 | 55 | ✅ |
| LONG_MAX | 70 | 70 | ✅ |
| NEUTRAL_LOW | 45 | 45 | ✅ |
| NEUTRAL_HIGH | 55 | 55 | ✅ |
| NEUTRAL_MAX_STD | 10.0 | 10.0 | ✅ |

---

## 影响评估

### 正面影响
1. **信号质量提升**: 中性区间收紧减少模糊信号
2. **风险控制增强**: Fallback 置信度降低避免过度交易
3. **解析稳定性**: JSON 格式强化减少解析失败
4. **标准统一**: 所有模块使用一致的评分区间定义

### 潜在影响
1. **信号数量可能减少**: 中性区间收紧可能导致更多 NEUTRAL 判定
2. **Fallback 权重降低**: 规则评分的置信度降低可能影响 LLM 不可用时的决策质量

---

## 后续建议

1. **监控信号变化**: 观察修复后 1-2 周的信号数量和质量变化
2. **回测验证**: 使用历史数据回测新参数组合的表现
3. **日志增强**: 建议在 aggregator 中添加中性信号判定的详细日志
4. **配置化**: 考虑将评分区间常量移至配置文件，便于动态调整

---

## 备份恢复

如需恢复至修复前版本：
```bash
cd /opt/ftseek/FTSeek-US
cp backups/voting_p0_20260327_214829/*.py app/agents/voting/
```

---

**修复完成时间**: 2026-03-27 21:48  
**状态**: ✅ 全部修复完成，测试通过
