# FTSeek 港股系统框架分析报告

> 精英组深度分析 | 生成时间: 2026-03-28

---

## 一、项目概览

### 代码规模

| 维度 | 数值 |
|------|------|
| 总代码行数 | ~40,000 行 |
| Python模块数 | 50+ 个 |
| 核心目录 | app/ (28个子目录) |
| 配置文件 | config/ (YAML/JSON) |

### 架构设计

```
/opt/ftseek/FTSeek/
├── app/
│   ├── strategies/        # 策略模块 (fusion_v2, ma_cross, rsi_rebound)
│   ├── risk/              # 风控模块 (circuit_breaker, trend_filter, risk_manager)
│   ├── trader/            # 交易执行 (executor, position_manager, order_tracker)
│   ├── signal_engine/     # 信号引擎 (fusion_engine)
│   ├── data/              # 数据模块 (capital_flow, stock_screener)
│   ├── ai/                # AI模块 (signal_analyzer)
│   ├── v4/                # V4版本 (paper_trading)
│   ├── core/              # 核心模块 (config_manager, state_manager)
│   ├── reporters/         # 报告模块 (daily_stats, weekly_report)
│   └── utils/             # 工具模块
├── config/                # 配置文件
│   ├── strategy_params.json
│   ├── risk_config.yaml
│   └── watchlist.json
└── data/                  # 数据存储
    ├── ftseek.db
    └── reviews/
```

---

## 二、核心模块分析

### 2.1 策略模块

#### fusion_v2.py - 融合策略

**优点**:
- ✅ 模块化设计，技术指标独立计算
- ✅ 支持 RSI/MACD/MA/布林带 等多种指标
- ✅ 可配置参数权重

**问题**:
- ⚠️ 新闻情绪模块已注释掉（第38行）
- ⚠️ 买入阈值默认0.65可能过高
- ⚠️ 技术指标计算可优化（建议使用ta-lib）

**建议**:
```python
# 建议启用情绪分析或使用替代方案
from app.ai.signal_analyzer import analyze_sentiment

# 调整买入阈值
self.buy_threshold = self.config.get("buy_threshold", 0.55)  # 降低阈值
```

#### signal_engine/fusion_engine.py

**优点**:
- ✅ 完整的信号评分体系
- ✅ 支持 BUY/SELL 概率输出
- ✅ 模块化数据结构

**问题**:
- ⚠️ 情绪分析部分被移除
- ⚠️ 缺少信号去重机制

---

### 2.2 风控模块

#### circuit_breaker.py - 熔断器

**优点**:
- ✅ 区分技术错误和市场异常
- ✅ 支持连续拒绝熔断
- ✅ 日内重置机制

**问题**:
- ⚠️ reject_limit=8 可能过高（建议降至5）
- ⚠️ 缺少波动率触发熔断

**建议**:
```python
# 降低熔断阈值
self.reject_limit = 5  # 从8降到5

# 添加波动率熔断
if volatility > 0.05:
    self.trigger_pause(duration=180)  # 高波动暂停3分钟
```

#### risk_manager.py - 风险管理

**优点**:
- ✅ 五层风控检查体系
- ✅ 完整的配置系统
- ✅ 钉钉告警支持

**问题**:
- ⚠️ min_confidence=0.60 可能有效信号被过滤
- ⚠️ max_daily_trades=10 在当前胜率下过于宽松

**建议**:
```python
# 调整风控参数
min_confidence: 0.55    # 降低以获取更多信号
max_daily_trades: 5     # 减少交易频率
max_single_position: 0.15  # 降低单笔仓位
```

---

### 2.3 执行模块

#### executor.py - 交易执行器

**优点**:
- ✅ 支持港股/美股双市场
- ✅ 动态仓位计算
- ✅ 幂等锁防止重复下单
- ✅ 滑点记录

**问题**:
- ⚠️ 依赖大量外部模块，耦合度高
- ⚠️ 每手股数配置分散

**建议**:
```python
# 集中管理每手股数配置
LOT_SIZES = {
    "HK.09992": 200,  # 泡泡玛特
    "HK.00981": 500,  # 中芯国际
    "HK.03690": 100,  # 美团
    "HK.00700": 100,  # 腾讯
}
```

---

### 2.4 数据模块

#### data_subscriber.py

