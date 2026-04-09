# 交易策略三维度可行性分析报告

**分析日期**: 2026-04-08  
**分析目标**: 评估在快速突发事件中获利、日常提升盈利胜率的策略逻辑可行性

---

## 一、执行摘要

当前 FTSeek 策略架构**已具备三维度自动权重分配的基础设施**，但存在以下改进空间：

| 维度 | 当前状态 | 可行性 | 改进建议 |
|------|---------|--------|---------|
| 1. 突发新闻权重 | 有 `is_breaking` 标志和快速通道，**无显式权重提升公式** | ⚠️ 中等 | 新增突发新闻权重倍增器 |
| 2. 港股美股双向跟随 | 代码结构支持双市场，**美股交易返回"暂不支持"** | ❌ 不可行 | 分阶段启用美港跨市场信号关联 |
| 3. 市场指标动态权重 | 已有 ADX 市场状态识别和 `regime_weights` | ✅ 可行 | 细化 ADX/波动率权重公式 |

**总体评估**: 策略逻辑框架可行，需针对快速突发事件场景进行权重公式增强。

---

## 二、维度一：突发新闻权重分配

### 2.1 当前实现

**已有的基础设施**:

```python
# news_integration.py:329-334
# 突发新闻快速通道：优先返回
breaking_news = [n for n in filtered_news if getattr(n, 'is_breaking', False)]
normal_news = [n for n in filtered_news if not getattr(n, 'is_breaking', False)]
result_news = breaking_news[:5] + normal_news[:15]
```

```python
# news_integration.py:215
analyzed_news.append({
    'is_breaking': getattr(news, 'is_breaking', False),
    ...
})
```

**新闻情绪分析器**:
- 动态缓存 TTL（基于 VIX 波动率）
- 情绪得分衰减（半衰期 30 分钟）
- 突发新闻优先排序

### 2.2 缺失环节

1. **权重提升公式缺失**: `consensus_engine.py` 中 `SENTIMENT_STRATEGIES` 没有针对突发新闻的显式权重增益
2. **情绪阈值动态调整**: 突发新闻场景下 `min_sentiment_score` 未自动放宽/收紧
3. **快速止盈触发**: 突发新闻驱动的交易没有专门的止盈参数

### 2.3 推荐改进方案

#### 方案 A: 共识层权重倍增（推荐）

在 `consensus_engine.py` 的 `_calc_weight()` 中增加突发新闻检查：

```python
def _calc_weight(self, strategy: str, regime: str, code: str = None) -> float:
    base = self.base_weights.get(strategy, 0.5)
    
    # 突发新闻权重倍增
    if strategy in SENTIMENT_STRATEGIES and code:
        has_breaking_news = self._check_breaking_news(code, lookback_hours=2)
        if has_breaking_news:
            base *= 2.0  # 突发新闻权重翻倍
            logger.info(f"[突发新闻] {code} 权重 x2.0")
    
    # ... 原有市场状态调整逻辑
```

#### 方案 B: 情绪得分阈值动态调整

在 `sentiment_ai.py` 中根据新闻时效性调整阈值：

```python
# 突发新闻场景：降低阈值至 0.3（原 0.5），提高敏感度
if has_breaking_news:
    self.min_sentiment_score = 0.3
else:
    self.min_sentiment_score = 0.5
```

#### 方案 C: 快速止盈参数（配合风险管理）

在 `risk_config.yaml` 中新增突发新闻止盈配置：

```yaml
breaking_news_exit:
  enabled: true
  take_profit_pct: 2.0      # 快速止盈 2%（原 4%）
  trailing_stop_pct: 0.5    # 紧密追踪 0.5%（原 1.5%）
  max_holding_minutes: 30   # 最长持仓 30 分钟
```

### 2.4 可行性评分：7/10

**优势**:
- 已有 `is_breaking` 标志和快速通道
- 情绪衰减机制已实现
- 动态 TTL 基于波动率

**劣势**:
- 权重公式未与突发新闻联动
- 缺少专门的止盈参数配置
- 突发新闻定义依赖采集器，可能漏检

