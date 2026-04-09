# FTSeek 巴菲特理念实现报告

**版本**: v1.0
**日期**: 2026-04-06
**状态**: 实现完成

---

## 一、实现概述

本次实现将巴菲特的价值投资理念融入 FTSeek 量化交易系统，完成了以下核心模块：

1. **质量评分器 (QualityScorer)** - 基本面因子评分
2. **持仓周期管理器 (HoldingPeriodManager)** - 质量决定的持仓期限
3. **资金水位管理器 (WaterLevelManager)** - 动态风险调整
4. **止盈止损管理器 (ProfitLossManager)** - "落袋为安"理念融合
5. **估值计算器 (ValuationCalculator)** - 安全边际计算

---

## 二、模块实现详情

### 2.1 质量评分器 (QualityScorer)

**文件**: `app/factors/quality_scorer.py`

**功能**:
- 计算 Quality Score (0-100 分)
- 企业分级 (A/B/C/JUNK)
- 质量变化追踪

**评分维度**:
| 维度 | 权重 | 说明 |
|------|------|------|
| ROE | 30% | 5 年平均 ROE (巴菲特核心指标) |
| 负债率 | 20% | 资产负债率 |
| 现金流 | 20% | 经营现金流 |
| 毛利率 | 15% | 毛利率及趋势 |
| 增长 | 15% | 营收增长率 |

**等级划分**:
- CLASS_A (80+): 优质企业，长期持有
- CLASS_B (60-79): 良好企业，中期持有
- CLASS_C (40-59): 一般企业，短期持有
- JUNK (<40): 垃圾企业，过滤

**使用示例**:
```python
from app.factors import get_quality_scorer, QualityClass

scorer = get_quality_scorer()

fundamentals = {
    'roe_5y_avg': 25,        # ROE 25%
    'debt_ratio': 0.25,      # 负债率 25%
    'operating_cashflow': 1e9,  # 正现金流
    'gross_margin': 0.60,    # 毛利率 60%
    'margin_trend': 0.05,    # 毛利率提升
    'revenue_growth_3y': 0.20,  # 20% 增长
}

result = scorer.calculate('HK.00700', fundamentals)
print(f"质量评分：{result.score} ({result.class_.value})")
```

---

### 2.2 持仓周期管理器 (HoldingPeriodManager)

**文件**: `app/factors/holding_period_manager.py`

**功能**:
- 根据质量等级设定持仓期限
- 质量复查机制
- 恶化时离场决策

**持仓期限配置**:
| 质量等级 | 持仓期限 | 复查间隔 | 恶化离场 |
|----------|----------|----------|----------|
| CLASS_A | 无限制 | 24 小时 | 是 |
| CLASS_B | 168 小时 (7 天) | 12 小时 | 是 |
| CLASS_C | 48 小时 | 6 小时 | 是 |
| JUNK | 24 小时 | 4 小时 | 是 |

**使用示例**:
```python
from app.factors import get_holding_period_manager, QualityClass

manager = get_holding_period_manager()

# 添加持仓
manager.add_position('HK.00700', QualityClass.CLASS_A, 500.0)

# 检查是否应该离场
should_exit, reason = manager.should_exit('HK.00700')
if should_exit:
    print(f"应该离场：{reason}")

# 获取持仓信息
info = manager.get_position_info('HK.00700')
print(f"持仓信息：{info}")
```

---

### 2.3 资金水位管理器 (WaterLevelManager)

**文件**: `app/factors/water_level_manager.py`

**功能**:
- 根据资金水位动态调整风险
- 交易频率限制
- 风险预算管理
- 质量要求联动

**水位等级**:
| 等级 | 范围 | 状态 | 最大仓位 | 日交易次数 | 最低质量 |
|------|------|------|----------|------------|----------|
| GREEN | 95%+ | 安全区 | 20% | 5 | C |
| YELLOW | 85-95% | 警戒区 | 15% | 3 | B |
| ORANGE | 75-85% | 危险区 | 10% | 1 | A |
| RED | <75% | 熔断区 | 0% | 0 | - |

