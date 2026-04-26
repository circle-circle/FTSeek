# 跨市场套利与事件驱动策略使用指南

## 概述

本文档介绍 FTSeek v5.0 新增的跨市场套利和事件驱动策略模块。

## 新增模块

### 1. Lead-Lag 追踪器 (`app/strategy/lead_lag_tracker.py`)

**功能**: 实时检测跨市场价格联动的滞后关系

**核心 API**:
```python
from app.strategy.lead_lag_tracker import get_lead_lag_tracker

tracker = get_lead_lag_tracker(window_size=300, max_lag_seconds=120)

# 记录价格变动
tracker.record_price_move("US.TSLA", timestamp, 5.0, volume=1000000)

# 检测滞后关系
metrics = tracker.detect_lag("US.TSLA", "HK.00700")
# metrics.lag_seconds: 滞后秒数
# metrics.correlation: 相关性
# metrics.strength: 响应强度
# metrics.confidence: 置信度
```

**使用场景**:
- 跨市场套利：检测美股→港股的响应延迟
- 事件驱动：检测新闻发布后股票价格的响应时间
- ADR 套利：检测 ADR 与港股/美股正股的价格联动

---

### 2. 新闻 - 股票映射器 (`app/factors/news_mapper.py`)

**功能**: 自动识别新闻影响的股票 (规则 + AI 混合模式)

**核心 API**:
```python
from app.factors.news_mapper import get_news_mapper

mapper = get_news_mapper(use_ai=True)  # 启用 AI 分析

result = mapper.map_news_to_stocks(
    news_id="news_001",
    title="腾讯控股发布财报，净利润同比增长 30%",
    content="..."
)

# result.direct_matches: 直接提及的股票
# result.industry_matches: 行业关联股票
# result.impact_chain: 产业链影响
# result.entities: 实体识别结果
```

**使用场景**:
- 突发新闻→自动识别受益/受损股票
- 行业政策→映射到产业链相关股票
- 财报季→快速定位超预期/低于预期的公司

---

### 3. 动态仓位计算器 (`app/risk/dynamic_sizer.py`)

**功能**: 根据信号质量动态调整仓位、止损、止盈

**核心 API**:
```python
from app.risk.dynamic_sizer import get_dynamic_positionSizer

sizer = get_dynamic_positionSizer()

params = sizer.calculate(
    arb_score=0.85,        # 套利评分 (0-1)
    news_sentiment=0.7,    # 新闻情绪 (-1 到 1)
    volatility=0.25        # 波动率
)

# params.position_pct: 建议仓位 (8%)
# params.stop_loss_pct: 止损 (-1.5%)
# params.take_profit_pct: 止盈 (4%)
# params.signal_quality: 质量等级 (excellent/good/fair/poor)
# params.confidence: 置信度
```

**信号质量与参数调整**:

| 质量等级 | arb_score | 仓位系数 | 止损系数 | 止盈系数 |
|---------|-----------|---------|---------|---------|
| Excellent | > 0.8 | x1.6 | x0.75 | x1.33 |
| Good | 0.6-0.8 | x1.2 | x0.9 | x1.1 |
| Fair | 0.4-0.6 | x1.0 | x1.0 | x1.0 |
| Poor | < 0.4 | x0.5 | x1.25 | x0.8 |

---

### 4. 影响持续时间预测器 (`app/factors/duration_predictor.py`)

**功能**: 预测新闻事件的影响持续时间，输出退出时机建议

**核心 API**:
```python
from app.factors.duration_predictor import get_duration_predictor

predictor = get_duration_predictor()

result = predictor.predict(
    event_type="earnings",     # 事件类型
    sentiment_score=-0.7,      # 情绪评分
    news_count=15,             # 相关新闻数量
    market_cap="large",        # 市值规模
    volatility=0.25            # 波动率
)

# result.expected_duration_minutes: 预期持续时间 (120 分钟)
# result.exit_signal: 退出建议 ("10:30 前退出")
# result.confidence: 置信度
# result.duration_tier: 持续时间等级
```

**事件类型基础持续时间**:

| 事件类型 | 基础持续时间 | 说明 |
|---------|-------------|------|
| 财报 (earnings) | 120 分钟 | 财报消化时间 |
| 并购 (ma) | 240 分钟 | 重大事件，影响较长 |
| 监管政策 (regulation) | 180 分钟 | 政策影响中等 |
| 分析师评级 (analyst) | 30 分钟 | 短期影响 |
| 地缘政治 (geopolitical) | 300 分钟 | 长期影响 |

---

### 5. 事件绩效追踪器 (`app/consensus/event_performance_tracker.py`)

**功能**: 记录事件→交易→结果的完整链路，支持策略自学习

**核心 API**:
```python
from app.consensus.event_performance_tracker import get_event_performance_tracker

tracker = get_event_performance_tracker()

# 记录事件生成
tracker.record_event(
    event_id="evt_001",
    event_type="earnings",
    predicted_direction="buy",
    target_stocks=[{"code": "HK.00700", "direction": "long"}],
    arb_score=0.8,
    news_sentiment=0.6
)

# 记录交易退出
tracker.record_outcome(
    event_id="evt_001",
    trade_id="trade_001",
    code="HK.00700",
    actual_return=0.035,
    holding_minutes=45,
    exit_reason="take_profit"
)

# 获取绩效统计
stats = tracker.get_performance_stats("earnings", days=30)
# stats.win_rate: 胜率
# stats.avg_return: 平均收益
# stats.direction_accuracy: 预测准确率
```

