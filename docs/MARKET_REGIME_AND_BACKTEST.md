# 🎯 市场状态识别 + 回测框架

**实施日期**: 2026-02-26  
**版本**: v1.0

---

## ✅ 已完成模块

### 1. 市场状态识别 (Market Regime Detection)

**文件**: `app/strategy/market_regime.py`

#### 功能特性

- **5 种市场状态**:
  - `STRONG_TREND_UP` - 强上涨趋势
  - `WEAK_TREND_UP` - 弱上涨趋势
  - `RANGE_BOUND` - 震荡市
  - `WEAK_TREND_DOWN` - 弱下跌趋势
  - `STRONG_TREND_DOWN` - 强下跌趋势
  - `HIGH_VOLATILITY` - 高波动
  - `LOW_VOLATILITY` - 低波动

- **判断指标**:
  - ADX (趋势强度)
  - 价格 vs MA50 (趋势方向)
  - ATR 百分位 (波动率)
  - 布林带宽度 (波动率确认)

- **策略推荐**: 根据市场状态推荐最优策略及权重

#### 使用示例

```python
from app.strategy.market_regime import market_regime_detector

# 更新数据
detector = market_regime_detector
detector.update_data("HK.00700", kline)

# 检测市场状态
regime = detector.detect("HK.00700")

print(f"市场状态：{regime.regime_type.value}")
print(f"趋势方向：{regime.trend_direction.value}")
print(f"波动率：{regime.volatility_state.value}")
print(f"置信度：{regime.confidence:.2f}")

# 获取推荐策略
recommendations = detector.get_recommended_strategies(regime)
print(f"推荐策略：{recommendations}")
```

#### 输出示例

```
[HK.00700] 市场状态：strong_trend_up | ADX=32.5 | ATR%=75.2 | 趋势=strong_up | 置信度=0.85
```

---

### 2. 回测框架 (Backtesting Framework)

**文件**: 
- `app/backtest/backtester.py` - 回测引擎核心
- `app/backtest/data_loader.py` - 数据加载器
- `app/backtest/api.py` - API 接口

#### 功能特性

- **单策略回测**: 评估单个策略性能
- **多策略对比**: 同时对比多个策略
- **性能指标**: 15+ 专业量化指标
- **交易记录**: 完整的交易历史
- **资金曲线**: 权益和回撤曲线

#### 关键指标

| 类别 | 指标 |
|------|------|
| 收益 | 总收益率、年化收益率 |
| 风险 | 最大回撤、夏普比率、索提诺比率 |
| 胜率 | 胜率、盈利因子、平均盈亏 |
| 交易 | 总交易数、最大连胜/连败 |

#### 使用示例

```python
from app.backtest.backtester import Backtester
from app.strategy.rsi_strategy import RSIStrategy
from app.backtest.data_loader import generate_sample_data

# 准备数据
data = {"HK.00700": generate_sample_data("HK.00700", days=252)}

# 创建策略
strategy = RSIStrategy({"period": 14, "overbought": 70, "oversold": 30})

# 运行回测
backtester = Backtester(
    strategy=strategy,
    data=data,
    initial_capital=100000.0,
    fee_rate=0.0003,
    slippage=0.001,
)

result = backtester.run()
print(result.metrics.summary())
```

#### 输出示例

```
================================================================================
  回测性能报告
================================================================================
总收益率：         15.23%
年化收益率：       15.23%
总交易次数：           24
胜率：             54.17%
盈利因子：          1.85
最大回撤：          8.42%
夏普比率：          1.32
索提诺比率：        1.89
最终资金：     115230.45
================================================================================
```

---

### 3. API 接口

**端点**: `/api/backtest/*`

#### 运行单策略回测

```bash
curl "http://localhost:8899/api/backtest/run?strategy=rsi&code=HK.00700&days=252&token=YOUR_TOKEN"
```

#### 多策略对比

```bash
curl "http://localhost:8899/api/backtest/compare?codes=HK.00700&days=252&token=YOUR_TOKEN"
```

#### 列出可回测策略

```bash
curl "http://localhost:8899/api/backtest/strategies?token=YOUR_TOKEN"
```

---

## 📁 文件结构

```
/opt/ftseek/FTSeek/
├── app/
│   ├── strategy/
│   │   ├── market_regime.py       # 市场状态识别 ⭐ 新增
│   │   ├── __init__.py            # 导出 market_regime
│   │   └── ...
│   └── backtest/
│       ├── __init__.py            # 回测模块 ⭐ 新增
│       ├── backtester.py          # 回测引擎核心
│       ├── data_loader.py         # 数据加载器
│       └── api.py                 # API 接口
├── docs/
│   ├── MARKET_REGIME_AND_BACKTEST.md  # 本文档 ⭐ 新增
│   ├── BACKTEST_GUIDE.md          # 回测使用指南
│   └── QUANT_UPGRADE_PLAN.md      # 量化升级方案
├── backtest_example.py            # 回测示例脚本 ⭐ 新增
└── ...
```

