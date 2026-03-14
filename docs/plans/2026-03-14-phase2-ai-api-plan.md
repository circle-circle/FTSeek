# FTSeek 升级 Phase 2: AI 大模型集成方案

**版本**: V2.0 (API 集成版)  
**日期**: 2026-03-14  
**变更**: 从本地 ML 改为调用现有 AI 大模型 API

---

## 🎯 方案调整原因

### 原方案问题
- ❌ 需要本地训练 XGBoost/LSTM
- ❌ 需要 GPU/大量内存资源
- ❌ 维护成本高

### 新方案优势
- ✅ 直接调用现有 AI 大模型 API
- ✅ 无需本地训练和推理
- ✅ 零资源占用
- ✅ 快速集成

---

## 📋 Phase 2 新计划 (API 集成版)

### Task 2.1: AI 服务接口层

**目标**: 创建统一的 AI 服务接口

**文件**:
- `app/ai/__init__.py`
- `app/ai/base.py`
- `app/ai/providers/qwen.py` (阿里云通义千问)
- `app/ai/providers/glm.py` (智谱 GLM)

**功能**:
```python
class AIService:
    def analyze_sentiment(self, text: str) -> float:
        """情绪分析"""
        pass
    
    def extract_features(self, data: dict) -> dict:
        """特征提取"""
        pass
    
    def predict_direction(self, code: str, context: dict) -> dict:
        """价格方向预测"""
        pass
```

**预计**: 3 小时

---

### Task 2.2: 提示词工程

**目标**: 设计优化的金融分析提示词

**文件**:
- `app/ai/prompts.py`

**提示词模板**:
```python
SENTIMENT_ANALYSIS = """
分析以下新闻的情绪倾向，返回 -1 到 1 之间的分数：
新闻：{text}
分数："""

MARKET_PREDICTION = """
基于以下信息预测股票短期走势：
- 代码：{code}
- 技术指标：{indicators}
- 资金流：{capital_flow}
- 情绪：{sentiment}
- 市场环境：{market_context}

请返回：
1. 方向：上涨/下跌/震荡
2. 置信度：0-100%
3. 理由：简要说明"""
```

**预计**: 4 小时

---

### Task 2.3: AI 增强情绪因子

**目标**: 用 AI API 替代简单词典法

**文件**:
- `app/factors/sentiment_ai.py`

**功能**:
```python
class AISentimentFactor(BaseFactor):
    def __init__(self, api_provider: str = 'qwen'):
        self.ai_service = get_ai_service(api_provider)
    
    def calculate(self, code: str, data: dict):
        # 调用 AI API 分析新闻情绪
        news_list = data.get('news', [])
        scores = []
        
        for news in news_list:
            text = f"{news['title']} {news['content']}"
            score = self.ai_service.analyze_sentiment(text)
            scores.append(score)
        
        return FactorResult(
            code=code,
            factor_name='sentiment_ai',
            value=sum(scores) / len(scores) if scores else 0.0,
            metadata={'ai_provider': api_provider, 'count': len(scores)}
        )
```

**预计**: 2 小时

---

### Task 2.4: AI 市场预测因子

**目标**: 基于 AI 的市场方向预测

**文件**:
- `app/factors/market_prediction.py`

**功能**:
```python
class AIPredictionFactor(BaseFactor):
    def calculate(self, code: str, data: dict):
        # 准备上下文
        context = {
            'code': code,
            'indicators': self._extract_indicators(data),
            'capital_flow': self._extract_flow(data),
            'price_history': data.get('klines', [])[-20:]
        }
        
        # 调用 AI API 预测
        result = self.ai_service.predict_direction(code, context)
        
        # 转换为因子值 (-1 到 1)
        direction_map = {'上涨': 1, '震荡': 0, '下跌': -1}
        score = direction_map.get(result['direction'], 0)
        confidence = result['confidence'] / 100
        
        return FactorResult(
            code=code,
            factor_name='ai_prediction',
            value=score * confidence,
            metadata=result
        )
```

**预计**: 3 小时

---

### Task 2.5: AI 融合策略 (FusionV3)

**目标**: 将 AI 预测集成到 FusionV2

**文件**:
- `app/strategy/fusion_v3.py`

**融合逻辑**:
```python
class FusionV3Strategy(FusionV2Strategy):
    """融合策略 V3 - AI 增强版"""
    
    def __init__(self, config=None):
        super().__init__(config)
        self.ai_service = get_ai_service()
        self.ai_weight = config.get('ai_weight', 0.3) if config else 0.3
    
    def generate_signal(self, code: str, quote: dict):
        # 1. 传统因子计算 (技术面 40% + 情绪面 30%)
        traditional_score = super().generate_signal(code, quote)
        
        # 2. AI 预测 (30%)
        ai_result = self.ai_service.predict_direction(code, self._build_context(code, quote))
        ai_score = self._normalize_ai_score(ai_result)
        
        # 3. 融合
        final_score = (
            traditional_score * (1 - self.ai_weight) +
            ai_score * self.ai_weight
        )
        
        return self._create_signal(code, final_score, ai_result)
```

**预计**: 4 小时

---

### Task 2.6: 特性开关配置

**目标**: 通过环境变量控制 AI 功能

**文件**:
- `.env.example` (更新)
- `app/config.py` (更新)