**使用示例**:
```python
from app.factors import get_water_level_manager, WaterLevel

manager = get_water_level_manager(initial_capital=1000000)

# 更新资金状态
manager.update_capital(current_capital=950000)

# 检查是否可以交易
can_trade, reason = manager.can_trade(quality_class='B')
if can_trade:
    print("允许交易")
else:
    print(f"禁止交易：{reason}")

# 获取最大仓位
max_position = manager.get_max_position(quality_class='A')
print(f"最大仓位：{max_position:.0%}")

# 获取水位状态
state = manager.get_state()
print(f"当前水位：{state.current_ratio:.0%}, 等级：{state.current_level.value}")
```

---

### 2.4 止盈止损管理器 (ProfitLossManager)

**文件**: `app/factors/profit_loss_manager.py`

**功能**:
- 三层止盈架构：分批止盈 + 趋势止盈 + 估值止盈
- 动态止损：初始止损 → 保本止损 → 追踪止盈
- 质量等级差异化止盈
- 水位联动止盈

**三层止盈架构**:
```
第一层：分批止盈 (落袋为安)
  - 盈利 5%: 止盈 25% 仓位
  - 盈利 10%: 止盈 25% 仓位
  - 目的：锁定部分利润，降低持仓成本

第二层：趋势止盈 (让利润奔跑)
  - SuperTrend 转向：止盈 50%
  - MACD 死叉：止盈 50%
  - 目的：跟随趋势，赚取主升浪

第三层：估值止盈 (高估才卖)
  - PE 分位点 > 80%: 清仓
  - 质量恶化：清仓
  - 目的：优质股长期持有，享受复利
```

**动态止损演进**:
```
入场时:
  止损价 = 入场价 × (1 - 3%)

盈利 3% 后:
  止损价 = 入场价 × (1 + 1%)  # 保本止损

盈利 5% 后:
  止损价 = 最高价 × (1 - 3%)  # 追踪止盈

盈利 10%/20%/30% 后:
  止损价逐级上移，锁定利润
```

**使用示例**:
```python
from app.factors import get_profit_loss_manager, QualityClass

manager = get_profit_loss_manager()

# 添加持仓
manager.add_position('HK.00700', 500.0, 100, QualityClass.CLASS_A)

# 更新价格
manager.update_price('HK.00700', 520.0)  # 上涨 4%

# 检查止盈止损
exit_signal = manager.check_exit('HK.00700', 520.0)
if exit_signal:
    print(f"离场信号：{exit_signal.exit_type}, {exit_signal.reason}")

# 检查分批止盈
partial_signal = manager.check_partial_exit('HK.00700')
if partial_signal:
    print(f"分批止盈：{partial_signal.ratio:.0%} 仓位")

# 处理趋势信号
trend_signal = manager.on_trend_signal('HK.00700', 'supertrend_turn')
if trend_signal:
    print(f"趋势止盈：{trend_signal.ratio:.0%} 仓位")
```

---

### 2.5 估值计算器 (ValuationCalculator)

**文件**: `app/factors/valuation.py`

**功能**:
- 计算内在价值 (简化 DCF 模型)
- 计算 PE 历史分位点
- 计算安全边际
- 生成估值信号

**估值方法**:
1. **DCF 折现现金流模型**
   - 公式：内在价值 = 未来 5 年收益总和 + 终值
   - 折现率：10%
   - 终值 PE: 15

2. **格雷厄姆公式**
   - 公式：V = EPS × (8.5 + 2g)
   - 8.5: 零增长 PE 基准
   - g: 预期增长率

3. **PE 历史分位点**
   - 表示当前 PE 高于历史多少比例
   - >80%: 高估
   - <20%: 低估

**估值信号**:
| 信号 | 安全边际 | 说明 |
|------|----------|------|
| STRONG_BUY | >50% | 强烈买入 |
| BUY | 30-50% | 买入 |
| HOLD | -10% 到 30% | 持有 |
| SELL | 高估 10-30% | 卖出 |
| STRONG_SELL | >30% | 强烈卖出 |

