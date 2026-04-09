# FTSeek 策略与指标完整清单

**文档版本**: v2.0  
**更新日期**: 2026-03-15  
**策略总数**: 10 个 (7 大基础策略 + 3 个融合策略)

---

## 📊 策略总览

| 序号 | 策略名称 | 类型 | 信号类型 | 状态 | 优化情况 |
|------|----------|------|----------|------|----------|
| 1 | **RSI** | 震荡指标 | BUY/SELL | ✅ 活跃 | ✅ 做空优化 (70→65) |
| 2 | **MACD** | 趋势指标 | BUY/SELL | ✅ 活跃 | ✅ 正常 |
| 3 | **SuperTrend** | 趋势指标 | BUY/SELL | ✅ 活跃 | ✅ 敏感度优化 (3.0→2.5) |
| 4 | **Bollinger Bands** | 震荡/突破 | BUY/SELL | ✅ 活跃 | ✅ 正常 |
| 5 | **MA Cross** | 趋势指标 | BUY/SELL | ✅ 活跃 | ✅ 正常 |
| 6 | **Price Break** | 突破策略 | BUY/SELL | ✅ 活跃 | ✅ 正常 |
| 7 | **Volume Alert** | 成交量 | ALERT | ✅ 活跃 | ✅ 正常 |
| 8 | **Confluence** | 多因子融合 | BUY/SELL | ✅ 活跃 | ✅ 权重 1.2 |
| 9 | **Confluence V2** | 市场状态增强 | BUY/SELL | ✅ 活跃 | ✅ 正常 |
| 10 | **Fusion V2** | 信号融合引擎 | BUY/SELL | ✅ 活跃 | ✅ 正常 |

---

## 📈 基础策略详解

### 1. RSI 策略 (Relative Strength Index)

**策略类型**: 震荡指标  
**信号方向**: BUY (超卖反转) / SELL (超买回落)  
**文件**: `app/strategy/rsi_strategy.py`

#### 指标说明
相对强弱指标，衡量价格变动速度和幅度，判断超买超卖状态。

#### 买卖信号

| 信号 | 触发条件 | 逻辑说明 |
|------|----------|----------|
| **BUY** | RSI 从 ≤30 上穿 >30 | 超卖区反转，价格可能反弹 |
| **SELL** | RSI 从 ≥65 下穿 <65 | 超买区回落，价格可能回调 |

#### 配置参数
```python
{
  "period": 14,              # RSI 计算周期
  "overbought": 65,          # 超买阈值 (2026-03-15 从 70 降至 65)
  "oversold": 30,            # 超卖阈值
  "short_overbought": 65,    # 做空专用超买阈值
  "long_oversold": 30        # 做多专用超卖阈值
}
```

#### 优化记录
- **2026-03-15**: 超买阈值从 70 降至 65，更早发现超买信号
- **效果**: SELL 信号预计增加 30-40%

#### 代码示例
```python
# 超卖反转 → BUY
if prev_rsi <= 30 and rsi_val > 30:
    return Signal(action=SignalAction.BUY)

# 超买回落 → SELL (优化后)
if prev_rsi >= 65 and rsi_val < 65:  # 从 70 降至 65
    return Signal(action=SignalAction.SELL)
```

---

### 2. MACD 策略 (Moving Average Convergence Divergence)

**策略类型**: 趋势指标  
**信号方向**: BUY (金叉) / SELL (死叉)  
**文件**: `app/strategy/macd_strategy.py`

#### 指标说明
移动平均收敛发散指标，通过快慢线交叉判断趋势变化。

#### 买卖信号

| 信号 | 触发条件 | 逻辑说明 |
|------|----------|----------|
| **BUY** | MACD 柱状图从负转正 | 金叉信号，上涨趋势开始 |
| **SELL** | MACD 柱状图从正转负 | 死叉信号，下跌趋势开始 |

