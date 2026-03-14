# 📈 FTSeek 量化策略升级方案

**版本**: v2.0  
**日期**: 2026-02-26  
**目标**: 基于现有策略体系，结合业界最新量化研究成果进行全面升级

---

## 📊 现有策略评估

### 当前策略矩阵

| 策略 | 类型 | 状态 | 优势 | 局限 |
|------|------|------|------|------|
| `price_break` | 突破 | ✅ | 简单直接 | 假突破多 |
| `rsi` | 震荡 | ✅ | 超买超卖清晰 | 趋势市失效 |
| `macd` | 趋势 | ✅ | 经典可靠 | 滞后性明显 |
| `bollinger` | 波动率 | ✅ | 自适应带宽 | 单边市易穿带 |
| `supertrend` | 趋势跟踪 | ✅ | 止损明确 | 震荡市频繁止损 |
| `confluence` | 多因子 | ✅ | 降低假信号 | 参数复杂 |
| `ma_cross` | 趋势 | ❌ | 经典 | 严重滞后 |
| `volume_alert` | 成交量 | ❌ | 确认信号 | 需配合其他指标 |

### 现有指标库

✅ 已实现: RSI, MACD, Bollinger, SuperTrend, ATR, ADX, SMA, EMA, VWAP, StochRSI

---

## 🚀 升级方向

### 一、新增核心指标 (优先级: 高)

#### 1. 动量类指标

| 指标 | 作用 | 实现难度 | 优先级 |
|------|------|----------|--------|
| **ROC (Rate of Change)** | 价格变化率，衡量动量强度 | ⭐ | 🔴 |
| **Momentum** | 简单动量指标 | ⭐ | 🟡 |
| **Williams %R** | 超买超卖，比 RSI 更敏感 | ⭐⭐ | 🟡 |
| **CCI (Commodity Channel Index)** | 识别周期性转折 | ⭐⭐ | 🟡 |

#### 2. 趋势强度类

| 指标 | 作用 | 实现难度 | 优先级 |
|------|------|----------|--------|
| **ADX + DI** | 趋势强度 + 方向 | ⭐⭐ (ADX 已有) | 🔴 |
| **Aroon** | 趋势强弱和转折 | ⭐⭐ | 🟡 |
| **Linear Regression Slope** | 趋势斜率 | ⭐⭐ | 🟡 |
| **Ichimoku Cloud** | 综合趋势系统 | ⭐⭐⭐⭐ | 🟢 |

#### 3. 波动率类

| 指标 | 作用 | 实现难度 | 优先级 |
|------|------|----------|--------|
| **ATR Percentile** | ATR 相对历史位置 | ⭐⭐ | 🔴 |
| **Keltner Channel** | 波动率通道 | ⭐⭐ | 🟡 |
| **Donchian Channel** | 突破通道 | ⭐ | 🟡 |
| **Historical Volatility** | 历史波动率 | ⭐⭐ | 🟡 |

#### 4. 成交量类

| 指标 | 作用 | 实现难度 | 优先级 |
|------|------|----------|--------|
| **OBV (On Balance Volume)** | 量价关系 | ⭐ | 🔴 |
| **Volume Profile** | 成交量分布 | ⭐⭐⭐ | 🟢 |
| **MFI (Money Flow Index)** | 资金流量 | ⭐⭐ | 🟡 |
| **VWAP Deviation** | 偏离 VWAP 程度 | ⭐⭐ | 🟡 |

#### 5. 市场结构类

| 指标 | 作用 | 实现难度 | 优先级 |
|------|------|----------|--------|
| **Pivot Points** | 支撑阻力位 | ⭐ | 🔴 |
| **Fractal (Bill Williams)** | 高低点识别 | ⭐⭐ | 🟡 |
| **Market Regime Filter** | 趋势/震荡识别 | ⭐⭐⭐ | 🔴 |

---

### 二、策略优化建议 (优先级: 高)

#### 1. Confluence 策略增强

**当前问题**: 简单计数，未考虑信号质量

