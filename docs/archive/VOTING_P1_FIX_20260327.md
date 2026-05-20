# FTSeek-US Voting 系统 P1 修复报告

**日期**: 2026-03-27  
**执行时间**: 21:51 GMT+8  
**状态**: ✅ 完成

---

## 备份信息

**备份位置**: `/opt/ftseek/FTSeek-US/backups/voting_p1_20260327_215144/`

备份文件包含:
- `voting_config.py`
- `voting_coordinator.py`
- `voting_aggregator.py`
- `model_agent.py`

---

## P1 修复清单

### ✅ P1-1: 权重分配优化

**文件**: `app/agents/voting/voting_config.py`

**变更内容**:
- 为 `model_weights` 添加详细注释，说明各模型权重分配依据
- DeepSeek 30% 权重最高，因趋势是最强预测因子
- MiniMax 25% 权重，情绪驱动短期波动
- GLM5 25% 权重，客观数据支撑
- Qwen35 20% 权重，中长线重要但短期影响较小
- 添加注释：权重基于专家经验，后续应基于历史准确率动态调整

**代码示例**:
```python
# 权重基于专家经验，后续应基于历史准确率动态调整
model_weights={
    ModelRole.DEEPSEEK: 0.30,   # 趋势/动量分析 - 权重最高，因趋势是最强预测因子
    ModelRole.MINIMAX: 0.25,   # 新闻情绪分析 - 情绪驱动短期波动
    ModelRole.GLM5: 0.25,      # 技术指标分析 - 客观数据支撑
    ModelRole.QWEN35: 0.20,    # 基本面/估值分析 - 中长线重要但短期影响较小
}
```

---

### ✅ P1-2: 模型命名统一

**文件**: `app/agents/voting/voting_config.py`

**修正内容**:
- GLM5 实际调用 `glm-4.7`，已在代码中修正
- 为 `model_mapping` 添加注释说明实际模型版本

**代码示例**:
```python
# 模型版本说明：实际调用的 LLM 模型标识
model_mapping={
    ModelRole.DEEPSEEK: "deepseek/deepseek-chat",
    ModelRole.MINIMAX: "bailian/MiniMax-M2.5",
    ModelRole.GLM5: "bailian/glm-4.7",      # 修正：GLM5 → glm-4.7
    ModelRole.QWEN35: "bailian/qwen3.5-plus",
}
```

---

### ✅ P1-3: 超时配置统一

**文件**: 
- `app/agents/voting/voting_coordinator.py`
- `app/agents/voting/model_agent.py`

**变更内容**:
- `VotingCoordinator.timeout_seconds`: 30 秒 → **90 秒**
- `ModelAgent.API_TIMEOUT`: 60 秒 → **90 秒**
- 统一超时配置，匹配思维链模型响应时间

**代码示例**:
```python
# voting_coordinator.py
class VotingCoordinator:
    def __init__(
        self,
        config: Optional[VotingConfig] = None,
        timeout_seconds: int = 90,  # 统一为 90 秒，匹配思维链模型响应时间
    ):
        # ...

# model_agent.py
class ModelAgent:
    API_TIMEOUT = 90  # 与 VotingCoordinator.timeout_seconds 保持一致
```

---

### ✅ P1-4: 添加动态权重调整

**文件**: `app/agents/voting/voting_aggregator.py`

**新增功能**:
- `aggregate()` 方法新增 `market_data` 参数
- 高波动市场 (VIX > 25) 自动调整权重:
  - 降低 GLM5(技术) 权重 10%
  - 提高 Qwen35(基本面) 权重 10%
- 添加日志记录动态权重调整事件

**代码示例**:
```python
def aggregate(self, scores: List[ModelScore], market_data: Dict[str, Any] = None) -> Dict[str, Any]:
    # 动态权重调整：高波动市场降低技术模型权重
    weights = self.config.model_weights.copy()
    
    if market_data:
        vix = market_data.get("vix", 20)
        if vix > self.config.VIX_HIGH_THRESHOLD:  # 高波动市场
            # 降低 GLM5(技术) 权重，提高 Qwen35(基本面) 权重
            weights[ModelRole.GLM5] = max(0.15, weights[ModelRole.GLM5] - self.config.VIX_WEIGHT_ADJUSTMENT)
            weights[ModelRole.QWEN35] = min(0.30, weights[ModelRole.QWEN35] + self.config.VIX_WEIGHT_ADJUSTMENT)
            logger.info(f"[动态权重] VIX={vix:.1f} > {self.config.VIX_HIGH_THRESHOLD}, 降低技术模型权重，提高基本面权重")
    
    # 使用调整后的权重计算加权平均
    # ...
```

**测试结果**:
- 正常市场 (VIX=20): final_strength=58
- 高波动市场 (VIX=30): final_strength=57 (权重调整后)

---

### ✅ P1-5: 配置集中化

**文件**: `app/agents/voting/voting_config.py`

**新增常量**:
```python
@dataclass
class VotingConfig:
    # 一致性检测阈值
    MAX_STD_DEV = 15.0           # 标准差超过此值需人工审核
    MIN_CONSENSUS = 0.55         # 一致性低于此值需人工审核
    
    # 中性信号检测
    NEUTRAL_LOW = 45
    NEUTRAL_HIGH = 55
    NEUTRAL_MAX_STD = 10.0
    
    # Fallback 置信度
    FALLBACK_CONFIDENCE_COMPLETE = 0.40
    FALLBACK_CONFIDENCE_CORE = 0.30
    FALLBACK_CONFIDENCE_MINIMAL = 0.20
    FALLBACK_CONFIDENCE_MAX = 0.50
    
    # 动态权重调整
    VIX_HIGH_THRESHOLD = 25.0
    VIX_WEIGHT_ADJUSTMENT = 0.10
```

**同步更新**:
- `voting_aggregator.py` 使用配置中的常量替代硬编码值
- `voting_coordinator.py` 传递 `market_data` 到聚合器

---

## 测试验证结果

### 1. Python 语法检查
```bash
✓ Python 语法检查通过
```

### 2. 模块导入测试
```
✓ 配置加载成功
  - MAX_STD_DEV: 15.0
  - MIN_CONSENSUS: 0.55
  - VIX_HIGH_THRESHOLD: 25.0
  - VIX_WEIGHT_ADJUSTMENT: 0.1
✓ 权重配置验证通过
✓ 模型映射验证通过
```

### 3. 动态权重逻辑测试
```
✓ 正常市场 (VIX=20): final_strength=58
✓ 高波动市场 (VIX=30): final_strength=57
✓ Coordinator timeout: 90 秒
✓ ModelAgent API_TIMEOUT: 90 秒
```

### 4. 综合测试
```
✅ 所有测试通过!
```

---

## 变更总结

| 文件 | 变更类型 | 描述 |
|------|---------|------|
| `voting_config.py` | 增强 | 添加权重注释、模型版本说明、配置常量 |
| `voting_coordinator.py` | 修复 | 超时统一为 90 秒，传递 market_data |
| `voting_aggregator.py` | 增强 | 动态权重调整、使用配置常量 |
| `model_agent.py` | 修复 | API_TIMEOUT 统一为 90 秒 |

---

## 后续建议

1. **回测验证**: 基于历史数据验证权重分配的合理性
2. **动态权重优化**: 考虑引入更多市场指标 (如 VVIX、put/call ratio)
3. **权重学习**: 实现基于历史准确率的自动权重调整机制
4. **监控告警**: 对动态权重调整事件添加飞书告警

---

**修复完成时间**: 2026-03-27 21:52 GMT+8  
**执行人**: FTSeek-US Subagent