**配置项**:
```bash
# AI 服务配置
FTSEEK_ENABLE_AI=false  # 默认关闭

# API Provider 选择
AI_PROVIDER=qwen  # qwen/glm/bailian

# API Key (从对应平台获取)
QWEN_API_KEY=your_key_here
GLM_API_KEY=your_key_here

# AI 权重
FTSEEK_AI_WEIGHT=0.3

# 限流配置
AI_REQUESTS_PER_MINUTE=10
AI_DAILY_BUDGET=100  # 每日最大调用次数
```

**预计**: 1 小时

---

### Task 2.7: 缓存层

**目标**: 减少 API 调用，降低成本

**文件**:
- `app/ai/cache.py`

**功能**:
```python
class AICache:
    def __init__(self, ttl: int = 3600):
        self.ttl = ttl  # 缓存 1 小时
        self._cache = {}
    
    def get(self, key: str) -> Optional[dict]:
        """获取缓存"""
        if key in self._cache:
            data, timestamp = self._cache[key]
            if time.time() - timestamp < self.ttl:
                return data
            del self._cache[key]
        return None
    
    def set(self, key: str, data: dict):
        """设置缓存"""
        self._cache[key] = (data, time.time())
```

**使用场景**:
- 相同股票的重复查询
- 相同新闻的情绪分析
- 历史数据不变化时

**预计**: 2 小时

---

### Task 2.8: 成本监控

**目标**: 监控 API 调用和成本

**文件**:
- `app/ai/cost_tracker.py`

**功能**:
```python
class CostTracker:
    def __init__(self):
        self.daily_calls = 0
        self.daily_cost = 0.0
        self.budget = float(os.getenv('AI_DAILY_BUDGET', 100))
    
    def record_call(self, tokens_used: int, cost: float):
        self.daily_calls += 1
        self.daily_cost += cost
        
        if self.daily_cost > self.budget:
            logger.warning(f"AI 预算超支：{self.daily_cost:.2f} > {self.budget}")
            # 可触发限流
    
    def get_status(self) -> dict:
        return {
            'daily_calls': self.daily_calls,
            'daily_cost': self.daily_cost,
            'budget': self.budget,
            'remaining': self.budget - self.daily_cost
        }
```

**预计**: 2 小时

---

### Task 2.9: 测试

**目标**: 完整测试 AI 集成功能

**文件**:
- `tests/ai/test_ai_service.py`
- `tests/ai/test_factors.py`
- `tests/ai/test_fusion_v3.py`

**测试用例**:
- AI 服务连接测试
- 情绪分析准确性
- 预测因子计算
- FusionV3 融合逻辑
- 缓存功能
- 成本监控

**预计**: 4 小时

---

### Task 2.10: 文档和部署

**目标**: 完成部署文档

**文件**:
- `docs/AI_INTEGRATION_GUIDE.md`

**内容**:
- API Key 获取指南
- 配置步骤
- 使用示例
- 成本估算
- 最佳实践

**预计**: 2 小时

---

## 📊 新方案对比

| 维度 | 原方案 (本地 ML) | 新方案 (AI API) |
|------|-----------------|----------------|
| **资源需求** | GPU/大量内存 | 零本地资源 |
| **开发周期** | 2-3 周 | 3-5 天 |
| **维护成本** | 高 (模型训练/更新) | 低 (API 调用) |
| **准确率** | 依赖训练数据 | 依赖大模型能力 |
| **成本** | 硬件 + 电力 | API 调用费 |
| **灵活性** | 低 (重新训练) | 高 (切换模型) |

---

## 💰 成本估算

### API 调用成本 (参考)

| Provider | 价格 | 预估日用量 | 月成本 |
|----------|------|-----------|--------|
| **Qwen** | ¥0.008/1K tokens | 1000 次 × 500 tokens | ¥120 |
| **GLM** | ¥0.005/1K tokens | 1000 次 × 500 tokens | ¥75 |
| **Bailian** | ¥0.01/1K tokens | 1000 次 × 500 tokens | ¥150 |

### 优化措施
- ✅ 缓存层：减少 50%+ 调用
- ✅ 限流：控制每日预算
- ✅ 批量调用：降低单次成本

**预估月成本**: ¥50-150 (有缓存和限流)

---

## 🚀 实施时间线

```
Day 1: Task 2.1-2.3 (AI 服务层 + 提示词 + 情绪因子)
Day 2: Task 2.4-2.6 (预测因子 + FusionV3 + 配置)
Day 3: Task 2.7-2.10 (缓存 + 监控 + 测试 + 文档)
```

**总周期**: 3 天  
**总工时**: 27 小时

---

## ✅ 验收标准

- [ ] AI 服务可正常调用
- [ ] 情绪因子使用 AI 分析
- [ ] 预测因子返回有效结果
- [ ] FusionV3 融合成功
- [ ] 缓存层工作正常
- [ ] 成本监控准确
- [ ] 所有测试通过
- [ ] 文档完整

---

## 🎯 下一步

**请确认**:
1. 使用哪个 AI Provider? (Qwen/GLM/Bailian/其他)
2. API Key 是否已准备好？
3. 每日预算限制？(建议 ¥100 起步)
4. 是否立即开始 Task 2.1?

---

*方案版本：V2.0 (API 集成版)*  
*创建时间：2026-03-14*  
*状态：待确认*
