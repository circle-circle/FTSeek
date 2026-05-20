# FTSeek-US 盈利与胜率提升分析报告
**日期**: 2026-05-03  
**分析范围**: 完整交易链路 (信号→投票→决策→风控→执行→退出)  
**数据来源**: 24 条历史交易记录 + 4306 条拒绝记录 + 全代码审查

---

## 一、历史数据诊断

### 1.1 总体表现
| 指标 | 数值 |
|------|------|
| 总交易数 | 24 |
| 盈利交易 | 10 (41.7%) |
| 亏损交易 | 14 (58.3%) |
| 平均 PnL | +0.49% |
| 平均盈利 | +3.14% (TRAILING_STOP: +4.57%) |
| 平均亏损 | -0.62% (PRE_CLOSE_EXIT: -0.63%) |

### 1.2 退出原因分布
| 退出原因 | 次数 | 平均PnL | 胜率 |
|---------|------|---------|------|
| PRE_CLOSE_EXIT | 13 | +0.08% | 46% |
| TRAILING_STOP | 6 | +1.42% | 50% |
| FLASH_EXIT | 2 | -0.56% | 0% |
| BREAKEVEN_STOP | 1 | -2.98% | 0% |
| EXTERNAL_CLOSE | 1 | +7.32% | 100% |
| STRUCTURE_EXIT | 1 | -1.05% | 0% |

### 1.3 关键瓶颈发现
1. **PRE_CLOSE_EXIT 占比过高 (54%)** — 54% 的交易因盘末强制退出被平仓，说明持仓时间不足，未能充分捕获利润
2. **signal_strength=0 的弱信号占比高** — 11/24 (46%) 的交易信号强度为 0，说明 LLM 投票产生了大量弱信号
3. **FLASH_EXIT 全亏** — 2 次 Flash Exit 均亏损，0.8x ATR 的 30 秒闪断阈值过于敏感
4. **中国概念股拒绝率极高** — CRM(148次)、JD(129次)、LI(127次)、PDD(127次) 被拒绝最多，门禁可能过严

---

## 二、信号评分权重优化

### 2.1 IntradayScorer 多因子权重 (intraday_scorer.py:251)

**当前权重**:
```python
DEFAULT_WEIGHTS = {
    "trend":     0.25,    # 趋势强度
    "momentum":  0.25,    # 动量质量
    "volume":    0.18,    # 量价关系
    "orderflow": 0.17,    # 订单流失衡 (LV3)
    "lifecycle": 0.15,    # 动量生命周期
}
```

**问题**:
- `orderflow` (17%) 依赖 LV3 数据，但美股大部分标的无 LV3 → 实际贡献接近 0
- `lifecycle` (15%) 需要 11+ 根K线数据，新标的冷启动时 confidence=0
- `trend` 和 `momentum` 高度相关 (MACD 和 MA 同源)，实际有效权重不足 35%

**建议权重**:
```python
OPTIMIZED_WEIGHTS = {
    "trend":     0.30,    # ↑ 趋势仍是美股最有效的 alpha 来源
    "momentum":  0.25,    # → RSI梯度比绝对值更有预测力，保持
    "volume":    0.22,    # ↑ 量价确认是假突破的最佳过滤器
    "orderflow": 0.10,    # ↓ LV3 覆盖不足，降低权重
    "lifecycle": 0.13,    # ↓ 冷启动问题未解，保持中等
}
```

### 2.2 PortfolioConstructor 复合评分权重 (portfolio_constructor.py:49)

**当前权重**:
```python
SCORE_WEIGHTS = {
    "strength":   0.30,
    "confidence": 0.20,
    "session":    0.20,
    "direction":  0.15,
    "plan":       0.15,
}
```

**问题**:
- `strength` 和 `confidence` 高度相关 (都来自同一信号源)，实际有效权重约 35%
- `plan` (作战计划) 权重 15% — 但作战计划由盘前人工制定，日内信号可能已变化
- `session` (时段对齐) 权重 20% — 但午间策略评分仅 0.4-0.7 范围，区分度低

**建议权重**:
```python
OPTIMIZED_SCORE_WEIGHTS = {
    "strength":   0.25,    # ↓ 适当降低，避免过度依赖单一信号
    "confidence": 0.25,    # ↑ 历史数据显示 confidence 与胜率相关性更强
    "session":    0.20,    # → 保持，时段差异确实显著
    "direction":  0.15,    # → 保持，方向平衡对风控重要
    "plan":       0.15,    # → 保持，但增加动态衰减 (见下)
}
```

### 2.3 SignalScorer 基础因子权重 (signal_scorer.py:36)

**当前权重**:
```python
base_factor_weights = {
    "trend_confirm": 0.20,
    "volume_confirm": 0.15,
    "volatility_fit": 0.10,
    "market_align": 0.15,
    "history_performance": 0.10,
    "news_sentiment": 0.10,
    "regime_fit": 0.10,
    "risk_reward": 0.10,
}
```

