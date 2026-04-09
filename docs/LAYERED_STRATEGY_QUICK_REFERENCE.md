# 分层策略快速参考指南

**更新日期**: 2026-04-09  
**版本**: v3.0.0

---

## 一、新增策略一览

| 策略名称 | 类型 | 功能 | 优先级 |
|---------|------|------|--------|
| `event_driven` | 事件驱动 | 突发新闻→LLM 分析→信号 | 高 |
| `cross_market_arbitrage` | 跨市场 | 美港股联动套利 | 高 |
| `enhanced_confluence` | 技术增强 | 多周期共振 + 积小胜 | 中 |

---

## 二、策略触发条件

### 2.1 事件驱动策略

**触发条件**:
- 检测到 10 类事件 (财报、指引、并购、产品、监管、高管、诉讼、评级、宏观、地缘)
- LLM 分析置信度 > 70%
- 事件时效性未过期

**信号特征**:
- 优先级：1 (高) 或 2 (中)
- 时效性：日内 / 1-3 天 / 1 周+
- 置信度：70-95%

**示例新闻**:
```
"腾讯控股 Q4 财报超预期 净利润增长 30%"
"美团获分析师上调评级 目标价看至 200 港元"
"美国宣布对华加征关税 科技股承压"
```

---

### 2.2 跨市场套利策略

**触发条件**:

| 套利类型 | 触发阈值 | 目标市场 | 交易时间 |
|---------|---------|---------|---------|
| 美股→港股 | 隔夜涨跌>3% | 港股 | 09:30 AM 开盘 |
| 港股→美股 | 白天涨跌>3% | 美股 | 09:30 PM 盘前 |
| ADR 溢价 | 溢价>5% | 港股/美股 | 实时 |

**信号特征**:
- 优先级：1 (高)
- 置信度：50 + |涨跌幅| × 10 (%)
- 冷却时间：12 小时

**示例场景**:
```
美股 BABA 隔夜 +4.5% → 港股 09988 开盘做多
港股 00700 白天 +3.5% → 美股 TCEHY 盘前做多
```

---

### 2.3 技术增强策略

**触发条件**:
- 多周期共振检测 (5M/15M/1H/1D)
- 共振得分 >= 0.75 (4 周期中 3 周期同向)
- 胜率 > 55%

**信号特征**:
- 优先级：2 (高共振) 或 3 (低共振)
- 仓位调整：高共振 +20%, 低胜率 -50%
- 止盈策略：2% 目标 + 50% 快止 + 1% 移动止损

**共振得分计算**:
```
得分 = 同向周期数 / 总周期数
4 周期同向 → 1.0 (完全共振)
3 周期同向 → 0.75 (强共振)
2 周期同向 → 0.50 (中等)
1 周期同向 → 0.25 (弱)
```

---

## 三、股票池分层

### Tier 1 (核心股票) - 50% 仓位

**港股**:
- HK.00700 腾讯控股
- HK.03690 美团
- HK.09988 阿里巴巴
- HK.09618 京东集团

**美股**:
- US.AAPL 苹果
- US.MSFT 微软
- US.NVDA 英伟达
- US.GOOG 谷歌

---

### Tier 2 (观察股票) - 30% 仓位

**港股科技**:
- HK.01810 小米集团
- HK.01024 快手
- HK.09999 网易

**港股汽车**:
- HK.09866 蔚来
- HK.02015 理想汽车
- HK.09868 小鹏汽车

**美股中概**:
- US.BABA 阿里
- US.JD 京东

---

### Tier 3 (机会股票) - 20% 仓位

动态添加，需满足:
- 明确催化事件
- 事件驱动策略确认
- 置信度 > 80%

---

## 四、仓位管理规则

### 4.1 基础仓位

| 分层 | 总仓位 | 单只上限 |
|------|--------|---------|
| Tier 1 | 50% | 20% |
| Tier 2 | 30% | 15% |
| Tier 3 | 20% | 10% |

### 4.2 动态调整

**基于胜率**:
```
胜率 > 65% → 1.0x 正常仓位
胜率 55-65% → 0.7x 降仓位
胜率 45-55% → 0.5x 轻仓
胜率 < 45% → 0.3x 观望
```

**基于共振**:
```
共振得分 >= 0.75 → +20% 加仓
共振得分 0.50-0.75 → 正常
共振得分 < 0.50 → -50% 降仓
```

### 4.3 集中度限制

**相关集群上限 35%**:
```
tech_global: 腾讯、美团、阿里、京东、小米、快手
hk_finance: 汇丰、平安、招行、中行、建行、工行
ev_global: 蔚来、理想、小鹏、特斯拉
```

---

## 五、止盈止损策略

### 5.1 积小胜止盈

**参数**:
```python
target_return = 2%         # 目标收益
quick_take_profit = 50%    # 50% 仓位快速止盈
trailing_stop = 1%         # 1% 移动止损
```

**执行流程**:
```
1. 盈利达 2% → 平仓 50% 锁定利润
2. 剩余 40% → 设置保本止损
3. 移动止损 → 从峰值回撤 1% 触发
```

### 5.2 动态止损