---

## 三、维度二：港股美股双向跟随策略

### 3.1 当前实现

**代码结构支持双市场**:

```python
# data_subscriber.py
# 订阅港股 + 美股行情（数据结构已支持）
if code.startswith("HK."):
    # 港股处理逻辑
elif code.startswith("US."):
    # 美股处理逻辑
```

**实际交易限制**:

```python
# trader/executor.py
def execute_trade(code, action, ...):
    if code.startswith("US."):
        return False, "暂不支持美股交易"  # 硬编码限制
```

**共识引擎中的策略分类**:

```python
# consensus_engine.py:72-82
TREND_STRATEGIES = {"supertrend", "macd", "ma_cross"}
OSCILLATOR_STRATEGIES = {"rsi", "bollinger"}
# 无跨市场关联策略
```

### 3.2 缺失环节

1. **跨市场信号关联**: 无逻辑关联港股和美股的同类标的（如腾讯控股 vs 纳斯达克 ETF）
2. **时区处理**: 美股交易时段与港股不同，无时区同步机制
3. **交易执行**: 美股交易接口未实现

### 3.3 推荐改进方案（分阶段）

#### 阶段 1: 跨市场信号关联（低门槛）

创建 `app/strategy/cross_market_correlation.py`:

```python
"""
跨市场信号关联器

功能:
1. 识别港股美股关联标的
2. 美股开盘后港股跟随信号
3. 美股前一日收盘对港股开盘影响
"""

CROSS_MARKET_PAIRS = {
    "HK.00700": {"us_pair": "US.FUTU", "correlation": 0.75},  # 腾讯 vs 富途
    "HK.09988": {"us_pair": "US.BABA", "correlation": 0.90},  # 阿里 vs 阿里美股
    "HK.03690": {"us_pair": "US.MTCN", "correlation": 0.60},  # 美团 vs 中概互联
}

def check_cross_market_signal(hk_code: str, hk_action: str) -> Tuple[bool, str]:
    """
    检查跨市场信号
    
    逻辑:
    1. 美股前一日收盘涨跌幅 > 3% → 港股开盘跟随
    2. 美股盘中涨跌幅 > 5% → 港股午盘后跟随
    3. 中概股指数异动 → 港股科技股跟随
    """
```

#### 阶段 2: 时间同步机制

```python
# app/strategy/cross_market_timing.py

MARKET_HOURS = {
    "HK": {"open": "09:30", "close": "16:00", "timezone": "Asia/Hong_Kong"},
    "US": {"open": "21:30", "close": "04:00", "timezone": "America/New_York"},
}

def is_market_overlap(hk_code: str, us_code: str) -> bool:
    """检查双市场是否同时开盘"""
    # 港股 14:00-16:00 = 美股 02:00-04:00（次日）
    # 重叠时段信号权重更高
```

#### 阶段 3: 交易执行启用

修改 `trader/executor.py`:

```python
def execute_trade(code, action, ...):
    if code.startswith("US."):
        # 新增：调用富途美股 API
        return self._execute_us_trade(code, action, ...)
```

### 3.4 可行性评分：4/10（当前） → 8/10（分阶段实施后）

**当前障碍**:
- 美股交易硬编码禁止
- 无跨市场关联逻辑
- 时区同步缺失

**实施建议**:
1. **优先阶段 1**（1-2 天）：信号关联不触及交易执行，风险低
2. **验证相关性**（1 周）：收集数据验证关联标的实际相关性
3. **阶段 2-3**（实盘后）：稳定后再考虑跨市场交易

---

## 四、维度三：基于行情指标的动态权重

### 4.1 当前实现

**ADX 市场状态识别** (`app/strategy/market_regime.py`):

```python
def detect(code: str) -> MarketRegimeType:
    adx_val = adx(highs, lows, closes, 14)
    
    if adx_val > 25:
        return MarketRegimeType.STRONG_TREND_UP  # 或 DOWN
    elif adx_val < 20:
        return MarketRegimeType.RANGE_BOUND
    else:
        return MarketRegimeType.NEUTRAL
```

