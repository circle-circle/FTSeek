# FTSeek V3 策略配置指南

> 三策略引擎完整配置说明 | 2026-03-01

## 📋 目录

1. [策略架构](#策略架构)
2. [策略配置](#策略配置)
3. [PriceBreak 策略](#pricebreak-策略)
4. [RSIRebound 策略](#rsirebound 策略)
5. [MACross 策略](#macross 策略)
6. [策略组合](#策略组合)
7. [回测与优化](#回测与优化)

## 🏗️ 策略架构

FTSeek V3 采用三策略引擎架构：

```
┌─────────────────────────────────────────┐
│           策略引擎 (Strategy Engine)     │
├─────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐       │
│  │ PriceBreak  │  │ RSIRebound  │       │
│  │  突破策略    │  │  反弹策略    │       │
│  └─────────────┘  └─────────────┘       │
│  ┌─────────────┐                        │
│  │  MACross    │                        │
│  │  均线策略    │                        │
│  └─────────────┘                        │
└─────────────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────┐
│         信号融合 (Signal Fusion)         │
│  - 权重分配                              │
│  - 冲突解决                              │
│  - 最终决策                              │
└─────────────────────────────────────────┘
```

## ⚙️ 策略配置

### 配置文件位置

`config/strategy_config.yaml`

### 基础配置

```yaml
strategies:
  # 全局配置
  global:
    enabled: true               # 启用策略引擎
    mode: "fusion"              # 模式：fusion(融合) | single(单一)
    signal_threshold: 0.6       # 信号强度阈值
    
  # PriceBreak 策略
  price_break:
    enabled: true
    weight: 0.5                 # 权重 50%
    params:
      lookback_period: 20       # 回看周期
      break_threshold: 0.02     # 突破阈值 2%
      volume_confirm: true      # 成交量确认
      
  # RSIRebound 策略
  rsi_rebound:
    enabled: true
    weight: 0.3                 # 权重 30%
    params:
      rsi_period: 14            # RSI 周期
      oversold_threshold: 30    # 超卖阈值
      overbought_threshold: 70  # 超买阈值
      
  # MACross 策略
  ma_cross:
    enabled: true
    weight: 0.2                 # 权重 20%
    params:
      short_period: 5           # 短期均线
      long_period: 20           # 长期均线
      signal_smoothing: 3       # 信号平滑
```

## 📈 PriceBreak 策略

### 策略原理

价格突破策略基于技术分析中的突破理论：

1. **识别区间**: 计算过去 N 日的最高价和最低价
2. **突破确认**: 当前价格突破区间边界
3. **成交量确认**: 突破时成交量放大
4. **信号生成**: 突破上轨→买入，突破下轨→卖出

### 参数详解

```yaml
price_break:
  enabled: true
  weight: 0.5
  params:
    lookback_period: 20         # 回看周期 (日)
    break_threshold: 0.02       # 突破阈值 (2%)
    volume_confirm: true        # 成交量确认
    volume_ratio: 1.5           # 成交量倍数
    stop_loss: 0.05             # 止损 5%
    take_profit: 0.15           # 止盈 15%
```

### 信号示例

```
📈 PriceBreak 买入信号
股票：HK.00700
突破价：320.00 HKD
20 日高点：312.50 HKD
突破幅度：+2.40%
成交量：1,234,567 (1.8x 平均)
信号强度：0.85
```

### 适用场景

- ✅ 趋势明显的股票
- ✅ 突破关键阻力/支撑位
- ✅ 成交量配合放大
- ❌ 震荡行情 (假突破多)

## 📉 RSIRebound 策略

### 策略原理

RSI 反弹策略基于相对强弱指数的超买超卖理论：

1. **RSI 计算**: 计算 14 日 RSI 指标
2. **超卖识别**: RSI < 30 视为超卖
3. **超买识别**: RSI > 70 视为超买
4. **反弹信号**: 超卖后 RSI 回升→买入

### 参数详解

```yaml
rsi_rebound:
  enabled: true
  weight: 0.3
  params:
    rsi_period: 14              # RSI 周期
    oversold_threshold: 30      # 超卖阈值
    overbought_threshold: 70    # 超买阈值
    rebound_confirm: 5          # 反弹确认天数
    stop_loss: 0.08             # 止损 8%
    take_profit: 0.20           # 止盈 20%
```

### 信号示例

```
📉 RSIRebound 买入信号
股票：HK.09992
当前 RSI: 28.5 (超卖)
RSI 低点：25.3 (3 日前)
反弹确认：RSI 连续 2 日上升
信号强度：0.72
```

### 适用场景

- ✅ 震荡行情
- ✅ 超跌反弹机会
- ✅ 基本面稳定的股票
- ❌ 单边下跌趋势

## 📊 MACross 策略

### 策略原理

均线交叉策略基于移动平均线的金叉死叉理论：

1. **均线计算**: 计算短期和长期均线
2. **金叉识别**: 短均线上穿长均线→买入信号
3. **死叉识别**: 短均线下穿长均线→卖出信号
4. **趋势确认**: 结合均线斜率确认趋势

### 参数详解

```yaml
ma_cross:
  enabled: true
  weight: 0.2
  params:
    short_period: 5             # 短期均线 (日)
    long_period: 20             # 长期均线 (日)
    signal_smoothing: 3         # 信号平滑
    trend_confirm: true         # 趋势确认
    stop_loss: 0.06             # 止损 6%
    take_profit: 0.18           # 止盈 18%
```

### 信号示例

```
📊 MACross 金叉信号
股票：HK.01810
5 日均线：18.50 HKD
20 日均线：18.20 HKD
交叉幅度：+1.65%
均线斜率：向上
信号强度：0.68
```

### 适用场景

- ✅ 趋势行情
- ✅ 中长期投资
- ✅ 流动性好的股票
- ❌ 短期震荡

## 🎯 策略组合

### 融合模式

V3 支持策略信号融合，综合多个策略的判断：

```python
# 信号融合示例
策略信号:
  PriceBreak:  买入 (强度 0.85, 权重 50%)
  RSIRebound:  观望 (强度 0.40, 权重 30%)
  MACross:     买入 (强度 0.68, 权重 20%)
  
融合计算:
  买入得分 = 0.85×0.5 + 0.40×0.3 + 0.68×0.2 = 0.681
  最终决策: 买入 (得分 > 0.6 阈值)
```

### 权重配置建议

#### 保守型
```yaml
price_break:  { weight: 0.3 }
rsi_rebound:  { weight: 0.5 }
ma_cross:     { weight: 0.2 }
```

#### 平衡型
```yaml
price_break:  { weight: 0.5 }
rsi_rebound:  { weight: 0.3 }
ma_cross:     { weight: 0.2 }
```

#### 激进型
```yaml
price_break:  { weight: 0.6 }
rsi_rebound:  { weight: 0.2 }
ma_cross:     { weight: 0.2 }
```

### 单一策略模式

仅使用单个策略（用于测试或特定行情）：

```yaml
strategies:
  global:
    mode: "single"
    active_strategy: "price_break"
```

## 🧪 回测与优化

### 运行回测

```bash
# 全策略回测
python3 backtest_example.py

# 单策略回测
python3 -c "
from app.strategies.price_break import PriceBreakStrategy
strategy = PriceBreakStrategy()
results = strategy.backtest('HK.00700', '2025-01-01', '2026-03-01')
print(results)
"
```

### 参数优化

```bash
# 运行参数优化
python3 optimize_example.py
```

### 回测指标

| 指标 | 说明 | 目标值 |
|------|------|--------|
| 胜率 | 盈利交易占比 | > 50% |
| 盈亏比 | 平均盈利/平均亏损 | > 2.0 |
| 夏普比率 | 风险调整后收益 | > 1.0 |
| 最大回撤 | 最大亏损幅度 | < 15% |
| 年化收益 | 年化收益率 | > 20% |

## 📊 策略表现监控

### 实时查看

```bash
# 查看策略信号日志
tail -f logs/strategy_signals.log

# 查看策略表现
python3 scripts/strategy_stats.py
```

### 策略统计示例

```
📊 策略表现统计 (2026-02)

PriceBreak:
  交易次数：15
  胜率：73.3%
  盈亏比：3.2
  贡献收益：+HK$12,500

RSIRebound:
  交易次数：8
  胜率：62.5%
  盈亏比：2.5
  贡献收益：+HK$4,200

MACross:
  交易次数：5
  胜率：80.0%
  盈亏比：4.1
  贡献收益：+HK$6,800

总计:
  交易次数：28
  综合胜率：71.4%
  总收益：+HK$23,500
```

## 🔧 故障排查

### 问题 1: 策略信号未生成

**检查清单:**
- [ ] 策略是否启用 (`enabled: true`)
- [ ] 数据源是否正常
- [ ] 参数配置是否合理
- [ ] 查看策略日志

### 问题 2: 信号过于频繁

**解决方案:**
- 提高 `signal_threshold` 阈值
- 增加 `lookback_period` 周期
- 降低策略权重

### 问题 3: 策略表现不佳

**优化步骤:**
1. 回测历史数据
2. 分析亏损交易
3. 调整策略参数
4. 重新优化权重

---

*最后更新：2026-03-01 | 版本：V3.0.0*
