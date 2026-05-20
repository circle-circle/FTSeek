# 智能自适应轮询架构设计

## 背景约束

| 约束 | 现状 | 影响 |
|------|------|------|
| **无富途行情订阅** | 无法实时推送 | 只能主动轮询 |
| **免费 API 限流** | TwelveData 8/min, Finnhub 60/min, AV 5/min | 不能高频轮询所有股票 |
| **美股波动大** | NVDA/TSLA 日内可达 5-10% | 30 分钟扫描错过完整波段 |
| **盘前/盘后交易** | 美东 04:00-09:30 / 16:00-20:00 | 传统交易时间判断不适用 |

## 核心矛盾

```
低频率数据 (API 限流)  vs  高波动市场 (需要快速响应)
```

## 解决方案：三层智能自适应

### 1. 分层轮询调度

```
┌─────────────────────────────────────────────────────────────┐
│                    三层优先级轮询                            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  P0 持仓股 (3-5 分钟)   ← 止损/止盈必须及时                 │
│    ├── NVDA (持仓中)                                        │
│    └── TSLA (持仓中)                                        │
│                                                             │
│  P1 高波动股 (10-15 分钟) ← 潜在交易机会                    │
│    ├── AAPL (新闻活跃)                                      │
│    └── BABA (财报季)                                        │
│                                                             │
│  P2 普通观察股 (20-30 分钟) ← 常规扫描                      │
│    ├── MSFT                                                 │
│    ├── GOOGL                                                │
│    └── ...                                                  │
│                                                             │
│  每轮 API 预算: 10 次 (TwelveData 8/min 限制内)             │
└─────────────────────────────────────────────────────────────┘
```

### 2. 事件驱动触发

```
外部事件 → 立即轮询相关股票 (不受固定间隔限制)

事件类型:
├── 重大新闻 (LLM 判定 HIGH impact) → 相关股票立即轮询
├── SPY/QQQ 异动 (> 0.5% 波动) → 所有持仓股立即轮询
├── VIX 飙升 (> 25) → 全部层级触发
└── 持仓股接近止损 (< 3%) → 该股轮询间隔缩短至 3 分钟
```

### 3. 本地价格预测引擎

```
在两次 API 调用之间，基于技术指标估算价格区间:

估算公式 (几何布朗运动):
  estimated = last_price + trend_bias + sentiment_bias
  upper_bound = estimated + volatility × √(hours) × price
  lower_bound = estimated - volatility × √(hours) × price

用途:
├── 持仓股止损风险预估 (不消耗 API)
├── 判断是否需要提前触发轮询
└── 置信度随时间衰减 (每小时 -15%)

示例:
  NVDA 上次轮询: 15 分钟前, price=$850, ATR=$15, trend=UP
  → 预估当前区间: [$842, $862], 置信度 85%
  → 止损线 $830, 下界 $842 > $830 → 安全
```

## 自适应频率调整规则

```
价格变化幅度        →  轮询间隔调整
────────────────────────────────────
> 3% (大幅波动)    →  间隔 ÷ 2 (最快 3 分钟)
1% - 3%            →  保持不变
0.1% - 1%          →  间隔 × 1.2
< 0.1% (几乎不动)  →  间隔 × 1.1
连续 3 次 < 0.1%   →  直接跳到 max_interval
```

## API 调用优化

### 旧架构 (固定 30 分钟全量扫描)

```
30 只股票 × 1 次 full_market_context = 30 次 API 调用
→ 超过 TwelveData 8/min 限制
→ 缓存 900 秒，数据过期
```

### 新架构 (自适应批次)

```
每轮扫描 (30 分钟周期内):
├── P0 持仓股 (3 只): 3 × 6 次 = 18 次
├── P1 高波动 (5 只): 5 × 2 次 = 10 次
├── P2 普通 (22 只): 22 × 1 次 = 22 次
└── 事件驱动 (按需): 0-5 次

总计: ~50 次/30 分钟 ≈ 1.7 次/分钟
→ 远低于 TwelveData 8/min 限制
→ 持仓股响应时间从 30 分钟降至 3-5 分钟
```

## 集成点

### scheduler_core.py
```python
# 初始化
self.polling_controller = AdaptivePollingController(api_budget_per_scan=10)
for sym in self.watchlist:
    self.polling_controller.register_symbol(sym, StockTier.WATCHLIST)

# 开仓时升级
self.polling_controller.update_tier(symbol, StockTier.POSITION)
```

### scheduler_tasks.py
```python
# 扫描循环
batch = self.polling_controller.get_polling_batch()
for symbol in batch:
    _process_symbol_impl(...)

# 处理结果
self.polling_controller.update_result(symbol, price, change_pct, context)

# 监控循环
at_risk = self.polling_controller.check_stop_loss_risk_for_positions(pos_info)
for risk in at_risk:
    # 预估止损风险告警
```

## 监控指标

```
轮询系统状态:
├── 总股票数: 30
├── 层级分布: P0=3, P1=5, P2=22
├── 强制队列: 0
├── 本轮 API 调用: 8/10
└── 数据源状态:
    ├── TwelveData: 6 tokens remaining
    ├── Finnhub: 55 calls remaining
    └── AlphaVantage: 3 calls/min, 480/day
```

## 未来扩展

1. **WebSocket 实时数据** → 如果获取到富途美股订阅，直接切换为事件驱动
2. **多数据源负载均衡** → 根据剩余 token 动态分配请求
3. **盘前/盘后策略** → 降低轮询频率至 15-20 分钟
4. **机器学习预测** → 用历史数据训练更准确的价格预测模型
