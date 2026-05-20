# FTSeek-US 策略识别与股票筛选全链路审计报告

**审计日期**: 2026-04-30
**审计范围**: 策略识别 → 信号生成 → 信号去重 → 市场状态 → 盘前计划 → 盘中执行 全链路

---

## 一、策略识别逻辑审计

### 1.1 已注册策略清单

| 策略名 | 类名 | 版本 | 文件 | 状态 |
|--------|------|------|------|------|
| `ma_cross` | MACrossStrategy | 1.0.0 | ma_cross.py | ✅ 已注册 |
| `price_break` | PriceBreakStrategy | 1.0.0 | price_break.py | ✅ 已注册 |
| `rsi_rebound` | RSIReboundStrategy | 1.0.0 | rsi_rebound.py | ✅ 已注册 |

**注册机制**: 使用 `@register_strategy` 装饰器自动注册到 `_strategy_registry` 全局字典。`__init__.py` 显式导入三个策略类触发装饰器执行。

### 1.2 策略权重配置

| 来源 | price_break | ma_cross | rsi_rebound | 其他 | 总和 |
|------|-------------|----------|-------------|------|------|
| unified_config.yaml | 0.36 | 0.28 | 0.36 | - | **1.00** |
| consensus.py 默认值 | 0.25 | 0.20 | 0.25 | macd 0.15, bollinger 0.10, supertrend 0.05 | **1.00** |

### 1.3 ⚠️ CRITICAL: 正交化器中声明但未实现的策略

在 `strategy_orthogonalizer.py` 和 `config/unified_config.yaml` 的 `STRATEGY_ORTHOGONALIZATION.strategy_groups` 中声明了以下策略，但**没有对应实现文件**:

| 策略名 | 分组 | 是否有实现 | 影响 |
|--------|------|-----------|------|
| `macd` | trend | ❌ 无 | 正交化权重计算时引用 0.1 默认值，可能导致权重偏差 |
| `supertrend` | trend | ❌ 无 | 同上 |
| `bollinger` | reversion | ❌ 无 | 同上 |
| `rsi` | reversion | ❌ 无（注意：已有 rsi_rebound） | 命名冲突，可能是遗漏或别名 |
| `news` | sentiment | ❌ 无独立策略 | 情绪数据通过 context 注入 |
| `llm` | sentiment | ❌ 无独立策略 | 投票系统使用 |
| `sentiment` | sentiment | ❌ 无独立策略 | 情绪数据通过 context 注入 |

**风险等级: HIGH** — 如果某个未实现策略因配置错误被激活，会以默认权重 0.1 参与正交化计算，造成信号权重失真。

### 1.4 ⚠️ HIGH: 策略模块与主执行管线脱节

**关键发现**: 策略模块 (`strategies/`) → `SignalGenerator` → `SignalDeduplicator` → `ConsensusEngine` 这条链路在**实际盘中执行中并未被使用**。

实际盘中执行管线 (`signal_pipeline.py`):
```
_process_symbol_impl()
  → _generate_technical_signal() → IntradayScorer.score()  [盘中 TRADING 阶段]
  → voting_coordinator.run_voting()                          [非盘中阶段]
  → _build_signal_from_vote() → SignalScore
  → 风控 → 下单
```

而 modular 策略链路:
```
SignalGenerator.generate_signal() → 各策略.execute()
  → SignalDeduplicator.deduplicate()
  → ConsensusEngine.generate_consensus()
```

**这条链路存在于代码中但在 `signal_pipeline.py` 中从未被调用。**

**影响**: 
- `SignalDeduplicator` 和 `ConsensusEngine` 实际上不在交易执行路径中
- 信号去重逻辑（冷却期/冲突检测）在盘中扫描时**不生效**
- 正交化器 (`StrategyOrthogonalizer`) 仅在 `ConsensusEngine` 中使用，而 `ConsensusEngine` 不在执行路径中

---