**使用示例**:
```python
from app.factors import get_valuation_calculator, ValuationSignal

calculator = get_valuation_calculator()

# 添加 PE 历史数据
calculator.add_pe_history('HK.00700', 25.0)
calculator.add_pe_history('HK.00700', 28.0)
calculator.add_pe_history('HK.00700', 30.0)

# 计算估值
fundamentals = {
    'eps': 20.0,
    'pe_ttm': 25.0,
    'growth_rate': 0.15,
    'book_value': 100.0,
    'roe': 0.25,
}

result = calculator.calculate('HK.00700', 500.0, fundamentals)
print(f"内在价值：{result.intrinsic_value:.2f}")
print(f"安全边际：{result.margin_of_safety_pct:.0%}")
print(f"估值信号：{result.signal.value}")
```

---

## 三、配置文件

**文件**: `config/config.yaml`

新增配置章节：

### 3.1 质量评分器配置
```yaml
quality_scorer:
  enabled: true
  weights:
    roe: 0.30
    debt: 0.20
    cashflow: 0.20
    margin: 0.15
    growth: 0.15
  class_thresholds:
    class_a: 80
    class_b: 60
    class_c: 40
  position_limits:
    class_a: 0.30
    class_b: 0.20
    class_c: 0.10
    junk: 0.00
```

### 3.2 持仓周期配置
```yaml
holding_period:
  enabled: true
  max_holding_hours:
    class_a: null
    class_b: 168
    class_c: 48
    junk: 24
  review_intervals:
    class_a: 24
    class_b: 12
    class_c: 6
    junk: 4
```

### 3.3 资金水位配置
```yaml
water_level:
  enabled: true
  initial_capital: 1000000
  levels:
    green:
      min_ratio: 0.95
      max_single_position: 0.20
      max_daily_trades: 5
    yellow:
      min_ratio: 0.85
      max_single_position: 0.15
      max_daily_trades: 3
    orange:
      min_ratio: 0.75
      max_single_position: 0.10
      max_daily_trades: 1
    red:
      min_ratio: 0.0
      max_single_position: 0.00
      max_daily_trades: 0
      action: LIQUIDATE
```

### 3.4 止盈止损配置
```yaml
profit_loss:
  enabled: true
  partial_take_profit:
    level_1:
      trigger: 0.05
      ratio: 0.25
    level_2:
      trigger: 0.10
      ratio: 0.25
    trend_exit:
      enabled: true
      ratio: 0.50
  dynamic_stop_loss:
    initial_stop:
      percent: 0.03
    breakeven:
      trigger_profit: 0.03
      stop_level: 0.01
    trailing:
      trigger_profit: 0.05
      trail_percent: 0.03
```

---

## 四、集成方案

### 4.1 买入流程集成

```python
# app/data_subscriber.py

from app.factors import (
    get_quality_scorer,
    get_water_level_manager,
    get_holding_period_manager,
    get_profit_loss_manager,
    QualityClass,
)

async def _on_consensus(self, code: str, result: ConsensusResult):
    """达成买入共识后的处理流程 (改进版)"""
    
    # 1. 获取质量评分
    quality = quality_scorer.calculate(code, fundamentals)
    
    # 2. 检查是否可买入 (过滤垃圾股)
    if quality.class_ == QualityClass.JUNK:
        logger.warning(f"质量评级为 JUNK，拒绝买入：{code}")
        return
    
    # 3. 检查水位是否允许交易
    can_trade, reason = water_level.can_trade(quality.class_.value)
    if not can_trade:
        logger.warning(f"水位限制，拒绝买入：{reason}")
        return
    
    # 4. 计算合理仓位
    max_position = water_level.get_max_position(quality.class_.value)
    size = position_sizer.calculate(..., max_position_percent=max_position)
    
    # 5. 执行买入
    executor.buy(code, size.quantity)
    
    # 6. 添加持仓记录
    holding_period.add_position(code, quality.class_, entry_price)
    profit_loss.add_position(code, entry_price, size.quantity, quality.class_)
    
    # 7. 启动质量复查定时器
    self._start_quality_review_timer(code)
```

### 4.2 持仓监控集成

