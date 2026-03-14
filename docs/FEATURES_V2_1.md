# FTSeek v2.1 模块化版本 - 功能总结

**更新日期**: 2026-02-27  
**版本**: v2.0 → v2.1  
**核心改进**: 完全模块化设计 + 方向概率显示

---

## 🎯 核心特性

### 1. 完全模块化设计 ✅

融合引擎由 4 个独立模块组成，所有模块可**独立配置和热插拔**:

```
FusionEngine
├── TechnicalIndicatorModule    # 技术指标模块
│   ├── RSI 评分
│   ├── MACD 评分
│   ├── MA 评分
│   ├── Volume 评分
│   └── Bollinger 评分
│
├── SentimentAnalysisModule     # 情绪分析模块
│   ├── 新闻抓取
│   ├── 情绪分析
│   └── 置信度计算
│
├── SignalFusionModule          # 信号融合模块
│   ├── 权重融合 (60% 技术 + 40% 情绪)
│   ├── 方向概率计算
│   └── 信号等级判定
│
└── SignalOutputModule          # 信号输出模块
    ├── 信号过滤
    ├── 格式化输出
    └── 钉钉消息生成
```

**优势**:
- ✅ 每个模块可独立替换
- ✅ 配置参数集中管理
- ✅ 支持热插拔 (无需重启)
- ✅ 易于测试和维护

---

### 2. 信号方向概率显示 ✅

**改进前**: 只有简单的 BUY/SELL 信号

**改进后**: 
```
方向：BUY
概率：
买入：███████ 75%
卖出：██ 25%
置信度：80%
等级：strong
```

**概率计算逻辑**:
```python
# 将 -1 到 1 的分数映射到 0-1 概率
buy_probability = (final_score + 1) / 2
sell_probability = 1 - buy_probability

# 根据信号等级调整显示
if signal_level == 'weak':
    # 弱信号时概率趋近 50%
    buy_probability = 0.5 + (buy_probability - 0.5) * 0.5
```

**测试数据**:
| 场景 | 技术分 | 情绪分 | 买入概率 | 卖出概率 | 决策 |
|------|--------|--------|----------|----------|------|
| 强烈看涨 | +0.85 | +0.60 | 87.5% | 12.5% | BUY |
| 强烈看跌 | -0.72 | -0.50 | 18.4% | 81.6% | SELL |
| 中性震荡 | +0.08 | -0.10 | 50.2% | 49.8% | NEUTRAL |
| 技术强情绪弱 | +0.88 | 0.00 | 76.4% | 23.6% | BUY |

---

### 3. 信号等级分类 ✅

| 等级 | 分数范围 | 买入概率 | 操作建议 |
|------|----------|----------|----------|
| **strong** | >0.8 | >90% | 强烈推荐，高优先级 |
| **recommended** | 0.6-0.8 | 70-90% | 推荐，正常推送 |
| **neutral** | 0.4-0.6 | 60-70% | 中性，可选推送 |
| **weak** | <0.4 | 50-60% | 弱信号，不推送 |

---

## 🔧 配置方式

### 方式 1: 环境变量 (推荐)

```bash
# 融合权重
export TECHNICAL_WEIGHT=0.6
export SENTIMENT_WEIGHT=0.4

# 信号等级阈值
export THRESHOLD_STRONG=0.8
export THRESHOLD_RECOMMENDED=0.6
export THRESHOLD_NEUTRAL=0.4

# 输出配置
export MIN_CONFIDENCE=0.4
export MIN_SIGNAL_LEVEL=neutral

# 指标权重
export INDICATOR_WEIGHTS='{"rsi":0.25,"macd":0.25,"ma":0.20,"volume":0.15,"bollinger":0.15}'
```

### 方式 2: 配置文件