**优点**:
- ✅ 完整的市场数据订阅
- ✅ K线/Tick数据处理

**问题**:
- ⚠️ 代码量过大（113KB）
- ⚠️ 建议拆分为独立模块

---

## 三、当前表现分析

### 3.1 关键指标

| 指标 | 当前值 | 目标值 | 差距 |
|------|--------|--------|------|
| 胜率 | **33.9%** | 55% | -21.1% |
| 盈亏比 | **0.33** | 1.5 | -1.17 |
| 最大回撤 | **>20%** | <15% | 超限 |
| 总盈亏 | **-39,406 HKD** | 正盈利 | 亏损 |

### 3.2 问题诊断

#### 胜率低的原因

1. **止损过宽** - 历史数据显示4次交易亏损超过8%
2. **趋势逆势** - 下跌趋势中买入
3. **信号质量低** - 置信度阈值过滤不够
4. **策略失效** - price_break策略表现不佳

#### 盈亏比低的原因

1. **止损执行延迟** - 止损未及时触发
2. **止盈过早** - 止盈3%可能过早平仓
3. **平均盈利<平均亏损** - 0.67% vs -1.05%

---

## 四、优化建议

### 4.1 策略优化 (P0 - 紧急)

| 优化项 | 当前 | 建议 | 预期效果 |
|--------|------|------|----------|
| 买入阈值 | 0.65 | 0.55 | +10%信号量 |
| 趋势过滤 | 已启用 | 增强确认 | 避免逆势 |
| RSI阈值 | 70/30 | 60/40 | 更敏感 |
| 共识阈值 | 1.0 | 0.8 | 降低共识难度 |

### 4.2 风控优化 (P0 - 紧急)

| 优化项 | 当前 | 建议 | 预期效果 |
|--------|------|------|----------|
| 止损 | -3% | **-2%** | 减少单笔亏损 |
| 止盈 | +3% | +4% | 放大盈利 |
| 保本触发 | +1.5% | +1.2% | 更早保本 |
| 每日最大交易 | 10笔 | **5笔** | 控制频率 |

### 4.3 代码重构 (P1 - 重要)

| 模块 | 问题 | 建议 |
|------|------|------|
| data_subscriber.py | 113KB过大 | 拆分为独立模块 |
| fusion_v2.py | 情绪分析禁用 | 启用或替换 |
| executor.py | 耦合度高 | 解耦外部依赖 |

### 4.4 新功能开发 (P2 - 一般)

| 功能 | 说明 | 优先级 |
|------|------|--------|
| ATR动态止损 | 根据波动调整止损 | P1 |
| 凯利仓位管理 | 科学仓位计算 | P2 |
| 多时间框架 | 1分钟+5分钟+日线 | P2 |
| 回测系统完善 | 策略验证 | P1 |

---

## 五、代码质量评估

### 5.1 优点

| 方面 | 评价 |
|------|------|
| 模块化 | ✅ 高度模块化，职责清晰 |
| 可配置性 | ✅ YAML/JSON配置，热更新支持 |
| 错误处理 | ✅ 完善的异常处理和日志 |
| 风控体系 | ✅ 五层风控，四级熔断 |
| 文档 | ✅ 较完善的注释和文档 |

### 5.2 待改进

| 方面 | 问题 | 建议 |
|------|------|------|
| 测试覆盖 | 单元测试不足 | 增加pytest测试 |
| 类型标注 | 部分缺少 | 添加类型提示 |
| 代码重复 | 策略代码重复 | 抽象基类优化 |
| 依赖管理 | 无requirements.txt | 添加依赖清单 |

---

## 六、具体优化代码建议

### 6.1 增强趋势过滤

```python
# app/risk/trend_filter.py 增强版

def check_trend_enhanced(self, code: str, klines: list) -> Dict:
    """增强趋势判断"""
    prices = [k['close'] for k in klines]
    
    # 多均线确认
    ma5 = sum(prices[-5:]) / 5
    ma10 = sum(prices[-10:]) / 10
    ma20 = sum(prices[-20:]) / 20
    
    # 趋势强度
    trend_strength = 0
    if ma5 > ma10 > ma20:
        trend_strength = 1  # 强上涨
    elif ma5 > ma10:
        trend_strength = 0.5  # 弱上涨
    elif ma5 < ma10 < ma20:
        trend_strength = -1  # 强下跌
    elif ma5 < ma10:
        trend_strength = -0.5  # 弱下跌
    
    return {
        "trend": "UP" if trend_strength > 0 else "DOWN" if trend_strength < 0 else "NEUTRAL",
        "strength": abs(trend_strength),
        "allow_buy": trend_strength > 0,
        "allow_sell": trend_strength < 0,
    }
```

