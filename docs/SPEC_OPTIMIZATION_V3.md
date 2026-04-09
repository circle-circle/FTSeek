# FTSeek 优化设计规格书 V3.0

**版本**: 3.0  
**日期**: 2026-03-23  
**状态**: 设计阶段 (待实施)

---

## 一、问题诊断

### 1.1 当前架构问题

#### 问题1: 策略重复投票
```
当前状态:
┌─────────────────────────────────────────────────────────┐
│                    共识引擎投票                          │
├─────────────────────────────────────────────────────────┤
│  RSI策略 (0.8)      ──► 投票                            │
│  MACD策略 (0.8)     ──► 投票   ← 与 confluence 内部重复 │
│  Bollinger策略 (0.8)──► 投票   ← 与 confluence 内部重复 │
│  SuperTrend策略 (1.0)──► 投票  ← 与 confluence 内部重复 │
│  confluence (1.2)   ──► 投票   ← 已包含上述所有因子！   │
│  confluence_v2 (1.0)──► 投票   ← 又包含了一遍！         │
└─────────────────────────────────────────────────────────┘
结果: 同一信号被重复计算，分数虚高，假信号增多
```

**影响**:
- `confluence` 策略内部已使用 RSI、MACD、Bollinger、SuperTrend、MA Cross
- 这些因子又被独立策略重复投票
- 导致信号分数虚高，假信号增多

#### 问题2: 固定60秒窗口
```python
# 当前代码
self.window = 60  # 固定60秒
```
**问题**: 不适应不同市场节奏
- 趋势市需要更长窗口确认
- 震荡市需要更短窗口捕捉

#### 问题3: 固定阈值
```python
# 当前代码
self.threshold = 1.5  # 固定阈值
```
**问题**: 不适应市场波动
- 高波动期需要更高阈值过滤噪音
- 低波动期需要更低阈值捕捉信号

#### 问题4: 新闻情绪权重过高
```python
# 当前代码
"sentiment_ai": 0.9,    # 权重过高
"news_sentiment": 0.8,  # 与资金流向情绪重复
```
**问题**: 
- 情绪数据更新频率低（分钟级）
- 单一新闻事件可能造成误判
- 作为主变量权重过高

#### 问题5: 交易成本未考虑
**问题**: 
- 港股印花税 0.1% + 佣金最低3港币
- 高频信号可能导致成本侵蚀利润

---

## 二、优化目标

| 目标 | 指标 | 当前值 | 目标值 |
|------|------|--------|--------|
| 信号质量 | 胜率 | 23% | ≥55% |
| 信号频率 | 日信号数 | 150+ | 20-50 |
| 成本控制 | 日均交易成本 | 未知 | <利润10% |
| 策略冗余 | 重复投票比例 | ~60% | 0% |

---

## 三、优化方案设计

### 3.1 策略层级架构 (消除重复)