#### 配置参数
```python
{
  "fast_period": 12,         # 快线周期
  "slow_period": 26,         # 慢线周期
  "signal_period": 9,        # 信号线周期
  "signal_mode": "histogram" # "histogram" | "zero_cross" | "both"
}
```

#### 指标计算
```python
MACD 线 = EMA(12) - EMA(26)
信号线 = EMA(9, MACD 线)
柱状图 = MACD 线 - 信号线

# 金叉：柱状图从负转正
if prev_hist < 0 and histogram >= 0:
    return BUY

# 死叉：柱状图从正转负
if prev_hist > 0 and histogram <= 0:
    return SELL
```

---

### 3. SuperTrend 策略

**策略类型**: 趋势跟踪指标  
**信号方向**: BUY (翻多) / SELL (翻空)  
**文件**: `app/strategy/supertrend_strategy.py`

#### 指标说明
基于 ATR (平均真实波幅) 的自适应趋势跟踪指标，TradingView 最受欢迎的趋势指标之一。

#### 买卖信号

| 信号 | 触发条件 | 逻辑说明 |
|------|----------|----------|
| **BUY** | 方向从 "down" 翻为 "up" | 趋势转多，价格突破上轨 |
| **SELL** | 方向从 "up" 翻为 "down" | 趋势转空，价格跌破下轨 |

#### 配置参数
```python
{
  "atr_period": 10,            # ATR 计算周期
  "multiplier": 2.5,           # ATR 乘数 (2026-03-15 从 3.0 降至 2.5)
  "short_multiplier": 2.5,     # 做空专用乘数
  "long_multiplier": 3.0       # 做多专用乘数
}
```

#### 优化记录
- **2026-03-15**: 乘数从 3.0 降至 2.5，轨道宽度缩小 16.7%
- **效果**: 信号翻转更频繁，更早捕捉趋势变化，SELL 信号预计增加 20-30%

#### 指标计算
```python
# SuperTrend 轨道
上轨 = (High + Low) / 2 + multiplier × ATR
下轨 = (High + Low) / 2 - multiplier × ATR

# 方向判定
if close > 上轨：direction = "up"
if close < 下轨：direction = "down"

# 信号生成
if prev_dir == "down" and direction == "up":
    return BUY
if prev_dir == "up" and direction == "down":
    return SELL
```

---

### 4. Bollinger Bands 策略 (布林带)

**策略类型**: 震荡/突破指标  
**信号方向**: BUY (下轨反弹) / SELL (上轨回落) / BREAKOUT  
**文件**: `app/strategy/bollinger_strategy.py`

#### 指标说明
布林带由中轨 (移动平均线) 和上下轨 (标准差) 组成，判断价格波动区间。

#### 买卖信号

**模式 1: 震荡回归 (Reversion)**
| 信号 | 触发条件 | 逻辑说明 |
|------|----------|----------|
| **BUY** | %B < 0 后反弹 | 价格触及下轨后反弹，超卖 |
| **SELL** | %B > 1 后回落 | 价格触及上轨后回落，超买 |

**模式 2: 突破 (Breakout)**
| 信号 | 触发条件 | 逻辑说明 |
|------|----------|----------|
| **BUY** | 价格突破上轨 (%B > 1) | 强势突破，追涨 |
| **SELL** | 价格跌破下轨 (%B < 0) | 弱势突破，杀跌 |

#### 配置参数
```python
{
  "period": 20,              # 中轨周期
  "std_dev": 2.0,            # 标准差倍数
  "mode": "reversion"        # "reversion" | "breakout"
}
```

#### 指标计算
```python
中轨 = SMA(20)
标准差 = StdDev(20)
上轨 = 中轨 + 2×标准差
下轨 = 中轨 - 2×标准差

%B = (价格 - 下轨) / (上轨 - 下轨)

# 震荡模式
if %B < 0 后反弹 → BUY
if %B > 1 后回落 → SELL

# 突破模式
if 价格 > 上轨 → BUY
if 价格 < 下轨 → SELL
```

