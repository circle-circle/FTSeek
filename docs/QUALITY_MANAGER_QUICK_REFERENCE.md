# FTSeek 质量管理器 - 快速参考指南

## 核心 API

### 质量评分器
```python
from app.factors import get_quality_scorer

scorer = get_quality_scorer()
quality = scorer.calculate(code, fundamentals)
# fundamentals: {roe_5y_avg, debt_ratio, operating_cashflow, gross_margin, margin_trend, revenue_growth_3y}
```

### 持仓周期管理器
```python
from app.factors import get_holding_period_manager, QualityClass

hp = get_holding_period_manager()
hp.add_position(code, QualityClass.CLASS_A, entry_price)
should_exit, reason = hp.should_exit(code)
```

### 资金水位管理器
```python
from app.factors import get_water_level_manager

wl = get_water_level_manager(initial_capital=1000000)
wl.update_capital(current_capital)
can_trade, reason = wl.can_trade('A')  # 'A'/'B'/'C'/'JUNK'
max_pos = wl.get_max_position('A')
```

### 止盈止损管理器
```python
from app.factors import get_profit_loss_manager, QualityClass

pl = get_profit_loss_manager(water_level_manager=wl)
pl.add_position(code, entry_price, quantity, QualityClass.CLASS_A)
pl.update_price(code, current_price)
signal = pl.check_exit(code, price)  # ExitSignal or None
```

### 估值计算器
```python
from app.factors import get_valuation_calculator

val = get_valuation_calculator()
val.add_pe_history(code, pe_ratio)  # 添加 PE 历史数据
result = val.calculate(code, price, fundamentals)
# result: {dcf_value, graham_value, fair_value, margin_of_safety_pct, signal, notes}
```

---

## 质量等级标准

| 等级 | 分数 | 持仓期限 | 止损 | 止盈 |
|------|------|---------|------|------|
| CLASS_A | 80-100 | 无限制 | 5% | 估值止盈 |
| CLASS_B | 60-79 | 7 天 | 3% | 分批止盈 |
| CLASS_C | 40-59 | 2 天 | 3% | 一次性止盈 |
| JUNK | <40 | 1 天 | 2% | 快速离场 |

---

## 水位等级配置

| 水位 | 资金比率 | 交易次数 | 最大仓位 | 质量要求 |
|------|---------|---------|---------|---------|
| GREEN | 95%+ | 5 | 20% | C+ |
| YELLOW | 85-95% | 3 | 15% | B+ |
| ORANGE | 75-85% | 1 | 10% | A |
| RED | <75% | 0 | 0% | 禁止交易 |

---

## 买入检查流程

```python
def should_buy(code, price, fundamentals):
    # 1. 质量检查
    quality = scorer.calculate(code, fundamentals)
    if quality.class_ == QualityClass.JUNK:
        return False, "垃圾股"
    
    # 2. 水位检查
    can_trade, reason = wl.can_trade(quality.class_.value)
    if not can_trade:
        return False, reason
    
    # 3. 估值检查
    val_result = val.calculate(code, price, fundamentals)
    if val_result.signal.value in ('SELL', 'STRONG_SELL'):
        return False, "估值过高"
    
    return True, f"质量{quality.class_.value}级，估值{val_result.signal.value}"
```

---

## 文件位置

- 模块代码：`app/factors/`
- 配置文件：`config/config.yaml` (第 18 章)
- 集成示例：`scripts/quality_manager_integration_example.py`
- 测试脚本：`scripts/test_quality_integration.py`
- 完整文档：`docs/QUALITY_MANAGER_IMPLEMENTATION_COMPLETE.md`