`config/system_config.yaml`:
```yaml
fusion_engine:
  technical:
    weights:
      rsi: 0.25
      macd: 0.25
      ma: 0.20
      volume: 0.15
      bollinger: 0.15
  fusion:
    technical_weight: 0.6
    sentiment_weight: 0.4
    thresholds:
      strong: 0.8
      recommended: 0.6
      neutral: 0.4
  output:
    min_confidence: 0.4
    min_signal_level: neutral
```

### 方式 3: 代码动态配置

```python
from app.signal.fusion_engine import reset_fusion_engine

config = {
    'fusion': {
        'technical_weight': 0.7,  # 调整技术权重到 70%
        'sentiment_weight': 0.3,
        'thresholds': {
            'strong': 0.75  # 降低强烈推荐阈值
        }
    }
}

engine = reset_fusion_engine(config)
```

---

## 📊 钉钉消息格式

### 强烈看涨信号

```
🔴 HK.00700 BUY 信号

标的：HK.00700 腾讯控股

方向：BUY

概率:
买入：█████████ 87.5%
卖出：█ 12.5%

置信度：83.0%
等级：recommended

技术分：+0.85
情绪分：+0.60

原因：方向:BUY | 置信度:83% | 技术分:+0.85 | 情绪分:+0.60 | RSI:25.0 | MACD:金叉

⏰ 有效期 15 分钟

[✅ 确认执行] [⏰ 延期 15 分钟] [❌ 忽略]
```

### 强烈看跌信号

```
🟢 HK.00700 SELL 信号

标的：HK.00700 腾讯控股

方向：SELL

概率:
买入：███ 18.4%
卖出：█████████████ 81.6%

置信度：71.2%
等级：recommended

技术分：-0.72
情绪分：-0.50

原因：方向:SELL | 置信度:71% | 技术分:-0.72 | 情绪分:-0.50 | RSI:75.0 | MACD:死叉

⏰ 有效期 15 分钟

[✅ 确认执行] [⏰ 延期 15 分钟] [❌ 忽略]
```

---

## 🧪 测试结果

### 模块化测试

```bash
$ python3 test_modular.py

======================================================================
  FTSeek v2.1 模块化引擎 - 方向概率测试
======================================================================

📦 已加载模块:
  - 技术指标：TechnicalIndicatorModule
  - 情绪分析：SentimentAnalysisModule
  - 信号融合：SignalFusionModule
  - 信号输出：SignalOutputModule

⚙️  当前配置:
  技术权重：60%
  情绪权重：40%
  阈值：strong=0.8

🧪 信号生成测试:
  ✅ 强烈看涨：BUY 87.5% / SELL 12.5%
  ✅ 强烈看跌：BUY 18.4% / SELL 81.6%
  ✅ 中性震荡：BUY 50.2% / SELL 49.8%
  ✅ 技术强情绪弱：BUY 76.4% / SELL 23.6%

概率可视化对比:
  强烈看涨     | 🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🔴🔴  | BUY 87.5% SELL 12.5%
  强烈看跌     | 🟢🟢🟢🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴  | BUY 18.4% SELL 81.6%
  中性震荡     | 🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🔴🔴🔴🔴🔴🔴🔴🔴🔴  | BUY 50.2% SELL 49.8%
  技术强情绪弱   | 🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🟢🔴🔴🔴🔴  | BUY 76.4% SELL 23.6%

✅ 所有测试完成
```

---

## 📁 修改文件清单

### 核心模块更新

| 文件 | 变更 | 说明 |
|------|------|------|
| `app/signal/fusion_engine.py` | 🔴 完全重写 | 模块化设计，4 个独立模块 |
| `app/strategy/fusion_v2.py` | 🟡 更新 | 使用新模块化引擎 |
| `app/signal/__init__.py` | 🟢 新增 | 导出新模块 |

### 新增文档

| 文件 | 说明 |
|------|------|
| `docs/MODULAR_CONFIG.md` | 模块化配置指南 |
| `test_modular.py` | 模块化测试脚本 |

---

## 🚀 快速验证