```
┌────────────────────────────────────────────────────────────┐
│                     策略分层模型 V3                         │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  【第一层: 原子策略】(不可再分的基础因子)                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐      │
│  │ RSI      │ │ MACD     │ │ Bollinger│ │ SuperTrend│      │
│  │ 权重:0.5 │ │ 权重:0.5 │ │ 权重:0.5 │ │ 权重:0.6 │      │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘      │
│                                                            │
│  【第二层: 组合策略】(内部调用原子策略，输出单一投票)        │
│  ┌───────────────────────────────────────────────────┐    │
│  │ confluence (融合RSI+MACD+Bollinger+SuperTrend+MA) │    │
│  │ 权重: 1.2 (替代所有原子策略的投票)                  │    │
│  │ 投票数: 1票 (不再是5票)                            │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【第三层: 元策略】(跨周期/跨市场决策)                      │
│  ┌───────────────────────────────────────────────────┐    │
│  │ market_regime + trend_filter + capital_flow       │    │
│  │ 功能: 确认/过滤，不参与投票                        │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【确认层: 情绪/资金流】(降级为确认变量)                    │
│  ┌─────────────────┐ ┌─────────────────┐                  │
│  │ sentiment_ai    │ │ capital_flow    │                  │
│  │ 功能: 过滤/确认 │ │ 功能: 过滤/确认 │                  │
│  │ 投票权: 0       │ │ 投票权: 0       │                  │
│  └─────────────────┘ └─────────────────┘                  │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

#### 策略分组定义

| 分组 | 策略 | 投票权 | 说明 |
|------|------|--------|------|
| **原子层** | rsi, macd, bollinger, supertrend, ma_cross | 各0.5 | 基础因子，可单独使用 |
| **组合层** | confluence, confluence_v2 | 各1.2 | 替代原子层，避免重复 |
| **信号层** | price_break, volume_alert | 各0.4 | 辅助信号 |
| **确认层** | sentiment_ai, capital_flow | 0 | 不投票，仅确认/过滤 |

#### 互斥规则
```python
# 当启用 confluence 时，自动禁用其包含的原子策略
EXCLUSION_RULES = {
    "confluence": ["rsi", "macd", "bollinger", "supertrend", "ma_cross"],
    "confluence_v2": ["rsi", "macd", "bollinger", "supertrend", "ma_cross"],
}
```

---

### 3.2 多周期窗口设计

```
┌────────────────────────────────────────────────────────────┐
│                    多周期窗口系统                           │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  日线窗口 (T+1决策)                                        │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ 窗口: 收盘后 → 次日开盘前                           │  │
│  │ 策略: MA趋势, 均线支撑/压力                         │  │
│  │ 输出: 日线方向 (up/down/neutral)                    │  │
│  └─────────────────────────────────────────────────────┘  │
│                          ↓                                 │
│  小时窗口 (波段确认)                                       │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ 窗口: 300秒 (5分钟)                                 │  │
│  │ 策略: MACD, Bollinger, SuperTrend                   │  │
│  │ 输出: 波段方向确认                                  │  │
│  └─────────────────────────────────────────────────────┘  │
│                          ↓                                 │
│  分钟窗口 (入场时机)                                       │
│  ┌─────────────────────────────────────────────────────┐  │
│  │ 窗口: 动态 30-120秒 (根据波动率调整)                │  │
│  │ 策略: RSI, volume_alert                            │  │
│  │ 输出: 入场信号                                      │  │
│  └─────────────────────────────────────────────────────┘  │
│                                                            │
│  多周期对齐: 日线↑ + 小时↑ + 分钟↑ → 高置信度信号         │
│              日线↓ + 小时↑ + 分钟↑ → 低置信度/过滤        │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

#### 窗口配置
```python
MULTI_TIMEFRAME_WINDOWS = {
    "daily": {
        "window": 86400,  # 24小时
        "strategies": ["ma_trend", "support_resistance"],
        "weight": 0.3,  # 日线方向权重
    },
    "hourly": {
        "window": 300,   # 5分钟
        "strategies": ["macd", "bollinger", "supertrend"],
        "weight": 0.4,
    },
    "minute": {
        "window": "dynamic",  # 动态窗口
        "min": 30,
        "max": 120,
        "adjust_by": "volatility",  # 根据波动率调整
        "strategies": ["rsi", "volume_alert"],
        "weight": 0.3,
    },
}
```

---

### 3.3 动态阈值设计

```python
class DynamicThresholdManager:
    """动态阈值管理器"""
    
    def __init__(self):
        self.base_threshold = 1.0
        self.min_threshold = 0.6
        self.max_threshold = 2.0
    
    def calculate_threshold(
        self,
        volatility: float,      # ATR% (0.01-0.05)
        adx: float,             # ADX值 (10-50)
        signal_count: int,      # 最近1小时信号数
        win_rate: float,        # 近期胜率
    ) -> float:
        """
        动态计算阈值
        
        高波动 + 低ADX + 高信号数 → 提高阈值 (过滤噪音)
        低波动 + 高ADX + 低信号数 → 降低阈值 (捕捉信号)
        """
        # 波动率调整因子
        vol_factor = 1.0 + (volatility - 0.02) * 5  # 波动率±2% → 阈值±0.1
        
        # ADX调整因子 (趋势强 = 可以降低阈值)
        adx_factor = 1.0 - (adx - 20) * 0.005  # ADX=30 → 阈值×0.95
        
        # 信号频率调整因子 (信号多 = 提高阈值)
        freq_factor = 1.0 + max(0, (signal_count - 20)) * 0.01
        
        # 胜率调整因子 (胜率高 = 可以降低阈值)
        win_factor = 1.0 - (win_rate - 0.5) * 0.2
        
        threshold = self.base_threshold * vol_factor * adx_factor * freq_factor * win_factor
        return max(self.min_threshold, min(self.max_threshold, threshold))
```