**升级方案**:
```python
# 加权评分系统
factor_weights = {
    'rsi': 0.8,      # 超买超卖权重
    'macd': 1.0,     # 趋势确认权重
    'supertrend': 1.2,  # 主趋势权重最高
    'volume': 0.6,   # 成交量确认
    'adx': 1.0,      # 趋势强度过滤
}

# 信号质量评分 (0-1)
signal_quality = (
    factor_weight * signal_strength * 
    regime_alignment * volume_confirmation
)

# 动态阈值
entry_threshold = base_threshold * (1 - market_confidence)
```

#### 2. RSI 策略升级

**新增功能**:
- RSI 背离检测 (价格新高但 RSI 未新高 = 潜在反转)
- RSI 趋势线突破
- 多时间周期 RSI 确认

```python
def rsi_divergence(prices, rsi_values, lookback=5):
    """检测 RSI 背离"""
    #  bullish divergence: 价格创新低，RSI 未创新低
    #  bearish divergence: 价格创新高，RSI 未创新高
    pass
```

#### 3. MACD 策略升级

**新增功能**:
- MACD 背离检测
- 多周期 MACD 确认
- MACD 柱状图斜率变化

#### 4. SuperTrend 策略升级

**新增功能**:
- 多周期 SuperTrend 共振 (日线 + 小时线)
- 动态 multiplier (基于 ATR 百分位)
- SuperTrend 与 EMA 过滤结合

---

### 三、风险管理体系 (优先级: 最高)

#### 1. 仓位管理

```python
class PositionSizer:
    """动态仓位计算器"""
    
    def calculate_position(self, signal, account_info):
        # Kelly 公式改进版
        win_rate = self.get_strategy_win_rate(signal.strategy)
        avg_win = self.get_avg_win(signal.strategy)
        avg_loss = self.get_avg_loss(signal.strategy)
        
        kelly_pct = (win_rate * avg_win - (1-win_rate) * avg_loss) / avg_win
        position_size = min(kelly_pct * 0.5, 0.20)  # 最多 20% 仓位
        
        # 根据波动率调整
        volatility = self.get_current_volatility(signal.code)
        if volatility > self.vol_threshold:
            position_size *= 0.7  # 高波动降仓
        
        return position_size
```

#### 2. 止损策略

| 止损类型 | 适用场景 | 实现难度 |
|----------|----------|----------|
| **固定百分比** | 所有策略 | ⭐ |
| **ATR 止损** | 趋势策略 | ⭐⭐ |
| **SuperTrend 止损** | 趋势跟踪 | ⭐ |
| **时间止损** | 突破策略 | ⭐ |
| **移动止损** | 盈利后保护 | ⭐⭐ |
| **分级止损** | 大仓位 | ⭐⭐⭐ |

#### 3. 风险控制

```python
class RiskManager:
    """全局风险管理"""
    
    # 单笔风险限制
    MAX_RISK_PER_TRADE = 0.02  # 2%
    
    # 总仓位限制
    MAX_TOTAL_EXPOSURE = 0.80  # 80%
    
    # 单标的限制
    MAX_POSITION_PER_STOCK = 0.20  # 20%
    
    # 相关性限制
    MAX_CORRELATED_EXPOSURE = 0.40  # 高相关标的合计不超过 40%
    
    # 日亏损限制
    MAX_DAILY_LOSS = 0.05  # 5%
    
    # 回撤限制
    MAX_DRAWDOWN = 0.15  # 15%
```

---

### 四、市场状态识别 (优先级: 高)

#### 1. 市场体制分类

```python
class MarketRegime:
    """市场状态识别"""
    
    def classify(self, data):
        """
        返回市场状态:
        - STRONG_TREND_UP (强上涨趋势)
        - WEAK_TREND_UP (弱上涨趋势)
        - RANGE_BOUND (震荡)
        - WEAK_TREND_DOWN (弱下跌趋势)
        - STRONG_TREND_DOWN (强下跌趋势)
        - HIGH_VOLATILITY (高波动)
        - LOW_VOLATILITY (低波动)
        """
        adx = calculate_adx(data)
        trend_direction = self.get_trend_direction(data)
        volatility = self.get_volatility_percentile(data)
        
        if adx > 30:
            return "STRONG_TREND_" + trend_direction
        elif adx < 20:
            return "RANGE_BOUND"
        else:
            return "WEAK_TREND_" + trend_direction
```