```bash
# 1. 运行测试
cd /opt/ftseek/FTSeek
export API_TOKEN=test123
export FTSEEK_DEV_MODE=1
python3 test_modular.py

# 2. 验证模块加载
python3 -c "
from app.signal.fusion_engine import get_fusion_engine
engine = get_fusion_engine()
print(f'技术模块：{type(engine.technical_module).__name__}')
print(f'情绪模块：{type(engine.sentiment_module).__name__}')
print(f'融合模块：{type(engine.fusion_module).__name__}')
print(f'输出模块：{type(engine.output_module).__name__}')
"

# 3. 验证概率显示
python3 -c "
from app.signal.fusion_engine import TechnicalScore, SentimentScore, get_fusion_engine
engine = get_fusion_engine()
tech = TechnicalScore(composite_score=0.8)
sent = SentimentScore(score=0.6, confidence=0.8)
final, conf, level, buy, sell = engine.fusion_module.fuse(tech, sent)
print(f'BUY 概率：{buy:.1%}')
print(f'SELL 概率：{sell:.1%}')
"
```

---

## 🎯 常见配置场景

### 场景 1: 更保守 (只推送高置信度)

```bash
export MIN_CONFIDENCE=0.6
export MIN_SIGNAL_LEVEL=strong
export THRESHOLD_STRONG=0.7
```

### 场景 2: 更激进 (更多交易机会)

```bash
export MIN_CONFIDENCE=0.3
export MIN_SIGNAL_LEVEL=neutral
export PER_STOCK_DAILY_LIMIT=5
```

### 场景 3: 重视技术面

```bash
export TECHNICAL_WEIGHT=0.7
export SENTIMENT_WEIGHT=0.3
export INDICATOR_WEIGHTS='{"rsi":0.30,"macd":0.30,"ma":0.20,"volume":0.10,"bollinger":0.10}'
```

### 场景 4: 重视情绪面

```bash
export TECHNICAL_WEIGHT=0.5
export SENTIMENT_WEIGHT=0.5
```

---

## 📚 文档索引

| 文档 | 用途 |
|------|------|
| `docs/MODULAR_CONFIG.md` | 模块化配置指南 |
| `QUICKSTART_V2.md` | 快速启动指南 |
| `docs/UPGRADE_GUIDE.md` | 升级指南 |
| `docs/COMPLETION_SUMMARY.md` | 完成总结 |
| `PROJECT_STATUS.md` | 项目状态 |

---

## ✅ 验收标准

| 功能 | 状态 | 验证方法 |
|------|------|----------|
| 模块化设计 | ✅ | 4 个独立模块，可获取/替换 |
| 方向概率显示 | ✅ | BUY%/SELL% 概率显示 |
| 信号等级 | ✅ | strong/recommended/neutral/weak |
| 环境变量配置 | ✅ | 支持所有关键参数 |
| 配置文件 | ✅ | YAML 格式支持 |
| 动态配置 | ✅ | 支持热插拔 |
| 钉钉消息 | ✅ | 概率条可视化 |
| 测试脚本 | ✅ | test_modular.py 通过 |

**验收结果**: ✅ **全部通过**

---

## 🎉 总结

FTSeek v2.1 实现了:

1. ✅ **完全模块化设计** - 4 个独立模块，可热插拔
2. ✅ **方向概率显示** - BUY%/SELL% 概率可视化
3. ✅ **信号等级分类** - strong/recommended/neutral/weak
4. ✅ **多种配置方式** - 环境变量/配置文件/代码动态
5. ✅ **完善的测试** - 模块化测试脚本

**核心优势**:
- 🔧 **易调整** - 所有参数可配置
- 🔌 **易扩展** - 模块可独立替换
- 📊 **易理解** - 概率显示直观清晰
- 🧪 **易测试** - 模块化便于单元测试

---

**状态**: 🎉 **已完成，可立即使用!**
