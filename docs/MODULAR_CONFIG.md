# FTSeek v2.1 模块化配置指南

**核心特性**: 完全模块化设计，所有组件可独立配置和替换

---

## 🏗️ 模块化架构

```
融合引擎 v2.1
├── TechnicalIndicatorModule (技术指标模块)
│   ├── RSI 评分
│   ├── MACD 评分
│   ├── MA 评分
│   ├── Volume 评分
│   └── Bollinger 评分
├── SentimentAnalysisModule (情绪分析模块)
│   ├── 新闻抓取
│   ├── 情绪分析
│   └── 置信度计算
├── SignalFusionModule (信号融合模块)
│   ├── 权重融合
│   ├── 方向概率计算
│   └── 信号等级判定
└── SignalOutputModule (信号输出模块)
    ├── 信号过滤
    ├── 格式化输出
    └── 钉钉消息生成
```

**所有模块可独立替换和配置!**

---

## 📝 配置方式

### 方式 1: 环境变量 (推荐)

```bash
# 融合权重
export TECHNICAL_WEIGHT=0.6        # 技术指标权重 (默认 60%)
export SENTIMENT_WEIGHT=0.4        # 情绪权重 (默认 40%)

# 信号等级阈值
export THRESHOLD_STRONG=0.8        # 强烈推荐阈值
export THRESHOLD_RECOMMENDED=0.6   # 推荐阈值
export THRESHOLD_NEUTRAL=0.4       # 中性阈值

# 输出配置
export MIN_CONFIDENCE=0.4          # 最小置信度
export MIN_SIGNAL_LEVEL=neutral    # 最小信号等级

# 技术指标权重
export INDICATOR_WEIGHTS='{"rsi":0.25,"macd":0.25,"ma":0.20,"volume":0.15,"bollinger":0.15}'

# 频率限制
export PER_STOCK_DAILY_LIMIT=3     # 单股每日最多 3 次
export GLOBAL_DAILY_LIMIT=20       # 全局每日最多 20 次
```

### 方式 2: 配置文件

编辑 `config/system_config.yaml`:

```yaml
# 融合引擎配置
fusion_engine:
  # 技术指标模块
  technical:
    weights:
      rsi: 0.25
      macd: 0.25
      ma: 0.20
      volume: 0.15
      bollinger: 0.15
    rsi_period: 14
    rsi_overbought: 70
    rsi_oversold: 30
  
  # 情绪分析模块
  sentiment:
    timeout: 10
    min_news_count: 1
    lookback_days: 3
  
  # 融合模块
  fusion:
    technical_weight: 0.6
    sentiment_weight: 0.4
    thresholds:
      strong: 0.8
      recommended: 0.6
      neutral: 0.4
  
  # 输出模块
  output:
    min_confidence: 0.4
    min_signal_level: neutral
```

### 方式 3: 代码动态配置

```python
from app.signal.fusion_engine import reset_fusion_engine

# 自定义配置
config = {
    'fusion': {
        'technical_weight': 0.7,  # 调整技术权重到 70%
        'sentiment_weight': 0.3,
        'thresholds': {
            'strong': 0.75  # 降低强烈推荐阈值
        }
    },
    'output': {
        'min_confidence': 0.5  # 提高最小置信度
    }
}

# 重置引擎
engine = reset_fusion_engine(config)
```

---

## 📊 信号展现格式

### 钉钉消息示例

```
🔴 HK.00700 BUY 信号

标的：HK.00700 腾讯控股

方向：BUY

概率:
买入：███████ 75%
卖出：██ 25%

置信度：80%
等级：strong
技术分：+0.65
情绪分：+0.40

原因：方向:BUY | 置信度:80% | 技术分:+0.65 | 情绪分:+0.40 | RSI:35.0 | MACD:金叉

⏰ 有效期 15 分钟

[✅ 确认执行] [⏰ 延期 15 分钟] [❌ 忽略]
```

### API 返回格式

```json
{
  "code": "HK.00700",
  "name": "腾讯控股",
  "price": 520.00,
  "direction": "BUY",
  "buy_probability": "75.0%",
  "sell_probability": "25.0%",
  "confidence": "80.0%",
  "signal_level": "strong",
  "technical_score": "+0.65",
  "sentiment_score": "+0.40",
  "reason": "方向:BUY | 置信度:80% | ...",
  "strategy": "fusion_v2"
}
```

---

## 🔧 模块替换指南

### 替换技术指标模块

