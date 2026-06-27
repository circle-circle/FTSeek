# RFC-054 补充: 富途数据能力审计与架构优化

> **日期**: 2026-05-30
> **关联**: RFC-054-trend-first-signal-architecture.md
> **状态**: 发现重大架构优化机会

---

## 1. 核心发现: 富途已提供 RFC-054 所需的绝大部分数据

### 1.1 富途 LV3 数据能力全景

```
FutuLV3Provider (已有, 生产运行)
├── 实时报价 (Push Quote) — 毫秒级延迟
├── K线数据 (1M/5M/15M/30M/60M/DAY) — 本地计算技术指标
├── 逐笔成交 (Push Ticker) — 主买/主卖方向 + 量
├── 买卖盘深度 (Push OrderBook) — 10档盘口
├── 批量快照 (get_batch_quote) — 20只/次
└── 健康监控 + 自动重连

MarketDataRouter.get_full_market_context() (已有)
├── LV3 技术指标 (RSI/MACD/BB/ATR/SuperTrend/MA/VWAP/KDJ)
├── trend: UP/DOWN/NEUTRAL (均线排列判断)
├── volume_ratio: 当前量/5日均量
├── slope_5m: 5分钟价格斜率
├── market_regime: TREND_UP/TREND_DOWN/RANGE/HIGH_VOL/LOW_VOL
├── order_flow_imbalance: 买卖力量比
├── ticker_buy_sell_ratio: 逐笔买卖比
├── ob_depth_imbalance: 盘口深度失衡
├── spy_price, spy_ma20, spy_ma50: SPY 大盘数据
└── vix, vix_regime: 波动率数据
```

### 1.2 RFC-054 需求 vs 富途已有能力对照

| RFC-054 需求 | 富途已有 | 当前状态 | 问题 |
|-------------|---------|---------|------|
| 市场 regime 判断 | ✅ `_calc_market_regime()` | 已在 raw dict 中 | **SymbolContext.from_raw() 丢弃了** |
| 趋势方向 (UP/DOWN) | ✅ `_calc_trend()` | 已在 raw dict 中 | **SymbolContext.from_raw() 丢弃了** |
| 量比 (volume_ratio) | ✅ `lv3_technical_calculator` | 已在 raw dict 中 | **SymbolContext.from_raw() 丢弃了** |
| 5分钟价格斜率 | ✅ `slope_5m` | 已在 raw dict 中 | **SymbolContext.from_raw() 丢弃了** |
| 订单流失衡 | ✅ `order_flow_imbalance` | 已在 raw dict 中 | **SymbolContext.from_raw() 丢弃了** |
| 逐笔买卖情绪 | ✅ `ticker_buy_sell_ratio` | 已在 raw dict 中 | **SymbolContext.from_raw() 丢弃了** |
| SPY 大盘数据 | ✅ `spy_price/ma20/ma50` | SymbolContext 已映射 | ✅ 可用 |
| ATR | ✅ `atr`, `atr_pct` | SymbolContext 已映射 | ✅ 可用 |
| RSI/MACD/BB/ST | ✅ 全部指标 | SymbolContext 已映射 | ✅ 可用 |
| 板块热度 | ⚠️ `SectorLeaderDetector` | 独立模块, 不在数据流中 | 需要注入 |
| 资金流 (capital_flow) | ✅ `order_flow_imbalance` + `ticker` | 已在 raw dict 中 | **SymbolContext.from_raw() 丢弃了** |

**结论**: RFC-054 的 6/8 个数据需求已被富途 + MarketDataRouter 满足，但数据在 `SymbolContext.from_raw()` 转换时被**丢弃**。

---

## 2. 根因: SymbolContext 数据管道存在 "瓶颈"

```
数据流:

Futu LV3 → MarketDataRouter.get_full_market_context()
  ↓ (raw dict, 50+ 字段, 包含 market_regime/trend/volume_ratio/slope_5m/...)
  
SymbolContext.from_raw(raw_dict)
  ↓ (dataclass, 仅映射 ~25 字段)
  ↓ ⚠️ market_regime, trend, volume_ratio, slope_5m, order_flow_imbalance 被丢弃
  
signal_stage.process(ctx)
  ↓ trend_first.evaluate(ctx)
  ↓ ⚠️ 无法获取 regime/trend/volume_ratio → 方案被迫用 metadata hack 注入
```