**动态因子权重** (`confluence_v2.py:60-76`):

```python
self.regime_weights = {
    MarketRegimeType.STRONG_TREND_UP: {
        "supertrend": 0.8, "macd": 1.3, "rsi": 0.7, "bollinger": 0.8,
    },
    MarketRegimeType.RANGE_BOUND: {
        "rsi": 1.5, "bollinger": 1.5, "supertrend": 0.3, "macd": 0.7,
    },
    MarketRegimeType.HIGH_VOLATILITY: {
        "bollinger": 1.3, "supertrend": 0.4, "rsi": 0.9,
    },
}
```

**共识层权重调整** (`consensus_engine.py:325-347`):

```python
def _calc_weight(self, strategy: str, regime: str) -> float:
    base = self.base_weights.get(strategy, 0.5)
    
    if regime == "trending":
        if strategy in TREND_STRATEGIES:
            return base * TREND_BOOST  # 1.5
        elif strategy in OSCILLATOR_STRATEGIES:
            return base * TREND_DECAY   # 0.6
    # ...
```

### 4.2 缺失环节

1. **ADX 粒度不足**: 仅使用 25/20 阈值，未利用 ADX 连续值
2. **波动率权重公式**: 高波动场景权重调整较粗糙
3. **VIX 联动**: VIX 监控器已存在但未与权重公式深度集成

### 4.3 推荐改进方案

#### 方案 A: ADX 连续权重公式

```python
def _calc_weight(self, strategy: str, adx_value: float) -> float:
    base = self.base_weights.get(strategy, 0.5)
    
    # ADX 连续权重函数（非线性）
    if strategy in TREND_STRATEGIES:
        # ADX 20-50 线性增长，50+ 饱和
        trend_factor = min(1.0, max(0.0, (adx_value - 20) / 30))
        return base * (1.0 + 0.5 * trend_factor)  # 0.5x ~ 1.5x
    
    elif strategy in OSCILLATOR_STRATEGIES:
        # ADX 越低，震荡策略权重越高
        range_factor = max(0.0, min(1.0, (40 - adx_value) / 20))
        return base * (1.0 + 0.5 * range_factor)  # 0.5x ~ 1.5x
```

#### 方案 B: 波动率调整权重

```python
def _get_volatility_adjustment(code: str) -> float:
    """
    基于 ATR/VIX 的波动率调整因子
    
    Returns:
        float: 调整因子 (0.5 ~ 1.5)
               < 0.7 = 低波动 (加仓)
               > 1.3 = 高波动 (减仓)
    """
    atr = get_atr(code, period=14)
    vix_state = get_vix_monitor().get_state()
    
    # ATR/价格 比率
    atr_ratio = atr / current_price if current_price > 0 else 0
    
    if atr_ratio > 0.05 or vix_state.state == "high_volatility":
        return 0.7  # 高波动，降低仓位
    elif atr_ratio < 0.02 and vix_state.state == "low_volatility":
        return 1.3  # 低波动，提高仓位
    else:
        return 1.0  # 正常
```

#### 方案 C: 综合权重公式（推荐）

```python
def calc_final_weight(strategy: str, code: str) -> float:
    """
    综合权重公式
    
    final_weight = base_weight × market_regime_factor × volatility_factor × news_factor
    """
    base = DEFAULT_BASE_WEIGHTS.get(strategy, 0.5)
    
    # 1. 市场状态因子（ADX）
    adx = get_adx(code)
    regime_factor = _get_regime_factor(strategy, adx)
    
    # 2. 波动率因子（ATR/VIX）
    vol_factor = _get_volatility_factor(code)
    
    # 3. 新闻因子（突发新闻）
    news_factor = _get_news_factor(code)
    
    final = base * regime_factor * vol_factor * news_factor
    
    # 限制范围
    return max(base * 0.5, min(base * 2.0, final))
```

### 4.4 可行性评分：9/10

**优势**:
- ADX 市场状态识别已实现
- `regime_weights` 已定义
- 共识引擎已有 `_calc_weight()` 框架
- VIX 监控器可用

