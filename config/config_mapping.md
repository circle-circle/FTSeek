# FTSeek 配置映射文档 (SSOT)

**最后更新**: 2026-05-20  
**用途**: 明确 `app/core/config_defaults.py` 与 `config/unified_config.yaml` 的映射关系，防止配置漂移。

---

## 规则

1. **unified_config.yaml** = 运行时配置 SSOT (所有业务逻辑从此读取)
2. **config_defaults.py** = 代码默认值 SSOT (所有 `config.get(key, default)` 的 fallback)
3. **修改配置时**: 必须同时更新两者
4. **验证**: `scripts/config_audit.py` 自动检查一致性

---

## 配置映射表

### 风控 - 置信度

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `MIN_CONFIDENCE` | `risk_limits.min_confidence` | `0.45` | 信号最低置信度门槛 |
| `DECISION_BUFFER_MIN_CONFIDENCE` | `strategy.decision_buffer.min_confidence` | `0.45` | 决策缓冲最低置信度 |
| `CONFIDENCE_ADJUSTER_MIN_CONFIDENCE` | `strategy.confidence_adjuster.min_confidence` | `0.45` | 置信度调整器最低门槛 |

### 决策缓冲层

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `DECISION_BUFFER_SIGNAL_COOLDOWN` | `strategy.decision_buffer.signal_cooldown` | `120` | 信号冷却秒数 |
| `DECISION_BUFFER_MAX_PENDING` | `strategy.decision_buffer.max_pending` | `500` | 最大待处理信号数 |
| `DECISION_BUFFER_MAX_POSITIONS` | `strategy.decision_buffer.max_positions` | `null` | null=动态计算 |

### 置信度调整器

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `CONFIDENCE_ADJUSTER_MAX_SINGLE_PENALTY` | `strategy.confidence_adjuster.max_single_penalty` | `0.15` | 单因子最大惩罚 |
| `CONFIDENCE_ADJUSTER_MAX_TOTAL_PENALTY` | `strategy.confidence_adjuster.max_total_penalty` | `0.40` | 总惩罚上限 |
| `CONFIDENCE_ADJUSTER_MAX_SINGLE_BOOST` | `strategy.confidence_adjuster.max_single_boost` | `0.10` | 单因子最大增强 |
| `CONFIDENCE_ADJUSTER_MAX_TOTAL_BOOST` | `strategy.confidence_adjuster.max_total_boost` | `0.20` | 总增强上限 |

### 仓位 & 亏损限制

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `MAX_SINGLE_POSITION` | `risk_limits.max_single_position` | `0.08` | 单仓位最大占比 8% |
| `MAX_TOTAL_POSITION` | `risk_limits.max_total_position` | `0.75` | 总仓位最大占比 75% |
| `MAX_DAILY_TRADES` | `risk_limits.max_daily_trades` | `5` | 每日最大交易次数 |
| `MAX_DAILY_LOSS` | `risk_limits.max_daily_loss` | `0.03` | 日亏损上限 3% |
| `MAX_WEEKLY_LOSS` | `risk_limits.max_weekly_loss` | `0.08` | 周亏损上限 8% |
| `MAX_MONTHLY_LOSS` | `risk_limits.max_monthly_loss` | `0.15` | 月亏损上限 15% |
| `MAX_DRAWDOWN` | `risk_limits.max_drawdown` | `0.12` | 最大回撤 12% |
| `CONSECUTIVE_LOSS_LIMIT` | `risk_limits.consecutive_loss_limit` | `3` | 连续亏损次数触发暂停 |
| `STRATEGY_MIN_WINRATE` | `risk_limits.strategy_min_winrate` | `0.45` | 策略最低胜率 |

### 止损 / 止盈

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `DEFAULT_STOP_LOSS_PCT` | `stop_loss.default_percent` | `0.03` | 默认止损百分比 |
| `DEFAULT_TAKE_PROFIT_PCT` | `take_profit.default_percent` | `0.10` | 默认止盈百分比 |
| `TRAILING_STOP_PCT` | `stop_loss.trailing_percent` | `0.02` | 移动止损百分比 |
| `TRAILING_TP_PCT` | `take_profit.trailing_percent` | `0.02` | 移动止盈百分比 |
| `MAX_HOLDING_HOURS` | `stop_loss.max_holding_hours` | `48` | 最大持仓小时 |
| `ATR_MULTIPLIER` | `stop_loss.atr_multiplier` | `2.5` | ATR 止损乘数 |
| `BREAKEVEN_TRIGGER` | `take_profit.breakeven.trigger_profit` | `0.03` | 保本触发利润 |
| `BREAKEVEN_LEVEL` | `take_profit.breakeven.stop_level` | `0.01` | 保本止损位 |