---

### 3.4 情绪策略降级设计

```
┌────────────────────────────────────────────────────────────┐
│                   情绪策略降级方案                          │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  【当前】情绪作为投票变量                                   │
│  ┌───────────────────────────────────────────────────┐    │
│  │ score = Σ(strategy_weight × direction)            │    │
│  │        + sentiment_weight × sentiment_direction   │ ← 问题│
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【优化后】情绪作为确认/过滤变量                            │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 1. 先计算技术信号分数                              │    │
│  │    tech_score = Σ(tech_strategy_weight × dir)     │    │
│  │                                                    │    │
│  │ 2. 技术信号达到阈值后，检查情绪确认                │    │
│  │    if tech_score >= threshold:                    │    │
│  │        if sentiment_direction == signal_direction:│    │
│  │            confidence *= 1.2  # 情绪一致，置信度提升│    │
│  │        elif abs(sentiment_score) > 0.5:           │    │
│  │            confidence *= 0.7  # 情绪强烈反向，降权 │    │
│  │        # 情绪中性或弱反向，不影响                   │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【情绪过滤规则】                                           │
│  - 极端负面情绪 (score < -0.7) + BUY信号 → 过滤           │
│  - 极端正面情绪 (score > 0.7) + SELL信号 → 过滤           │
│  - 情绪数据过期 (>30分钟) → 不参与确认                     │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

### 3.5 退出引擎增强设计

```
┌────────────────────────────────────────────────────────────┐
│                    组合式退出引擎 V2                        │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  【退出优先级队列】(数字越小越优先)                         │
│                                                            │
│  P1: 硬止损 (ATR止损)                                      │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 触发: 价格跌破 entry - 1.5×ATR                    │    │
│  │ 动作: 立即全仓平仓                                │    │
│  │ 优先级: 最高                                      │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  P2: 结构止损 (百分比止损)                                 │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 触发: 亏损达到 max(2%, 1.5×ATR%)                  │    │
│  │ 动作: 立即全仓平仓                                │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  P3: 时间止损                                              │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 触发: 持仓时间 > max_holding_hours (默认24h)      │    │
│  │ 动作: 全仓平仓 (除非盈利>3%且趋势向上)            │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  P4: 移动止盈                                              │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 触发: 盈利>5%后，从峰值回撤>2×ATR                 │    │
│  │ 动作: 全仓平仓                                    │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  P5: 分批止盈                                              │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 触发: 盈利>3% → 平仓30%                           │    │
│  │       盈利>5% → 平仓剩余                          │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  P6: 固定止盈                                              │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 触发: 盈利达到 take_profit (动态配置)             │    │
│  │ 动作: 全仓平仓                                    │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【新增: 成本感知退出】                                     │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 最小盈利要求 = 交易成本 × 2                        │    │
│  │ 港股: 佣金3港币 + 印花税0.1% + 交易费 ≈ 0.15%     │    │
│  │ 美股: 佣金0 + 交易费 ≈ 0.02%                       │    │
│  │                                                    │    │
│  │ 止盈阈值 = max(固定止盈, 成本×3)                  │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

### 3.6 市场状态自适应权重