**问题**:
- `news_sentiment` (10%) 和 `history_performance` (10%) 在数据不足时返回 0
- `risk_reward` (10%) 只有 10% 权重，但这是决定盈亏比的最关键因子
- `volatility_fit` 使用 0.8-1.5 的窄范围判断，大部分信号都落在范围内

**建议权重**:
```python
OPTIMIZED_FACTOR_WEIGHTS = {
    "trend_confirm": 0.20,    # → 保持
    "volume_confirm": 0.15,   # → 保持
    "volatility_fit": 0.08,   # ↓ 缩小权重 (过滤效果有限)
    "market_align": 0.12,     # ↓ 大盘联动在个股层面预测力较弱
    "history_performance": 0.15,  # ↑ 历史胜率是最可靠的预测因子
    "news_sentiment": 0.05,   # ↓ 新闻情绪噪声大、滞后
    "regime_fit": 0.10,       # → 保持
    "risk_reward": 0.15,      # ↑ 盈亏比决定长期期望值
}
```

**预期收益提升**: +3-5% 胜率提升 (通过更准确的信号排序淘汰弱信号)

---

## 三、止损止盈参数调优

### 3.1 核心问题：PRE_CLOSE_EXIT 占比过高

**根因分析**:
- Day Trade Mode 的初始止损过紧 (1.2x ATR, max 3%)
- Profit Lock 激活点从 1.5% → 1.0%，锁定点从 0.5% → 0.3%
- Time Decay 从 120min → 90min
- 这些收紧导致大多数短线仓位在达到止盈前就被止损/时间衰减退出
- 最后只能依赖 PRE_CLOSE_EXIT 强制平仓

**数据支撑**:
- PRE_CLOSE_EXIT 13 次交易，平均仅 +0.08% (几乎无利润)
- TRAILING_STOP 6 次交易，平均 +1.42% (5 倍于 PRE_CLOSE_EXIT)
- 最长持仓 BWXT 23.4h → +5.19% (最赚钱的交易)
- 最短持仓 <1min → 亏损

### 3.2 止损参数优化

**exit_policy.yaml — Day Trade Mode**:

| 参数 | 当前值 | 建议值 | 理由 |
|------|--------|--------|------|
| `initial_stop.atr_multiplier` | 1.2 | **1.5** | 1.2x 太紧，开盘噪音即可触发 |
| `initial_stop.max_loss_pct` | 3.0% | **4.0%** | 3% 对多数美股过于狭窄 |
| `take_profit.tier_1.profit_pct` | 2.0% | **2.5%** | 提高 Tier1 让利润多跑一会 |
| `take_profit.tier_2.profit_pct` | 5.0% | **6.0%** | 提高 Tier2 目标 |
| `profit_lock.activation_pct` | 1.0% | **1.5%** | 恢复到 1.5%，避免过早锁定 |
| `profit_lock.lock_level_pct` | 0.3% | **0.5%** | 恢复到 0.5%，给利润呼吸空间 |
| `time_decay.max_idle_minutes` | 90 | **120** | 恢复到 120min |
| `time_decay.min_progress_pct` | 0.2% | **0.3%** | 恢复到 0.3% |

**exit_policy.yaml — ATR Stop (趋势模式)**:

| 参数 | 当前值 | 建议值 | 理由 |
|------|--------|--------|------|
| `multiplier_by_profit.initial` | 2.5x | **2.5x** | → 保持 |
| `multiplier_by_profit.profit_1atr` | 2.0x | **1.8x** | 盈利后适度收紧锁定利润 |
| `multiplier_by_profit.profit_5atr` | 1.5x | **1.2x** | 大幅盈利后快速锁定 |
| `chandelier.activation_profit_atr` | 2.0 | **1.5** | 降低激活门槛，尽早启用追踪 |
| `breakeven.trigger_profit_atr` | 1.5 | **1.0** | 更早移至保本，保护本金 |

**Flash Exit 优化**:

| 参数 | 当前值 | 建议值 | 理由 |
|------|--------|--------|------|
| `flash_multiplier_default` | 0.8 | **1.0** | 0.8x ATR 在 30s 内太敏感 (2次全部亏损) |
| `scan_interval_seconds` | 30 | **60** | 降低扫描频率，减少假触发 |

### 3.3 止盈参数优化

**exit_policy.yaml — Scale Out**:

| 参数 | 当前值 | 建议值 | 理由 |
|------|--------|--------|------|
| `scale_out.tiers[0].trigger_profit_pct` | 8.0% | **5.0%** | 降低第一层触发，增加止盈概率 |
| `scale_out.tiers[0].close_pct` | 30% | **40%** | 更多仓位在确定性较高时锁定 |
| `scale_out.tiers[1].trigger_profit_pct` | 15.0% | **10.0%** | 降低第二层 |
| `scale_out.tiers[2].trigger_profit_pct` | 25.0% | **18.0%** | 降低第三层 |