**这是典型的 "管道瓶颈" 反模式**: 上游生产了丰富数据，中游 dataclass 只传递子集，下游被迫重新获取或 hack。

---

## 3. 优化方案: 扩展 SymbolContext (非补丁式注入)

### 3.1 方案对比

| 方案 | 代码量 | 优雅度 | 可维护性 | 风险 |
|------|--------|--------|---------|------|
| **A: metadata hack 注入** (原 RFC-054) | ~50行 | ❌ 低 | ❌ 差 (隐式依赖) | 🟡 中 |
| **B: 扩展 SymbolContext + from_raw()** | ~30行 | ✅ 高 | ✅ 好 (显式类型) | ✅ 低 |
| C: 重构整条管道 | ~500行 | ✅ 高 | ✅ 好 | 🔴 高 (影响面大) |

**选择方案 B**: 在 `SymbolContext` 中新增字段，在 `from_raw()` 中映射已有数据。

### 3.2 具体代码变更

#### 3.2.1 扩展 SymbolContext (pipeline_contracts.py)

```python
@dataclass
class SymbolContext:
    # ... 现有字段保持不变 ...
    
    # ── 新增: 趋势与市场状态 (RFC-054 Trend-First 所需) ──
    # 这些数据已在 MarketDataRouter.get_full_market_context() 中计算,
    # 但之前 from_raw() 未映射, 导致下游策略无法获取。
    
    market_regime: str = ""           # "TREND_UP"/"TREND_DOWN"/"RANGE"/"HIGH_VOL"/"LOW_VOL"
    trend: str = ""                   # "UP"/"DOWN"/"NEUTRAL" (个股均线排列)
    volume_ratio: float = 1.0         # 当前成交量 / 5日均量
    slope_5m: float = 0.0             # 5分钟价格斜率 (%)
    order_flow_imbalance: float = 0.0 # 买卖力量失衡 [-1, 1]
    ticker_buy_sell_ratio: float = 1.0  # 逐笔买卖比
    high_distance_pct: float = 0.0    # 距日内高点距离 (%)
```

#### 3.2.2 扩展 from_raw() (pipeline_contracts.py)

```python
@staticmethod
def from_raw(symbol, context, earnings_status=None, vix_multipliers=None):
    ctx = SymbolContext(symbol=symbol)
    # ... 现有映射保持不变 ...
    
    # ── 新增: 趋势与市场状态映射 ──
    ctx.market_regime = str(context.get("market_regime", "") or "")
    ctx.trend = str(context.get("trend", "") or "")
    ctx.volume_ratio = float(context.get("volume_ratio", 1.0) or 1.0)
    ctx.slope_5m = float(context.get("slope_5m", 0.0) or 0.0)
    ctx.order_flow_imbalance = float(context.get("order_flow_imbalance", 0.0) or 0.0)
    ctx.ticker_buy_sell_ratio = float(context.get("ticker_buy_sell_ratio", 1.0) or 1.0)
    ctx.high_distance_pct = float(context.get("high_distance_pct", 0.0) or 0.0)
    
    # SPY 数据 (已有 spy_price, spy_ma200, 新增 ma20/ma50)
    ctx.spy_ma20 = float(context.get("spy_ma20", 0) or 0)
    ctx.spy_ma50 = float(context.get("spy_ma50", 0) or 0)
    
    return ctx
```

### 3.3 对 RFC-054 代码的影响

#### 删除: `_inject_regime_and_sector()` (不再需要)

原方案需要在 `signal_stage.py` 中手动注入 regime/sector metadata。扩展 SymbolContext 后:
- `ctx.market_regime` 直接从 from_raw() 获取 ✅
- `ctx.volume_ratio` 直接从 from_raw() 获取 ✅  
- `ctx.trend` 直接从 from_raw() 获取 ✅
- `ctx.order_flow_imbalance` 替代 `capital_flow.score` ✅

**唯一仍需注入的**: `sector_info` (板块热度) — 这确实不在 MarketDataRouter 数据流中，需要保留 `_inject_sector_info()` 但大幅简化。

#### 简化: `trend_first.py` 直接使用 ctx 字段