#### 2. 策略 - 市场状态匹配

| 市场状态 | 推荐策略 | 禁用策略 |
|----------|----------|----------|
| 强上涨趋势 | SuperTrend, MACD | RSI 做空 |
| 强下跌趋势 | SuperTrend, MACD | RSI 做多 |
| 震荡市 | RSI, Bollinger | 趋势跟踪 |
| 高波动 | 降低仓位 | 突破策略 |
| 低波动 | 突破策略 | - |

---

### 五、新策略开发 (优先级: 中)

#### 1. 多时间周期策略

```python
class MultiTimeframeStrategy:
    """多时间周期共振策略"""
    
    def check_signal(self, code):
        # 日线趋势
        daily_trend = self.get_trend(code, '1D')
        # 1 小时趋势
        hourly_trend = self.get_trend(code, '1H')
        # 15 分钟入场
        entry_signal = self.get_entry(code, '15M')
        
        # 只在多周期共振时交易
        if daily_trend == hourly_trend == entry_signal:
            return Signal(...)
```

#### 2. 配对交易策略

```python
class PairsTrading:
    """统计套利策略"""
    
    def find_pairs(self):
        # 基于相关性找出配对股票
        # 如：腾讯 vs 阿里 (同为互联网)
        pass
    
    def check_spread(self, pair):
        # 计算价差 Z-Score
        # Z > 2: 做空高价股，做多低价股
        # Z < -2: 反向操作
        pass
```

#### 3. 季节性策略

```python
class SeasonalStrategy:
    """季节性/日历效应策略"""
    
    # 一月效应
    # 周末效应
    # 财报季效应
    # 节假日效应
```

---

### 六、回测框架 (优先级: 最高)

#### 当前缺失

❌ 无历史数据回测能力  
❌ 无策略性能评估  
❌ 无参数优化功能

#### 建议架构

```python
class Backtester:
    """回测引擎"""
    
    def __init__(self, strategy, data):
        self.strategy = strategy
        self.data = data  # 历史 K 线数据
        
    def run(self, initial_capital=100000):
        """运行回测"""
        for bar in self.data:
            signal = self.strategy.on_kline(bar)
            if signal:
                self.execute(signal)
        
        return self.calculate_metrics()
    
    def calculate_metrics(self):
        """计算性能指标"""
        return {
            'total_return': ...,
            'sharpe_ratio': ...,
            'max_drawdown': ...,
            'win_rate': ...,
            'profit_factor': ...,
            'avg_trade': ...,
            'consecutive_losses': ...,
        }
```

#### 关键性能指标

| 指标 | 目标值 | 说明 |
|------|--------|------|
| **年化收益率** | >20% | 跑赢大盘 |
| **夏普比率** | >1.5 | 风险调整后收益 |
| **最大回撤** | <15% | 风险控制 |
| **胜率** | >45% | 不需要很高 |
| **盈亏比** | >2.0 | 赚多赔少 |
| **盈利因子** | >1.5 | 总盈利/总亏损 |

---

### 七、参数优化 (优先级: 中)

#### 1. 网格搜索

```python
def optimize_rsi():
    best_params = None
    best_sharpe = 0
    
    for period in range(7, 21, 2):
        for overbought in range(60, 80, 5):
            for oversold in range(20, 40, 5):
                sharpe = backtest(period, overbought, oversold)
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = (period, overbought, oversold)
    
    return best_params
```

#### 2. 遗传算法 (进阶)

```python
class GeneticOptimizer:
    """遗传算法参数优化"""
    
    def evolve(self, generations=100, population=50):
        # 初始化种群
        # 选择、交叉、变异
        # 返回最优参数组合
        pass
```

#### 3. 前向分析 (Walk-Forward)

```
训练集 (6 个月) → 优化参数 → 测试集 (1 个月) → 验证
      ↓
滚动窗口
```

