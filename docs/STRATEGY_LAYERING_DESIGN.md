# FTSeek 分层策略系统设计文档

## 概述

当前系统已有基础架构，但缺乏**分层决策机制**和**事件驱动的快速响应能力**。本设计文档提出一个三层策略架构，整合消息面、跨市场套利和技术指标。

---

## 一、现状分析

### 1.1 现有策略架构
```
app/strategy/
├── confluence_v2.py    # 多因子融合策略 (RSI+MACD+BB+SuperTrend+MA+Volume)
├── sentiment_ai.py     # AI 情绪分析 (基于新闻)
├── supertrend.py       # 趋势跟踪
├── open_close_effect.py # 开盘/收盘效应
└── fusion_v2.py        # 融合引擎
```

**不足**:
- ❌ 没有股票分层/板块归类逻辑
- ❌ 突发事件响应慢 (依赖定时新闻抓取)
- ❌ 美港股跨市场联动未充分利用
- ❌ 缺乏快速下单机制

### 1.2 现有股票池管理
```
config/hk_us_mapping.json  # 50 对港股 - 美股 ADR 映射
├── 互联网：7 对
├── 金融：11 对
├── 能源：3 对
├── 新能源汽车：4 对
└── ...
```

**不足**:
- ❌ 只有 ADR 映射，没有板块/行业分类
- ❌ 没有优先级分层
- ❌ 没有动态股票池管理

---

## 二、分层策略架构设计

### 2.1 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Layer 3: 事件驱动层                            │
│              (突发利好/利空 → 大模型分析 → 高优信号)              │
├─────────────────────────────────────────────────────────────────┤
│                    Layer 2: 跨市场套利层                          │
│         (美股夜间表现 → 港股开盘套利 / 港股表现 → 美股期货)       │
├─────────────────────────────────────────────────────────────────┤
│                    Layer 1: 技术指标层                            │
│              (现有 confluence_v2 + 增强逻辑)                      │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│                    股票池分层管理                                 │
│  Tier 1: 核心股票 (高流动性 + 明确催化剂) → 权重 50%             │
│  Tier 2: 观察股票 (中等流动性 + 技术形态) → 权重 30%             │
│  Tier 3: 机会股票 (事件驱动) → 权重 20%                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、详细设计

### 3.1 股票分层与板块归类

#### 股票分层标准

| Tier | 标准 | 权重 | 更新频率 |
|------|------|------|---------|
| **Tier 1: 核心** | 日成交额>1 亿 + 明确催化剂 + 技术形态好 | 50% | 每日调整 |
| **Tier 2: 观察** | 日成交额>5000 万 + 技术形态 | 30% | 每日调整 |
| **Tier 3: 机会** | 突发事件/催化 | 20% | 实时调整 |

#### 板块分类配置

```yaml
# config/stock_pool.yaml
sectors:
  technology:
    name: "科技"
    stocks:
      - "HK.00700"  # 腾讯
      - "HK.03690"  # 美团
      - "HK.09988"  # 阿里
      - "US.AAPL"   # 苹果
      - "US.MSFT"   # 微软
      - "US.NVDA"   # 英伟达
    correlation_group: "tech_global"
    
  finance:
    name: "金融"
    stocks:
      - "HK.00005"  # 汇丰
      - "HK.02318"  # 平安
      - "HK.03968"  # 招行
    correlation_group: "hk_finance"
    
  ev_auto:
    name: "新能源汽车"
    stocks:
      - "HK.09866"  # 蔚来
      - "HK.02015"  # 理想
      - "HK.09868"  # 小鹏
      - "US.TSLA"   # 特斯拉
    correlation_group: "ev_global"
```

---

### 3.2 Layer 3: 事件驱动层 (基于大模型)

#### 功能
- 实时监听突发新闻/公告
- 使用大模型分析利好/利空程度
- 生成高优先级交易信号

#### 流程
```
新闻源 (API/RSS) 
    ↓
事件检测器 (关键词匹配 + 异常检测)
    ↓
大模型分析 (Qwen/GPT)
    - 情绪打分 (-1 到 1)
    - 影响程度评估 (high/medium/low)
    - 相关股票识别
    - 操作建议 (买入/卖出/观望)
    ↓
信号生成器
    ↓
优先推送 (钉钉/微信)
```