```python
class MarketRegimeWeightManager:
    """市场状态权重管理器"""
    
    # 市场状态定义
    REGIMES = {
        "strong_trend_up": {
            "adx_range": (30, 100),
            "trend": "up",
            "weight_adjust": {
                "supertrend": 1.5,    # 趋势策略加强
                "macd": 1.3,
                "ma_cross": 1.2,
                "rsi": 0.6,           # 震荡策略减弱
                "bollinger": 0.7,
                "confluence": 1.3,
            },
        },
        "weak_trend_up": {
            "adx_range": (20, 30),
            "trend": "up",
            "weight_adjust": {
                "supertrend": 1.2,
                "macd": 1.1,
                "rsi": 0.8,
                "bollinger": 0.9,
            },
        },
        "range_bound": {
            "adx_range": (0, 20),
            "trend": "neutral",
            "weight_adjust": {
                "supertrend": 0.6,    # 趋势策略减弱
                "macd": 0.8,
                "rsi": 1.5,           # 震荡策略加强
                "bollinger": 1.4,
                "confluence": 1.2,
            },
        },
        "weak_trend_down": {
            "adx_range": (20, 30),
            "trend": "down",
            "weight_adjust": {
                "supertrend": 1.2,
                "macd": 1.1,
                "rsi": 0.8,
            },
        },
        "strong_trend_down": {
            "adx_range": (30, 100),
            "trend": "down",
            "weight_adjust": {
                "supertrend": 1.5,
                "macd": 1.3,
                "rsi": 0.6,
            },
        },
        "high_volatility": {
            "atr_pct": (0.03, 1.0),
            "weight_adjust": {
                # 高波动时提高阈值，降低权重
                "__threshold_multiplier__": 1.3,
            },
        },
    }
    
    def get_adjusted_weights(
        self,
        base_weights: Dict[str, float],
        adx: float,
        trend: str,
        atr_pct: float,
    ) -> Tuple[Dict[str, float], float]:
        """
        返回调整后的权重和阈值乘数
        """
        regime = self._detect_regime(adx, trend, atr_pct)
        adjustments = self.REGIMES.get(regime, {}).get("weight_adjust", {})
        
        adjusted_weights = {}
        threshold_mult = 1.0
        
        for strategy, base_weight in base_weights.items():
            adj = adjustments.get(strategy, 1.0)
            adjusted_weights[strategy] = base_weight * adj
        
        threshold_mult = adjustments.get("__threshold_multiplier__", 1.0)
        
        return adjusted_weights, threshold_mult
```

---

### 3.7 策略归因与自学习

```
┌────────────────────────────────────────────────────────────┐
│                  策略归因与自学习系统                        │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  【数据采集】                                               │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 每笔交易记录:                                      │    │
│  │ - signal_id: 信号ID                               │    │
│  │ - agree_strategies: ['supertrend', 'macd']       │    │
│  │ - disagree_strategies: ['rsi']                   │    │
│  │ - market_regime: 'trending'                      │    │
│  │ - entry_price, exit_price                        │    │
│  │ - pnl, pnl_pct                                   │    │
│  │ - hold_duration                                  │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【归因分析】                                               │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 策略贡献度 = Σ(该策略参与交易的pnl) / 交易次数    │    │
│  │                                                    │    │
│  │ 策略A胜率 = 该策略参与的盈利交易数 / 总交易数     │    │
│  │                                                    │    │
│  │ 单独贡献 = 该策略独自正确时的盈亏                  │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【权重调整】                                               │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 新权重 = 基准权重 × clamp(胜率/0.5, 0.7, 1.3)    │    │
│  │                                                    │    │
│  │ 调整周期: 每20笔交易或每24小时                     │    │
│  │ 调整限制: 单次最大±10%, 累计最大±30%              │    │
│  │ 最小样本: 5笔交易                                  │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【市场状态归因】                                           │
│  ┌───────────────────────────────────────────────────┐    │
│  │ trending_market:                                  │    │
│  │   supertrend: 胜率65%, 贡献+1200                   │    │
│  │   rsi: 胜率35%, 贡献-800                          │    │
│  │                                                    │    │
│  │ range_market:                                     │    │
│  │   rsi: 胜率58%, 贡献+600                          │    │
│  │   supertrend: 胜率40%, 贡献-400                   │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

### 3.8 交易成本与流动性约束

```
┌────────────────────────────────────────────────────────────┐
│                  成本与流动性约束系统                        │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  【交易成本模型】                                           │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 港股:                                              │    │
│  │   佣金 = max(交易额×0.03%, 3港币)                 │    │
│  │   印花税 = 交易额×0.1% (仅卖出)                   │    │
│  │   交易费 = 交易额×0.005%                          │    │
│  │   总成本 ≈ 买入0.035% + 卖出0.135% = 0.17%       │    │
│  │                                                    │    │
│  │ 美股:                                              │    │
│  │   佣金 = 0                                         │    │
│  │   交易费 ≈ 0.02%                                   │    │
│  │   总成本 ≈ 0.04%                                   │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【最小盈利要求】                                           │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 最小预期盈利 = 总成本 × 2                          │    │
│  │ 港股: 最低 0.34%                                   │    │
│  │ 美股: 最低 0.08%                                   │    │
│  │                                                    │    │
│  │ 信号过滤:                                          │    │
│  │   if expected_pnl_pct < min_profit:              │    │
│  │       reject_signal("预期盈利不足以覆盖成本")      │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【流动性约束】                                             │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 日均成交额 < 5000万港币 → 最大仓位减半            │    │
│  │ 日均成交额 < 1000万港币 → 禁止交易                │    │
│  │                                                    │    │
│  │ 买卖价差 > 1% → 禁止市价单，必须限价              │    │
│  │ 买卖价差 > 2% → 禁止交易                          │    │
│  │                                                    │    │
│  │ 开收盘15分钟 → 禁止大额订单 (避免冲击成本)        │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
│  【日内交易频率限制】                                       │
│  ┌───────────────────────────────────────────────────┐    │
│  │ 单标的日内交易次数限制:                            │    │
│  │   max_daily_trades = 3 + (日均成交额/10亿)×2      │    │
│  │   泡泡玛特 (日成交5亿) → max=4次                  │    │
│  │   中芯国际 (日成交20亿) → max=7次                 │    │
│  │                                                    │    │
│  │ 成本预算:                                          │    │
│  │   日均交易成本 < 日均预期利润的20%                 │    │
│  └───────────────────────────────────────────────────┘    │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

