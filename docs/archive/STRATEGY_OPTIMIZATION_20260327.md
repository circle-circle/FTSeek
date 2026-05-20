# FTSeek-US 策略优化报告

**日期**: 2026-03-27 21:30  
**版本**: v2.0  
**目标**: 提升策略胜率，降低回撤

---

## 优化概览

本次优化包含 3 项关键改进：

| 优化项 | 内容 | 预期效果 |
|--------|------|----------|
| 优化 1 | ATR 倍数调整 (2.5× → 2.0×) | 收紧止损，降低单笔亏损 |
| 优化 2 | 市场状态过滤 (SPY MA200 + VIX) | 避免熊市和恐慌期交易 |
| 优化 3 | 入场信号优化 (技术面 + 信心阈值) | 提高入场质量，减少假信号 |

---

## 优化 1：调整 ATR 倍数（2.5× → 2.0×）

### 修改文件
- `app/risk/exit_engine.py`

### 修改内容

```python
# 之前
TRAIL_ATR_MULT = 2.5
ATR_STOP_MULT = 2.5

# 现在
TRAIL_ATR_MULT = 2.0
ATR_STOP_MULT = 2.0
```

### 影响范围
1. **移动止损间距**: 从 ATR×2.5 收紧至 ATR×2.0
2. **初始止损计算**: 从 ATR×2.5 收紧至 ATR×2.0

### 预期效果
- ✅ 单笔最大亏损降低约 20%
- ✅ 盈利保护更及时
- ⚠️ 可能增加被震荡洗出的概率

---

## 优化 2：添加市场状态过滤

### 新增文件
- `app/analysis/market_state.py`

### 功能说明

#### 1. SPY 200 日均线过滤
- **逻辑**: SPY > MA200 → 上涨趋势，允许交易
- **数据源**: Alpha Vantage TIME_SERIES_DAILY
- **缓存**: 30 分钟

#### 2. VIX 恐慌指数过滤
- **逻辑**: VIX < 25 → 市场平稳，允许交易
- **数据源**: Finnhub Quote API
- **缓存**: 30 分钟

#### 3. 综合判断
```python
def can_trade() -> bool:
    return spy_above_ma200() and vix_below_25()
```

### 集成点
- `app/risk/pre_trade_check.py` - 在信号检查前增加市场状态检查

### 预期效果
- ✅ 避免熊市下跌趋势中开仓
- ✅ 避免市场恐慌期（VIX ≥ 25）交易
- ✅ 降低系统性风险暴露

---

## 优化 3：优化入场信号逻辑

### 3.1 增加技术面确认

**新增文件**: `app/analysis/technical_confirm.py`

**确认指标**:

| 指标 | 条件 | 说明 |
|------|------|------|
| RSI(14) | 50 < RSI < 70 | 避免超买区追高 |
| MACD | MACD > Signal 且 Histogram > 0 | 金叉确认 |
| 成交量 | 当日量 > 20 日均量 | 确认突破有效性 |

**使用方式**:
```python
from app.analysis.technical_confirm import TechnicalConfirm

confirm = TechnicalConfirm()
result = confirm.check("AAPL")
if result.passed:
    # 技术面确认通过
    pass
```

### 3.2 增加基本面评分权重

**修改文件**: `app/analysis/fundamental_filter.py`

**权重调整**:

| 因子 | 之前 | 现在 | 变化 |
|------|------|------|------|
| 技术面 | 70% | 50% | -20% |
| 基本面 | 30% | 40% | +10% |
| 市场状态 | 0% | 10% | +10% |

**代码**:
```python
TECHNICAL_WEIGHT = 0.50
FUNDAMENTAL_WEIGHT = 0.40
MARKET_WEIGHT = 0.10
```

### 3.3 提高信心阈值

**修改文件**: `app/risk/pre_trade_check.py`

```python
# 之前
min_confidence: float = 0.50

# 现在
min_confidence: float = 0.65
```

### 预期效果
- ✅ 入场信号质量提升
- ✅ 减少低信心交易
- ✅ 基本面因子权重提升，更符合中长期投资风格

---

## 文件变更清单

### 新增文件
1. `app/analysis/market_state.py` - 市场状态过滤模块
2. `app/analysis/technical_confirm.py` - 技术面确认模块

### 修改文件
1. `app/risk/exit_engine.py` - ATR 倍数调整
2. `app/risk/pre_trade_check.py` - 市场状态检查 + 信心阈值
3. `app/analysis/fundamental_filter.py` - 评分权重配置

### 备份文件
- `backups/20260327_213000/exit_engine.py`
- `backups/20260327_213000/pre_trade_check.py`

---

## 测试验证

### 语法检查
```bash
✓ exit_engine.py 语法检查通过
✓ pre_trade_check.py 语法检查通过
✓ market_state.py 语法检查通过
✓ technical_confirm.py 语法检查通过
✓ fundamental_filter.py 语法检查通过
```

### 模块导入测试
```bash
✓ exit_engine 导入成功
✓ pre_trade_check 导入成功
✓ market_state 导入成功
✓ technical_confirm 导入成功
✓ fundamental_filter 导入成功 (权重：技术面=0.5, 基本面=0.4, 市场=0.1)
```

---

## 配置要求

### API 密钥
确保以下环境变量已配置：

```bash
# Alpha Vantage (SPY 数据、技术指标)
export ALPHAVANTAGE_API_KEY="your_key"

# Finnhub (VIX 数据)
export FINNHUB_API_KEY="your_key"
```

### 可选配置
如需调整参数，可修改以下常量：

```python
# market_state.py
VIX_THRESHOLD = 25  # VIX 恐慌阈值

# technical_confirm.py
RSI_MIN = 50
RSI_MAX = 70
VOLUME_RATIO_MIN = 1.0

# fundamental_filter.py
TECHNICAL_WEIGHT = 0.50
FUNDAMENTAL_WEIGHT = 0.40
MARKET_WEIGHT = 0.10

# pre_trade_check.py
min_confidence = 0.65
```

---

## 回测建议

建议在实盘前进行回测验证：

1. **回测周期**: 2024-01-01 至 2026-03-27
2. **对比基准**: 优化前策略 (v1.0)
3. **关键指标**:
   - 胜率 (Win Rate)
   - 最大回撤 (Max Drawdown)
   - 夏普比率 (Sharpe Ratio)
   - 盈亏比 (Profit/Loss Ratio)

---

## 风险提示

1. **过度优化风险**: 参数可能在历史数据上表现良好，但未来未必有效
2. **数据依赖风险**: 市场状态过滤依赖外部 API，需确保数据稳定性
3. **交易频率下降**: 更严格的过滤条件可能导致交易机会减少

---

## 后续计划

- [ ] 运行历史回测，验证优化效果
- [ ] 模拟盘运行 2 周，观察实际表现
- [ ] 根据实盘反馈微调参数
- [ ] 编写技术面确认集成文档

---

**优化完成时间**: 2026-03-27 21:30  
**执行人**: FTSeek-US Subagent  
**状态**: ✅ 已完成代码修改和测试验证
