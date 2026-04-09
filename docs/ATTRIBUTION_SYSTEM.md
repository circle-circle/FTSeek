# 交易归因系统实现文档

## 概述

基于 GPT 模型分析建议，为 FTSeek 实现了完整的交易归因系统，解决以下核心问题：

1. **哪个策略真的赚钱** - 策略表现归因
2. **哪些市场阶段适合哪个策略** - 市场状态归因
3. **执行质量对收益的影响** - 执行质量归因
4. **新闻事件对交易的贡献** - 新闻影响归因
5. **共识权重是否合理** - 共识权重归因

## 架构设计

```
交易归因系统
├── 数据层
│   ├── signal_outcomes (信号质量记录表)
│   ├── strategy_attribution (策略归因表)
│   ├── market_regime_attribution (市场状态归因表)
│   ├── execution_attribution (执行质量归因表)
│   ├── consensus_attribution (共识权重归因表)
│   └── news_attribution (新闻影响归因表)
│
├── 分析引擎层
│   ├── AttributionEngine (归因引擎)
│   │   ├── analyze_strategy_performance()
│   │   ├── analyze_market_regime_performance()
│   │   ├── analyze_execution_quality()
│   │   ├── analyze_consensus_weights()
│   │   ├── analyze_news_impact()
│   │   └── generate_attribution_report()
│   │
│   └── StrategyHealthTracker (策略健康度追踪器)
│       ├── record_trade()
│       ├── record_signal()
│       ├── get_health_metrics()
│       └── get_weight_recommendations()
│
└── API 层
    ├── GET /api/v2/attribution/strategies - 策略归因
    ├── GET /api/v2/attribution/regimes - 市场状态归因
    ├── GET /api/v2/attribution/execution - 执行质量归因
    ├── GET /api/v2/attribution/news - 新闻影响归因
    ├── GET /api/v2/attribution/report - 完整归因报告
    ├── GET /api/v2/attribution/weights/recommend - 权重建议
    ├── GET /api/v2/attribution/health - 策略健康度
    └── POST /api/v2/attribution/health/trade - 记录交易健康度
```

## 核心模块

### 1. 归因引擎 (`app/analytics/attribution_engine.py`)

#### 策略归因分析

```python
from app.analytics.attribution_engine import get_attribution_engine

engine = get_attribution_engine()

# 分析各策略表现
results = engine.analyze_strategy_performance(days=30)

for strategy in results:
    print(f"{strategy.strategy_name}:")
    print(f"  胜率：{strategy.win_rate:.1%}")
    print(f"  盈亏比：{strategy.profit_factor:.2f}")
    print(f"  质量评分：{strategy.quality_score}/100")
    print(f"  市场适配：{strategy.regime_fit}")
```

**输出指标**:
- 交易统计：总数/胜数/负数
- 胜率、平均盈亏、盈亏比
- 最大盈利/最大亏损
- 平均持仓时间
- 质量评分 (0-100)
- 市场状态适配度字典
- 置信度校准 (置信度 vs 实际胜率)

#### 市场状态归因

```python
# 分析各市场状态下的表现
regime_results = engine.analyze_market_regime_performance(days=30)

for regime in regime_results:
    print(f"{regime.regime_name}:")
    print(f"  胜率：{regime.win_rate:.1%}")
    print(f"  最佳策略：{regime.best_strategy}")
    print(f"  最差策略：{regime.worst_strategy}")
```

**输出指标**:
- 各市场状态的交易统计
- 各策略在该市场下的表现
- 最佳/最差策略识别

#### 完整归因报告

```python
# 生成综合报告
report = engine.generate_attribution_report(period="30d")

print("关键发现:")
for insight in report["key_insights"]:
    print(f"  • {insight}")

print(f"最佳策略：{report['best_strategy']}")
print(f"需优化策略：{report['worst_strategy']}")
```

### 2. 策略健康度追踪器 (`app/analytics/strategy_health.py`)

#### 实时追踪策略状态

```python
from app.analytics.strategy_health import (
    get_strategy_health_tracker,
    TradeRecord,
)

tracker = get_strategy_health_tracker()

# 记录交易结果
trade = TradeRecord(
    trade_id="trade_001",
    strategy="confluence_v2",
    code="HK.00700",
    action="BUY",
    entry_price=380.0,
    exit_price=395.0,
    pnl=1500.0,
    pnl_percent=0.0395,
    entry_time="2026-04-09T10:00:00",
    exit_time="2026-04-09T14:00:00",
    market_regime="trending",
)

tracker.record_trade(trade)

# 获取健康度
metrics = tracker.get_health_metrics("confluence_v2")
print(f"健康等级：{metrics.health_level}")  # EXCELLENT/GOOD/NORMAL/POOR/CRITICAL
print(f"建议权重：{metrics.recommended_weight}")
```