---

### 5. MA Cross 策略 (均线交叉)

**策略类型**: 趋势指标  
**信号方向**: BUY (金叉) / SELL (死叉)  
**文件**: `app/strategy/ma_cross.py`

#### 指标说明
快慢移动平均线交叉，判断趋势转折。

#### 买卖信号

| 信号 | 触发条件 | 逻辑说明 | 俗称 |
|------|----------|----------|------|
| **BUY** | 快线从下上穿慢线 | 上涨趋势开始 | 金叉 |
| **SELL** | 快线从上穿下慢线 | 下跌趋势开始 | 死叉 |

#### 配置参数
```python
{
  "fast_period": 5,          # 快线周期
  "slow_period": 20          # 慢线周期
}
```

#### 指标计算
```python
快线 = MA(5)
慢线 = MA(20)

# 金叉
if 快线 < 慢线 → 快线 > 慢线:
    return BUY

# 死叉
if 快线 > 慢线 → 快线 < 慢线:
    return SELL
```

---

### 6. Price Break 策略 (价格突破)

**策略类型**: 突破策略  
**信号方向**: BUY (突破上限) / SELL (跌破下限)  
**文件**: `app/strategy/price_break.py`

#### 指标说明
当股价突破预设的价格阈值时产生信号。

#### 买卖信号

| 信号 | 触发条件 | 逻辑说明 |
|------|----------|----------|
| **BUY** | 价格 ≥ 上限阈值 | 突破阻力位，可能继续上涨 |
| **SELL** | 价格 ≤ 下限阈值 | 跌破支撑位，可能继续下跌 |

#### 配置参数
```python
{
  "thresholds": {
    "HK.00700": {
      "upper": 500.0,        # 上限阈值
      "lower": 400.0         # 下限阈值
    },
    "HK.09988": {
      "upper": 150.0,
      "lower": 120.0
    }
  }
}
```

#### 特点
- 需要手动配置每个股票的价格阈值
- 适合关键支撑/阻力位突破交易
- 防止重复触发 (触发后重置)

---

### 7. Volume Alert 策略 (成交量异动)

**策略类型**: 成交量指标  
**信号类型**: ALERT (提醒)  
**文件**: `app/strategy/volume_alert.py`

#### 指标说明
检测成交量异常放大，提示可能的重大行情。

#### 信号

| 信号 | 触发条件 | 逻辑说明 |
|------|----------|----------|
| **ALERT** | 成交量 ≥ 2.0× 均量 | 成交量异常放大，可能有重大事件 |

#### 配置参数
```python
{
  "volume_ratio_threshold": 2.0,  # 成交量倍数阈值
  "lookback_periods": 20          # 均量计算周期
}
```

#### 指标计算
```python
均量 = MA(20, volume)
倍率 = 当前成交量 / 均量

if 倍率 ≥ 2.0:
    return ALERT
```

#### 特点
- 仅发出 ALERT 信号，不直接交易
- 可作为其他策略的确认条件
- 适合发现突发事件/财报发布等

---

## 🔀 融合策略详解

### 8. Confluence 策略 (多因子融合)

**策略类型**: 综合策略  
**信号方向**: BUY/SELL  
**文件**: `app/strategy/confluence_strategy.py`  
**共识权重**: 1.2 (最高)

#### 策略说明
融合多个技术指标，综合判断买卖信号。

#### 融合因子
- RSI 超买超卖
- MACD 趋势
- 均线方向
- 成交量确认
- 价格位置

#### 信号生成
```python
score = RSI 分数 + MACD 分数 + MA 分数 + Volume 分数 + Price 分数

if score ≥ 阈值：return BUY
if score ≤ -阈值：return SELL
```

#### 特点
- 多因子确认，信号质量高
- 共识权重最高 (1.2)
- 适合趋势 + 震荡混合行情

---

### 9. Confluence V2 策略 (市场状态增强版)

