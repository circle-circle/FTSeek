# 🛡️ 风险管理模块

**实施日期**: 2026-02-26  
**版本**: v1.0

---

## ✅ 核心功能

### 1. 仓位管理 (Position Sizing)

**文件**: `app/risk/position_sizer.py`

#### 5 种仓位计算方法

| 方法 | 说明 | 适用场景 |
|------|------|----------|
| **固定比例** | 固定仓位比例 | 保守策略 |
| **Kelly 公式** | 根据胜率和盈亏比优化 | 有历史数据的策略 |
| **固定风险** ⭐ | 固定每笔风险金额 | 推荐，风险可控 |
| **波动率调整** | 根据 ATR 调整仓位 | 高波动市场 |
| **风险平价** | 各标的风险均等 | 多标的组合 |

#### 使用示例

```python
from app.risk.position_sizer import PositionSizer, PositionSizingMethod

sizer = PositionSizer(
    initial_capital=100000.0,
    risk_per_trade=0.02,  # 每笔风险 2%
    method=PositionSizingMethod.FIXED_RISK,
)

position = sizer.calculate(
    code="HK.00700",
    signal_price=380.0,
    stop_loss_price=361.0,  # 止损价
    signal_strength=0.9,
)

print(f"数量：{position.quantity}股")
print(f"风险：¥{position.risk_amount:,.0f} ({position.risk_percent:.1%})")
```

---

### 2. 止损策略 (Stop Loss)

**文件**: `app/risk/stop_loss.py`

#### 6 种止损方法

| 方法 | 说明 | 优点 |
|------|------|------|
| **固定百分比** | 固定%止损 | 简单直接 |
| **ATR 止损** ⭐ | 基于波动率 | 自适应市场 |
| **SuperTrend** | 趋势跟踪止损 | 让利润奔跑 |
| **移动止损** | 随价格移动 | 保护盈利 |
| **时间止损** | 超时退出 | 避免资金占用 |
| **分级止损** | 多档止损 | 分批退出 |

#### 使用示例

```python
from app.risk.stop_loss import StopLossCalculator, StopLossType

calc = StopLossCalculator(
    default_stop_percent=0.05,  # 5%
    atr_multiplier=2.0,         # 2x ATR
)

# ATR 止损
stop = calc.calculate(
    entry_price=380.0,
    direction="BUY",
    method=StopLossType.ATR,
    atr_value=9.5,  # ATR 值
)

print(f"止损价：¥{stop.price:.2f}")
print(f"距离：{stop.distance_percent:.1%}")
```

---

### 3. 风险管理核心 (Risk Manager)

**文件**: `app/risk/risk_manager.py`

#### 风险限制

```python
RiskLimit:
├── max_position_percent: 0.20      # 单标的最大 20%
├── max_total_exposure: 0.80        # 总仓位最大 80%
├── max_daily_loss: 0.05            # 日亏损最大 5%
├── max_drawdown: 0.15              # 最大回撤 15%
├── max_var_percent: 0.03           # 最大 VaR 3%
├── max_correlated_exposure: 0.40   # 高相关标的 40%
└── max_trades_per_day: 10          # 每日最多 10 笔
```

#### 使用示例

```python
from app.risk.risk_manager import RiskManager, RiskLimit

limits = RiskLimit(
    max_daily_loss=0.05,
    max_drawdown=0.15,
)

risk_mgr = RiskManager(
    initial_capital=100000.0,
    risk_per_trade=0.02,
    limits=limits,
)

# 检查风险限制
can_trade, reason = risk_mgr.check_risk_limits(signal)

# 计算仓位
position = risk_mgr.calculate_position(
    signal=signal,
    stop_loss_price=361.0,
    volatility=9.5,
)

# 执行交易
risk_mgr.execute_trade(signal, position, stop_loss)

# 更新价格
risk_mgr.update_position("HK.00700", 385.0)

# 获取风险指标
summary = risk_mgr.get_summary()
print(summary)
```

---

## 📊 测试结果

### 示例输出

