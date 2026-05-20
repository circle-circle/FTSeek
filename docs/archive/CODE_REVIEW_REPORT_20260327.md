# FTSeek-US 代码 Review 复盘报告

**Review 日期**: 2026-03-27  
**Review 范围**: `/opt/ftseek/FTSeek-US`  
**Review 重点**: 策略逻辑、风险管理、止盈止损、胜率收益优化  
**Review 人**: FTSeek-US Subagent

---

## 一、当前架构和逻辑总结

### 1.1 整体架构

FTSeek-US 采用**多引擎 + 多 Agent**架构，包含四大核心引擎：

```
┌─────────────────────────────────────────────────────────────┐
│                      FTSeek-US                               │
│                  美股自动化交易系统                           │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  决策引擎    │  │  风险引擎    │  │  执行引擎    │       │
│  │  Decision    │→ │  Risk        │→ │  Execution   │       │
│  │  Engine      │  │  Engine      │  │  Engine      │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│         ↑                                       ↓            │
│         └───────────────────────────────────────────┐        │
│                          ↑                          │        │
│              ┌───────────────────────┐              │        │
│              │    观测引擎           │←─────────────┘        │
│              │  Observability        │                       │
│              │  Engine               │                       │
│              └───────────────────────┘                       │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**代码规模**:
- 核心引擎代码：~2700 行 (scheduler + risk + execution + models)
- 决策引擎：~1000 行 (signal_generator + signal_scorer + consensus + market_regime)
- 测试代码：~40 个测试文件
- 文档：~30 个 Markdown 文件

### 1.2 策略逻辑

**投资风格定位**: 中长线 (持仓周期 30-90 天 / 1-3 个月)

**策略流程**:
```
行情获取 → 新闻情绪分析 → 多模型投票 → 共识决策 → 风控检查 → 下单执行
   ↓
每 30 分钟扫描一次信号
   ↓
每 5 分钟监控持仓止损止盈
```

**信号评分系统** (0-100 分):
| 评分因子 | 权重 | 说明 |
|----------|------|------|
| 趋势确认 | 20% | 均线/突破确认 |
| 成交量放大 | 15% | 量比>1.5 |
| 大盘同向 | 15% | 标普/纳指同向 |
| 波动率适配 | 10% | ATR 在合理范围 |
| 历史表现 | 10% | 近 20 次信号胜率 |
| 新闻情绪 | 10% | LLM 情绪分析 |
| 市场状态适配 | 10% | 趋势/震荡匹配 |
| 风险收益比 | 10% | 盈亏比估算 |

**决策阈值**:
- < 50 分：不交易
- 50-70 分：小仓位 (25%)
- > 70 分：正常仓位 (50-100%)

**多模型投票系统**:
- DeepSeek (30%): 趋势/动量分析
- MiniMax (25%): 新闻情绪分析
- GLM-4.7 (25%): 技术指标分析
- Qwen3.5-Plus (20%): 基本面/估值分析
- 共识阈值：0.55 (55% 一致性)

### 1.3 风险管理机制

**四级熔断系统**:

| 熔断级别 | 触发条件 | 暂停时长 |
|----------|----------|----------|
| 策略级 | 连续 5 笔亏损 | 30 分钟 |
| 品种级 | 5 分钟波动率 > 5% | 动态冻结 |
| 账户级 | 日内回撤 > 3% | 暂停开新仓 |
| 系统级 | 行情断流 > 60 秒 | 立即停止 |

**三段式风控**:
- **预交易风控**: 信号强度、置信度、有效期检查
- **持仓中风控**: 浮盈回撤、浮亏扩大、波动率监控
- **交易后风控**: 滑点验证、回撤基准更新

**仓位限制**:
- 单品种最大仓位：25-30%
- 单方向最大敞口：50%
- 总仓位上限：80%

**VIX 监控**:
- VIX > 30: 预警，降低仓位
- VIX > 40: 紧急告警，暂停新开仓
- 数据源：AlphaVantage API (10 分钟缓存)

### 1.4 止盈止损逻辑

**退出引擎** (优先级从高到低):

1. **硬止损** (Hard Stop Loss)
   - 固定止损：-8% ~ -10% (中长线放宽)
   - 优先级最高，立即全仓止损

2. **移动止损** (Trailing Stop)
   - 激活条件：盈利 ≥ 1%
   - 止损间距：ATR × 2
   - 只上移 (多头) / 只下移 (空头)

3. **三档分批止盈** (Scale Out)
   - 第一档：+15% 平仓 40%
   - 第二档：+25% 平仓 35%
   - 第三档：+40% 平仓 25%

4. **时间止损** (Time Stop)
   - 持仓上限：168 小时 (7 天)
   - 到期自动平仓

**退出原因分类**:
- `STOP_LOSS`: 硬止损触发
- `TRAILING_STOP`: 移动止损触发
- `TAKE_PROFIT`: 止盈触发
- `TIME_STOP`: 时间止损触发
- `STRUCTURE_EXIT`: 市场结构破坏
- `MANUAL`: 手动平仓
- `RISK_CUT`: 风控强平

### 1.5 执行机制

**交易频率限制**:
- 单日最大交易数：5 笔
- 单只股票日交易上限：2 笔
- 信号冷却时间：60 分钟 (同标的)

**连续亏损熔断**:
- 连续亏损 2 笔后暂停交易 24 小时 (USER.md 配置)
- 策略级连续 5 笔亏损暂停 30 分钟 (代码实现)

**预交易检查**:
- 信号强度：effective_strength ≥ 50 (SHORT: 100-strength)
- 置信度：≥ 0.50
- 信号有效期：≤ 30 分钟

**订单执行流程**:
```
CREATED → VALIDATED → SENT → ACKED → PARTIALLY_FILLED → FILLED
                         ↓
                  REJECTED / CANCELED / EXPIRED