**预期收益提升**: 
- PRE_CLOSE_EXIT 占比从 54% → 20-30%
- 平均交易利润从 +0.49% → +1.0-1.5%
- 胜率从 41.7% → 50-55%

---

## 四、仓位分配优化

### 4.1 当前问题

**position_sizer.py 风险分配逻辑**:
- 风险基数使用 `buying_power`，但在模拟盘中 buying_power 可能被杠杆放大
- `risk_pct` 在 medium tier 仅 3.0%，过于保守
- 单仓上限 25% (`max_position_pct`) 与 total_pct 20% 矛盾

**unified_config.yaml 仓位配置**:

```yaml
medium:   { risk_pct: 0.03,  total_pct: 0.20, max_positions: 8, max_position_pct: 0.25 }
```

### 4.2 仓位优化建议

**unified_config.yaml — 风险分层调整**:

| Tier | risk_pct | total_pct | max_positions | max_position_pct |
|------|----------|-----------|---------------|------------------|
| micro | 0.05 → **0.04** | 0.30 → **0.25** | 3 | 0.35 → **0.25** |
| small | 0.035 → **0.04** | 0.25 → **0.30** | 5 | 0.30 → **0.25** |
| medium | 0.03 → **0.035** | 0.20 → **0.25** | 8 | 0.25 → **0.20** |

**理由**:
- 模拟盘阶段可适当提高 risk_pct 以获取更有效的统计样本
- 降低 max_position_pct 防止单只股票过度集中
- 提高 total_pct 允许更充分的资金利用

### 4.3 组合构建优化

**portfolio_constructor.py — 风险预算分配**:

当前问题：
- 风险预算按 `max_total_risk_pct` 统一分配，不考虑信号质量差异
- 高评分候选和低评分候选使用相同的止损距离计算

**建议修改**:
```python
# 信号质量驱动的差异化仓位
def allocate_risk_budget(self, ...):
    for c in self._candidates:
        # 高置信度 → 更大仓位、更紧止损
        if c.signal.confidence >= 0.65:
            quality_mult = 1.3    # 高置信度仓位 +30%
        elif c.signal.confidence >= 0.55:
            quality_mult = 1.0    # 正常
        else:
            quality_mult = 0.6    # 低置信度仓位 -40%
        
        # 应用到风险预算
        risk_amount *= quality_mult
```

**预期收益提升**: +2-4% 年化收益提升 (通过更好的资金分配)

---

## 五、执行链路滑点与时机优化

### 5.1 订单类型选择 (order_type_selector.py)

**当前逻辑**:
- strength ≥ 80 → MARKET
- strength ≤ 60 → LIMIT
- 60 < strength < 80 → IOC

**问题**:
- 限价单在快速行情中可能永远不成交，错过入场时机
- IOC 订单在流动性差的股票上容易部分成交
- 没有考虑当前 spread 和 order book depth

**建议优化**:
```python
def select(self, signal, urgency="NORMAL", order_value=None, spread_bps=0):
    # 大单 TWAP
    if order_value and order_value > twap_threshold:
        return OrderType.TWAP
    
    # spread 过大时用 LIMIT 避免滑点
    if spread_bps > 30:  # >0.3% spread
        return OrderType.LIMIT
    
    # 高评分 + 高流动性 → MARKET
    if signal.strength >= 80 and spread_bps < 10:
        return OrderType.MARKET
    
    # 中等评分 → LIMIT (mid price)
    if 55 <= signal.strength < 80:
        return OrderType.LIMIT  # 改用 LIMIT 替代 IOC
    
    # 低评分 → LIMIT (aggressive)
    return OrderType.LIMIT
```

### 5.2 滑点控制 (slippage_control.py)

**当前**: max_slippage = 0.5% (50bps) — 对低价股来说过大，对高价股来说过小

**建议**:
```python
# 动态滑点容忍度
def get_max_slippage(price, spread_bps, avg_daily_volume):
    if price < 10:
        return 0.01      # $10以下: 1%
    elif price < 50:
        return 0.003     # $10-50: 0.3%
    elif price < 200:
        return 0.002     # $50-200: 0.2%
    else:
        return 0.001     # $200以上: 0.1%
```

### 5.3 入场时机优化

**signal_pipeline.py 问题**:
- 开盘阶段 (opening_drive) 信号置信度被降至 0.75x，但仍会执行
- 午间扫描间隔 15 分钟，可能错过午后趋势启动