```
风险管理演示
================================================================================
初始资金：¥100,000
单笔风险：2.0%

执行交易...
✅ HK.09988:
   数量：135 股
   价值：¥19,980
   止损：¥140.60 (5.0%)
   风险：¥1,001 (1.4%)

✅ US.AAPL:
   数量：81 股
   价值：¥19,845
   止损：¥232.75 (5.0%)
   风险：¥1,005 (1.6%)

风险指标摘要:
资金：
  总资金：¥100,000
  可用：¥60,175
  已用：¥39,825

仓位:
  总仓位：39.8%
  持仓数：2

盈亏:
  未实现：0.00
  已实现：0.00
  总计：0.00

风险:
  最大回撤：0.0%
  当前回撤：0.0%
  胜率：0.0%
```

✅ 测试通过！

---

## 📁 文件清单

```
/opt/ftseek/FTSeek/
├── app/risk/
│   ├── __init__.py               ⭐ 模块初始化
│   ├── position_sizer.py         ⭐ 仓位管理 (12.5KB)
│   ├── stop_loss.py              ⭐ 止损策略 (11.1KB)
│   └── risk_manager.py           ⭐ 风险管理核心 (14.8KB)
├── risk_example.py               ⭐ 示例脚本 (5.4KB)
└── docs/RISK_MANAGEMENT_MODULE.md ⭐ 文档
```

---

## 🚀 快速开始

### 运行示例

```bash
cd /opt/ftseek/FTSeek
python3.8 risk_example.py
```

### 集成到策略

```python
from app.risk.risk_manager import risk_manager

# 在策略中
def on_kline(self, code, kline):
    signal = {...}
    
    # 检查风险
    can_trade, reason = risk_manager.check_risk_limits(signal)
    if not can_trade:
        return None
    
    # 计算仓位和止损
    position = risk_manager.calculate_position(signal)
    stop_loss = risk_manager.stop_loss_calculator.calculate(...)
    
    # 返回带风险信息的信号
    return Signal(
        code=code,
        quantity=position.quantity,
        stop_loss=stop_loss.price,
        ...
    )
```

---

## 💡 最佳实践

### 1. 仓位管理

```python
# ✅ 推荐：固定风险法
PositionSizingMethod.FIXED_RISK

# 每笔风险 2%，保证连续亏损 10 次只损失 20%
risk_per_trade=0.02
```

### 2. 止损设置

```python
# ✅ 推荐：ATR 止损 (自适应波动率)
StopLossType.ATR
atr_multiplier=2.0  # 2x ATR

# 波动大时止损宽，波动小时止损窄
```

### 3. 风险限制

```python
# 保守配置
RiskLimit(
    max_daily_loss=0.03,      # 日亏损 3%
    max_drawdown=0.10,        # 回撤 10%
    max_position_percent=0.15, # 单标的 15%
)

# 激进配置
RiskLimit(
    max_daily_loss=0.05,      # 日亏损 5%
    max_drawdown=0.15,        # 回撤 15%
    max_position_percent=0.25, # 单标的 25%
)
```

### 4. 分级止损

```python
# 大仓位使用分级止损
tiered = TieredStopLoss(n_tiers=3)
tiers = tiered.calculate_tiers(
    entry_price=380.0,
    total_quantity=1000,
    direction="BUY",
    atr_value=9.5,
)

# 第 1 级：1.5x ATR (33% 仓位)
# 第 2 级：2.5x ATR (33% 仓位)
# 第 3 级：3.5x ATR (34% 仓位)
```

---

## ⚠️ 注意事项

### 1. 避免过度风险

- 单笔风险不超过 2-3%
- 日亏损限制在 5% 以内
- 最大回撤控制在 15% 以内

### 2. 止损要合理

- 太紧：容易被震荡出局
- 太宽：单笔损失过大
- 建议：1.5-3x ATR

### 3. 定期评估

- 每周检查风险指标
- 每月评估策略表现
- 根据表现调整参数

---

## 📚 相关文档

- [回测框架](BACKTEST_GUIDE.md)
- [参数优化](PARAMETER_OPTIMIZATION_AND_INTEGRATION.md)
- [市场状态识别](MARKET_REGIME_AND_BACKTEST.md)

---

**实施完成**: 2026-02-26 14:55  
**测试状态**: ✅ 通过