### 6.2 动态止损优化

```python
# app/risk/dynamic_stop.py

def calculate_atr_stop(self, klines: list, multiplier: float = 2.0) -> float:
    """ATR动态止损"""
    # 计算ATR
    tr_list = []
    for i in range(1, min(15, len(klines))):
        high = klines[-i]['high']
        low = klines[-i]['low']
        prev_close = klines[-i-1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)
    
    atr = sum(tr_list) / len(tr_list)
    current_price = klines[-1]['close']
    
    # 止损位 = 当前价 - multiplier * ATR
    stop_price = current_price - multiplier * atr
    stop_pct = (current_price - stop_price) / current_price
    
    return {
        "stop_price": stop_price,
        "stop_pct": stop_pct,
        "atr": atr,
    }
```

### 6.3 信号质量提升

```python
# 信号质量评分

def score_signal(signal: Signal, market_data: Dict) -> float:
    """信号质量评分 (0-100)"""
    score = 0
    
    # 1. 趋势确认 (+20)
    trend = market_data.get("trend", {})
    if trend.get("allow_buy") and signal.action == SignalAction.LONG:
        score += 20
    elif trend.get("allow_sell") and signal.action == SignalAction.SHORT:
        score += 20
    
    # 2. 置信度 (+25)
    score += signal.confidence * 25
    
    # 3. 成交量确认 (+15)
    if market_data.get("volume_ratio", 1) > 1.5:
        score += 15
    
    # 4. 技术指标一致 (+20)
    tech_score = market_data.get("technical_score", 0)
    score += max(0, tech_score * 20)
    
    # 5. 历史胜率 (+20)
    strategy_winrate = get_strategy_winrate(signal.strategy)
    score += strategy_winrate * 20
    
    return score

# 只执行评分>60的信号
if score_signal(signal, market_data) >= 60:
    execute(signal)
```

---

## 七、执行计划

### Week 1 (立即执行)

| 任务 | 负责角色 | 预期效果 |
|------|----------|----------|
| 止损收紧到-2% | 策略师+执行官 | 减少单笔亏损 |
| 每日交易限制5笔 | 策略师+执行官 | 控制频率 |
| 买入阈值降至0.55 | 策略师+执行官 | 增加信号量 |

### Week 2-4

| 任务 | 负责角色 | 预期效果 |
|------|----------|----------|
| 启用情绪分析或替代 | 策略师 | 提升信号质量 |
| ATR动态止损开发 | 策略师+执行官 | 智能止损 |
| 策略权重优化 | AutoSearch | 找最优参数 |

### Month 2-3

| 任务 | 负责角色 | 预期效果 |
|------|----------|----------|
| 代码重构拆分 | 协调器+执行官 | 提升可维护性 |
| 回测系统完善 | 分析师+策略师 | 策略验证 |
| 多时间框架分析 | 策略师 | 提升判断精度 |

---

## 八、风险提示

1. **当前胜率过低 (33.9%)**，建议暂停实盘，专注于模拟盘优化
2. **趋势过滤器已添加但可能效果有限**，需要配合参数调整
3. **止损优化需要数据验证**，建议先在模拟盘测试
4. **代码修改需要严格审批**，避免引入新问题

---

## 九、结论

FTSeek项目架构清晰、模块化程度高，具备良好的扩展性。当前主要问题是**策略胜率过低 (33.9%)** 和 **盈亏比不佳 (0.33)**。

**核心优化方向**：
1. 收紧止损到-2%，控制单笔亏损
2. 降低买入阈值到0.55，增加有效信号
3. 增强趋势过滤，避免逆势交易
4. 每日交易限制5笔，控制频率

**预期效果**：经过2-3个月优化，胜率可提升至45-50%，盈亏比提升至1.0以上。

---

*分析完成: 2026-03-28*
*分析者: 精英组 (分析师+策略师+风控官)*