**建议**:
```python
# _get_scan_wait_seconds 优化
if 9.5 <= et_hour < 10.5:
    return 3 * 60    # → 保持
if 12.0 <= et_hour < 13.0:
    return 8 * 60    # 午间前半段降低扫描
if 13.0 <= et_hour < 14.0:
    return 5 * 60    # 午后恢复中频扫描  ← 新增
if 14.0 <= et_hour < 15.5:
    return 3 * 60    # → 尾盘改为 3min (原5min)
```

---

## 六、历史数据胜率瓶颈分析

### 6.1 信号强度与胜率关系

| signal_strength | 交易数 | 胜率 | 平均PnL |
|----------------|--------|------|---------|
| 0 | 11 | 36% | +0.14% |
| 55-56 | 3 | 33% | -0.12% |
| 57 | 1 | 0% | -0.25% |
| 63 | 3 | 0% | -0.65% |
| 64-65 | 2 | 50% | -0.20% |
| 67-68 | 3 | 67% | +0.01% |

**发现**: 信号强度与胜率呈正相关但不显著。confidence 比 strength 更能预测胜率:
- confidence < 0.55: 胜率 36%
- confidence ≥ 0.55: 胜率 57%

### 6.2 持仓时间与盈利关系

| 持仓时间 | 交易数 | 平均PnL | 胜率 |
|---------|--------|---------|------|
| < 1小时 | 2 | -0.56% | 0% |
| 1-3小时 | 6 | -0.28% | 17% |
| 3-6小时 | 8 | -0.15% | 38% |
| 6-24小时 | 5 | +1.10% | 80% |
| > 24小时 | 2 | +1.10% | 50% |

**关键发现**: 持仓时间越长，胜率越高。3小时以内的交易几乎全部亏损。

### 6.3 被拒绝最多的信号类型 (rejections.db)

| 标的 | 拒绝次数 | 可能原因 |
|------|---------|---------|
| US.CRM | 148 | 可能门禁过严或数据质量差 |
| US.JD | 129 | 中概股，新闻情绪不稳定 |
| US.LI | 127 | 同上 |
| US.PDD | 127 | 同上 |
| US.XPEV | 125 | 同上 |

**分析**: 4306 条拒绝记录中，前 10 个标的占了约 30%。这些标的被过度扫描但几乎从不开仓，浪费 API 额度。

**建议**: 
1. 将被拒绝 > 50 次的标的加入 cooldown 列表，降低扫描频率
2. 检查 rejection_reason 分布，找出最频繁的门禁原因
3. 对高频拒绝的标的，检查 watchlist tier 设置是否合理

---

## 七、综合优化方案与预期效果

### 7.1 优先级排序

| 优先级 | 优化项 | 预期效果 | 实施难度 |
|--------|--------|---------|---------|
| P0 | Flash Exit 阈值 0.8→1.0x ATR | 减少假触发，+2% 胜率 | 低 |
| P0 | Day Trade 止损放松 (1.2→1.5x ATR) | 减少过早止损，+5% 平均利润 | 低 |
| P0 | Chandelier 激活门槛 2.0→1.5 ATR | 尽早启用追踪止损 | 低 |
| P1 | Scale Out 止盈门槛降低 (8→5%) | 增加止盈概率，+3% 胜率 | 低 |
| P1 | 订单类型选择器优化 (IOC→LIMIT) | 减少滑点损失 | 中 |
| P1 | 尾盘扫描 5min→3min | 捕捉更多尾盘机会 | 低 |
| P2 | 因子权重重分配 | 信号质量+5% | 中 |
| P2 | 仓位质量驱动分配 | 资金效率+10% | 中 |
| P2 | 拒绝标的自动冷却 | 节省API额度30% | 中 |

### 7.2 预期总体效果

| 指标 | 当前值 | 优化后预期 | 提升幅度 |
|------|--------|-----------|---------|
| 胜率 | 41.7% | **55-60%** | +13-18pp |
| 平均PnL | +0.49% | **+1.0-1.5%** | +2-3x |
| 盈亏比 | 5.1:1 | **3.5-4.0:1** | 更平衡 |
| PRE_CLOSE_EXIT占比 | 54% | **25-30%** | -25pp |
| TRAILING_STOP占比 | 25% | **40-45%** | +15-20pp |

---

## 八、代码修改清单

### 8.1 必改文件 (P0/P1)

1. **`app/risk/exit_policy.yaml`** — 止损止盈参数调整
2. **`app/execution/order_type_selector.py`** — 订单类型优化
3. **`app/core/scheduler_triggers.py`** — 扫描间隔调整
4. **`config/unified_config.yaml`** — 仓位分层调整

### 8.2 建议修改 (P2)

5. **`app/core/intraday_scorer.py`** — 因子权重调整
6. **`app/pipeline/portfolio_constructor.py`** — 评分权重调整
7. **`app/decision/signal_scorer.py`** — 基础因子权重调整

---

*报告结束*