**策略类型**: 综合策略 (增强版)  
**信号方向**: BUY/SELL  
**文件**: `app/strategy/confluence_v2.py`

#### 策略说明
在 Confluence 基础上增加市场状态识别 (ADX)。

#### 增强功能
- **市场状态识别**:
  - ADX > 25: 趋势市 → 趋势策略权重 ×1.5
  - ADX < 20: 震荡市 → 震荡策略权重 ×1.5
  - 20 ≤ ADX ≤ 25: 中性市 → 基准权重

#### 动态权重
```python
if ADX > 25:  # 趋势市
  SuperTrend 权重 ×1.5
  MACD 权重 ×1.5
  MA Cross 权重 ×1.5
elif ADX < 20:  # 震荡市
  RSI 权重 ×1.5
  Bollinger 权重 ×1.5
```

---

### 10. Fusion V2 策略 (信号融合引擎)

**策略类型**: 信号融合  
**信号方向**: BUY/SELL/NEUTRAL  
**文件**: `app/strategy/fusion_v2.py`

#### 策略说明
独立于 7 大策略之外的融合引擎，计算综合评分和方向概率。

#### 融合架构
```
技术面 (55%) + 情绪面 (25%) + AI 预测 (20%) = 最终评分
```

#### 技术面评分 (55%)
| 指标 | 权重 | 评分范围 |
|------|------|----------|
| RSI | 25% | -1 ~ +1 |
| MACD | 25% | -1 ~ +1 |
| 均线 | 20% | -1 ~ +1 |
| 成交量 | 15% | -1 ~ +1 |
| 布林带 | 15% | -1 ~ +1 |

#### 情绪面评分 (25%)
- 新闻情绪分析 (财联社/华尔街见闻等)
- 社交情绪分析
- 评分范围：-1 ~ +1

#### AI 预测评分 (20%) ⚠️ 待启用
- Bailian API 调用
- 市场趋势预测
- 个股走势分析

#### 输出格式
```python
{
  "direction": "BUY",           # BUY/SELL/NEUTRAL
  "buy_probability": "75%",     # 买入概率
  "sell_probability": "25%",    # 卖出概率
  "confidence": "80%",          # 置信度
  "signal_level": "strong",     # strong/recommended/neutral/weak
  "technical_score": "+0.65",   # 技术分
  "sentiment_score": "+0.40"    # 情绪分
}
```

---

## 🧠 辅助模块

### Market Regime Detector (市场状态检测)

**文件**: `app/strategy/market_regime.py`

#### 功能
使用 ADX 指标识别市场状态：
- **趋势市**: ADX > 25
- **震荡市**: ADX < 20
- **中性市**: 20 ≤ ADX ≤ 25

#### 应用
- 为 Confluence V2 提供市场状态
- 为共识引擎提供 ADX 值
- 动态调整策略权重

---

### Consensus Engine (加权共识引擎)

**文件**: `app/strategy/consensus_engine.py`

#### 功能
对 7 大策略信号进行加权评分，决定是否推送。

#### 策略权重
| 策略 | 基准权重 | 类型 |
|------|----------|------|
| Confluence | 1.2 | 综合型 |
| SuperTrend | 1.0 | 趋势型 |
| RSI | 0.8 | 震荡型 |
| MACD | 0.8 | 趋势 + 动量 |
| Bollinger | 0.8 | 震荡/突破 |
| MA Cross | 0.6 | 经典趋势 |
| Price Break | 0.5 | 简单突破 |

#### 市场自适应
```python
if ADX > 25:  # 趋势市
  趋势策略权重 ×1.5
  震荡策略权重 ×0.6
elif ADX < 20:  # 震荡市
  震荡策略权重 ×1.5
  趋势策略权重 ×0.6
```

#### 触发条件
```python
score = Σ(策略权重 × 方向)  # BUY=+1, SELL=-1

if score ≥ +1.5: 推送 BUY
if score ≤ -1.5: 推送 SELL
否则：不推送
```

