# FTSeek 场景识别系统使用指南

**版本**: v1.0 (Phase 1)  
**更新时间**: 2026-04-09

---

## 一、快速开始

### 1.1 导入模块

```python
from app.scenario import (
    get_scenario_classifier,
    get_scenario_router,
    get_scenario_memory,
    ScenarioType,
)
```

### 1.2 场景分类示例

```python
# 初始化分类器
classifier = get_scenario_classifier()

# 准备 K 线数据
klines = [
    {'open': 100, 'high': 102, 'low': 99, 'close': 101, 'volume': 1000000},
    # ... 更多 K 线
]

# 执行场景分类
result = classifier.classify("HK.00700", klines)

print(f"场景：{result.scenario_type.value}")
print(f"置信度：{result.confidence:.2f}")
print(f"宏观状态：{result.macro_regime.value}")
print(f"ADX: {result.adx_value:.1f}")
```

### 1.3 策略路由示例

```python
from app.scenario import get_scenario_router, ScenarioType

# 初始化路由器
router = get_scenario_router()

# 获取某场景的策略组合
result = router.route(ScenarioType.TREND_STRONG)

print(f"主策略：{result.primary_strategies}")
print(f"辅助策略：{result.secondary_strategies}")
print(f"抑制策略：{result.suppressed_strategies}")
print(f"风控模板：{result.risk_template}")
print(f"策略权重：{result.active_strategies}")
```

---

## 二、场景类型说明

### 2.1 核心场景 (5 个)

| 场景 | 代码 | 描述 | 识别条件 |
|------|------|------|---------|
| **强趋势** | `TREND_STRONG` | 明确趋势方向 | ADX > 30, 价格偏离 MA |
| **震荡市** | `RANGE_BOUND` | 区间震荡 | ADX < 20 |
| **跳空事件** | `GAP_EVENT` | 开盘跳空 | 跳空 > 2% |
| **波动扩张** | `VOL_EXPANSION` | 波动率快速上升 | ATR 百分位上升 > 15% |
| **反转预备** | `REVERSAL_SETUP` | 超买/超卖 + 动量衰竭 | RSI > 70 或 < 30 |

### 2.2 扩展场景 (Phase 2+)

- `TREND_START` - 趋势启动 (ADX 20-30, 上升初期)
- `TREND_EXHAUST` - 趋势衰竭 (ADX > 40, 背离)
- `VOL_CONTRACTION` - 波动收缩
- `NEWS_DRIVEN` - 新闻驱动
- `BREAKOUT_SETUP` - 突破预备

---

## 三、配置指南

### 3.1 场景参数配置

```yaml
# config/scenario_config.yaml
scenario_classifier:
  confirm_window: 3        # 场景确认 K 线数
  cool_down_minutes: 2     # 切换冷却时间 (分钟)
  confidence_threshold: 0.65  # 置信度阈值

scenarios:
  TREND_STRONG:
    priority: 3
    strategies:
      primary: ["turtle", "supertrend"]
      secondary: ["123_rule"]
      suppressed: ["rsi"]
```

### 3.2 自定义场景映射

```python
from app.scenario import ScenarioRouter

custom_config = {
    "routing_config": {
        "TREND_STRONG": {
            "primary": ["my_custom_strategy"],
            "secondary": ["supertrend"],
            "risk_template": "aggressive_trend",
        }
    }
}

router = ScenarioRouter(config=custom_config)
```

---

## 四、场景分析 API

### 4.1 查询场景表现

```python
from app.analytics.signal_analytics import get_analytics

analytics = get_analytics()

# 查询某场景的交易记录
outcomes = analytics.query_by_scenario('trend_strong', days=30)

# 分析各场景表现
perf = analytics.analyze_scenario_performance(days=90)
for scenario, stats in perf.items():
    print(f"{scenario}:")
    print(f"  交易数：{stats['total_trades']}")
    print(f"  胜率：{stats['win_rate']:.1%}")
    print(f"  平均盈亏：{stats['avg_pnl_pct']:.2f}%")
```

### 4.2 场景统计摘要

```python
# 按场景分组统计
summary = analytics.get_summary(days=30, group_by='scenario')

for row in summary.get('data', []):
    print(f"{row['scenario']}: "
          f"{row['total_trades']}笔，"
          f"胜率{row['win_rate']:.1%}，"
          f"平均盈亏{row['avg_pnl_pct']:.2f}%")
```

---

## 五、集成到现有系统

### 5.1 集成到 DataSubscriber

```python
# app/data_subscriber.py

from app.scenario import get_scenario_classifier, get_scenario_router

class DataSubscriber:
    def __init__(self):
        self.scenario_classifier = get_scenario_classifier()
        self.scenario_router = get_scenario_router()

    def on_kline_update(self, code, klines):
        # 1. 场景分类
        scenario_result = self.scenario_classifier.classify(code, klines)

        # 2. 策略路由
        route_result = self.scenario_router.route(scenario_result.scenario_type)

        # 3. 通知共识引擎更新权重
        self.consensus_engine.update_scenario_weights(code, route_result)
```