**优化空间**:
- ADX 连续值权重公式
- 波动率与权重联动
- 多因子综合权重公式

---

## 五、综合决策链路设计

### 5.1 单笔交易决策流程图

```
┌─────────────────────────────────────────────────────────────────┐
│                     1. K 线数据到达                              │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     2. 市场状态识别                              │
│  - ADX 计算 → trending/ranging/neutral                          │
│  - ATR 计算 → volatility_state                                  │
│  - VIX 状态 → low/normal/high                                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                   3. 突发新闻检查（2 小时内）                      │
│  - is_breaking = True → news_factor = 2.0                       │
│  - sentiment_score > threshold → 确认方向                        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                  4. 各策略因子评估                                │
│  - confluence_v2: RSI/MACD/BB/SuperTrend/MA/Volume              │
│  - supertrend: 趋势跟踪                                          │
│  - sentiment_ai: 新闻情绪（加权突发新闻）                           │
│  → 各策略输出 (direction, base_weight)                          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                5. 动态权重计算（核心）                            │
│  weight = base × regime_factor × vol_factor × news_factor       │
│                                                                  │
│  示例：突发新闻 + 高波动 + 震荡市                                │
│  - confluence_v2: 1.5 × 0.6(regime) × 0.7(vol) × 2.0(news) = 1.26│
│  - supertrend:    1.0 × 0.6(regime) × 0.7(vol) × 1.0(no news)=0.42│
│  - sentiment_ai:  0.5 × 1.0(neutral) × 1.0(vol) × 2.0(news)=1.0 │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     6. 共识评分                                  │
│  score = Σ(weight × direction)                                  │
│  score >= +threshold → BUY                                      │
│  score <= -threshold → SELL                                     │
│  |score| < threshold → 跳过                                     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     7. 置信度计算                                │
│  confidence = technical_part(50%) + sentiment_part(50%)         │
│  - technical_part: score/threshold + 同向票数占比                │
│  - sentiment_part: 资金流向情绪（BUY 看流入，SELL 看流出）          │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     8. 风控检查                                  │
│  - 仓位限制：max_position_per_stock ≤ 20%                       │
│  - 止损检查：stop_loss_pct = -3% (或动态)                        │
│  - 价格检查：chase_reject_threshold ≤ 5%                        │
│  - 信号质量：quality_score ≥ 0.4                                │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     9. 执行交易                                  │
│  - 生成 trade_id (TradeChain)                                   │
│  - 记录决策链路：signal → consensus → risk → execution          │
│  - 调用 Futu API 下单                                            │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    10. 止盈止损监控                               │
│  - 正常市：TP=4%, SL=-3%                                        │
│  - 突发新闻：TP=2%, SL=-2%, max_hold=30min                     │
│  - 高波动：TP=8%, SL=-5%, trailing=3%                          │
└─────────────────────────────────────────────────────────────────┘
```

### 5.2 决策链路追踪 ID

每笔交易生成唯一 `trace_id`，贯穿全流程：

```python
# consensus_engine.py:558
trace_id = f"TRC-{uuid.uuid4().hex[:12].upper()}"

# 示例：TRC-A1B2C3D4E5F6

# 链路记录:
# 1. 信号层：SIG-XXXX → TRC-XXXX (consensus_engine.py:768)
# 2. 决策层：decision_data.trace_id = TRC-XXXX
# 3. 执行层：order_id → TRC-XXXX (trade_recorder.py)
# 4. 风控层：pnl_outcome → TRC-XXXX (signal_analytics.py)
```

### 5.3 输出示例（日志）