```

**幂等机制**:
- 唯一 `client_order_id`: `FTUS_{signal_id}_{timestamp}`
- 防止重复下单
- 断线重连后状态恢复

### 1.6 代码质量

**模块化设计**:
- ✅ 四大引擎职责清晰
- ✅ 数据模型集中管理 (`app/core/models.py`)
- ✅ 事件流统一存储 (`app/core/events.py`)
- ✅ 配置与代码分离 (`config.py` + `.env.us`)

**错误处理**:
- ✅ 大部分关键路径有 try-except
- ✅ 异常日志记录完善
- ⚠️ 部分模块异常处理后直接 pass，可能掩盖问题

**日志记录**:
- ✅ 使用 Python logging 模块
- ✅ 日志级别分明 (INFO/WARN/ERROR)
- ✅ 关键操作都有日志
- ⚠️ 缺少结构化日志 (JSON 格式)

**配置管理**:
- ✅ 环境变量敏感信息
- ✅ 配置文件版本控制
- ⚠️ 部分配置硬编码在代码中

---

## 二、发现的问题和风险点

### 2.1 策略层面问题

#### 🔴 严重问题

**问题 1: 中长线定位与信号频率不匹配**
- **现象**: 信号扫描间隔 30 分钟，但持仓周期 30-90 天
- **风险**: 过度频繁的信号生成可能导致过度交易
- **影响**: 增加交易成本，降低胜率
- **位置**: `app/scheduler.py:SCAN_INTERVAL_MINUTES = 30`

**问题 2: 信号评分权重缺乏回测验证**
- **现象**: 8 个评分因子权重凭经验设定
- **风险**: 权重可能不符合实际市场规律
- **影响**: 信号质量不稳定，胜率波动大
- **位置**: `app/decision/signal_scorer.py:factor_weights`

**问题 3: 多模型投票权重未动态调整**
- **现象**: 模型权重固定 (DeepSeek 30%, MiniMax 25% 等)
- **风险**: 无法适应不同市场环境
- **影响**: 某些市场状态下决策质量下降
- **位置**: `app/agents/voting/voting_config.py:model_weights`

#### 🟡 中等问题

**问题 4: 选股逻辑过于依赖技术面**
- **现象**: 信号生成主要基于价格突破、均线等技术指标
- **风险**: 忽略基本面因素 (财报、估值、行业趋势)
- **影响**: 中长线持仓可能买入基本面恶化的股票
- **位置**: `app/decision/signal_generator.py:example_price_break_strategy`

**问题 5: 缺乏行业/板块分散**
- **现象**: 持仓限制只控制单品种和总仓位
- **风险**: 可能过度集中于单一行业 (如科技股)
- **影响**: 行业风险暴露过高
- **位置**: `app/risk/position_limits.py`

### 2.2 风险管理问题

#### 🔴 严重问题

**问题 6: 止损幅度过大 (对中长线)**
- **现象**: 默认止损 -8% ~ -10%
- **风险**: 对于中长线交易，单次亏损过大
- **影响**: 连续 3 次止损即亏损 24-30%，难以恢复
- **位置**: `config.py:US_DEFAULT_STOP_LOSS = -0.08`

**问题 7: 缺少账户级回撤实时监控**
- **现象**: `drawdown_controller` 只在交易后更新
- **风险**: 无法及时捕捉盘中大幅回撤
- **影响**: 可能超出最大回撤限制
- **位置**: `app/risk/engine.py:intra_trade_check`

**问题 8: 系统级熔断恢复逻辑缺失**
- **现象**: 系统熔断触发后无自动恢复机制
- **风险**: 需要人工干预才能恢复交易
- **影响**: 可能错过市场机会
- **位置**: `app/risk/circuit_breaker.py:SystemCircuitBreaker`

#### 🟡 中等问题

**问题 9: VIX 监控频率过低**
- **现象**: VIX 数据 10 分钟缓存
- **风险**: 市场恐慌时 VIX 可能快速上升
- **影响**: 风险预警延迟
- **位置**: `app/risk/vix_monitor.py:CACHE_TTL_SECONDS = 600`

**问题 10: 持仓跌幅监控不完善**
- **现象**: 只在浮亏超过止损 80% 时警告
- **风险**: 预警太晚，接近实际止损
- **影响**: 没有足够时间反应
- **位置**: `app/risk/engine.py:intra_trade_check`

### 2.3 止盈止损问题

#### 🔴 严重问题

**问题 11: 移动止损激活阈值过低**
- **现象**: 盈利 ≥ 1% 即激活移动止损
- **风险**: 对于中长线交易，1% 盈利太容易被震荡触发
- **影响**: 过早退出，无法享受大趋势
- **位置**: `app/risk/exit_engine.py:TRAIL_ACTIVATION_PCT = 0.01`

**问题 12: 三档止盈比例固定**
- **现象**: 止盈档位和比例硬编码
- **风险**: 无法适应不同波动率的股票
- **影响**: 高波动股票可能过早止盈，低波动股票可能无法触达
- **位置**: `app/risk/exit_engine.py:_check_take_profit_levels`

#### 🟡 中等问题

**问题 13: 时间止损缺乏灵活性**
- **现象**: 固定 168 小时 (7 天) 时间止损
- **风险**: 不同策略应有不同持仓周期
- **影响**: 可能过早退出长期趋势交易
- **位置**: `.env.us:TIME_STOP_HOURS=168`

**问题 14: 缺少基于市场结构的退出**
- **现象**: `STRUCTURE_EXIT` 枚举存在但未实现
- **风险**: 无法识别关键支撑/阻力突破
- **影响**: 可能在市场转势时未及时退出
- **位置**: `app/core/models.py:ExitReason.STRUCTURE_EXIT`

### 2.4 执行机制问题

#### 🟡 中等问题

**问题 15: 交易频率限制配置不一致**
- **现象**: USER.md 配置"连续亏损 2 笔暂停 24 小时"，代码实现"连续 5 笔暂停 30 分钟"
- **风险**: 用户预期与实际行为不符
- **影响**: 风险控制力度不足
- **位置**: `USER.md` vs `app/risk/circuit_breaker.py`

**问题 16: 订单重试机制简单**
- **现象**: 固定重试 3 次，延迟 5 秒
- **风险**: 无法适应不同市场状况
- **影响**: 可能在流动性差时过度重试或过早放弃
- **位置**: `app/execution/engine.py:max_retry_attempts = 3`

**问题 17: 滑点控制阈值过严**
- **现象**: 最大滑点 0.5%
- **风险**: 高波动股票可能频繁下单失败
- **影响**: 错过交易机会
- **位置**: `app/execution/engine.py:max_slippage_ratio = 0.005`

### 2.5 代码质量问题

#### 🟡 中等问题

**问题 18: 部分异常处理过于宽泛**
- **现象**: 多处使用 `except Exception: pass`
- **风险**: 掩盖潜在问题，难以调试
- **影响**: 故障排查困难
- **位置**: `app/risk/engine.py:intra_trade_check`, `app/risk/engine.py:post_trade_check`

**问题 19: 配置分散在多处**
- **现象**: 配置分布在 `config.py`, `.env.us`, `scheduler.py`, `voting_config.py`
- **风险**: 配置管理混乱，容易遗漏
- **影响**: 部署和维护困难
- **位置**: 多处

**问题 20: 缺少集成测试**
- **现象**: 单元测试覆盖各模块，但缺少端到端集成测试
- **风险**: 模块间集成问题无法发现
- **影响**: 生产环境可能出现意外行为
- **位置**: `tests/` 目录

### 2.6 数据与监控问题

#### 🟡 中等问题

**问题 21: 交易记录存储简单**
- **现象**: 使用 SQLite 存储，缺少数据备份机制
- **风险**: 数据丢失风险
- **影响**: 无法进行长期回测和分析
- **位置**: `app/storage/trade_store.py`

**问题 22: 缺少性能归因分析**
- **现象**: 没有按策略/股票/时间段统计胜率
- **风险**: 无法识别哪些策略有效
- **影响**: 无法优化策略参数
- **位置**: 缺少相关模块

---

## 三、改进建议 (优先级排序)

### P0: 紧急修复 (1 周内)

#### 建议 1: 调整移动止损激活阈值
**问题**: 1% 激活阈值过低，不适合中长线
**建议**: 
```python
# app/risk/exit_engine.py
TRAIL_ACTIVATION_PCT = 0.05  # 改为 5% (与中长线匹配)
TRAIL_ATR_MULT = 2.5         # 放宽至 ATR×2.5
```
**预期效果**: 减少过早退出，提高盈亏比

#### 建议 2: 统一交易频率限制配置
**问题**: USER.md 与代码实现不一致
**建议**: 
```python
# app/risk/circuit_breaker.py
class StrategyCircuitBreaker:
    def __init__(self, consecutive_losses_threshold: int = 2, pause_duration_seconds: int = 86400):
        # 连续 2 笔亏损暂停 24 小时
