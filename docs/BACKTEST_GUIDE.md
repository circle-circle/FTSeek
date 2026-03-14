# 📊 FTSeek 回测框架使用指南

**版本**: v1.0  
**日期**: 2026-02-26

---

## 🚀 快速开始

### 1. 运行示例脚本

```bash
cd /opt/ftseek/FTSeek
python3.8 backtest_example.py
```

### 2. 查看回测结果

输出示例:
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

## 📖 核心概念

### Backtester (回测器)

```python
from app.backtest.backtester import Backtester
from app.strategy.rsi_strategy import RSIStrategy

# 1. 准备数据
data = {
    "HK.00700": [...],  # K 线数据列表
}

# 2. 创建策略
strategy = RSIStrategy({"period": 14, "overbought": 70, "oversold": 30})

# 3. 创建回测器
backtester = Backtester(
    strategy=strategy,
    data=data,
    initial_capital=100000.0,
    fee_rate=0.0003,      # 万三手续费
    slippage=0.001,       # 千一滑点
    risk_per_trade=0.10,  # 每笔交易 10% 资金
)

# 4. 运行回测
result = backtester.run()

# 5. 查看结果
print(result.metrics.summary())
```

### MultiStrategyBacktester (多策略对比)

```python
from app.backtest.backtester import MultiStrategyBacktester

# 创建对比回测器
multi = MultiStrategyBacktester(data=data, initial_capital=100000.0)

# 添加多个策略
multi.add_strategy("RSI", RSIStrategy({...}))
multi.add_strategy("MACD", MACDStrategy({...}))
multi.add_strategy("SuperTrend", SuperTrendStrategy({...}))

# 生成对比报告
report = multi.compare()
print(report)
```

---

## 📈 数据加载

### 从 CSV 加载

```python
from app.backtest.data_loader import DataLoader

loader = DataLoader(data_dir="/opt/ftseek/FTSeek/data")

# 加载单个文件
klines = loader.load_csv("HK_00700_day.csv", code="HK.00700")

# 加载多个文件
data = loader.load_multiple_csv({
    "HK.00700": "data/HK_00700_day.csv",
    "HK.09988": "data/HK_09988_day.csv",
})

# 对齐时间戳 (多标的回测必需)
aligned_data = loader.align_timestamps(data)
```

### CSV 格式要求

```csv
time_key,open,high,low,close,volume
2024-01-02 00:00:00,380.5,385.0,378.0,383.2,1234567
2024-01-03 00:00:00,383.0,388.5,381.5,386.8,2345678
```

---

## 🎯 关键性能指标

### 收益指标

| 指标 | 含义 | 优秀标准 |
|------|------|----------|
| **总收益率** | 总收益/本金 | >20% |
| **年化收益率** | 年化后的收益率 | >20% |
| **最终资金** | 回测结束时的资金 | - |

### 风险指标

| 指标 | 含义 | 优秀标准 |
|------|------|----------|
| **最大回撤** | 最大资金跌幅 | <15% |
| **夏普比率** | 风险调整后收益 | >1.5 |
| **索提诺比率** | 只考虑下行风险 | >2.0 |

### 交易质量

| 指标 | 含义 | 优秀标准 |
|------|------|----------|
| **胜率** | 盈利交易占比 | >45% |
| **盈利因子** | 总盈利/总亏损 | >1.5 |
| **平均盈亏** | 每笔交易平均盈亏 | >0 |
| **最大连胜** | 最大连续盈利次数 | - |
| **最大连败** | 最大连续亏损次数 | <5 |

---

## 🔧 API 接口

### 运行单策略回测

```bash
curl "http://localhost:8899/api/backtest/run?strategy=rsi&code=HK.00700&days=252&token=你的 token"
```

响应:
```json
{
  "status": "ok",
  "strategy": "rsi",
  "code": "HK.00700",
  "days": 252,
  "metrics": {
    "total_return": "15.23%",
    "win_rate": "54.17%",
    "profit_factor": "1.85",
    "max_drawdown": "8.42%",
    "sharpe_ratio": "1.32"
  },
  "trade_count": 24
}
```

### 多策略对比

```bash
curl "http://localhost:8899/api/backtest/compare?codes=HK.00700&days=252&token=你的 token"
```

### 列出可回测策略

```bash
curl "http://localhost:8899/api/backtest/strategies?token=你的 token"
```

---

## 📝 实战示例

### 示例 1: RSI 策略参数优化

```python
from app.backtest.backtester import Backtester
from app.strategy.rsi_strategy import RSIStrategy
from app.backtest.data_loader import generate_sample_data

data = {"HK.00700": generate_sample_data("HK.00700", days=252)}

best_sharpe = 0
best_params = None

# 网格搜索最优参数
for period in [10, 14, 18, 21]:
    for overbought in [65, 70, 75]:
        for oversold in [25, 30, 35]:
            strategy = RSIStrategy({
                "period": period,
                "overbought": overbought,
                "oversold": oversold,
            })
            
            backtester = Backtester(strategy, data)
            result = backtester.run(show_progress=False)
            
            if result.metrics.sharpe_ratio > best_sharpe:
                best_sharpe = result.metrics.sharpe_ratio
                best_params = (period, overbought, oversold)

print(f"最优参数：period={best_params[0]}, OB={best_params[1]}, OS={best_params[2]}")
print(f"夏普比率：{best_sharpe:.2f}")
```

### 示例 2: 使用市场状态过滤

```python
from app.strategy.market_regime import market_regime_detector
from app.backtest.backtester import Backtester

# 在策略中加入市场状态判断
detector = market_regime_detector

# 更新市场状态数据
for kline in data["HK.00700"]:
    detector.update_data("HK.00700", kline)

# 获取当前市场状态
regime = detector.detect("HK.00700")
print(f"当前市场状态：{regime.regime_type.value}")

# 根据市场状态调整策略
if regime.regime_type.value == "strong_trend_up":
    # 强上涨趋势，只做多
    strategy_config = {"only_long": True}
elif regime.regime_type.value == "range_bound":
    # 震荡市，使用 RSI
    strategy_config = {"use_rsi": True}
```

---

## ⚠️ 注意事项

### 1. 回测≠实盘

回测结果不代表未来表现，需考虑：
- 滑点和手续费
- 市场冲击成本
- 流动性限制
- 数据质量

### 2. 避免过度拟合

- 不要在单一数据集上过度优化
- 使用 Walk-Forward 分析
- 保留样本外测试数据

### 3. 数据质量

- 确保数据无缺失
- 处理除权除息
- 注意时区和交易时间

---

## 📚 下一步

1. **运行示例**: `python3.8 backtest_example.py`
2. **加载真实数据**: 从富途导出历史 K 线
3. **参数优化**: 网格搜索最优参数
4. **策略对比**: 找出最适合当前市场的策略
5. **实盘验证**: 小资金实盘测试

---

## 🆘 常见问题

### Q: 回测收益率很高但实盘不行？

A: 可能原因:
- 回测未考虑滑点和手续费
- 过度拟合历史数据
- 实盘执行延迟

### Q: 如何导入真实历史数据？

A: 使用 DataLoader 从 CSV 加载:
```python
loader = DataLoader()
data = loader.load_csv("your_data.csv", code="HK.00700")
```

### Q: 如何评估策略是否过拟合？

A: 
- 样本外测试表现
- 参数敏感性分析
- 跨标的测试

---

**祝回测顺利！📈**