## 二、信号去重审计

### 2.1 去重逻辑 (`signal_dedup.py`)

```
去重流程:
  1. 过期检查 → signal.is_expired()
  2. 冷却期检查 → 同方向信号 cooldown_seconds 内不重复（默认 120s）
  3. 冲突检查 → 反方向强信号（strength>70）在 cooldown*2 窗口内拒绝
  4. 通过 → 加入缓存
```

### 2.2 ⚠️ MEDIUM: 去重模块未被主链路调用

如上所述，`SignalDeduplicator` 未被 `signal_pipeline.py` 调用。取而代之的是：

- **冷却**: `scheduler._is_in_cooldown(symbol)` 在扫描层面实现
- **冲突**: 在 `order_executor` 中的持仓冲突处理逻辑
- **过期**: `SignalScore.is_expired()` 在构建信号后检查

**结论**: `SignalDeduplicator` 类目前是**死代码**，其逻辑没有被实际执行。

### 2.3 ⚠️ MEDIUM: 冲突检测阈值不对称

```python
# _has_conflict()
if recent_signal.direction == opposite_direction and
   recent_signal.created_at > conflict_window and
   recent_signal.strength > 70:  # ← 只有 strength > 70 才视为冲突
```

**问题**: 如果反方向信号 strength ≤ 70（但可能是有效信号，比如 65），不会被识别为冲突。这可能导致系统同时持有多空信号的矛盾。

### 2.4 ✅ 冷却窗口设计合理

- 同方向冷却: 120s
- 反方向冲突窗口: 240s（冷却的2倍）
- 缓存限制: 50条/symbol
- 30分钟自动清理

---

## 三、市场状态与策略匹配审计

### 3.1 市场状态识别 (`market_regime.py`)

```
输入: spy_price, spy_ma20, spy_ma50, vix, atr, avg_atr, price_change_5d

判断逻辑:
  1. HIGH_VOL (优先级最高): VIX>25 或 ATR > avg_atr*1.5
  2. TREND_UP: price > ma20 > ma50 AND 5日涨幅 > 2%
  3. TREND_DOWN: price < ma20 < ma50 AND 5日跌幅 > 2%
  4. LOW_VOL: VIX<15 或 ATR < avg_atr*0.7
  5. RANGE: 默认
```

### 3.2 ⚠️ HIGH: 枚举值不一致

| 模块 | 枚举值 | 文件 |
|------|--------|------|
| MarketRegime 模型 | `HIGH_VOL`, `LOW_VOL`, `TREND_UP`, `TREND_DOWN`, `RANGE` | core/models.py |
| 策略返回 | `TREND_UP`, `TREND_DOWN`, `RANGE` | 各策略文件 |
| signal_scorer._score_regime_fit | `"HIGH_VOLATILITY"` ← **不一致!** | signal_scorer.py L330 |

```python
# signal_scorer.py L330
elif regime == "HIGH_VOLATILITY":  # ← 实际应为 "HIGH_VOL"
    if signal.strategy_id in ["supertrend"]:
```

**影响**: `signal_scorer` 的 regime_fit 评分在高波动状态下**永远不会命中**正确分支，始终走 else 分支返回 0。这导致高波动状态下策略评分缺少应有的调节。

### 3.3 策略与市场状态匹配

| 策略 | 返回的 regime | 适合的市场状态 |
|------|--------------|---------------|
| ma_cross (多头) | TREND_UP | TREND_UP ✅ |
| ma_cross (空头) | TREND_DOWN | TREND_DOWN ✅ |
| price_break (突破) | TREND_UP | TREND_UP ✅ |
| price_break (跌破) | TREND_DOWN | TREND_DOWN ✅ |
| rsi_rebound (超卖反弹) | RANGE | RANGE ✅ |
| rsi_rebound (超买回调) | RANGE | RANGE ✅ |

**评价**: 策略返回的 regime 与实际适用场景匹配合理。