```
**预期效果**: 风险控制符合用户预期

#### 建议 3: 修复异常处理
**问题**: `except Exception: pass` 掩盖问题
**建议**: 
```python
# app/risk/engine.py
except Exception as e:
    logger.error(f"[风控] 持仓检查异常 {position.symbol}: {e}", exc_info=True)
```
**预期效果**: 便于故障排查

### P1: 重要改进 (2-4 周)

#### 建议 4: 优化止损策略
**问题**: 固定 -8% 止损对中长线过大
**建议**: 
```python
# 基于 ATR 的动态止损
stop_loss = entry_price - (ATR × 3)  # 多头
# 或基于支撑位
stop_loss = recent_swing_low * 0.98
```
**预期效果**: 降低单次亏损，提高生存率

#### 建议 5: 增加行业分散限制
**问题**: 无行业敞口控制
**建议**: 
```python
# app/risk/position_limits.py
class SectorLimitsChecker:
    max_sector_exposure = 0.40  # 单行业不超过 40%
```
**预期效果**: 降低行业集中风险

#### 建议 6: 实现市场结构退出
**问题**: STRUCTURE_EXIT 未实现
**建议**: 
```python
# app/risk/exit_engine.py
def _check_structure_exit(self, position, market_data):
    if position.direction == Direction.LONG:
        if current_price < key_support_level:
            return True, ExitReason.STRUCTURE_EXIT