```python
# 修正前 (hack):
regime = ctx.metadata.get("regime", "RANGE")  # 依赖外部注入
volume_ratio = getattr(ctx, 'volume_ratio', 1.0)  # 不存在
capital_flow = ctx.metadata.get("capital_flow", {})  # 依赖外部注入

# 修正后 (原生):
regime = ctx.market_regime  # 直接在 dataclass 上
volume_ratio = ctx.volume_ratio  # 直接在 dataclass 上
order_flow = ctx.order_flow_imbalance  # 富途实时数据
```

---

## 4. 修正后的架构

### 4.1 数据流 (修正后)

```
Futu LV3 → MarketDataRouter.get_full_market_context()
  ↓ raw dict (50+ 字段)
  
SymbolContext.from_raw(raw_dict)  ← 扩展: 映射 market_regime/trend/volume_ratio/...
  ↓ typed dataclass (30+ 字段)
  
signal_stage.process(ctx)
  ├── fast_lane (不变)
  ├── trend_first.evaluate(ctx)  ← 直接用 ctx.market_regime / ctx.volume_ratio
  │   └── _inject_sector_info(ctx)  ← 仅板块数据需要注入 (SectorLeaderDetector)
  ├── technical (不变)
  └── llm_vote (不变)
```

### 4.2 文件变更清单 (修正后)

```
修改:
  app/core/pipeline_contracts.py        +15行 (SymbolContext 新字段 + from_raw 映射)
  app/strategy/pure/trend_first.py      ~140行 (使用 ctx 原生字段, 不依赖 metadata)
  app/pipeline/stages/signal_stage.py   +25行 (_inject_sector_info + best-hit 编排)
  app/strategy/pure/__init__.py         +2行 (import trend_first)
  config/unified_config.yaml            +30行 (strategies.trend_first)

不修改:
  app/data/futu_lv3_provider.py         不变 (数据已提供)
  app/data/market_data_router.py        不变 (数据已计算)
  app/data/lv3_technical_calculator.py  不变 (指标已计算)
  app/risk/sector_leader.py             不变 (只被调用)
  app/strategy/regime_adapter.py        不变 (只被调用)
  app/risk/sector_limits.py             不变 (保护已存在)
```

---

## 5. 富途数据的充分利用方案

### 5.1 闪崩检测: 用 slope_5m 替代 change_pct

```python
def _check_price_stability(ctx: SymbolContext, config: Dict) -> bool:
    """L0-b: 闪崩检测
    使用 slope_5m (富途已计算的5分钟价格斜率) 比 change_pct (日内累计) 更灵敏"""
    
    if ctx.slope_5m < -2.0:  # 5分钟跌>2%
        if ctx.volume_ratio > 2.0:  # 放量
            return False  # 闪崩拦截
    return True
```

### 5.2 资金流确认: 用 order_flow_imbalance + ticker_buy_sell_ratio

```python
# 替代原来 metadata["capital_flow"]["score"] 的 hack
def _eval_capital_flow(ctx: SymbolContext) -> float:
    """从富途实时数据评估资金流方向
    
    order_flow_imbalance: 盘口买卖力量比 [-1, 1]
    ticker_buy_sell_ratio: 逐笔成交买卖比 (1.0=均衡, >1=买入多)
    
    返回: 资金流修正系数 (0.85~1.15)
    """
    # 盘口失衡
    ofi = ctx.order_flow_imbalance  # [-1, 1]
    # 逐笔买卖比
    tbsr = ctx.ticker_buy_sell_ratio  # [0, ∞), 1.0=均衡
    
    # 综合评分: 两者方向一致时信号更强
    if ofi > 0.1 and tbsr > 1.2:
        return 1.15  # 强资金流入
    elif ofi < -0.1 and tbsr < 0.8:
        return 0.85  # 强资金流出
    else:
        return 1.0   # 中性
```

### 5.3 突破确认: 用 volume_ratio (富途已计算)

```python
def _eval_breakout(ctx: SymbolContext, direction: str, config: Dict) -> Optional[float]:
    """放量突破入场
    volume_ratio 已由 LV3TechnicalCalculator 计算 (当前量/5日均量)"""
    
    vol_ratio = ctx.volume_ratio  # 直接使用, 不再 getattr hack
    min_vol = config.get("breakout_min_volume_ratio", 2.0)
    
    if vol_ratio < min_vol:
        return None
    # ...
```

### 5.4 趋势确认: 用 trend + market_regime (富途已计算)