#### 健康度评分维度

| 维度 | 权重 | 说明 |
|------|------|------|
| 胜率 | 30% | 最近 20 笔交易胜率 |
| 盈亏比 | 25% | 平均盈利/平均亏损 |
| 连续亏损 | 20% | 最大连续亏损次数 |
| 市场适配 | 15% | 当前市场状态下的历史表现 |
| 信号稳定性 | 10% | 信号频率是否适中 |

#### 健康等级划分

| 评分 | 等级 | 权重调整 |
|------|------|---------|
| ≥80 | EXCELLENT | +0.5 |
| 60-79 | GOOD | +0.2 |
| 40-59 | NORMAL | 0 |
| 20-39 | POOR | -0.3 |
| <20 | CRITICAL | -0.5 |

### 3. API 接口 (`app/api/attribution.py`)

#### RESTful API

```bash
# 策略表现归因
curl http://localhost:8899/api/v2/attribution/strategies?days=30

# 市场状态归因
curl http://localhost:8899/api/v2/attribution/regimes?days=30

# 执行质量归因
curl http://localhost:8899/api/v2/attribution/execution?days=30

# 新闻影响归因
curl http://localhost:8899/api/v2/attribution/news?days=30

# 完整归因报告 (7d/30d/90d)
curl http://localhost:8899/api/v2/attribution/report?period=30d

# 权重建议
curl http://localhost:8899/api/v2/attribution/weights/recommend?days=30

# 策略健康度
curl http://localhost:8899/api/v2/attribution/health

# 策略健康度详情
curl http://localhost:8899/api/v2/attribution/health/confluence_v2

# 记录交易健康度
curl -X POST http://localhost:8899/api/v2/attribution/health/trade \
  -H "Content-Type: application/json" \
  -d '{"trade_id":"t001","strategy":"confluence_v2","pnl_percent":0.05,...}'
```

## 集成到现有系统

### 1. 在交易执行后自动记录

修改 `app/trader/executor.py`:

```python
from app.analytics.strategy_health import get_strategy_health_tracker, TradeRecord

# 在 execute() 方法中，交易完成后添加:

def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
    # ... 现有交易逻辑 ...

    # 交易完成后记录健康度
    if trade_result.success and trade_type == 'CLOSE':
        tracker = get_strategy_health_tracker()

        trade = TradeRecord(
            trade_id=trade_result.order_id,
            strategy=signal.strategy,
            code=signal.code,
            action=signal.action.value,
            entry_price=position.entry_price,
            exit_price=trade_result.exit_price,
            pnl=trade_result.pnl,
            pnl_percent=trade_result.pnl_percent,
            entry_time=position.entry_time,
            exit_time=datetime.now().isoformat(),
            market_regime=current_regime,  # 从共识引擎获取
        )

        tracker.record_trade(trade)
```

### 2. 定期生成归因报告

添加定时任务 (可使用 `CronCreate`):

```python
from app.analytics.attribution_engine import get_attribution_engine
from app.notifier.dingtalk import DingTalkNotifier

def send_daily_attribution_report():
    """每日推送归因报告"""
    engine = get_attribution_engine()
    notifier = DingTalkNotifier(webhook=..., secret=...)

    # 生成 7 日报告
    report = engine.generate_attribution_report(period="7d")

    # 构建消息
    markdown = f"""
## 📊 FTSeek 交易归因日报

### 关键发现
{chr(10).join('• ' + i for i in report['key_insights'])}

### 策略表现
🏆 最佳：{report['best_strategy']}
📉 需优化：{report['worst_strategy']}

### 权重建议
{chr(10).join(f'{k}: {v:.3f}' for k, v in report.get('weight_recommendations', {}).items())}
"""

    notifier.send_markdown("交易归因日报", markdown)
```

### 3. 动态调整策略权重

在共识引擎中使用建议权重:

```python
from app.analytics.attribution_engine import get_attribution_engine

# 在 weight_calculator.py 中
class WeightCalculator:
    def __init__(self):
        self.engine = get_attribution_engine()
        # ... 其他初始化 ...

    def calculate_weight(self, strategy: str, market_regime: str = None) -> float:
        # 获取基础权重
        base = self.base_weights.get(strategy, 1.0)

        # 应用归因引擎的建议权重
        recommendations = self.engine.get_weight_recommendations(days=30)
        if strategy in recommendations:
            rec_weight = recommendations[strategy]
            # 平滑过渡：基础权重和建议权重的加权平均
            final_weight = base * 0.5 + rec_weight * 0.5
            logger.info(f"[{strategy}] 权重调整：{base:.2f} → {final_weight:.2f} (归因建议)")
            return final_weight

        return base
```

## 使用示例

### 示例 1: 识别最佳策略组合

```python
from app.analytics.attribution_engine import get_attribution_engine

engine = get_attribution_engine()
report = engine.generate_attribution_report(period="30d")

print("=== 策略表现排名 ===")
strategies = sorted(report["strategy_attribution"],
                   key=lambda x: x["quality_score"], reverse=True)

for i, s in enumerate(strategies, 1):
    print(f"{i}. {s['strategy_name']}: {s['quality_score']:.1f}分 "
          f"(胜率{s['win_rate']:.0%}, 盈亏比{s['profit_factor']:.2f})")

print("\n=== 市场状态适配 ===")
for regime in report["market_regime_attribution"]:
    print(f"{regime['regime_name']}: 最佳策略={regime['best_strategy']}")
```

### 示例 2: 策略健康度监控

```python
from app.analytics.strategy_health import get_strategy_health_tracker

tracker = get_strategy_health_tracker()
report = tracker.get_health_report()

print("=== 策略健康状态 ===")
for strategy, metrics in report["strategies"].items():
    status_icon = "🟢" if metrics["health_level"] == "EXCELLENT" else \
                  "🟡" if metrics["health_level"] == "GOOD" else "🔴"
    print(f"{status_icon} {strategy}: {metrics['health_level']} "
          f"({metrics['overall_health_score']:.0f}分) "
          f"建议权重={metrics['recommended_weight']:.2f}x")
```

### 示例 3: 权重调整决策

```python
# 获取当前权重建议
from app.analytics.attribution_engine import get_attribution_engine

engine = get_attribution_engine()
recommendations = engine.get_weight_recommendations(days=30)

print("=== 权重调整建议 ===")
for strategy, weight in sorted(recommendations.items(),
                               key=lambda x: x[1], reverse=True):
    if weight > 1.5:
        print(f"⬆️ {strategy}: 建议上调至 {weight:.2f}x (表现优秀)")
    elif weight < 0.7:
        print(f"⬇️ {strategy}: 建议下调至 {weight:.2f}x (表现欠佳)")
    else:
        print(f"➡️ {strategy}: 维持当前 {weight:.2f}x")
```

## 测试

```bash
# 运行完整测试
python tests/test_attribution_system.py

# 测试输出包括:
# 1. 策略表现归因分析
# 2. 市场状态归因分析
# 3. 新闻影响归因分析
# 4. 完整归因报告
# 5. 权重建议生成
# 6. 策略健康度追踪
# 7. 健康度实时更新
```

## 关键发现示例

```
🔑 关键发现:
   • 最佳策略：sentiment_ai (胜率 75%)
   • trending 市场下，supertrend 表现最佳
   • ranging 市场下，sentiment_ai 表现最佳
   • 新闻对交易有正向影响 (溢价 +5.2%)
   • confluence_v2 连续亏损 3 笔，建议观望

🏆 最佳策略：sentiment_ai
📉 需优化策略：supertrend

权重建议:
   sentiment_ai: 1.842
   confluence_v2: 1.235
   enhanced_confluence: 1.102
   supertrend: 0.658
```

## 后续优化方向

1. **实时性提升**: 在交易执行回调中自动记录
2. **可视化**: Dashboard 展示归因图表
3. **自动调优**: 根据归因结果自动调整权重
4. **归因告警**: 策略健康度低于阈值时推送告警
5. **回测对比**: 对比实盘与回测的归因差异

## 文件清单

```
app/analytics/
├── attribution_engine.py    # 归因引擎 (核心分析)
├── strategy_health.py       # 策略健康度追踪
└── trade_chain.py           # 交易全链路记录 (已有)

app/api/
└── attribution.py           # 归因 API 接口

tests/
└── test_attribution_system.py  # 测试脚本

docs/
└── ATTRIBUTION_SYSTEM.md    # 本文档
```