---

## 四、模块接口设计

### 4.1 新增模块

| 模块 | 文件路径 | 功能 |
|------|----------|------|
| 策略层级管理器 | `app/strategy/strategy_layer.py` | 管理策略分层和互斥规则 |
| 多周期窗口管理器 | `app/signal_engine/multi_timeframe.py` | 管理多周期窗口和信号对齐 |
| 动态阈值管理器 | `app/signal_engine/dynamic_threshold.py` | 动态计算信号阈值 |
| 情绪确认器 | `app/factors/sentiment_confirm.py` | 情绪作为确认/过滤变量 |
| 成本约束检查器 | `app/risk/cost_constraint.py` | 交易成本和流动性检查 |
| 策略归因器 | `app/analytics/strategy_attribution.py` | 策略归因分析和权重学习 |

### 4.2 修改模块

| 模块 | 文件路径 | 修改内容 |
|------|----------|----------|
| 共识引擎 | `app/strategy/consensus_engine.py` | 集成多周期、动态阈值、策略分层 |
| 退出引擎 | `app/risk/dynamic_exit_manager.py` | 增强退出优先级、成本感知 |
| 数据订阅器 | `app/data_subscriber.py` | 集成成本约束、流动性检查 |

---

## 五、配置文件设计

### 5.1 strategy_layers.yaml

```yaml
# 策略分层配置
version: "3.0"

layers:
  atomic:
    description: "原子策略 - 基础因子"
    strategies:
      - name: rsi
        base_weight: 0.5
        enabled: false  # 被 confluence 替代
      - name: macd
        base_weight: 0.5
        enabled: false
      - name: bollinger
        base_weight: 0.5
        enabled: false
      - name: supertrend
        base_weight: 0.6
        enabled: false

  composite:
    description: "组合策略 - 替代原子策略投票"
    strategies:
      - name: confluence
        base_weight: 1.2
        enabled: true
        replaces: [rsi, macd, bollinger, supertrend]
      - name: confluence_v2
        base_weight: 1.0
        enabled: true
        replaces: [rsi, macd, bollinger, supertrend]

  signal:
    description: "信号策略 - 辅助信号"
    strategies:
      - name: price_break
        base_weight: 0.4
        enabled: true
      - name: volume_alert
        base_weight: 0.3
        enabled: true

  confirmation:
    description: "确认策略 - 不参与投票，仅确认/过滤"
    strategies:
      - name: sentiment_ai
        mode: confirm
        confidence_boost: 0.2  # 情绪一致时置信度+20%
        confidence_penalty: 0.3  # 情绪反向时置信度-30%
      - name: capital_flow
        mode: filter
        extreme_inflow_filter: false  # 极端流入不过滤
        extreme_outflow_filter: true  # 极端流出过滤BUY信号

exclusion_rules:
  confluence: [rsi, macd, bollinger, supertrend, ma_cross]
  confluence_v2: [rsi, macd, bollinger, supertrend, ma_cross]
```