```python
async def _position_monitor_loop(self):
    """持仓监控循环 (每分钟运行一次)"""
    
    while self._running:
        await asyncio.sleep(60)
        
        for code in list(self._positions.keys()):
            # 1. 更新价格
            current_price = self._get_current_price(code)
            profit_loss.update_price(code, current_price)
            
            # 2. 检查止盈止损
            exit_signal = profit_loss.check_exit(code, current_price)
            if exit_signal:
                await self._execute_exit(code, exit_signal)
                continue
            
            # 3. 检查分批止盈
            partial_signal = profit_loss.check_partial_exit(code)
            if partial_signal:
                await self._execute_partial_exit(code, partial_signal)
            
            # 4. 检查持仓时间
            should_exit, reason = holding_period.should_exit(code)
            if should_exit:
                await self._exit_position(code, reason=f"时间到期：{reason}")
                continue
            
            # 5. 检查质量复查
            if holding_period.needs_review(code):
                fundamentals = await self._fetch_fundamentals(code)
                new_quality = quality_scorer.calculate(code, fundamentals)
                deteriorated, _ = holding_period.update_quality(
                    code, new_quality.class_, new_quality
                )
                if deteriorated:
                    await self._exit_position(code, reason="基本面恶化")
```

---

## 五、测试计划

### 5.1 单元测试

```python
# tests/factors/test_quality_scorer.py

def test_quality_scorer_a_class():
    """测试优质股评分"""
    scorer = get_quality_scorer()
    fundamentals = {
        'roe_5y_avg': 25,
        'debt_ratio': 0.25,
        'operating_cashflow': 1e9,
        'gross_margin': 0.60,
        'margin_trend': 0.05,
        'revenue_growth_3y': 0.20,
    }
    score = scorer.calculate('HK.00700', fundamentals)
    assert score.score >= 80
    assert score.class_ == QualityClass.CLASS_A

# tests/factors/test_water_level.py

def test_water_level_yellow():
    """测试警戒水位"""
    manager = get_water_level_manager(initial_capital=1000000)
    manager.update_capital(current_capital=900000)  # 90% 水位
    
    assert manager.get_current_level() == WaterLevel.YELLOW
    
    can_trade, _ = manager.can_trade('B')
    assert can_trade is True
    
    can_trade, _ = manager.can_trade('C')
    assert can_trade is False  # YELLOW 水位不允许买入 C 级

# tests/factors/test_profit_loss.py

def test_partial_take_profit():
    """测试分批止盈"""
    manager = get_profit_loss_manager()
    manager.add_position('HK.00700', 100.0, 100, QualityClass.CLASS_B)
    
    # 价格上涨 5%
    manager.update_price('HK.00700', 105.0)
    signal = manager.check_partial_exit('HK.00700')
    assert signal is not None
    assert signal.ratio == 0.25
    
    # 价格上涨 10%
    manager.update_price('HK.00700', 110.0)
    signal = manager.check_partial_exit('HK.00700')
    assert signal is not None
    assert signal.ratio == 0.25
```

### 5.2 回测验证

```python
# backtest_quality_strategy.py

"""
回测质量分级策略 vs 原始策略

回测期间：2024-01-01 至 2025-12-31
标的：港股 10 只成分股
"""

原始策略:
  - 固定 48 小时离场
  - 固定 10% 止盈
  - 固定 3% 止损

改进策略:
  - Class A: 无时间限制，估值止盈
  - Class B: 7 天，15% 止盈
  - Class C: 48 小时，10% 止盈

预期结果:
  - 胜率提升：+5-10%
  - 盈亏比提升：+20-30%
  - 最大回撤降低：-5-10%
  - 年化收益提升：+10-20%
```

---

## 六、API 参考

### 6.1 质量评分 API

```python
# 获取单例
scorer = get_quality_scorer(config=None)

# 计算评分
result = scorer.calculate(code, fundamentals)

# 获取缓存
cached = scorer.get_cached(code)

# 获取历史
history = scorer.get_history(code)

# 判断恶化
is_deteriorated = scorer.is_deteriorated(code, old_score, new_score)

# 重置 (测试用)
reset_quality_scorer()
```

### 6.2 持仓周期 API