#### 大模型提示词模板
```python
EVENT_ANALYSIS_PROMPT = """
你是一名专业交易员，分析以下突发事件对股市的影响。

事件类型：{event_type}
事件内容：{event_content}
相关股票：{related_stocks}

请分析:
1. 情绪倾向 (正面/负面/中性) 及打分 (-1 到 1)
2. 影响范围 (行业/个股/大盘)
3. 影响程度 (高/中/低)
4. 建议操作 (买入/卖出/观望)
5. 时效性 (日内/1-3 天/1 周+)
6. 置信度 (0-100%)

输出格式 (严格 JSON):
{{
    "sentiment": "positive/negative/neutral",
    "sentiment_score": 0.75,
    "impact_scope": "industry/stock/market",
    "impact_level": "high/medium/low",
    "action": "buy/sell/hold",
    "time_horizon": "intraday/1-3days/1week+",
    "confidence": 85,
    "target_stocks": [
        {"code": "HK.00700", "direction": "long", "priority": 1}
    ]
}}
"""
```

---

### 3.3 Layer 2: 跨市场套利层

#### 套利逻辑

**场景 1: 美股夜间表现 → 港股开盘套利**
```
美股 (前一交易日) 大涨/大跌
    ↓
港股 ADR 对应股票 (隔夜涨幅计算)
    ↓
港股开盘 (9:30 AM) 快速下单
    - 如果 ADR 涨>3%，港股开盘追涨
    - 如果 ADR 跌>3%，港股开盘做空/回避
```

**场景 2: 港股表现 → 美股期货/ADR 预判**
```
港股白天大涨/大跌
    ↓
对应美股 ADR (盘前/盘后)
    ↓
美股开盘 (9:30 PM HK time) 布局
```

#### 实现代码结构
```python
class CrossMarketArbitrage:
    """跨市场套利策略"""
    
    def analyze_us_to_hk(self, us_stocks: List[str]) -> List[Signal]:
        """
        分析美股夜间表现，生成港股开盘信号
        """
        signals = []
        for us_code in us_stocks:
            # 获取美股隔夜涨幅
            us_change = self.get_overnight_change(us_code)
            
            # 获取对应港股
            hk_code = self.hk_us_mapping.get(us_code)
            
            if us_change > 0.03:  # 涨>3%
                signal = Signal(
                    code=hk_code,
                    action=SignalAction.BUY,
                    priority=PRIORITY.HIGH,  # 高优先级
                    reason=f"ADR 隔夜大涨 {us_change:.1%}",
                    entry_type="market_open"  # 开盘市价单
                )
                signals.append(signal)
            
            elif us_change < -0.03:  # 跌>3%
                signal = Signal(
                    code=hk_code,
                    action=SignalAction.SELL,
                    priority=PRIORITY.HIGH,
                    reason=f"ADR 隔夜大跌 {us_change:.1%}",
                    entry_type="market_open"
                )
                signals.append(signal)
        
        return signals
    
    def analyze_hk_to_us(self, hk_stocks: List[str]) -> List[Signal]:
        """
        分析港股白天表现，生成美股盘前信号
        """
        # 类似逻辑...
        pass
```

---

### 3.4 Layer 1: 技术指标增强层

#### 现有 confluence_v2 增强

**增强 1: 积小胜为大胜逻辑**
```python
class SmallWinAccumulator:
    """
    积小胜为大胜策略
    
    核心思想:
    - 降低单笔盈利预期 (目标 1-3%)
    - 提高胜率 (>60%)
    - 快速止盈 (到达目标价 50% 平仓)
    - 移动止损 (保护盈利)
    """
    
    def calculate_position(self, signal: Signal) -> PositionSize:
        # 高胜率信号 → 正常仓位
        # 中等信号 → 降仓位
        
        win_rate = self.get_recent_win_rate(signal.strategy)
        
        if win_rate > 0.65:
            return self.base_position * 1.0
        elif win_rate > 0.55:
            return self.base_position * 0.7
        else:
            return self.base_position * 0.5
```

**增强 2: 多周期共振**
```python
def check_multi_timeframe_alignment(self, code: str) -> float:
    """
    检查多周期共振
    
    同时满足:
    - 5 分钟：上涨
    - 15 分钟：上涨
    - 1 小时：上涨
    - 日线：上涨
    
    → 共振得分 1.0 (最强)
    → 3/4 周期同向 → 0.75
    → 2/4 周期同向 → 0.5
    """
    timeframes = ['5m', '15m', '1h', '1d']
    aligned_count = 0
    
    for tf in timeframes:
        trend = self.get_trend(code, tf)
        if trend == 'up':
            aligned_count += 1
    
    return aligned_count / len(timeframes)
```