```
[2026-04-08 14:30:15] [市场状态] HK.00700 ADX=32.5 → trending, ATR=2.1% → normal_vol
[2026-04-08 14:30:15] [突发新闻] HK.00700 发现 2 条突发新闻 (14:00 财报超预期) → news_factor=2.0
[2026-04-08 14:30:16] [因子评估] HK.00700 confluence_v2 → BUY (RSI 45→55, MACD 金叉，SuperTrend up)
[2026-04-08 14:30:16] [因子评估] HK.00700 supertrend → BUY (趋势跟踪确认)
[2026-04-08 14:30:17] [因子评估] HK.00700 sentiment_ai → BUY (新闻情绪 0.82，突发×2.0)
[2026-04-08 14:30:18] [权重计算] HK.00700 
  - confluence_v2: 1.5 × 1.5(trend) × 1.0(vol) × 2.0(news) = 4.5
  - supertrend:    1.0 × 1.5(trend) × 1.0(vol) × 1.0(no news) = 1.5
  - sentiment_ai:  0.5 × 1.0(neutral) × 1.0(vol) × 2.0(news) = 1.0
[2026-04-08 14:30:18] [共识评分] HK.00700 score = +7.0 (阈值±1.5) → 强烈 BUY
[2026-04-08 14:30:18] [置信度] HK.00700 technical=0.85, sentiment=0.45 → confidence=0.92
[2026-04-08 14:30:19] [风控检查] HK.00700 仓位 15%/20% ✓, 止损 -3% ✓, 价格检查 ✓, 质量分 0.85 ✓
[2026-04-08 14:30:20] [交易执行] TRC-A1B2C3D4 买入 HK.00700 @420.5 × 200 股
[2026-04-08 14:30:25] [ DingTalk] 🟢 买入信号 | 腾讯控股 (HK.00700) 
  价格：420.5 HKD | 仓位：15% | 止损：407.9(-3%) | 止盈：437.3(+4%)
  决策链路：TRC-A1B2C3D4 | 置信度：92% | 突发新闻：是
```

---

## 六、实施优先级与时间估算

### P0: 核心决策链路可视化（1-2 天）

1. **日志增强**: 在关键节点输出 `trace_id` 和权重计算细节
2. **决策链路追踪**: 确保 `trade_recorder.py` 记录完整链路
3. **输出格式统一**: DingTalk 通知包含决策链路关键信息

### P1: 突发新闻权重增强（2-3 天）

1. **权重倍增器**: 修改 `consensus_engine._calc_weight()` 支持突发新闻检测
2. **快速止盈配置**: 在 `risk_config.yaml` 新增 `breaking_news_exit` 节
3. **测试验证**: 模拟突发新闻场景，验证权重提升和快速止盈触发

### P2: 市场指标权重细化（3-5 天）

1. **ADX 连续公式**: 实现非线性权重函数
2. **波动率联动**: 集成 VIX 监控器到权重计算
3. **综合权重公式**: 统一三维度因子乘法公式

### P3: 跨市场信号关联（1-2 周）

1. **阶段 1**: 关联标的映射和信号跟随逻辑
2. **阶段 2**: 时区同步和重叠时段识别
3. **阶段 3**: 实盘验证后启用美股交易

---

## 七、风险评估

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|---------|
| 突发新闻误判 | 中 | 中 | 设置 `min_sentiment_score` 下限，避免假新闻 |
| 权重过拟合 | 高 | 中 | UCB 探索机制 + 权重变化限制±30% |
| 跨市场关联失效 | 中 | 高 | 分阶段验证，先信号关联后交易 |
| 决策链路过长 | 低 | 低 | 异步日志，不阻塞交易流程 |

---

## 八、结论

**当前策略逻辑可行性评估**:

1. **突发新闻维度**: 7/10 - 基础设施完备，需权重公式增强
2. **跨市场维度**: 4/10 - 当前不可行，分阶段实施后可达 8/10
3. **市场指标维度**: 9/10 - 框架完备，仅需细化公式

**总体建议**: 
- **优先实施 P1（突发新闻权重）**，直接支持快速突发事件获利目标
- **同步实施 P0（决策链路可视化）**，提升可追溯性
- **跨市场关联延后**，待模拟盘稳定后分阶段启用

**预期效果**:
- 突发新闻场景反应时间缩短 50%（从分钟级降至秒级）
- 快速止盈触发更及时（2% vs 原 4%）
- 每笔交易决策链路可追溯（trace_id 贯穿全流程）