```python
# 获取单例
manager = get_holding_period_manager(config=None)

# 添加持仓
manager.add_position(code, quality_class, entry_price)

# 更新价格
manager.update_price(code, price)

# 更新质量
deteriorated, reason = manager.update_quality(code, new_quality)

# 检查离场
should_exit, reason = manager.should_exit(code)

# 检查复查
needs_review = manager.needs_review(code)

# 获取信息
info = manager.get_position_info(code)

# 获取所有持仓
positions = manager.get_all_positions()

# 获取到期预警
alerts = manager.get_expiry_alerts()
```

### 6.3 资金水位 API

```python
# 获取单例
manager = get_water_level_manager(initial_capital, config=None)

# 更新资金
manager.update_capital(current_capital, today_trades, today_pnl, today_loss)

# 检查交易
can_trade, reason = manager.can_trade(quality_class)

# 检查买入质量
can_buy, reason = manager.can_buy_quality(quality_class)

# 获取最大仓位
max_position = manager.get_max_position(quality_class)
max_total = manager.get_max_total_position()

# 获取风险
risk_per_trade = manager.get_risk_per_trade()
max_daily_loss = manager.get_max_daily_loss()
risk_budget = manager.get_remaining_risk_budget()

# 获取状态
state = manager.get_state()
state_dict = state.to_dict()

# 记录交易
manager.record_trade(pnl, loss)
```

### 6.4 止盈止损 API

```python
# 获取单例
manager = get_profit_loss_manager(water_level_manager, config)

# 添加持仓
manager.add_position(code, entry_price, quantity, quality_class)

# 更新价格
manager.update_price(code, price)

# 检查离场
exit_signal = manager.check_exit(code, current_price)

# 检查分批止盈
partial_signal = manager.check_partial_exit(code)

# 趋势信号
trend_signal = manager.on_trend_signal(code, signal_type)

# 估值信号
valuation_signal = manager.on_valuation_signal(code, pe_percentile)

# 质量恶化
quality_signal = manager.on_quality_deterioration(code)

# 减少持仓
manager.reduce_position(code, sold_quantity)

# 获取信息
info = manager.get_position_info(code)
positions = manager.get_all_positions()
```

### 6.5 估值 API

```python
# 获取单例
calculator = get_valuation_calculator(config)

# 添加 PE 历史
calculator.add_pe_history(code, pe)

# 计算估值
result = calculator.calculate(code, current_price, fundamentals)

# 获取合理区间
lower, upper = calculator.get_fair_value_range(code, intrinsic_value)

# 重置 (测试用)
reset_valuation_calculator()
```

---

## 七、总结

### 7.1 实现成果

| 模块 | 状态 | 文件行数 | 功能 |
|------|------|----------|------|
| QualityScorer | ✅ 完成 | ~300 行 | 质量评分、分级 |
| HoldingPeriodManager | ✅ 完成 | ~250 行 | 持仓期限管理 |
| WaterLevelManager | ✅ 完成 | ~350 行 | 资金水位联动 |
| ProfitLossManager | ✅ 完成 | ~400 行 | 止盈止损融合 |
| ValuationCalculator | ✅ 完成 | ~350 行 | 估值计算 |

### 7.2 巴菲特理念体现

| 理念 | 实现方式 |
|------|----------|
| 选好企业 | QualityScorer 评分筛选 |
| 长期持有 | Class_A 无时间限制 |
| 安全边际 | ValuationCalculator 计算 |
| 让利润奔跑 | 三层止盈架构 |
| 快速止损 | 动态止损机制 |
| 风险第一 | WaterLevelManager 动态风控 |

### 7.3 预期效果

- **胜率提升**: +5-10% (过滤垃圾股)
- **盈亏比提升**: +20-30% (让利润奔跑)
- **回撤降低**: -5-10% (动态风控)
- **年化收益**: +10-20% (优质股长期持有)

---

## 八、后续工作

1. **基本面数据接入** - 需要接入港股/美股基本面数据 API
2. **PE 历史数据收集** - 收集至少 3 年 PE 历史数据
3. **集成测试** - 与现有交易流程集成测试
4. **回测验证** - 多周期回测验证策略有效性
5. **模拟盘运行** - 模拟盘验证后再实盘

---

**文档结束**