### 5.2 集成到 ConsensusEngine

```python
# app/strategy/consensus_engine.py

def calculate_weight(self, strategy: str, scenario: str = None) -> float:
    """计算权重时考虑场景适配度"""
    base = self.base_weights.get(strategy, 1.0)

    if scenario:
        # 从路由器获取场景权重
        route_result = self.scenario_router.route(scenario)
        scenario_weight = route_result.active_strategies.get(strategy, 1.0)
        base *= scenario_weight

    return base
```

### 5.3 集成到信号记录

```python
# app/strategy/consensus_engine.py

@dataclass
class SignalOutcome:
    code: str
    action: str
    strategy: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    market_regime: str
    scenario: str  # Phase 1 新增
    # ... 其他字段
```

---

## 六、最佳实践

### 6.1 场景识别调优

1. **置信度阈值设置**:
   - 保守：0.75+ (减少误判，但可能错过机会)
   - 平衡：0.65 (默认值)
   - 激进：0.50+ (更多场景识别，但准确率下降)

2. **确认窗口设置**:
   - 短线交易：2-3 根 K 线
   - 中线交易：5-10 根 K 线

3. **冷却时间设置**:
   - 高频场景：1 分钟
   - 趋势场景：5-10 分钟

### 6.2 策略权重配置

**推荐配置原则**:
- 主策略：1.5x - 2.0x (场景最适配)
- 辅助策略：1.0x (正常参与)
- 抑制策略：0.2x - 0.4x (降低干扰)

### 6.3 场景切换监控

```python
# 监控场景切换频率
classifier.get_scenario_history(code)

# 如果切换过于频繁，考虑：
# 1. 提高置信度阈值
# 2. 增加确认窗口
# 3. 延长冷却时间
```

---

## 七、故障排查

### 7.1 场景识别返回 UNKNOWN

**可能原因**:
1. K 线数据不足 (最少需要 `ma_period + 10` 根)
2. 置信度低于阈值
3. 特征计算失败

**解决方法**:
```python
# 检查 K 线数据长度
if len(klines) < 30:
    print("K 线数据不足")

# 降低置信度阈值 (临时测试)
classifier.CONFIDENCE_THRESHOLD = 0.5
```

### 7.2 场景频繁切换

**可能原因**:
1. 确认窗口过小
2. 冷却时间过短
3. 行情本身波动剧烈

**解决方法**:
```python
# 增加确认窗口
classifier.CONFIRM_WINDOW = 5

# 延长冷却时间
memory.COOL_DOWN_MINUTES = 5
```

### 7.3 数据库 scenario 字段错误

**错误**: `no such column: scenario`

**解决方法**:
```bash
# 方案 1: 删除旧数据库重新创建
rm data/ftseek.db

# 方案 2: 手动添加字段
sqlite3 data/ftseek.db "ALTER TABLE signal_outcomes ADD COLUMN scenario TEXT;"
```

---

## 八、性能优化建议

### 8.1 计算优化

1. **缓存特征计算结果**:
   ```python
   # 场景结果缓存 1 分钟
   @lru_cache(maxsize=100)
   def classify_cached(code, kline_hash):
       ...
   ```

2. **按需计算**:
   - 仅在 K 线更新时计算
   - 非 Tick 级计算

3. **异步计算**:
   ```python
   # 后台线程运行场景识别
   threading.Thread(target=classifier.classify).start()
   ```

### 8.2 存储优化

1. **场景历史清理**:
   ```python
   # 保留最近 100 条记录
   memory._scenario_history[code] = memory._scenario_history[code][-100:]
   ```

2. **定期清理旧数据**:
   ```python
   # 清理 365 天前的场景记录
   db.cleanup_old_signal_outcomes(keep_days=365)
   ```

---

## 九、常见问题 (FAQ)

### Q1: 场景识别和 MarketRegime 有什么区别？

**A**: MarketRegime 是宏观 7 态识别，场景识别是细粒度 16 态。场景识别继承并扩展了 MarketRegime 的能力。

### Q2: 如何验证场景识别的准确率？

**A**: 使用 `analyze_scenario_performance()` 分析各场景的胜率和盈亏比，长期积累数据后可评估准确率。

### Q3: 场景路由会影响现有策略吗？

**A**: 场景路由通过调整权重来影响策略，不会禁用策略。抑制策略仍会参与，但权重较低。

### Q4: 如何自定义场景映射表？

**A**: 在 `config/scenario_config.yaml` 中配置，或直接修改 `CORE_SCENARIO_ROUTING` 字典。

### Q5: 场景识别会增加多少延迟？

**A**: 单次场景识别约 5-10ms，路由器约 1-2ms。建议每 1-3 分钟计算一次，而非每根 K 线。

---

## 十、参考资料

- [场景路由设计方案](SCENARIO_ROUTER_DESIGN.md)
- [场景路由器 Review 报告](SCENARIO_ROUTER_REVIEW.md)
- [Phase 1 实施报告](SCENARIO_PHASE1_COMPLETE.md)

---

**文档维护**: FTSeek Team  
**最后更新**: 2026-04-09