---

## 📊 策略分类总结

### 按类型分类

| 类型 | 策略 | 数量 |
|------|------|------|
| **趋势指标** | MACD, SuperTrend, MA Cross | 3 |
| **震荡指标** | RSI, Bollinger | 2 |
| **突破策略** | Price Break | 1 |
| **成交量** | Volume Alert | 1 |
| **综合策略** | Confluence, Confluence V2 | 2 |
| **融合引擎** | Fusion V2 | 1 |

### 按信号类型分类

| 信号类型 | 策略 | 数量 |
|----------|------|------|
| **BUY/SELL** | RSI, MACD, SuperTrend, Bollinger, MA Cross, Price Break, Confluence, Confluence V2, Fusion V2 | 9 |
| **ALERT** | Volume Alert | 1 |

### 按权重分类

| 权重 | 策略 | 说明 |
|------|------|------|
| **1.2** | Confluence | 最高权重，多因子融合 |
| **1.0** | SuperTrend | 趋势追踪主力 |
| **0.8** | RSI, MACD, Bollinger | 核心指标 |
| **0.6** | MA Cross | 经典趋势 |
| **0.5** | Price Break | 简单阈值 |

---

## 🎯 2026-03-15 优化总结

### RSI 策略优化
- **变更**: 超买阈值从 70 降至 65
- **原因**: 更早发现超买信号，增加 SELL 信号
- **效果**: SELL 信号预计 +30-40%

### SuperTrend 策略优化
- **变更**: ATR 乘数从 3.0 降至 2.5
- **原因**: 缩小轨道宽度，更敏感捕捉趋势
- **效果**: 轨道宽度 -16.7%, SELL 信号预计 +20-30%

### 港股特性适配
- **新增**: T+0 交易支持
- **新增**: 流动性分层 (Tier1/2/3)
- **新增**: 做空信号增强 (×1.2)
- **新增**: 无涨跌停适配 (止损 -8%)

---

## 📈 策略组合建议

### 保守型组合
- RSI (超买超卖)
- MACD (趋势确认)
- MA Cross (长期趋势)
- 触发条件：3 策略共振

### 激进型组合
- SuperTrend (趋势跟踪)
- Price Break (突破交易)
- Volume Alert (成交量确认)
- 触发条件：2 策略共振 + 成交量放大

### 平衡型组合 (当前默认)
- RSI + MACD + SuperTrend + Bollinger + MA Cross + Confluence
- 触发条件：加权评分 ≥ 1.5

---

## 🔧 运维命令

### 查看策略状态
```bash
# 钉钉命令
/策略列表
/策略详情 rsi
/策略详情 supertrend
```

### 动态调整参数
```bash
# 钉钉命令
/调参 rsi overbought=65
/调参 supertrend multiplier=2.5
```

### 查看日志
```bash
# 查看各策略信号
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep -E "RSI|MACD|SuperTrend|Bollinger"
```

---

## 📞 策略开发指南

### 添加新策略
1. 在 `app/strategy/` 创建 `xxx_strategy.py`
2. 继承 `BaseStrategy` 类
3. 实现 `on_kline()` 或 `on_quote()` 方法
4. 在 `__init__.py` 中注册

### 策略模板
```python
from app.strategy.base import BaseStrategy, Signal, SignalAction

class MyStrategy(BaseStrategy):
    name = "my_strategy"
    
    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.param = self.config.get("param", 14)
    
    def on_kline(self, code, kline, kline_type):
        if not self.enabled:
            return None
        
        # 计算指标
        indicator = self.calculate(kline)
        
        # 生成信号
        if indicator > threshold:
            return Signal(
                code=code,
                action=SignalAction.BUY,
                price=kline["close"],
                reason="买入原因",
                strategy=self.name
            )
        
        return None
```

---

**文档状态**: ✅ 完整  
**最后更新**: 2026-03-15 18:05  
**下次回顾**: 2026-03-22