---

## 完整工作流示例

### 跨市场套利流程

```
美股 TSLA 夜间大涨 5%
    ↓
Lead-Lag 追踪器：检测到 US.TSLA→HK.00700 的滞后关系
    ↓
新闻映射器：分析新闻"特斯拉财报超预期"→映射到 HK.00700/US.TSLA
    ↓
动态仓位计算器：arb_score=0.75 → 建议仓位 6%，止损 -1.8%，止盈 +4%
    ↓
持续时间预测器：earnings 事件 → 预期持续 120 分钟，10:30 前退出
    ↓
事件绩效追踪器：记录事件→等待交易结果→绩效分析→策略优化
```

### 事件驱动流程

```
突发新闻："电动车补贴退坡"
    ↓
新闻映射器：识别影响链→电动车/锂电池/光伏 → 股票列表
    ↓
持续时间预测器：regulation 事件 → 预期 180 分钟
    ↓
动态仓位计算器：sentiment=-0.6 → 仓位调整
    ↓
执行交易 → 记录结果 → 绩效分析
```

---

## 集成到现有策略

### 更新 `cross_market.py`

在 `CrossMarketArbitrage` 策略中集成新模块:

```python
from app.strategy.lead_lag_tracker import get_lead_lag_tracker
from app.risk.dynamic_sizer import get_dynamic_positionSizer

class CrossMarketArbitrage(BaseStrategy):
    def __init__(self, config=None):
        super().__init__(config)
        
        # 集成 Lead-Lag 追踪器
        self.lead_lag = get_lead_lag_tracker()
        
        # 集成动态仓位计算器
        self.sizer = get_dynamic_positionSizer()
    
    def on_quote(self, code, quote):
        # 记录价格变动
        self.lead_lag.record_price_move(
            code,
            quote['timestamp'],
            quote['change_pct']
        )
        
        # 检测套利机会
        if code.startswith('US.'):
            hk_code = self.get_hk_pair(code)
            if hk_code:
                metrics = self.lead_lag.detect_lag(code, hk_code)
                
                # 高相关性 + 显著滞后 → 套利信号
                if metrics.correlation > 0.7 and metrics.lag_seconds > 60:
                    params = self.sizer.calculate(
                        arb_score=metrics.strength,
                        news_sentiment=0.0,
                        volatility=0.2
                    )
                    
                    # 生成信号
                    return Signal(
                        code=hk_code,
                        action=SignalAction.BUY,
                        quantity=params.position_pct,
                        extra=params.to_dict()
                    )
```

---

## 配置文件更新

在 `config/config.yaml` 中添加新配置项:

```yaml
# 跨市场套利增强配置
cross_market_enhanced:
  # Lead-Lag 配置
  lead_lag:
    window_size: 300          # 滑动窗口 (秒)
    max_lag_seconds: 120      # 最大检测滞后
    min_sample_count: 20      # 最小样本数
    correlation_threshold: 0.7  # 相关性阈值
  
  # 动态仓位配置
  dynamic_sizer:
    base_position_pct: 0.05   # 基础仓位 5%
    max_position_pct: 0.10    # 最大仓位 10%
    base_stop_loss: -0.02     # 基础止损 2%
    base_take_profit: 0.03    # 基础止盈 3%
    
  # 事件驱动配置
  event_driven:
    enable_duration_predictor: true
    enable_performance_tracking: true
```

---

## 测试与验证

运行集成测试:

```bash
python3 examples/cross_market_event_demo.py
```

输出示例:

```
============================================================
1. Lead-Lag 追踪器演示
============================================================
Lead: US.TSLA → Lag: HK.00700
  滞后时间：180.0 秒
  相关性：0.850
  响应强度：0.720
  置信度：0.68

============================================================
3. 动态仓位计算器演示
============================================================
场景 1: 高质量信号 (arb=0.85, sentiment=0.7)
  仓位：8.0%
  止损：-1.5%
  止盈：4.0%
  质量等级：excellent
  置信度：0.82
```

---

## 注意事项

1. **Lead-Lag 追踪器**需要足够的样本数 (至少 20 个) 才能准确检测滞后关系
2. **新闻映射器**的 AI 模式需要配置 AI 服务 ( bailian/gpt)
3. **动态仓位**在高波动环境下会自动降低仓位
4. **绩效追踪**需要记录完整的事件→交易→结果链路才有分析价值

---

## 故障排查

### Lead-Lag 置信度为 0

**原因**: 样本数不足或数据质量差

**解决**: 
- 增加观察时间，累积更多样本
- 检查价格变动数据是否准确

### 新闻映射无结果

**原因**: 新闻内容未明确提及公司/行业

**解决**:
- 启用 AI 分析模式 (`use_ai=True`)
- 扩展公司别名表和行业关键词表

### 动态仓位过小

**原因**: 信号质量评分低

**解决**:
- 提高 `arb_score` 计算准确性
- 降低波动率敏感度配置