```python
from app.signal.fusion_engine import get_fusion_engine, TechnicalIndicatorModule

# 自定义技术指标模块
class MyTechnicalModule(TechnicalIndicatorModule):
    def calculate(self, code, indicators):
        # 自定义计算逻辑
        score = super().calculate(code, indicators)
        # 添加自定义指标
        score.custom_score = self._calc_custom(indicators)
        return score

# 替换模块
engine = get_fusion_engine()
engine.replace_module('technical', MyTechnicalModule())
```

### 替换情绪分析模块

```python
from app.signal.fusion_engine import SentimentAnalysisModule

class MySentimentModule(SentimentAnalysisModule):
    def calculate(self, code):
        # 使用自定义数据源
        news = self._fetch_from_custom_source(code)
        score = self._analyze(news)
        return score

# 替换
engine = get_fusion_engine()
engine.replace_module('sentiment', MySentimentModule())
```

### 替换融合模块

```python
from app.signal.fusion_engine import SignalFusionModule

class MyFusionModule(SignalFusionModule):
    def fuse(self, technical, sentiment):
        # 自定义融合算法
        final_score = self._custom_fuse(technical, sentiment)
        # 计算方向概率
        buy_prob = self._calc_buy_prob(final_score)
        return final_score, confidence, level, buy_prob, 1-buy_prob

# 替换
engine = get_fusion_engine()
engine.replace_module('fusion', MyFusionModule())
```

---

## 📈 常见配置场景

### 场景 1: 更保守的信号 (只推送高置信度)

```bash
export MIN_CONFIDENCE=0.6         # 提高置信度阈值到 60%
export MIN_SIGNAL_LEVEL=strong    # 只推送强烈推荐
export THRESHOLD_STRONG=0.7       # 降低强烈推荐阈值
```

### 场景 2: 更激进的信号 (更多交易机会)

```bash
export MIN_CONFIDENCE=0.3         # 降低置信度阈值到 30%
export MIN_SIGNAL_LEVEL=neutral   # 推送中性以上信号
export PER_STOCK_DAILY_LIMIT=5    # 提高单股限制
```

### 场景 3: 重视技术面 (技术 70% + 情绪 30%)

```bash
export TECHNICAL_WEIGHT=0.7
export SENTIMENT_WEIGHT=0.3
```

### 场景 4: 重视情绪面 (技术 50% + 情绪 50%)

```bash
export TECHNICAL_WEIGHT=0.5
export SENTIMENT_WEIGHT=0.5
```

### 场景 5: 调整指标权重 (更重视 RSI 和 MACD)

```bash
export INDICATOR_WEIGHTS='{
  "rsi": 0.35,
  "macd": 0.35,
  "ma": 0.15,
  "volume": 0.10,
  "bollinger": 0.05
}'
```

---

## 🧪 测试配置

### 测试当前配置

```bash
export API_TOKEN=test123
export FTSEEK_DEV_MODE=1

python3 -c "
from app.signal.fusion_engine import get_fusion_engine

engine = get_fusion_engine()
print(f'技术权重：{engine.fusion_module.technical_weight:.0%}')
print(f'情绪权重：{engine.fusion_module.sentiment_weight:.0%}')
print(f'阈值：strong={engine.fusion_module.thresholds[\"strong\"]}')
"
```

### 测试信号生成

```bash
python3 -c "
from app.signal.fusion_engine import get_fusion_engine

engine = get_fusion_engine()
indicators = {
    'current_price': 520, 'rsi': 35, 'macd': 2.5,
    'ma_fast': 515, 'ma_slow': 500,
    'volume': 1500000, 'volume_avg': 1000000,
    'bollinger_upper': 540, 'bollinger_lower': 500
}

result = engine.generate_signal('HK.00700', '腾讯', 520, indicators)
if result:
    print(f'方向：{result.direction}')
    print(f'概率：BUY {result.buy_probability:.1%} / SELL {result.sell_probability:.1%}')
    print(f'置信度：{result.confidence:.1%}')
    print(f'等级：{result.signal_level}')
"
```

---

## 🎯 信号等级说明

| 等级 | 分数范围 | 概率范围 | 操作建议 |
|------|----------|----------|----------|
| **strong** | >0.8 | >90% / <10% | 强烈推荐，高优先级推送 |
| **recommended** | 0.6-0.8 | 70-90% / 30-10% | 推荐，正常推送 |
| **neutral** | 0.4-0.6 | 60-70% / 40-30% | 中性，可选推送 |
| **weak** | <0.4 | 50-60% / 50-40% | 弱信号，不推送 |

---

## 📚 更多资源

- **快速启动**: `QUICKSTART_V2.md`
- **升级指南**: `docs/UPGRADE_GUIDE.md`
- **完成总结**: `docs/COMPLETION_SUMMARY.md`

---

**模块化设计，让调整和修改变得简单快捷!** 🚀