```
**预期效果**: 及时识别趋势反转

#### 建议 7: 增加策略回测框架
**问题**: 评分权重缺乏验证
**建议**: 
```bash
# 新增模块
app/backtest/
├── backtester.py
├── performance_analyzer.py
└── parameter_optimizer.py
```
**预期效果**: 数据驱动优化策略参数

### P2: 优化增强 (1-2 月)

#### 建议 8: 动态模型权重调整
**问题**: 多模型权重固定
**建议**: 
```python
# 根据近期表现动态调整
def update_model_weights(self, model_performance: Dict[str, float]):
    # 胜率高的模型权重增加
    for model, win_rate in model_performance.items():
        self.model_weights[model] *= (1 + win_rate)
```
**预期效果**: 自适应市场环境

#### 建议 9: 增加基本面筛选
**问题**: 选股过于依赖技术面
**建议**: 
```python
# app/data/fundamental_screener.py
class FundamentalScreener:
    min_market_cap = 10e9      # 市值 > 100 亿
    min_revenue_growth = 0.10  # 营收增长 > 10%
    max_pe_ratio = 50          # PE < 50
```
**预期效果**: 提高中长线选股质量

#### 建议 10: 实现账户级实时回撤监控
**问题**: 回撤监控延迟
**建议**: 
```python
# app/risk/engine.py
def monitor_realtime_drawdown(self, positions, current_prices):
    total_equity = sum(p.size * current_prices[p.symbol] for p in positions)
    drawdown = (peak_equity - total_equity) / peak_equity
    if drawdown > self.max_daily_drawdown:
        self.trigger_emergency_stop()
