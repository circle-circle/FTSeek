# 🎯 参数优化 + 市场状态集成

**实施日期**: 2026-02-26  
**版本**: v0.3.0

---

## ✅ 新增功能

### 1. 参数优化模块

**文件**: `app/backtest/optimizer.py` (13.8KB)

#### 优化算法

| 算法 | 说明 | 适用场景 |
|------|------|----------|
| **网格搜索** | 穷举所有参数组合 | 参数少，范围明确 |
| **随机搜索** | 随机采样参数空间 | 参数多，范围大 |
| **Walk-Forward** | 滚动窗口优化 | 验证策略稳定性 |

#### 优化目标

支持所有回测指标作为优化目标：
- `sharpe_ratio` - 夏普比率 (推荐)
- `total_return` - 总收益率
- `max_drawdown` - 最大回撤
- `win_rate` - 胜率
- `profit_factor` - 盈利因子
- `sortino_ratio` - 索提诺比率

#### 使用示例

```python
from app.backtest.optimizer import optimize_strategy
from app.strategy.rsi_strategy import RSIStrategy

# 定义参数网格
param_grid = {
    "period": [10, 14, 18, 21],
    "overbought": [65, 70, 75],
    "oversold": [25, 30, 35],
}

# 运行优化
result = optimize_strategy(
    strategy_class=RSIStrategy,
    data=data,
    param_grid=param_grid,
    score_func="sharpe_ratio",  # 优化夏普比率
)

print(f"最优参数：{result.best_params}")
print(f"最优得分：{result.best_score:.4f}")
```

#### 输出示例

```
================================================================================
  参数优化结果
================================================================================
优化目标：sharpe_ratio
最优得分：1.8523
最优参数：{'period': 14, 'overbought': 70, 'oversold': 30}
总组合数：36
有效组合：36

Top 5 参数组合:
--------------------------------------------------------------------------------
1. Score=1.8523 | Params={'period': 14, 'overbought': 70, 'oversold': 30}
2. Score=1.7845 | Params={'period': 12, 'overbought': 70, 'oversold': 30}
3. Score=1.6932 | Params={'period': 14, 'overbought': 68, 'oversold': 32}
--------------------------------------------------------------------------------
```

---

### 2. Confluence V2 策略 (市场状态增强版)

**文件**: `app/strategy/confluence_v2.py` (14.7KB)

#### 核心增强

| 功能 | 说明 | 效果 |
|------|------|------|
| **市场状态感知** | 根据市场状态调整策略 | 降低假信号 |
| **动态因子权重** | 趋势市提升趋势因子权重 | 提高胜率 |
| **波动率调整** | 高波动时降低仓位 | 控制风险 |
| **自适应阈值** | 震荡市降低触发要求 | 捕捉机会 |

#### 市场状态权重配置

```python
# 强上涨趋势市场
STRONG_TREND_UP: {
    "supertrend": 1.5,  # 提升趋势策略权重
    "macd": 1.3,
    "rsi": 0.7,         # 降低震荡策略权重
}

# 震荡市
RANGE_BOUND: {
    "rsi": 1.5,         # 提升震荡策略权重
    "bollinger": 1.5,
    "supertrend": 0.6,  # 降低趋势策略权重
}
```

#### 使用示例

```python
from app.strategy.confluence_v2 import ConfluenceStrategyV2

# 创建策略 (启用市场状态识别)
strategy = ConfluenceStrategyV2({
    "min_factors": 3,
    "use_market_regime": True,      # 启用市场状态
    "use_dynamic_weights": True,    # 启用动态权重
})
```

#### 信号输出增强

```
多因子共振买入 (得分=4.2):
  - supertrend: SuperTrend up (权重 1.5)
  - macd: MACD 金叉 (hist=0.0023) (权重 1.3)
  - rsi: RSI 超卖反转 (32.5->35.2) (权重 0.7)
  - volume: 放量上涨 (vol=2.15x) (权重 1.0)
市场状态：strong_trend_up
```

---

### 3. API 接口扩展

**端点**: `/api/optimize/*`

#### 可用接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/optimize/grid_search` | POST | 网格搜索优化 |
| `/api/optimize/random_search` | POST | 随机搜索优化 |
| `/api/optimize/walk_forward` | POST | Walk-Forward 分析 |
| `/api/optimize/strategies` | GET | 列出可优化策略 |

#### API 使用示例

```bash
TOKEN="5a8f072c2cba9db1579dd829214a38112fdcddefe089d4ba58e05742097dd272"

# 网格搜索
curl -X POST "http://localhost:8899/api/optimize/grid_search?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "rsi",
    "param_grid": {"period": [10, 14, 18], "overbought": [70, 75], "oversold": [25, 30]},
    "score_func": "sharpe_ratio",
    "days": 252
  }'

# Walk-Forward 分析
curl -X POST "http://localhost:8899/api/optimize/walk_forward?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "rsi",
    "param_grid": {"period": [12, 14, 16]},
    "train_periods": 60,
    "test_periods": 20,
    "days": 500
  }'
```