| 市场状态 | 止损幅度 | 说明 |
|---------|---------|------|
| 低波动 | 2.5% | 正常止损 |
| 中波动 | 3.0% | 放宽止损 |
| 高波动 | 3.5% | 避免噪音触发 |

---

## 六、信号处理流程

```
┌──────────────┐
│  新闻/行情输入  │
└──────────────┘
        ↓
┌──────────────┐
│  事件检测器    │ → 关键词匹配
└──────────────┘
        ↓
┌──────────────┐
│  LLM 分析      │ → 情绪/置信度/影响
└──────────────┘
        ↓
┌──────────────┐
│  置信度检查    │ → >70% ?
└──────────────┘
        ↓
┌──────────────┐
│  时效性检查    │ → 未过期 ?
└──────────────┘
        ↓
┌──────────────┐
│  股票池检查    │ → 是否在池内
└──────────────┘
        ↓
┌──────────────┐
│  冷却期检查    │ → 12h 内无信号 ?
└──────────────┘
        ↓
┌──────────────┐
│  生成信号      │ → 推送共识引擎
└──────────────┘
```

---

## 七、API 调用示例

### 7.1 获取分层策略管理器

```python
from app.strategy.layered_integration import get_layered_strategy_manager

manager = get_layered_strategy_manager(config={
    'base_position': 100
})
```

### 7.2 处理新闻

```python
news_list = [
    {
        'id': 'news_001',
        'title': '腾讯控股 Q4 财报超预期',
        'content': '...',
        'source': 'sina',
        'published_at': '2026-04-09T10:00:00',
        'related_stocks': ['HK.00700']
    }
]

signals = manager.process_news(news_list)
for signal in signals:
    print(f"{signal.signal.code} | 优先级={signal.priority} | 置信度={signal.confidence:.0%}")
```

### 7.3 分析美股隔夜

```python
us_data = [
    {
        'code': 'BABA',
        'name': 'Alibaba',
        'close_price': 85.5,
        'change_pct': 0.045,  # +4.5%
        'volume': 15000000
    }
]

signals = manager.analyze_us_overnight(us_data)
```

### 7.4 更新 K 线缓存

```python
kline = {
    'close': 350.0,
    'high': 355.0,
    'low': 348.0,
    'open': 349.0,
    'volume': 20000000
}

manager.update_kline_cache("HK.00700", "K_5M", kline)
```

---

## 八、测试命令

```bash
# 运行完整测试套件
PYTHONPATH=/opt/ftseek/FTSeek \
python -m pytest tests/strategy/test_layered_integration.py -v

# 测试单个模块
PYTHONPATH=/opt/ftseek/FTSeek \
python -m pytest tests/strategy/test_layered_integration.py::TestEventDrivenStrategy -v

# 查看测试覆盖报告
PYTHONPATH=/opt/ftseek/FTSeek \
python -m pytest tests/strategy/test_layered_integration.py --cov=app.strategy --cov-report=html
```

---

## 九、监控与调试

### 9.1 日志查看

```bash
# 查看事件驱动日志
tail -f logs/ftseek.log | grep "event_driven"

# 查看跨市场套利日志
tail -f logs/ftseek.log | grep "cross_market"

# 查看技术增强日志
tail -f logs/ftseek.log | grep "tech_enhancer"
```

### 9.2 策略状态查询

```bash
# API 查询
curl http://localhost:8899/api/v2/strategies | jq

# 查看分层摘要
curl http://localhost:8899/api/v2/layered/summary
```

### 9.3 常见问题

**Q: 事件驱动策略没有生成信号？**
```
A: 检查以下几点:
   1. 新闻是否被正确检测 (查看 EventDetector 日志)
   2. LLM 分析置信度是否 >70%
   3. 股票是否在股票池内
   4. 是否在冷却期内
```

**Q: 跨市场套利信号过多？**
```
A: 可以调整阈值:
   - US_TO_HK_THRESHOLD: 3% → 4% (减少触发)
   - 冷却时间：12h → 24h (降低频率)
```

**Q: 积小胜止盈过早触发？**
```
A: 调整参数:
   - target_return: 2% → 2.5% (提高目标)
   - quick_take_profit: 50% → 40% (减少止盈比例)
```

---

## 十、性能基准

### 10.1 预期表现

| 指标 | 改进前 | 改进后 | 提升 |
|------|--------|--------|------|
| 月收益 | ~5% | 8-12% | +60-140% |
| 胜率 | ~55% | 60-65% | +5-10pp |
| 月交易数 | ~20 | ~30 | +50% |
| 最大回撤 | ~8% | ~6% | -25% |

### 10.2 回测建议

```bash
# 使用回测框架测试新策略
python backtest_example.py \
  --strategies event_driven,cross_market_arbitrage,enhanced_confluence \
  --start_date 2025-01-01 \
  --end_date 2026-04-09
```

---

## 十一、参考资料

- [分层策略设计文档](docs/STRATEGY_LAYERING_DESIGN.md)
- [实现报告](docs/LAYERED_STRATEGY_IMPLEMENTATION.md)
- [股票池配置](config/stock_pool.yaml)
- [跨市场映射](config/hk_us_mapping.json)
- [测试用例](tests/strategy/test_layered_integration.py)

---

**最后更新**: 2026-04-09  
**维护者**: FTSeek Team
