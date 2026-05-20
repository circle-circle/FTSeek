# 百炼多模型调用测试报告

> **测试日期**: 2026-03-26 13:05  
> **测试环境**: FTSeek-US  
> **测试目的**: 验证百炼API Key调用多个模型的能力

---

## 一、测试配置

| 配置项 | 值 |
|--------|-----|
| API Key | `sk-dea55300e7104c88b...` |
| Base URL | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| 超时时间 | 60-120秒 |

---

## 二、测试模型列表

| 模型名称 | 模型ID | 提供商 | 用途 |
|----------|--------|--------|------|
| GLM-5 | `glm-5` | 智谱 | 风控和决策支持 |
| MiniMax-M2.5 | `MiniMax-M2.5` | MiniMax | 情绪分析和市场判断 |
| Qwen3.5-Plus | `qwen3.5-plus` | 阿里云 | 主力模型，通用任务 |

---

## 三、测试结果

### 3.1 详细测试记录

#### ✅ GLM-5 (智谱)

| 指标 | 值 |
|------|-----|
| 状态 | ✅ 成功 |
| 响应时间 | **39,637ms** (约40秒) |
| Token使用 | 输入: 165, 输出: 1,287, 总计: 1,452 |
| Reasoning Tokens | 1,231 |
| 评分结果 | strength: 75, confidence: 0.75, direction: LONG |

**响应内容**:
```json
{
  "strength": 75,
  "confidence": 0.75,
  "direction": "LONG",
  "reasoning": "股价站稳均线上方，MACD动能偏多，RSI处于健康强势区，趋势向上。"
}
```

---

#### ✅ MiniMax-M2.5 (MiniMax)

| 指标 | 值 |
|------|-----|
| 状态 | ✅ 成功 |
| 响应时间 | **8,362ms** (约8秒) |
| Token使用 | 输入: 172, 输出: 320, 总计: 492 |
| Reasoning Tokens | 257 |
| 评分结果 | strength: 75, confidence: 0.75, direction: LONG |

**响应内容**:
```json
{
  "strength": 75,
  "confidence": 0.75,
  "direction": "LONG",
  "reasoning": "价格站上MA5和MA20均线，MACD正向且处于多头区域，RSI中性偏多，短期趋势偏强"
}
```

---

#### ✅ Qwen3.5-Plus (阿里云)

| 指标 | 值 |
|------|-----|
| 状态 | ✅ 成功 |
| 响应时间 | **20,700ms** (约21秒) |
| Token使用 | 输入: 19, 输出: 1,051, 总计: 1,070 |
| Reasoning Tokens | 1,029 |

**注意**: Qwen3.5-Plus响应时间较长，建议增加超时时间到60秒以上。

---

### 3.2 测试汇总

| 模型 | 状态 | 响应时间 | 备注 |
|------|------|----------|------|
| GLM-5 | ✅ 成功 | 39.6s | 慢但稳定 |
| MiniMax-M2.5 | ✅ 成功 | 8.4s | 快速响应 |
| Qwen3.5-Plus | ✅ 成功 | 20.7s | 中等速度 |

**总体成功率: 100% (3/3)**

---

## 四、性能分析

### 4.1 响应时间对比

```
MiniMax-M2.5:   8.4s   ████████████ (最快)
Qwen3.5-Plus:  20.7s   ████████████████████████████
GLM-5:         39.6s   ████████████████████████████████████████████████████████ (最慢)
```

### 4.2 Token效率

| 模型 | 输入Tokens | 输出Tokens | 总Tokens |
|------|------------|------------|----------|
| GLM-5 | 165 | 1,287 | 1,452 |
| MiniMax-M2.5 | 172 | 320 | 492 |
| Qwen3.5-Plus | 19 | 1,051 | 1,070 |

---

## 五、建议配置

### 5.1 超时时间配置

```python
# 建议的最小超时时间
MODEL_TIMEOUTS = {
    "glm-5": 60,           # GLM-5 慢，需要60秒
    "MiniMax-M2.5": 30,    # MiniMax 快，30秒足够
    "qwen3.5-plus": 45,    # Qwen 中等，45秒
}
```

### 5.2 模型选择建议

| 场景 | 推荐模型 | 原因 |
|------|----------|------|
| 快速决策 | MiniMax-M2.5 | 响应最快 |
| 深度分析 | GLM-5 | 输出详细 |
| 通用任务 | Qwen3.5-Plus | 平衡性能 |

### 5.3 代码更新建议

```python
# 更新 agent_config.py 或 model_agent.py
class ModelAgent:
    # 增加超时时间
    API_TIMEOUT = 60  # 从25秒增加到60秒
    
    # 或者根据模型动态设置超时
    def _get_timeout(self) -> int:
        if "glm" in self.model_name:
            return 60
        elif "MiniMax" in self.model_name:
            return 30
        else:
            return 45
```

---

## 六、结论

✅ **百炼API Key可以成功调用GLM-5、MiniMax-M2.5、Qwen3.5-Plus三个模型**

⚠️ **注意事项**:
1. GLM-5响应时间较长(~40秒)，建议超时设置60秒以上
2. Qwen3.5-Plus响应时间中等(~20秒)，建议超时设置45秒以上
3. MiniMax-M2.5响应最快(~8秒)，适合快速决策场景

---

## 七、附录：完整模型列表

百炼API支持的模型ID (部分):

```
glm-5                    ✅ 已测试
glm-4.7
MiniMax-M2.5            ✅ 已测试
MiniMax-M2.1
MiniMax/MiniMax-M2.7
qwen3.5-plus            ✅ 已测试
qwen-plus
qwen-turbo
qwen-max
qwen3-max
deepseek-v3
deepseek-r1
kimi-k2.5
```

---

*测试完成时间: 2026-03-26 13:05*