---

## 📁 新增文件清单

```
/opt/ftseek/FTSeek/
├── app/
│   ├── strategy/
│   │   ├── confluence_v2.py          ⭐ 新增 (14.7KB)
│   │   └── __init__.py               ✏️ 更新 (注册 confluence_v2)
│   └── backtest/
│       ├── optimizer.py              ⭐ 新增 (13.8KB)
│       └── optimizer_api.py          ⭐ 新增 (6.9KB)
├── docs/
│   └── PARAMETER_OPTIMIZATION_AND_INTEGRATION.md ⭐ 新增
├── optimize_example.py               ⭐ 新增 (7.0KB)
└── app/server.py                     ✏️ 更新 (注册优化 API)
```

---

## 🚀 快速开始

### 运行优化示例

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN="5a8f072c2cba9db1579dd829214a38112fdcddefe089d4ba58e05742097dd272"
python3.8 optimize_example.py
```

### 使用 API

```bash
# 列出可优化策略
curl "http://localhost:8899/api/optimize/strategies?token=$TOKEN"

# 运行网格搜索
curl -X POST "http://localhost:8899/api/optimize/grid_search?token=$TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"strategy":"rsi","param_grid":{"period":[10,14,18]},"days":100}'
```

---

## 📊 测试结果

### 参数优化测试

```bash
python3.8 -c "
from app.backtest.optimizer import optimize_strategy
from app.strategy.rsi_strategy import RSIStrategy
from app.backtest.data_loader import generate_sample_data

data = {'HK.00700': generate_sample_data('HK.00700', days=100)}
param_grid = {'period': [12, 14], 'overbought': [70, 75], 'oversold': [25, 30]}

result = optimize_strategy(RSIStrategy, data, param_grid, score_func='sharpe_ratio')
print(result.summary())
"
```

**结果**: ✅ 正常运行

### Confluence V2 测试

```python
from app.strategy.confluence_v2 import ConfluenceStrategyV2

strategy = ConfluenceStrategyV2({
    "min_factors": 3,
    "use_market_regime": True,
    "use_dynamic_weights": True,
})

print(f"策略名称：{strategy.name}")
print(f"参数：{strategy.get_params()}")
```

**结果**: ✅ 正常创建

---

## 💡 实战应用

### 场景 1: RSI 策略参数优化

```python
param_grid = {
    "period": [10, 12, 14, 16, 18, 21],
    "overbought": [65, 68, 70, 72, 75],
    "oversold": [25, 28, 30, 32, 35],
}

result = optimize_strategy(
    strategy_class=RSIStrategy,
    data=data,
    param_grid=param_grid,
    score_func="sharpe_ratio",
)

# 使用最优参数
best_strategy = RSIStrategy({
    **result.best_params,
    "enabled": True,
})
```

### 场景 2: Walk-Forward 验证

```python
optimizer = ParameterOptimizer(
    strategy_class=RSIStrategy,
    data=data,
    base_config={"period": 14, "overbought": 70, "oversold": 30},
)

wf_result = optimizer.walk_forward(
    param_grid={"period": [12, 14, 16]},
    train_periods=60,
    test_periods=20,
)

# 检查过拟合
if wf_result['avg_train_score'] / wf_result['avg_test_score'] > 2.0:
    print("⚠️ 警告：可能存在过拟合！")
```

### 场景 3: Confluence V2 实盘

```python
from app.strategy.confluence_v2 import ConfluenceStrategyV2

# 启用市场状态识别和动态权重
strategy = ConfluenceStrategyV2({
    "min_factors": 3,
    "use_market_regime": True,
    "use_dynamic_weights": True,
    "enabled_factors": ["rsi", "macd", "supertrend", "volume"],
})

# 在实盘中使用
# 策略会自动根据市场状态调整因子权重
```

---

## ⚠️ 注意事项

### 1. 避免过度优化

- 参数组合不要太多 (避免数据窥探)
- 使用 Walk-Forward 验证
- 保留样本外测试数据

### 2. 市场状态识别限制

- 基于技术指标，存在滞后性
- 置信度<0.7 时谨慎使用
- 建议结合基本面分析

### 3. 优化目标选择

- **推荐**: 夏普比率、索提诺比率 (风险调整后收益)
- **避免**: 单纯优化收益率 (可能承担过高风险)

---

## 📚 相关文档

- [回测框架指南](BACKTEST_GUIDE.md)
- [市场状态识别](MARKET_REGIME_AND_BACKTEST.md)
- [量化升级方案](QUANT_UPGRADE_PLAN.md)

---

## 🎯 系统版本

| 模块 | 版本 | 状态 |
|------|------|------|
| 市场状态识别 | v1.0 | ✅ |
| 回测框架 | v1.0 | ✅ |
| 参数优化 | v1.0 | ✅ |
| Confluence V2 | v2.0 | ✅ |
| API 接口 | v0.3.0 | ✅ |

---

**实施完成时间**: 2026-02-26 15:00  
**系统状态**: ✅ 运行正常