---

## 🚀 快速开始

### 1. 运行回测示例

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
python3.8 backtest_example.py
```

### 2. 测试市场状态识别

```python
from app.strategy.market_regime import market_regime_detector
from app.backtest.data_loader import generate_sample_data

# 生成测试数据
data = generate_sample_data("HK.00700", days=100)

# 更新数据并检测
detector = market_regime_detector
for kline in data:
    detector.update_data("HK.00700", kline)

regime = detector.detect("HK.00700")
print(f"市场状态：{regime.regime_type.value}")
print(f"置信度：{regime.confidence:.2f}")
```

### 3. 使用 API

```bash
# 查看可用策略
curl "http://localhost:8899/api/backtest/strategies?token=$API_TOKEN"

# 运行回测
curl "http://localhost:8899/api/backtest/run?strategy=rsi&days=100&token=$API_TOKEN"
```

---

## 💡 实战应用

### 场景 1: 根据市场状态选择策略

```python
from app.strategy.market_regime import market_regime_detector

detector = market_regime_detector

# 更新实时数据
for kline in realtime_klines:
    detector.update_data(code, kline)

# 获取市场状态
regime = detector.detect(code)

# 根据状态调整策略权重
if regime.regime_type == MarketRegimeType.STRONG_TREND_UP:
    # 强上涨：提升趋势策略权重
    strategy_weights = {"supertrend": 1.0, "macd": 0.9, "rsi": 0.3}
elif regime.regime_type == MarketRegimeType.RANGE_BOUND:
    # 震荡市：提升震荡策略
    strategy_weights = {"rsi": 0.9, "bollinger": 0.9, "supertrend": 0.3}
```

### 场景 2: 策略参数优化

```python
from app.backtest.backtester import Backtester

best_params = None
best_sharpe = 0

for period in [10, 14, 18, 21]:
    for ob in [65, 70, 75]:
        for os in [25, 30, 35]:
            strategy = RSIStrategy({"period": period, "overbought": ob, "oversold": os})
            backtester = Backtester(strategy, data)
            result = backtester.run()
            
            if result.metrics.sharpe_ratio > best_sharpe:
                best_sharpe = result.metrics.sharpe_ratio
                best_params = (period, ob, os)

print(f"最优参数：{best_params}, 夏普：{best_sharpe:.2f}")
```

### 场景 3: 多策略组合

```python
from app.backtest.backtester import MultiStrategyBacktester

multi = MultiStrategyBacktester(data, initial_capital=100000.0)

# 运行所有策略
multi.add_strategy("RSI", RSIStrategy({...}))
multi.add_strategy("MACD", MACDStrategy({...}))
multi.add_strategy("SuperTrend", SuperTrendStrategy({...}))

# 对比结果
report = multi.compare()
print(report)

# 选择夏普比率最高的策略
best_strategy = max(multi.results.items(), 
                    key=lambda x: x[1].metrics.sharpe_ratio)
print(f"最优策略：{best_strategy[0]}")
```

---

## 📊 测试结果

### 回测框架测试

```
生成测试数据...
创建策略...
运行回测...

========================================
  回测性能报告
========================================
总收益率：         -2.60%
年化收益率：      -21.82%
总交易次数：            1
胜率：              0.00%
盈利因子：           0.00
最大回撤：          2.60%
夏普比率：          -6.26
索提诺比率：        -3.15
最终资金：       97397.47
========================================
```

✅ 回测框架运行正常！

### 市场状态识别测试

```python
# 测试数据生成
regime = detector.detect("HK.00700")
# 输出：市场状态：weak_trend_up, 置信度：0.65
```

✅ 市场状态识别正常！

---

## ⚠️ 注意事项

### 1. 模拟数据 vs 真实数据

当前示例使用随机生成的模拟数据，**实际使用时请加载真实历史数据**:

```python
from app.backtest.data_loader import DataLoader

loader = DataLoader()
data = loader.load_csv("HK_00700_day.csv", code="HK.00700")
```

### 2. 回测局限性

- 回测结果不代表未来表现
- 需考虑滑点、手续费、市场冲击
- 避免过度拟合历史数据

### 3. 市场状态识别

- 基于技术指标，存在滞后性
- 置信度<0.7 时谨慎使用
- 建议结合基本面分析

---

## 📚 相关文档

- [回测框架使用指南](BACKTEST_GUIDE.md)
- [量化升级方案](QUANT_UPGRADE_PLAN.md)
- [安全配置指南](../SECURITY.md)

---

## 🎯 下一步计划

1. ✅ 市场状态识别模块 - **已完成**
2. ✅ 回测框架核心 - **已完成**
3. ✅ API 接口 - **已完成**
4. ⏳ 真实数据加载 - 待实现
5. ⏳ Walk-Forward 分析 - 待实现
6. ⏳ 参数优化器 - 待实现

---

**实施完成！🎉**