### 5.2 multi_timeframe.yaml

```yaml
# 多周期窗口配置
version: "3.0"

timeframes:
  daily:
    window_seconds: 86400
    strategies: [ma_trend, support_resistance]
    weight: 0.3
    output: trend_direction  # up/down/neutral

  hourly:
    window_seconds: 300
    strategies: [macd, bollinger, supertrend]
    weight: 0.4
    min_votes: 2

  minute:
    window_mode: dynamic
    window_min: 30
    window_max: 120
    adjust_by: volatility
    strategies: [rsi, volume_alert]
    weight: 0.3

alignment_rules:
  full_alignment:
    # 日线↑ + 小时↑ + 分钟↑ → 高置信度
    description: "三周期同向"
    confidence_multiplier: 1.3

  partial_alignment:
    # 日线↑ + 小时↑ + 分钟↓ → 中等置信度
    description: "两周期同向"
    confidence_multiplier: 1.0

  counter_trend:
    # 日线↓ + 小时↑ + 分钟↑ → 低置信度
    description: "逆日线信号"
    confidence_multiplier: 0.5
```

### 5.3 cost_constraints.yaml

```yaml
# 成本与流动性约束配置
version: "3.0"

trading_costs:
  HK:
    commission_rate: 0.0003
    commission_min: 3.0  # 港币
    stamp_duty: 0.001    # 仅卖出
    trading_fee: 0.00005
    total_estimated: 0.0017  # 约0.17%

  US:
    commission_rate: 0
    trading_fee: 0.0002
    total_estimated: 0.0004  # 约0.04%

min_profit_requirements:
  HK: 0.0034  # 0.34%
  US: 0.0008  # 0.08%
  multiplier: 2.0  # 成本×2

liquidity_constraints:
  min_daily_volume_hkd: 10000000  # 1000万港币
  min_daily_volume_usd: 5000000   # 500万美元
  
  position_limit:
    low_liquidity: 0.05   # 流动性差 → 最大仓位5%
    medium_liquidity: 0.10
    high_liquidity: 0.15

  spread_limit:
    use_limit_order: 0.01   # 价差>1% → 必须限价单
    no_trade: 0.02          # 价差>2% → 禁止交易

daily_trade_limits:
  base_limit: 3
  volume_multiplier: 2.0  # 每10亿成交额+2次
  cost_budget_ratio: 0.2  # 日成本<预期利润20%
```

---

## 六、风险评估

### 6.1 技术风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 多周期同步复杂 | 中 | 使用消息队列解耦，添加超时机制 |
| 动态阈值过拟合 | 高 | 限制调整幅度，添加样本量检查 |
| 策略互斥遗漏 | 低 | 单元测试覆盖所有互斥组合 |

### 6.2 业务风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 信号减少过多 | 高 | 分阶段实施，监控信号质量 |
| 成本计算误差 | 中 | 使用保守估计，添加缓冲 |
| 流动性误判 | 中 | 多数据源验证，添加人工确认 |

---

## 七、验收标准

### 7.1 功能验收

- [ ] 策略重复投票比例 = 0%
- [ ] 多周期窗口正常工作
- [ ] 动态阈值根据市场状态调整
- [ ] 情绪策略不参与投票，仅确认/过滤
- [ ] 退出引擎按优先级执行
- [ ] 成本约束正确计算和过滤

### 7.2 性能验收

| 指标 | 当前值 | 目标值 |
|------|--------|--------|
| 日信号数 | 150+ | 20-50 |
| 信号胜率 | 23% | ≥55% |
| 策略冗余 | 60% | 0% |
| 成本占比 | 未知 | <10% |

---

*文档版本: 3.0*  
*创建时间: 2026-03-23*  
*状态: 待实施*