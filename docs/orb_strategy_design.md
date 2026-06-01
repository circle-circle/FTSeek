"""
ORB 策略设计文档 — RFC-056-DESIGN (连续突破模式 v2)

更新日期: 2026-06-01
核心改进: 移除时间限制,实现连续突破 + 动态持仓限制 + 隔夜持仓管理

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 0. 设计理念变更

### 旧设计 (时间受限)
- 仅在 09:45-11:00 入场
- 15:55 强制平仓
- 固定最大持仓数
- 不允许隔夜

### 新设计 (连续突破模式)
- 09:45-16:00 全天连续入场
- 允许隔夜持仓 (自动调整止损)
- 基于账户资金动态计算持仓上限
- 15 分钟滚动区间替代开盘区间

核心问题：如何定义合理的 entry_price 上下限，让利润奔跑，止损在最佳位置？

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 1. Entry Price 上下限：动态滚动区间构建

### 当前问题 (已解决)
- 原设计仅使用 09:30-09:45 的静态 opening range
- 无法适应盘中波动性变化
- 错过全天突破机会

### 改进方案：双模式区间构建

```
Mode 1: 开盘区间 (09:30-09:45)
  - orb_high = max(high[0:15])
  - orb_low  = min(low[0:15])
  - orb_width = (orb_high - orb_low) / orb_low * 100
  - 用途: 捕捉开盘突破机会

Mode 2: 滚动区间 (09:45-16:00, 每 5 秒更新)
  - rolling_high = max(high[-15:])  # 最近 15 根 K 线
  - rolling_low  = min(low[-15:])
  - rolling_width = (rolling_high - rolling_low) / rolling_low * 100
  - 用途: 捕捉全天连续突破机会

区间质量过滤:
  - 区间太窄: width < 0.2% → 不交易 (无趋势)
  - 区间太宽: width > 3.0% → 不交易 (过度波动)
  - 质量太低: quality_score < 0.5 → 不交易 (异常区间)
```

### 突破判定

```
LONG 突破条件:
  price > effective_high * (1 + breakout_buffer)
  AND breakout_buffer = max(0.1%, 0.3 * atr_14 / price)
  AND volume > 1.2 * avg_volume[20]
  AND vwap_slope > 0 (VWAP 斜率为正)

SHORT 突破条件:
  price < effective_low * (1 - breakout_buffer)
  AND breakout_buffer = max(0.1%, 0.3 * atr_14 / price)
  AND volume > 1.2 * avg_volume[20]
  AND vwap_slope < 0 (VWAP 斜率为负)
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 2. 止损位置：自适应 + 兜底

### 当前问题
- 固定 1.0 ATR，缺乏市场状态感知
- 高波动时太紧，低波动时太宽

### 改进方案：区间宽度 + ATR 组合止损

```
基础止损计算:
  sl_distance = max(
    orb_width * sl_width_mult,      # 基于区间宽度
    atr_14 * sl_atr_mult,            # 基于波动性
    min_sl_distance                   # 兜底最小距离
  )

Market Regime 自适应:
  ┌──────────────────────────────────────────────────────┐
  │ Regime      │ sl_width_mult │ sl_atr_mult │ 说明     │
  ├──────────────────────────────────────────────────────┤
  │ TREND_UP    │ 0.5           │ 1.2         │ 顺势宽松 │
  │ TREND_DOWN  │ 0.5           │ 1.2         │ 顺势宽松 │
  │ RANGE       │ 0.8           │ 1.5         │ 震荡适中 │
  │ HIGH_VOL    │ 0.3           │ 0.8         │ 高波收紧 │
  │ LOW_VOL     │ 1.0           │ 2.0         │ 低波放宽 │
  └──────────────────────────────────────────────────────┘

兜底机制:
  min_sl_distance = price * 0.5%  # 最小 0.5% 止损距离
  max_sl_distance = price * 3.0%  # 最大 3.0% 止损距离
  
  sl_distance = clamp(sl_distance, min_sl_distance, max_sl_distance)
```

### 止损位置示例

```
LONG 入场:
  entry_price = 100.0
  orb_high = 99.5, orb_low = 98.5 (orb_width = 1.0%)
  atr_14 = 0.8 (0.8%)
  regime = TREND_UP
  
  sl_distance = max(1.0% * 0.5, 0.8% * 1.2, 0.5%) = max(0.5%, 0.96%, 0.5%) = 0.96%
  sl_price = 100.0 * (1 - 0.0096) = 99.04
  
SHORT 入场:
  entry_price = 100.0
  orb_high = 101.5, orb_low = 100.5 (orb_width = 1.0%)
  atr_14 = 0.8 (0.8%)
  regime = HIGH_VOL
  
  sl_distance = max(1.0% * 0.3, 0.8% * 0.8, 0.5%) = max(0.3%, 0.64%, 0.5%) = 0.64%
  sl_price = 100.0 * (1 + 0.0064) = 100.64
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 3. 止盈机制：分层 + 动态追踪

### 当前问题
- 固定 2.0 ATR 止盈，无法让利润奔跑
- 缺乏分层策略

### 改进方案：三层止盈 + R-multiple 追踪

```
定义 R = sl_distance (风险单位)

Layer 1: 保守止盈 (1R)
  - 触发: profit >= 1.0 * R
  - 动作: 平仓 1/3, 止损移到 breakeven (entry_price)
  - 目的: 锁定部分利润，消除风险

Layer 2: 标准止盈 (2R)
  - 触发: profit >= 2.0 * R
  - 动作: 平仓 1/3, 激活追踪止损
  - 追踪参数: trail_distance = 1.0 * R
  - 目的: 平衡利润和回撤保护

Layer 3: 让利润奔跑 (>2R)
  - 剩余 1/3 仓位使用动态追踪
  - trail_distance = max(1.0 * R, 1.5 * atr_14)
  - 每创新高/新低，trail 跟随移动
  - 无固定止盈目标，让利润奔跑
  
追踪止损细节:
  LONG:
    trail_price = max(high_since_entry) - trail_distance
    当 price < trail_price 时平仓
    
  SHORT:
    trail_price = min(low_since_entry) + trail_distance
    当 price > trail_price 时平仓
```

### 利润奔跑示例

```
LONG 入场: entry=100, sl=99 (R=1.0)

价格走势:
  T+5min:  price=101 (+1R) → 平1/3, sl→100 (breakeven)
  T+15min: price=102 (+2R) → 平1/3, 激活trail=101
  T+30min: price=103 (+3R) → trail→102
  T+45min: price=104 (+4R) → trail→103
  T+60min: price=102.5 → 触发trail, 平仓剩余1/3

结果:
  1/3 @ 101: +1R
  1/3 @ 102: +2R
  1/3 @ 102.5: +2.5R
  平均: +1.83R
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 4. 区间质量评估

### 评分模型

```
quality_score = w1 * width_score + w2 * volume_score + w3 * vwap_score

width_score:
  - 0.3% <= orb_width <= 2.0%: 1.0 (理想)
  - 0.2% <= orb_width < 0.3%: 0.5 (偏窄)
  - 2.0% < orb_width <= 3.0%: 0.5 (偏宽)
  - 其他: 0.0 (不可交易)

volume_score:
  - volume_ratio = current_volume / avg_volume[20]
  - volume_ratio >= 1.5: 1.0 (放量)
  - 1.2 <= volume_ratio < 1.5: 0.7 (适度)
  - 0.8 <= volume_ratio < 1.2: 0.5 (平量)
  - volume_ratio < 0.8: 0.3 (缩量)

vwap_score:
  - price 在 VWAP 上方 (LONG): 1.0
  - price 在 VWAP 下方 (SHORT): 1.0
  - 其他: 0.5

权重:
  w1 = 0.4 (区间宽度最重要)
  w2 = 0.3 (成交量确认)
  w3 = 0.3 (VWAP 趋势)

质量门槛:
  quality_score >= 0.7: 正常仓位
  0.5 <= quality_score < 0.7: 半仓
  quality_score < 0.5: 不交易
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 5. 动态持仓限制

### 设计原则
- 基于账户资金动态计算最大持仓数
- 避免过度分散或过度集中
- 15:30 后收紧持仓限制 (隔夜风险)

### 计算公式

```
基础持仓数:
  base_positions = 账户资金 / 10000  # 每 1 万美元允许 1 个持仓

最小持仓数:
  min_positions = 2  # 至少允许 2 个持仓

最大持仓数:
  max_positions = config.max_positions (默认 10)

正常时段 (09:45-15:30):
  allowed = min(max(base_positions, min_positions), max_positions)

临近盘末 (15:30-16:00):
  allowed = min(max(base_positions, min_positions), max_positions) * 50%
  # 隔夜持仓限制更严格

示例:
  账户资金 = $50,000
  base_positions = 5
  min_positions = 2
  max_positions = 10
  
  正常时段: allowed = min(max(5, 2), 10) = 5
  临近盘末: allowed = 5 * 50% = 2 (最多 2 个隔夜持仓)
```

### 持仓计数逻辑

```python
orb_count = len(orb_engine.positions)

# 计入 PositionStore 中所有 orb_* 仓位
for pos in position_store.all():
    if pos.owner_strategy.startswith('orb_'):
        orb_count += 1

if orb_count >= allowed:
    break  # 不再开新仓
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 6. 隔夜持仓管理

### 设计原则
- 允许隔夜持仓 (不强制 15:55 平仓)
- 自动调整止损 (隔夜波动更大)
- 放宽追踪止损 (给趋势更多空间)

### 止损调整逻辑

```python
def _manage_overnight_positions(now, current_time):
    """15:30 后触发,每天只执行一次"""
    
    if current_time < dt_time(15, 30):
        return
    
    if already_adjusted_today():
        return
    
    for pos in positions:
        # 放宽止损距离 1.5x
        original_sl_distance = pos.sl_distance
        pos.sl_distance *= 1.5
        
        # 放宽追踪止损距离 1.3x
        if pos.trail_stop > 0:
            original_trail_distance = abs(pos.entry_price - pos.trail_stop)
            new_trail_distance = original_trail_distance * 1.3
            
            if pos.direction == "LONG":
                pos.trail_stop = pos.peak_price - new_trail_distance
            else:  # SHORT
                pos.trail_stop = pos.trough_price + new_trail_distance
    
    mark_adjusted_today()
```

### 隔夜持仓示例

```
入场: LONG @ 100, sl=99 (sl_distance=1.0), trail=101

15:30 调整:
  sl_distance = 1.0 * 1.5 = 1.5
  sl = 100 - 1.5 = 98.5
  trail_distance = 1.0 * 1.3 = 1.3
  trail = peak_price - 1.3 = 102 - 1.3 = 100.7

次日开盘:
  price = 101 → 继续持有 (未触发 sl=98.5 或 trail=100.7)
  price = 103 → trail 更新为 103 - 1.3 = 101.7
  price = 101.5 → 触发 trail, 平仓 @ 101.5
```

### 风险控制

```
隔夜持仓风险:
  - 跳空风险: 放宽止损 1.5x 提供缓冲
  - 波动风险: 追踪止损放宽 1.3x 避免被噪音扫出
  - 集中度风险: 15:30 后持仓限制收紧 50%

监控指标:
  - 隔夜持仓胜率 (目标: >= 45%)
  - 隔夜持仓平均 R-multiple (目标: >= 1.0R)
  - 跳空亏损次数 (目标: < 10% 隔夜持仓)
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 7. 完整交易流程 (连续突破模式)

```
09:30-09:45: Phase 1 - 构建开盘区间
  - 记录 orb_high, orb_low, orb_width
  - 计算 atr_14, vwap, avg_volume
  - 评估区间质量 quality_score
  - 同时监控已有持仓 (隔夜持仓)

09:45-16:00: Phase 2 - 连续突破模式
  每 5 秒执行:
    1. 构建 15 分钟滚动区间 (rolling_high/low)
    2. 检测突破条件 (price/volume/vwap)
    3. 评估区间质量 (>= 0.5 才交易)
    4. 计算自适应止损 (regime-aware)
    5. 检查动态持仓限制 (基于账户资金)
    6. 执行入场 (分层仓位)
    7. 监控持仓退出 (分层止盈 + 追踪止损)

15:30-16:00: 隔夜持仓管理
  - 自动放宽止损距离 * 1.5 (隔夜波动更大)
  - 追踪止损距离 * 1.3 (给隔夜趋势更多空间)
  - 限制新持仓数 (max_positions * 50%)

入场后: 三层止盈 + 动态追踪
  - Layer 1: 1R 平 1/3, sl→breakeven
  - Layer 2: 2R 平 1/3, 激活 trail
  - Layer 3: trail 跟随，让利润奔跑

隔夜持仓:
  - 不强制平仓,允许持仓过夜
  - 止损自动放宽 1.5x
  - 次日继续监控退出
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 8. 预期效果

### 胜率提升
- 连续突破模式: 捕捉全天突破机会 → 交易次数 +50%
- 区间质量过滤: 避免低质量区间 → 胜率 +5-10%
- 自适应止损: 避免被噪音扫出 → 胜率 +5-8%
- VWAP 确认: 顺势交易 → 胜率 +3-5%

### 利润奔跑
- 分层止盈: 锁定部分利润 + 保留奔跑仓位
- 动态追踪: 根据波动性调整 → 平均 R-multiple +0.5-1.0R
- 隔夜持仓: 捕捉多日趋势 → 平均 R-multiple +1.0-2.0R (隔夜)

### 风险控制
- 动态持仓限制: 基于资金规模 → 避免过度分散
- 隔夜止损放宽: 1.5x 缓冲 → 减少跳空亏损
- 区间质量评分: 低质量不交易 → 减少无效交易 30-40%
- 自适应止损: 高波动收紧 → 单笔最大亏损 -20%

### 关键指标目标

```
交易频率:
  - 连续突破模式: 5-10 笔/天 (vs 原 1-3 笔/天)
  - 隔夜持仓: 1-3 笔/周

胜率:
  - 日内交易: >= 50%
  - 隔夜交易: >= 45%
  - 总体: >= 48%

盈亏比:
  - 日内交易: >= 1.5R
  - 隔夜交易: >= 2.0R
  - 总体: >= 1.6R

最大回撤:
  - 单笔: <= 2.0% 账户资金
  - 日度: <= 5.0% 账户资金
  - 周度: <= 8.0% 账户资金
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 9. 实施优先级

### Phase 1 (已完成): 核心改进
  ✓ 动态区间扩展 (15 分钟滚动区间)
  ✓ 区间质量过滤 (width + volume)
  ✓ 自适应止损 (regime-aware)
  ✓ 连续突破模式 (09:45-16:00)

### Phase 2 (已完成): 止盈优化
  ✓ 三层止盈机制
  ✓ 动态追踪止损
  ✓ R-multiple 计算

### Phase 3 (已完成): 持仓管理
  ✓ 动态持仓限制 (基于账户资金)
  ✓ 隔夜持仓管理 (自动调整止损)
  ✓ 临近盘末持仓收紧

### Phase 4 (下周): 高级特性
  □ VWAP 斜率确认
  □ 历史区间宽度对比
  □ 成交量异常检测
  □ 隔夜持仓性能监控

### Phase 5 (第三周): 优化迭代
  □ 基于实盘数据调整参数
  □ A/B 测试不同持仓限制
  □ 隔夜持仓 vs 日内交易对比
  □ 多标的相关性分析

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 10. 交易来源区分

### 标识方案

```python
# ORB 引擎交易
trade_records.strategy_id = "orb_engine"
trade_records.owner_strategy = "orb_engine"
trade_records.source = "orb_breakout"
trade_records.metadata = {
    "orb_high": 100.5,
    "orb_low": 99.8,
    "quality_score": 0.75,
    "regime": "TREND_UP",
    "is_overnight": True/False,
    "is_rolling_range": True/False,
}

# 主线交易
trade_records.strategy_id = "main_pipeline"
trade_records.owner_strategy = "main"
trade_records.source = "main_signal"
```

### 查询示例

```sql
-- ORB 交易统计
SELECT 
    COUNT(*) as total_trades,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(AVG(pnl), 2) as avg_pnl,
    ROUND(SUM(pnl), 2) as total_pnl
FROM trade_records
WHERE strategy_id = 'orb_engine'
  AND created_at >= DATE('now', '-7 days');

-- 隔夜 vs 日内对比
SELECT 
    JSON_EXTRACT(metadata, '$.is_overnight') as is_overnight,
    COUNT(*) as trades,
    ROUND(AVG(pnl_r_multiple), 2) as avg_r
FROM trade_records
WHERE strategy_id = 'orb_engine'
GROUP BY is_overnight;

-- 滚动区间 vs 开盘区间
SELECT 
    JSON_EXTRACT(metadata, '$.is_rolling_range') as is_rolling,
    COUNT(*) as trades,
    ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_rate
FROM trade_records
WHERE strategy_id = 'orb_engine'
GROUP BY is_rolling;
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 11. 监控与告警

### 实时监控指标

```
ORB 引擎状态:
  - 当前持仓数 / 最大持仓数
  - 今日交易次数 / 今日盈亏
  - 滚动区间构建次数 / 成功次数
  - 突破检测次数 / 入场次数

交易质量:
  - 区间质量评分分布
  - 自适应止损距离分布
  - 分层止盈触发率
  - 追踪止损触发率

隔夜持仓:
  - 隔夜持仓数量
  - 隔夜止损调整次数
  - 隔夜持仓盈亏
  - 跳空亏损次数
```

### 告警规则

```
高优先级 (立即通知):
  - ORB 引擎崩溃
  - 单笔亏损 > 3% 账户资金
  - 日度亏损 > 5% 账户资金
  - 持仓数超过限制

中优先级 (15 分钟内通知):
  - 区间质量评分 < 0.5 连续 5 次
  - 连续 3 笔亏损
  - 隔夜持仓跳空亏损

低优先级 (每日汇总):
  - 交易频率低于预期
  - 胜率低于 45%
  - 盈亏比低于 1.2R
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 12. 总结

### 核心改进

1. **连续突破模式**: 从 09:45-11:00 扩展到 09:45-16:00,捕捉全天突破机会
2. **15 分钟滚动区间**: 动态更新区间,适应盘中波动
3. **动态持仓限制**: 基于账户资金计算,避免过度分散
4. **隔夜持仓管理**: 自动调整止损,捕捉多日趋势
5. **三层止盈 + 追踪止损**: 锁定利润,让利润奔跑

### 预期效果

- 交易频率: +50% (5-10 笔/天)
- 胜率: >= 48%
- 盈亏比: >= 1.6R
- 最大回撤: <= 8% 周度

### 关键成功因素

1. 区间质量评分 >= 0.5 才交易
2. 自适应止损根据市场状态调整
3. 分层止盈平衡利润和风险
4. 隔夜持仓放宽止损 1.5x
5. 15:30 后收紧持仓限制 50%

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