---

## 📋 实施路线图

### 第一阶段 (1-2 周): 基础增强

- [ ] 新增指标: ROC, OBV, ATR Percentile
- [ ] RSI 背离检测
- [ ] 市场状态识别模块
- [ ] 基础风险管理 (仓位、止损)

### 第二阶段 (2-4 周): 策略升级

- [ ] Confluence 策略加权评分
- [ ] 多时间周期框架
- [ ] SuperTrend 动态参数
- [ ] 策略 - 市场状态匹配

### 第三阶段 (4-6 周): 回测系统

- [ ] 历史数据下载和存储
- [ ] 回测引擎核心
- [ ] 性能指标计算
- [ ] 参数网格搜索

### 第四阶段 (6-8 周): 高级功能

- [ ] 遗传算法优化
- [ ] 配对交易策略
- [ ] 实时性能监控
- [ ] 自动报告生成

---

## 🔧 技术实现建议

### 1. 指标库扩展

```python
# app/strategy/indicators.py 新增

def roc(closes, period=10):
    """Rate of Change"""
    if len(closes) < period + 1:
        return None
    return (closes[-1] - closes[-period-1]) / closes[-period-1] * 100

def obv(closes, volumes):
    """On Balance Volume"""
    if len(closes) < 2:
        return None
    obv = 0
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]:
            obv += volumes[i]
        elif closes[i] < closes[i-1]:
            obv -= volumes[i]
    return obv

def atr_percentile(highs, lows, closes, period=14, lookback=100):
    """ATR 百分位"""
    if len(closes) < lookback + period:
        return None
    
    atr_values = []
    for i in range(period, len(closes)):
        atr = atr(highs[:i+1], lows[:i+1], closes[:i+1], period)
        if atr:
            atr_values.append(atr)
    
    current_atr = atr_values[-1]
    percentile = sum(1 for x in atr_values if x < current_atr) / len(atr_values)
    return percentile * 100
```

### 2. 配置扩展

```python
# config.py 新增

# 风险管理配置
RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.02"))  # 2%
MAX_TOTAL_EXPOSURE = float(os.getenv("MAX_TOTAL_EXPOSURE", "0.80"))  # 80%
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "0.05"))  # 5%
MAX_DRAWDOWN = float(os.getenv("MAX_DRAWDOWN", "0.15"))  # 15%

# 市场状态配置
ADX_TREND_THRESHOLD = float(os.getenv("ADX_TREND_THRESHOLD", "25"))
ADX_RANGE_THRESHOLD = float(os.getenv("ADX_RANGE_THRESHOLD", "20"))
VOLATILITY_HIGH_THRESHOLD = float(os.getenv("VOLATILITY_HIGH_THRESHOLD", "80"))  # 80 百分位
```

---

## 📊 预期效果

| 指标 | 当前 | 目标 | 提升 |
|------|------|------|------|
| 策略胜率 | ~40% | 50%+ | +25% |
| 盈亏比 | ~1.5 | 2.5+ | +67% |
| 最大回撤 | 未知 | <15% | 可控 |
| 夏普比率 | 未知 | >1.5 | 优秀 |
| 假信号过滤 | 基础 | 多层过滤 | 显著降低 |

---

## ⚠️ 注意事项

1. **不要过度优化** - 避免曲线拟合，保持策略简洁
2. **分散投资** - 不要把所有资金押在一个策略上
3. **持续监控** - 市场会变，策略需要定期评估
4. **风险控制优先** - 宁可错过，不要做错
5. **回测≠实盘** - 回测再好也要小资金验证

---

## 📚 参考资料

- 《Active Portfolio Management》- Grinold & Kahn
- 《Quantitative Trading》- Ernest Chan
- 《Advances in Financial Machine Learning》- Marcos López de Prado
- TradingView 指标库: https://www.tradingview.com/scripts/
- QuantConnect 策略库: https://www.quantconnect.com/lean

---

**下一步**: 告诉我你想优先实现哪个模块，我可以帮你写代码！🚀