```python
# Layer 1: 环境判断 — 直接用 ctx.market_regime
regime = ctx.market_regime  # 已由 _calc_market_regime() 计算
if regime in ("RANGE", "HIGH_VOL"):
    return None

# Layer 3: 入场确认 — 用 ctx.trend 作为额外确认
# trend 基于均线排列 (MA5>MA20>MA50=UP), 比 SuperTrend 更稳定
trend_aligned = (ctx.trend == "UP") == (direction == "LONG")
trend_bonus = 10 if trend_aligned else 0
```

---

## 6. 模块化设计原则

### 6.1 数据层 vs 策略层 分离

```
数据层 (已有, 不动):
  FutuLV3Provider → 原始行情
  LV3TechnicalCalculator → 技术指标计算
  MarketDataRouter → 数据融合 + regime 判断
  SymbolContext.from_raw() → 类型化传递  ← 扩展此处

策略层 (新增):
  trend_first.py → 消费 SymbolContext, 输出 SignalCandidate
  (纯函数, 零副作用, 不直接调用 Futu API)

注入层 (简化):
  signal_stage._inject_sector_info() → 仅板块数据
  (其他数据已通过 SymbolContext 传递)
```

### 6.2 与现有系统的融合点

| 融合点 | 方式 | 影响范围 |
|--------|------|---------|
| SymbolContext 扩展 | 新增字段, 默认值兼容 | 所有使用 SymbolContext 的代码无需修改 |
| from_raw() 扩展 | 新增映射, dict.get() 安全读取 | 无副作用 |
| signal_stage 编排 | 新增 trend_first 评估 | 仅影响信号选择逻辑 |
| technical 策略 | 不修改, 降级为 RANGE 市备用 | 保持兼容 |
| ExitMonitor | 消费 SignalCandidate.metadata 中的 ATR 参数 | 需要后续对接 |

---

## 7. 给多 Agent 评审的补充问题

8. **SymbolContext 扩展是否向后兼容?** 新增字段都有默认值 (空字符串/0.0/1.0), 现有策略不受影响。但需确认 `from_raw()` 的 `context.get()` 在 key 不存在时行为正确。

9. **market_regime 的粒度是否足够?** `_calc_market_regime()` 基于 VIX + trend, 没有 SPY MA20/MA50 交叉。RFC-054 的 regime 推断方案 (用 spy_price > spy_ma200) 与之不完全一致。应该统一使用哪个?

10. **order_flow_imbalance 的可靠性?** 盘口买卖量 (bid_vol/ask_vol) 在美股 Level 2 数据中可能包含大量隐藏单。逐笔数据 (ticker) 是否更可靠?

11. **板块数据注入是否应该走 SymbolContext?** 如果将 `sector_info` 也加入 SymbolContext, 需要在 market_data_stage 中调用 SectorLeaderDetector, 这会增加数据层的复杂度。当前保持策略层注入是否更合理?

---

## 8. 总结

### 8.1 关键改进

| 维度 | 原方案 (RFC-054 v2) | 优化后 (v3) |
|------|---------------------|-------------|
| regime 来源 | `_inject` hack (从 spy_price 推断) | `ctx.market_regime` (富途已计算) |
| volume_ratio | `getattr` 不存在字段 → 永远 1.0 | `ctx.volume_ratio` (富途已计算) |
| 闪崩检测 | `ctx.change_pct` (日内累计, 迟钝) | `ctx.slope_5m` (5分钟斜率, 灵敏) |
| 资金流 | metadata hack, 默认 0.5 | `ctx.order_flow_imbalance` (实时) |
| 代码量 | ~210行 (含 hack) | ~180行 (更简洁) |
| 新增文件 | 2 | 2 |
| 修改文件 | 3 | 4 (多改 pipeline_contracts.py) |
| 优雅度 | 🟡 metadata 隐式传递 | ✅ dataclass 显式类型 |
| 可维护性 | 🟡 新策略需要重复 hack | ✅ 扩展字段即可复用 |

### 8.2 实施顺序

```
Step 1: 扩展 SymbolContext + from_raw() (pipeline_contracts.py)
  → 零风险, 纯增加字段, 默认值兼容
  → 现有策略不受影响

Step 2: 编写 trend_first.py (使用 ctx 原生字段)
  → 纯函数, 零副作用

Step 3: 修改 signal_stage.py (注入 sector_info + best-hit 编排)
  → 仅板块数据需要注入, 其他走 ctx

Step 4: YAML 配置 + 测试

Step 5: 观测模式部署
```