### 共识引擎

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `CONSENSUS_WINDOW` | `strategy.consensus.window_seconds` | `90` | 共识窗口秒数 |
| `CONSENSUS_THRESHOLD` | `strategy.consensus.threshold` | `1.5` | 共识阈值 (需 >1.5 因子确认) |
| `CONSENSUS_ADX_TREND` | `strategy.consensus.adx_trend` | `25` | ADX 趋势判定阈值 |
| `CONSENSUS_ADX_RANGE` | `strategy.consensus.adx_range` | `20` | ADX 震荡判定阈值 |

### 信号冷却

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `SIGNAL_COOLDOWN` | `strategy.cooldown.signal_cooldown` | `120` | 信号冷却秒数 |

### 趋势过滤

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `TREND_FILTER_MIN_ADX` | `strategy.trend_filter.adx_range_gate` | `20` (CFG) / `18` (YAML) | ⚠️ 轻微漂移: CFG=20, YAML=18 |
| `TREND_FILTER_USE_MULTI_TIMEFRAME` | `strategy.trend_filter.use_multi_timeframe` | `true` | 启用多时间框架 |
| `TREND_FILTER_PREVENT_BUY_IN_DOWNTREND` | `strategy.trend_filter.prevent_buy_in_downtrend` | `true` | 下跌趋势拦截买入 |
| `TREND_FILTER_PREVENT_SHORT_IN_UPTREND` | `strategy.trend_filter.prevent_short_in_uptrend` | `true` | 上涨趋势拦截做空 |

### 入场价格检查

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `ENTRY_PRICE_HIGH_BUY_THRESHOLD` | `entry_price_checker.high_buy_warn` | `0.005` (CFG) / `0.01` (YAML) | ⚠️ 漂移: CFG=0.5%, YAML=1% |
| `ENTRY_PRICE_CHASE_WARNING_THRESHOLD` | `entry_price_checker.chase_threshold` | `0.03` | 追高警告阈值 |
| `ENTRY_PRICE_CHASE_BLOCK_THRESHOLD` | `entry_price_checker.panic_threshold` | `0.05` (CFG) / `0.03` (YAML) | ⚠️ 漂移: CFG=5%, YAML=3% |

### 熔断器

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `CIRCUIT_BREAKER_REJECT_LIMIT` | `risk_limits.circuit_breaker.max_reject_count` | `8` | 最大拒绝次数触发熔断 |
| `CIRCUIT_BREAKER_PAUSE_DURATION` | `risk_limits.circuit_breaker.pause_duration_sec` | `300` | 熔断暂停秒数 |
| `CIRCUIT_BREAKER_DRAWDOWN_LIMIT` | `risk_limits.circuit_breaker.daily_loss_halt` | `0.15` (CFG) / `0.03` (YAML) | ⚠️ 漂移: CFG=15%, YAML=3% (YAML 更保守) |

### 硬编码熔断守卫 (HardStopGuard)

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `HARD_STOP_DRAWDOWN_LIMIT` | `circuit_breaker.daily_loss_pct` | `0.03` | 日内回撤 3% 硬止损 |
| `HARD_STOP_CONSECUTIVE_LOSSES` | `loss_pause.consecutive_losses` | `3` | 连续亏损次数 |

### LLM 交易

| config_defaults.py | unified_config.yaml 路径 | 当前值 | 说明 |
|---|---|---|---|
| `LLM_TRADING_MIN_CONFIDENCE` | `smart_push.auto_execute_min_confidence` | `0.60` | LLM 自动执行最低置信度 |
| `LLM_TRADING_MANUAL_CONFIRM_MIN_CONFIDENCE` | `smart_push.manual_confirm_min_confidence` | `0.75` | 人工确认最低置信度 |

---

## 已知漂移项 (需决策)

| # | 参数 | CFG 值 | YAML 值 | 建议 |
|---|------|--------|---------|------|
| 1 | `TREND_FILTER_MIN_ADX` | 20 | 18 | 统一为 **18** (YAML 更敏感，已验证) |
| 2 | `ENTRY_PRICE_HIGH_BUY_THRESHOLD` | 0.005 | 0.01 | 统一为 **0.01** (YAML 更宽松，减少误拦截) |
| 3 | `ENTRY_PRICE_CHASE_BLOCK_THRESHOLD` | 0.05 | 0.03 | 统一为 **0.03** (YAML 更保守) |
| 4 | `CIRCUIT_BREAKER_DRAWDOWN_LIMIT` | 0.15 | 0.03 | 统一为 **0.03** (YAML 是日内回撤，CFG 是总回撤，语义不同，需保留两者) |

---

## 审计脚本

- `scripts/config_audit.py` — 自动验证 config_defaults.py 与 unified_config.yaml 一致性
- `scripts/config_drift_detector.py` — 检测配置漂移 (watchlist、env vars、YAML 完整性)
- `scripts/config_audit_sentinel.py` — 配置审计哨兵 (记录历史)
