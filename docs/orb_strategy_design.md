"""
ORB 策略设计文档 — RFC-056-DESIGN

核心问题：如何定义合理的 entry_price 上下限，让利润奔跑，止损在最佳位置？

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 1. Entry Price 上下限：动态区间构建

### 当前问题
- 使用 09:30-09:45 的静态 high/low
- 无法适应盘中波动性变化
- 区间质量参差不齐

### 改进方案：三层区间构建

```
Layer 1: 基础区间 (09:30-09:45)
  - orb_high = max(high[0:15])
  - orb_low  = min(low[0:15])
  - orb_width = (orb_high - orb_low) / orb_low * 100

Layer 2: 波动性过滤
  - atr_14 = ATR(14根1分钟K线)
  - historical_width = 过去5天同时段平均区间宽度
  - 质量评分 = orb_width / historical_width
  
  过滤条件:
  - 区间太窄: orb_width < 0.2% → 不交易 (无趋势)
  - 区间太宽: orb_width > 3.0% → 不交易 (过度波动)
  - 质量太低: quality_score < 0.5 → 不交易 (异常区间)
  - 质量太高: quality_score > 2.0 → 降低仓位 (可能反转)

Layer 3: 动态扩展 (09:45-11:00)
  - 每5分钟重新计算一次"有效区间"
  - effective_high = max(orb_high, rolling_high[5min])
  - effective_low  = min(orb_low,  rolling_low[5min])
  - 但只在突破方向扩展 (LONG只扩展high, SHORT只扩展low)
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

## 5. 完整交易流程

```
09:30-09:45: 构建基础区间
  - 记录 orb_high, orb_low, orb_width
  - 计算 atr_14, vwap, avg_volume
  - 评估区间质量 quality_score

09:45-11:00: 检测突破
  每5秒检查:
    1. 更新 effective_high/low (动态扩展)
    2. 检查突破条件 (price/volume/vwap)
    3. 评估区间质量 (>= 0.7 才交易)
    4. 计算自适应止损 (regime-aware)
    5. 执行入场 (分层仓位)

入场后: 三层止盈 + 动态追踪
  - Layer 1: 1R 平 1/3, sl→breakeven
  - Layer 2: 2R 平 1/3, 激活 trail
  - Layer 3: trail 跟随，让利润奔跑

15:55: 盘末强制平仓
  - 所有剩余仓位市价平仓
  - 避免隔夜风险
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 6. 预期效果

### 胜率提升
- 区间质量过滤: 避免低质量区间 → 胜率 +5-10%
- 自适应止损: 避免被噪音扫出 → 胜率 +5-8%
- VWAP 确认: 顺势交易 → 胜率 +3-5%

### 利润奔跑
- 分层止盈: 锁定部分利润 + 保留奔跑仓位
- 动态追踪: 根据波动性调整 → 平均 R-multiple +0.5-1.0R

### 风险控制
- 区间质量评分: 低质量不交易 → 减少无效交易 30-40%
- 自适应止损: 高波动收紧 → 单笔最大亏损 -20%

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 7. 实施优先级

Phase 1 (本周): 核心改进
  ✓ 动态区间扩展
  ✓ 区间质量过滤 (width + volume)
  ✓ 自适应止损 (regime-aware)

Phase 2 (下周): 止盈优化
  ✓ 三层止盈机制
  ✓ 动态追踪止损
  ✓ R-multiple 计算

Phase 3 (第三周): 高级特性
  ✓ VWAP 斜率确认
  ✓ 历史区间宽度对比
  ✓ 成交量异常检测

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