---

## 四、实现计划

### Phase 1: 股票分层与板块归类 (4 小时)
- [ ] 创建 `config/stock_pool.yaml`
- [ ] 实现 `StockPoolManager` 类
- [ ] 添加 Tier 分层逻辑
- [ ] 集成到现有策略

### Phase 2: 事件驱动层 (8 小时)
- [ ] 实现 `EventDetector` (新闻源监听)
- [ ] 集成大模型分析 (`QwenAnalyzer` 增强)
- [ ] 实现 `EventDrivenStrategy`
- [ ] 钉钉高优推送

### Phase 3: 跨市场套利层 (6 小时)
- [ ] 实现 `CrossMarketArbitrage` 类
- [ ] 获取美股隔夜数据
- [ ] 实现港股开盘快速下单
- [ ] 回测套利策略

### Phase 4: 技术指标增强 (4 小时)
- [ ] 实现 `SmallWinAccumulator`
- [ ] 实现多周期共振检测
- [ ] 优化止盈止损逻辑

---

## 五、预期效果

| 指标 | 当前 | 目标 | 提升 |
|------|------|------|------|
| 月收益率 | ~5% | 8-12% | +60-140% |
| 胜率 | ~55% | 60-65% | +5-10pp |
| 月交易次数 | ~20 | ~30 | +50% |
| 单笔平均盈利 | ~2.5% | ~1.8% | - (但次数增加) |
| 最大回撤 | ~8% | ~6% | -25% |

---

## 六、风险与对策

### 6.1 风险
1. **事件驱动误判**: 大模型可能误读新闻
2. **跨市场执行延迟**: 开盘瞬间流动性不足
3. **过度交易**: 高频信号导致手续费增加

### 6.2 对策
1. **置信度阈值**: 只在置信度>80% 时执行
2. **限价单 + 滑点控制**: 避免追高杀跌
3. **手续费纳入回测**: 确保扣除费用后仍盈利

---

## 七、配置文件示例

### config/stock_pool.yaml
```yaml
version: "2.0"
updated: "2026-04-09"

tiers:
  tier1:
    name: "核心股票"
    weight: 0.50
    criteria:
      min_daily_volume: 100000000  # 1 亿
      min_market_cap: 50000000000  # 500 亿
      require_catalyst: true
      
  tier2:
    name: "观察股票"
    weight: 0.30
    criteria:
      min_daily_volume: 50000000  # 5000 万
      min_market_cap: 10000000000  # 100 亿
      
  tier3:
    name: "机会股票"
    weight: 0.20
    criteria:
      require_event: true

sectors:
  technology:
    name: "科技"
    stocks: ["HK.00700", "HK.03690", "HK.09988", "US.AAPL", "US.MSFT", "US.NVDA"]
    correlation_group: "tech_global"
    
  finance:
    name: "金融"
    stocks: ["HK.00005", "HK.02318", "HK.03968"]
    correlation_group: "hk_finance"

# 跨市场映射
cross_market:
  us_to_hk:
    "TCEHY": "HK.00700"
    "BABA": "HK.09988"
    "JD": "HK.09618"
```

---

## 八、核心 API 设计

```python
# 股票池管理
pool = StockPoolManager()
tier1_stocks = pool.get_tier1_stocks()
sector_stocks = pool.get_stocks_by_sector("technology")

# 事件驱动
event_strategy = EventDrivenStrategy()
signals = event_strategy.analyze_breaking_news(news_list)

# 跨市场套利
arb = CrossMarketArbitrage()
us_signals = arb.analyze_us_overnight()  # 美股→港股
hk_signals = arb.analyze_hk_daytime()    # 港股→美股

# 技术增强
tech_enhancer = TechnicalEnhancer()
enhanced_signal = tech_enhancer.enhance_confluence(base_signal)
```

---

## 总结

本设计通过**三层策略架构**和**股票分层管理**，实现:
1. **快速响应**: 突发事件分钟级响应
2. **跨市场机会**: 充分利用美港股联动
3. **积小胜为大胜**: 高频低预期，累积收益

下一步：实现 Phase 1 (股票分层) 和 Phase 2 (事件驱动层)。