### 3.4 ⚠️ MEDIUM: MarketRegimeIdentifier 与 IntradayScorer 的 regime 定义不同

- `MarketRegimeIdentifier`: TREND_UP, TREND_DOWN, RANGE, HIGH_VOL, LOW_VOL
- `IntradayScorer._assess_regime`: LOW_VOLATILITY, NORMAL, HIGH_VOLATILITY, EXTREME_VOLATILITY

两者是**两套独立的体系**，没有共享。这可能导致市场状态判断在系统中存在不一致。

---

## 四、股票筛选标准审计

### 4.1 Watchlist 来源

统一配置文件 `unified_config.yaml` 定义了 5 只美股 + 2 只 ETF:

| 代码 | 名称 | Tier | 启用 |
|------|------|------|------|
| US.TSLA | Tesla | TIER1 | ✅ |
| US.NVDA | NVIDIA | TIER1 | ✅ |
| US.MU | Micron | TIER1 | ✅ |
| US.GOOG | Alphabet | TIER1 | ✅ |
| US.BABA | Alibaba | TIER1 | ✅ |
| US.SPY | S&P 500 ETF | ETF | ✅ |
| US.QQQ | QQQ ETF | ETF | ✅ |

### 4.2 筛选标准链路

```
盘前计划 (premarket_planner)
  → 生成 targets (3-5只) + restricted 列表
  → 保存到 daily_battle_plan_us.json

盘中扫描 (signal_pipeline)
  → 遍历 scheduler.watchlist 或 polling_controller batch
  → 作战计划门禁检查 (USBattlePlanManager.check_trade)
  → 宏观闸门 (VIX>30 或 SPY<MA200 → 直接拒绝)
  → 财报拦截 (blackout)
  → 信号生成 → 共识/强度/置信度过滤
  → 风控检查 → 下单
```

### 4.3 ✅ 筛选标准在链路中保持一致

- watchlist 作为基础股票池
- 盘前计划进一步筛选为 targets
- 盘中执行通过 battle_plan_manager 验证
- 多层门禁（宏观/财报/风控）逐层过滤

### 4.4 ⚠️ LOW: ETF 白名单 bypass

`USBattlePlanManager.check_trade` 中:
```python
ETF_WHITELIST = {"US.SPY", "US.QQQ", "US.DIA", "US.IWM", "US.UVXY"}
```

这些 ETF 即使不在当日 targets 中也能通过门禁。但 `DIA`, `IWM`, `UVXY` **不在 watchlist 中**，理论上不会被扫描到，白名单存在但不影响实际行为。

---

## 五、盘前计划 → 盘中执行衔接审计

### 5.1 数据流

```
premarket_planner.run()
  → LLM 分析 → 生成 JSON plan
  → 保存到 data/daily_battle_plan_us.json
  → 更新 dragonhead_pool regime + strength

盘中扫描:
  → USBattlePlanManager.load() 读取 plan
  → 快车道检查: 价格在 entry_zone 内 → 跳过投票直接下单
  → 非快车道: check_trade() 门禁
```

### 5.2 ✅ 快车道设计合理

当作战计划中的标的价格进入 entry_zone 时:
1. 方向匹配检查
2. RSI 合理性警告（做多 RSI>60 或做空 RSI<40 时标注）
3. 风险状态机 → 资本安全 → 风控 → 下单
4. **跳过投票/LLM**，减少延迟

### 5.3 ⚠️ MEDIUM: 盘前计划输出与执行输入的数据差异

| 盘前计划输出 | 执行层使用 | 差异 |
|-------------|-----------|------|
| `market_regime`: Risk-On/Risk-Off/Neutral | `MarketRegime`: TREND_UP/RANGE/HIGH_VOL | **需要映射转换** |
| `entry_zone`: [low, high] | 直接用于价格匹配 | ✅ 一致 |
| `status`: ACTIVE/CANCELLED | 用于门禁 | ✅ 一致 |
| `direction`: LONG/SHORT | `Direction.LONG/SHORT` | ✅ 一致 |