```
**预期效果**: 及时控制风险

#### 建议 11: 增加结构化日志
**问题**: 日志难以机器分析
**建议**: 
```python
import json
logger.info(json.dumps({
    "event": "order_filled",
    "symbol": "AAPL",
    "price": 175.23,
    "quantity": 100,
    "timestamp": datetime.now().isoformat()
}))
```
**预期效果**: 便于日志分析和告警

### P3: 长期规划 (3 月+)

#### 建议 12: 增加期权对冲策略
**问题**: 无对冲机制
**建议**: 
```python
# 持仓超过阈值时自动买入 Put 对冲
if portfolio_beta > 1.5:
    buy protective_put(symbol="SPY", expiry="30d")
```

#### 建议 13: 实现机器学习预测
**问题**: 策略基于规则
**建议**: 
```python
# 使用历史数据训练模型预测胜率
model = RandomForestClassifier()
model.fit(X_train, y_train)
win_probability = model.predict(signal_features)
```

#### 建议 14: 增加多账户管理
**问题**: 单账户限制
**建议**: 
```python
# 支持多个券商账户
accounts = {
    "futu_sim": FutuAccount(env=1),
    "futu_real": FutuAccount(env=0),
    "ibkr": IBKRAccount(),
}
```

---

## 四、中长线策略优化方向

### 4.1 策略定位优化

**当前问题**: 中长线定位 (30-90 天) 与短信号频率 (30 分钟) 不匹配

**优化方向**:
1. **分离信号频率与持仓周期**
   - 信号扫描：改为每 4-6 小时一次 (每日 4-6 次)
   - 持仓监控：保持每 5 分钟 (止损止盈检查)

2. **增加趋势确认周期**
   - 当前：基于日线/4 小时线
   - 建议：增加周线趋势确认，确保中长线方向一致

3. **降低交易频率**
   - 当前：单日最大 5 笔
   - 建议：单周最大 3-5 笔，避免过度交易

### 4.2 选股逻辑优化

**当前问题**: 技术面为主，缺乏基本面筛选

**优化方向**:
1. **基本面初筛**
   ```python
   # 中长线标的筛选条件
   min_market_cap = 10e9       # 市值 > 100 亿美元
   min_avg_volume = 1e6        # 日均成交 > 100 万股
   positive_earnings = True    # 最近季度盈利为正
   revenue_growth > 0.10       # 营收增长 > 10%
   ```

2. **行业轮动策略**
   - 识别当前市场主线行业
   - 优先选择强势行业中的龙头股
   - 避免夕阳行业

3. **财报季规避**
   - 财报发布前 3 天不新开仓
   - 持仓股票财报后根据表现决定是否持有

### 4.3 仓位管理优化

**当前问题**: 固定仓位比例，未考虑胜率和波动率

**优化方向**:
1. **凯利公式仓位**
   ```python
   # f* = (p × b - q) / b
   # p = 胜率, q = 1-p, b = 盈亏比
   position_size = (win_rate * profit_loss_ratio - (1-win_rate)) / profit_loss_ratio
   ```

2. **波动率调整仓位**
   ```python
   # 高波动股票降低仓位
   base_size = 0.25
   adjusted_size = base_size / (1 + ATR_ratio)
   ```

3. **连胜/连败调整**
   ```python
   # 连胜时适度加仓，连败时减仓
   if consecutive_wins >= 3:
       position_size *= 1.2
   elif consecutive_losses >= 2:
       position_size *= 0.5
   ```

### 4.4 止盈止损优化

**当前问题**: 固定比例止损止盈，不适应不同股票特性

**优化方向**:
1. **基于 ATR 的动态止损**
   ```python
   # 止损 = 入场价 - ATR × N
   # N 根据持仓周期调整：中长线 N=3-4
   stop_loss = entry_price - (atr * 3.5)
   ```

2. **基于支撑/阻力的止损**
   ```python
   # 多头止损 = 前低 × 0.98
   # 空头止损 = 前高 × 1.02
   ```

3. **时间 + 价格双重止盈**
   ```python
   # 达到目标价后，给股票 N 天时间继续上涨
   # 超时未创新高则止盈
   if days_since_entry > 30 and price < entry * 1.15:
       exit_position()
   ```

4. **分批建仓 + 分批止盈**
   ```python
   # 建仓：50% 初始 + 50% 确认后加仓
   # 止盈：30% @ +15%, 30% @ +25%, 40% @ +40%
   ```

### 4.5 胜率提升策略

**当前问题**: 胜率依赖固定权重，未动态优化

**优化方向**:
1. **策略表现追踪**
   ```python
   # 记录每个策略的胜率、盈亏比
   strategy_stats = {
       "price_break": {"win_rate": 0.55, "profit_factor": 1.8},
       "ma_cross": {"win_rate": 0.48, "profit_factor": 1.5},
   }
   ```

2. **动态权重调整**
   ```python
   # 近 20 笔交易胜率高的策略权重增加
   for strategy, stats in strategy_stats.items():
       if stats["win_rate"] > 0.55:
           strategy_weights[strategy] *= 1.2
   ```

3. **市场环境识别**
   ```python
   # 趋势市场：增加趋势策略权重
   # 震荡市场：增加均值回归策略权重
   if market_regime == "TREND_UP":
       strategy_weights["price_break"] = 0.40
       strategy_weights["rsi"] = 0.10
   ```

4. **避免高相关性信号**
   ```python
   # 如果多个策略给出相同方向信号，降低置信度
   if correlation(signals) > 0.8:
       confidence *= 0.8  # 降低过度自信
   ```

### 4.6 收益优化策略

**当前问题**: 收益依赖单次交易盈亏，缺少组合优化

**优化方向**:
1. **Sharpe Ratio 最大化**
   ```python
   # 优化目标：最大化 Sharpe = (收益 - 无风险利率) / 波动率
   # 通过调整仓位和止损参数实现
   ```

2. **Profit Factor 提升**
   ```python
   # Profit Factor = 总盈利 / 总亏损
   # 目标：> 2.0
   # 方法：提高盈亏比，降低亏损次数
   ```

3. **复利增长策略**
   ```python
   # 每月提取盈利的 50%，剩余 50% 复投
   # 账户增长时适度增加仓位上限
   if account_balance > initial * 1.5:
       max_position_per_symbol = 0.35  # 从 30% 提升至 35%
   ```

4. **尾部风险管理**
   ```python
   # 买入虚值 Put 保护极端下跌
   if VIX < 20 and SPY > MA200:
       buy_out_of_money_put("SPY", strike=0.90, expiry="90d")
   ```

---

## 五、总结

### 5.1 架构优势

✅ **多引擎架构清晰**: 决策、风险、执行、观测职责分明  
✅ **事件流设计优秀**: 支持交易回放和审计  
✅ **风控体系完善**: 四级熔断 + 三段式风控  
✅ **退出机制丰富**: 7 种退出原因覆盖全面  
✅ **代码质量良好**: 类型注解、文档、日志齐全  

### 5.2 主要风险

🔴 **中长线定位与执行频率不匹配**: 30 分钟信号扫描 vs 30-90 天持仓  
🔴 **止损幅度过大**: -8% 对中长线单次亏损过高  
🔴 **移动止损激活过早**: 1% 盈利激活不适合中长线  
🔴 **配置不一致**: USER.md 与代码实现存在差异  

### 5.3 优先行动项

| 优先级 | 改进项 | 预计用时 | 预期效果 |
|--------|--------|----------|----------|
| P0 | 移动止损激活阈值调至 5% | 1 小时 | 减少过早退出 |
| P0 | 统一交易频率限制配置 | 1 小时 | 风险控制一致 |
| P0 | 修复异常处理 | 2 小时 | 便于故障排查 |
| P1 | 基于 ATR 的动态止损 | 4 小时 | 降低单次亏损 |
| P1 | 增加行业分散限制 | 4 小时 | 降低行业风险 |
| P1 | 实现市场结构退出 | 8 小时 | 及时识别反转 |
| P2 | 策略回测框架 | 2-3 天 | 数据驱动优化 |

### 5.4 中长期优化方向

1. **策略层面**: 增加基本面筛选，降低信号频率，增加周线确认
2. **风控层面**: 实时回撤监控，动态仓位调整，行业分散
3. **执行层面**: 智能订单路由优化，滑点控制改进
4. **监控层面**: 结构化日志，性能归因分析，策略表现追踪

---

**报告生成时间**: 2026-03-27 21:30  
**Review 覆盖代码**: ~4000 行  
**Review 测试文件**: 40+ 个  
**Review 文档**: 30+ 个  

---

*FTSeek-US 代码 Review 完成*
