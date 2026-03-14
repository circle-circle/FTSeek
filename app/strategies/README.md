# FTSeek 策略引擎 v3.0

三策略引擎实现，支持统一接口和可配置参数。

## 📋 策略列表

### 1. 融合策略 v2 (FusionV2Strategy)

**策略逻辑**:
- 技术指标计算 (RSI/MACD/均线/成交量/布林带)
- 集成 Task 5 的情绪分析 (大模型新闻分析)
- 融合公式：`score = technical × 0.6 + sentiment × 0.4`

**配置参数**:
```yaml
fusion_v2:
  enabled: true
  weight: 0.5
  parameters:
    technical_weight: 0.6      # 技术权重
    sentiment_weight: 0.4      # 情绪权重
    buy_threshold: 0.65        # 买入阈值
    rsi_period: 14
    macd_fast: 12
    macd_slow: 26
```

**信号示例**:
```python
Signal(
    code='HK.00700',
    action=SignalAction.BUY,
    reason='融合评分=0.72 | 技术分=0.65 (60%) + 情绪分=0.45 (40%) | 等级=recommended',
    extra={
        'technical_score': 0.65,
        'sentiment_score': 0.45,
        'final_score': 0.72,
        'signal_level': 'recommended'
    }
)
```

---

### 2. 均线交叉策略 (MACrossStrategy)

**策略逻辑**:
- MA5 上穿 MA20 → 买入信号 (金叉)
- MA5 下穿 MA20 → 卖出信号 (死叉)
- 成交量过滤：成交量 > 1.5 倍均量才触发

**配置参数**:
```yaml
ma_cross:
  enabled: true
  weight: 0.3
  parameters:
    fast_ma: 5               # 快线周期
    slow_ma: 20              # 慢线周期
    volume_filter: 1.5       # 成交量过滤倍数
    min_confidence: 0.5
```

**信号示例**:
```python
Signal(
    code='HK.00700',
    action=SignalAction.BUY,
    reason='MA5(102.5) 上穿 MA20(98.3), 金叉买入，成交量 2500000',
    extra={
        'fast_ma': 102.5,
        'slow_ma': 98.3,
        'volume': 2500000,
        'confidence': 0.75
    }
)
```

---

### 3. RSI 反弹策略 (RSIReboundStrategy)

**策略逻辑**:
- RSI < 25 → 买入信号 (超卖反弹)
- RSI > 75 → 卖出信号 (超买回落)
- 持仓时间 ≤ 3 天 (超时强制平仓)

**配置参数**:
```yaml
rsi_rebound:
  enabled: true
  weight: 0.2
  parameters:
    rsi_period: 14           # RSI 周期
    oversold: 25             # 超卖阈值
    overbought: 75           # 超买阈值
    max_holding_days: 3      # 最大持仓天数
    min_confidence: 0.5
```

**信号示例**:
```python
Signal(
    code='HK.00700',
    action=SignalAction.BUY,
    reason='RSI(14)=28.5 从超卖区 (25) 反弹',
    extra={
        'rsi': 28.5,
        'prev_rsi': 22.3,
        'confidence': 0.68,
        'signal_type': 'oversold_rebound'
    }
)
```

---

## 🔧 统一接口

所有策略实现统一接口：

```python
from app.strategies import create_strategy, MarketData

# 创建策略
strategy = create_strategy('ma_cross', {
    'fast_ma': 5,
    'slow_ma': 20,
    'enabled': True
})

# 准备市场数据
market_data = MarketData(
    code='HK.00700',
    name='腾讯控股',
    price=380.5,
    volume=2500000,
    klines=[...],  # K 线数据列表
    timestamp=time.time()
)

# 生成信号
signal = strategy.generate_signal(market_data)

# 获取配置
config = strategy.get_config()
```

---

## 📊 策略配置

配置文件：`/opt/ftseek/FTSeek/app/strategies/config.yaml`

```yaml
strategies:
  fusion_v2:
    enabled: true
    weight: 0.5
    parameters:
      technical_weight: 0.6
      sentiment_weight: 0.4
      buy_threshold: 0.65
  
  ma_cross:
    enabled: true
    weight: 0.3
    parameters:
      fast_ma: 5
      slow_ma: 20
      volume_filter: 1.5
  
  rsi_rebound:
    enabled: true
    weight: 0.2
    parameters:
      rsi_period: 14
      oversold: 25
      overbought: 75
      max_holding_days: 3

global:
  default_quantity: 100
  signal_expire_seconds: 900
  max_daily_signals: 20
```

---

## 🧪 单元测试

运行测试：
```bash
cd /opt/ftseek/FTSeek
export API_TOKEN=test123
export FTSEEK_DEV_MODE=1

python3 -m unittest tests.test_strategies -v
```

测试覆盖：
- ✓ 配置加载
- ✓ 策略初始化
- ✓ 信号生成
- ✓ 技术指标计算
- ✓ 统一接口
- ✓ 集成测试

---

## 📈 使用示例

### 示例 1: 单策略使用

```python
from app.strategies import create_strategy
from app.strategies.ma_cross import MarketData

# 创建均线交叉策略
strategy = create_strategy('ma_cross', {
    'fast_ma': 5,
    'slow_ma': 20,
    'volume_filter': 1.5
})

# 准备数据
klines = [...]  # K 线数据
market_data = MarketData(
    code='HK.00700',
    name='腾讯控股',
    price=380.5,
    volume=2500000,
    klines=klines,
    timestamp=time.time()
)

# 生成信号
signal = strategy.generate_signal(market_data)
if signal:
    print(f"信号：{signal.action} {signal.code}")
    print(f"原因：{signal.reason}")
```

### 示例 2: 多策略并行

```python
from app.strategies import get_all_strategies

# 获取所有启用的策略
strategies = get_all_strategies()

# 并行生成信号
for name, strategy in strategies.items():
    signal = strategy.generate_signal(market_data)
    if signal:
        print(f"[{name}] {signal.action} {signal.code}")
```

### 示例 3: 动态调整配置

```python
# 更新策略配置
strategy.update_config({
    'fast_ma': 10,
    'slow_ma': 30,
    'enabled': True
})

# 验证配置
print(strategy.get_config())
```

---

## 🎯 验收标准

- [x] 3 个策略全部实现
- [x] 统一接口 (`generate_signal`, `get_config`)
- [x] 配置可调整 (YAML)
- [x] 单元测试通过 (20/20)

---

## 📝 注意事项

1. **情绪分析依赖**: FusionV2Strategy 依赖 Task 5 的 NewsAnalyzer，需要配置 `QWEN_API_KEY`
2. **数据要求**: 所有策略需要足够的 K 线数据 (至少 30 条)
3. **成交量过滤**: MA Cross 策略默认启用成交量过滤，可通过配置关闭
4. **持仓跟踪**: RSI Rebound 策略会跟踪持仓时间，超时自动平仓

---

## 🔗 相关文档

- [策略配置指南](../../../docs/STRATEGY_CONFIG.md)
- [Task 5: 新闻分析器](../analyzers/NEWS_ANALYZER_README.md)
- [重构计划](../../../docs/plans/2026-03-01-ftseek-v3-refactor-plan.md)