盘前计划在第 284-292 行做了 regime 映射:
```python
regime_map = {
    "Risk-On": MarketRegime.RISK_ON,
    "Risk-Off": MarketRegime.RISK_OFF,
    "Neutral": MarketRegime.ROTATION,
}
```

但 `MarketRegime.RISK_ON`, `RISK_OFF`, `ROTATION` 与 `MarketRegimeIdentifier` 返回的 `TREND_UP`, `TREND_DOWN`, `RANGE` **是不同枚举集**。如果 `core/models.py` 中的 `MarketRegime` 枚举同时包含这两组值则没问题，否则可能存在不匹配。

### 5.4 ⚠️ MEDIUM: 过期计划降级行为

计划过期 (>24h) 时，`_activate_all_watchlist()` 将所有 watchlist 标的加入 targets，方向为 `ANY`。这意味着:
- 过期后**所有 watchlist 标的均可交易**
- 不再受 LLM 盘前分析的限制
- 这是一个合理的兜底策略，但需确保不会在过期计划下过度交易

### 5.5 ⚠️ MEDIUM: 计划外标的后门机制

```python
# USBattlePlanManager.check_trade
# 计划外标的 → 技术面高评分后门
# 做多: strength ≥ 65
# 做空: strength ≤ 35 (effective ≥ 65)
```

这个后门允许**不在当日计划中的股票**在高信号强度下交易。设计意图是捕捉突发机会，但需注意:
- 后门强度阈值 65 低于盘中有效强度过滤阈值 (50)
- 与盘前计划的"只交易精选标的"理念存在张力

---

## 六、策略信号被错误拦截或放过的路径

### 6.1 🔴 CRITICAL: 三大策略模块的信号在盘中从未被使用

**最严重的问题**: `strategies/` 模块中的三个策略（ma_cross, price_break, rsi_rebound）在**盘中实际执行管线中未被调用**。

实际路径:
```
_process_symbol_impl → _generate_technical_signal → IntradayScorer.score()
```

`IntradayScorer` 使用的是**自有的 6 因子体系**（趋势/动量/量价/订单流/波动率/时段），而不是调用三大策略模块。

**影响**:
1. 策略模块的代码维护成本被浪费
2. `SignalGenerator.load_builtin_strategies()` 加载的三大策略永远不会在盘中被调用
3. `SignalDeduplicator` 和 `ConsensusEngine` 也是死代码
4. 正交化器 (`StrategyOrthogonalizer`) 仅在 `ConsensusEngine` 中使用 → **实际上也不在交易路径中**
5. `config/decision/strategy_weights` 配置的值**不会被盘中执行使用**

### 6.2 ⚠️ HIGH: RSI 阈值三重定义不一致

| 位置 | 超卖阈值 | 超买阈值 | 做空触发 |
|------|---------|---------|---------|
| rsi_rebound 策略 | RSI < 30 | RSI > 70 | RSI > 70 做空 |
| signal_pipeline 做空触发 | - | - | RSI > 85 或 (RSI>70 + MACD<0) |
| intraday_scorer 动量因子 | RSI < 40 看多 | RSI > 60 看空 | RSI>60 累积 bear_score |

三个模块对"什么是超买/超卖"的定义不同。虽然 `rsi_rebound` 策略不在主路径中，但 `intraday_scorer` 和 `signal_pipeline` 的阈值不一致可能导致:
- `signal_pipeline` 的做空触发更严格（RSI>85）
- `intraday_scorer` 在 RSI>60 就开始累积看空分数
- 同一个 RSI=75 的信号在两个模块中可能被不同处理

### 6.3 ⚠️ MEDIUM: 信号生成中 strategy_id 硬编码

`_build_signal_from_vote` 中:
```python
signal = SignalScore(
    strategy_id="voting_llm",  # ← 硬编码
    ...
)
```

快车道中:
```python
signal = SignalScore(
    strategy="BATTLE_PLAN_FAST_PATH",  # ← 使用 strategy 而非 strategy_id
    ...
)
```

字段名不一致 (`strategy_id` vs `strategy`)，可能导致下游 `SignalScorer` 的正交化器和策略权重查找失败（因为查 `strategy_id="BATTLE_PLAN_FAST_PATH"` 不存在于权重表中）。

### 6.4 ⚠️ MEDIUM: 做空信号 strength 计算逻辑

在信号强度过滤中:
```python
effective_strength = signal.strength if direction == "LONG" else (100 - signal.strength)
```

这意味着做空信号的 strength 越高（越接近 100），effective_strength 反而越低。但 `_build_signal_from_vote` 中 strength 来自 `vote_result["final_strength"]`，其含义是"偏向该方向的强度"。

如果投票结果是 SHORT，final_strength=80 表示强烈的做空信号，但 `effective_strength = 100 - 80 = 20`，**低于阈值 50 会被过滤掉**。

**这是逻辑 BUG**。做空信号需要 final_strength ≤ 35 才能通过过滤（effective ≥ 65），但这与 strength 的语义（越高越强）矛盾。

### 6.5 ✅ 宏观闸门设计合理

```
VIX > 30 → 拒绝开仓
SPY < MA200 → 拒绝开仓
```

两层宏观保护，防止在极端市场环境下开仓。

---

## 七、总结与建议

### 7.1 问题严重程度汇总

| 严重度 | 编号 | 问题 |
|--------|------|------|
| 🔴 CRITICAL | 6.1 | 三大策略模块、SignalDeduplicator、ConsensusEngine、StrategyOrthogonalizer 不在交易执行路径中 |
| 🔴 CRITICAL | 6.4 | 做空信号 effective_strength 计算 BUG（100-strength 导致强做空被过滤） |
| ⚠️ HIGH | 1.3 | 正交化器中声明但未实现的策略（macd/supertrend/bollinger/rsi 等） |
| ⚠️ HIGH | 3.2 | signal_scorer 中 `"HIGH_VOLATILITY"` 与模型 `"HIGH_VOL"` 枚举不一致 |
| ⚠️ HIGH | 6.2 | RSI 阈值在三处定义不一致 |
| ⚠️ MEDIUM | 2.2 | SignalDeduplicator 是死代码 |
| ⚠️ MEDIUM | 2.3 | 冲突检测 strength>70 阈值不对称 |
| ⚠️ MEDIUM | 3.4 | MarketRegimeIdentifier 与 IntradayScorer 两套独立 regime 体系 |
| ⚠️ MEDIUM | 5.3 | 盘前计划 regime (Risk-On/Off) 与执行层 regime 映射需确认 |
| ⚠️ MEDIUM | 5.5 | 计划外标的后门阈值 65 低于盘中强度阈值 |
| ⚠️ MEDIUM | 6.3 | SignalScore 字段名不一致 (strategy_id vs strategy) |

### 7.2 修复建议优先级

**P0 (立即修复)**:
1. 修复做空信号 effective_strength 计算逻辑 (6.4)
2. 修复 signal_scorer 中 HIGH_VOLATILITY → HIGH_VOL 的枚举不一致 (3.2)
3. 明确策略模块的定位：要么整合到 IntradayScorer 中，要么从执行路径中移除

**P1 (短期修复)**:
4. 清理或启用 SignalDeduplicator / ConsensusEngine / StrategyOrthogonalizer
5. 统一 RSI 阈值定义
6. 修复 SignalScore 字段名不一致
7. 确认 regime 枚举映射一致性

**P2 (中期优化)**:
8. 评估计划外标的后门阈值的合理性
9. 统一 MarketRegimeIdentifier 与 IntradayScorer 的 regime 体系
10. 清理未实现的策略声明

---

*审计完成*
