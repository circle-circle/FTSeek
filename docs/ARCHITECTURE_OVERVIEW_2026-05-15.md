# FTSeek-US 系统架构与交易链路总览

> **日期**: 2026-05-15
> **范围**: app/ 全部模块 + scripts/ + tests/ + config/
> **运行态**: 模拟盘 (TRADE_ENV=1) — Futu OpenD + Phase A 多策略协调 + Phase B.minimal 已实施

---

## 0. 一图全景

```
┌──────────────────────────────────────────────────────────────────────────┐
│                       main.py (DI 容器装配 + 启动守护)                    │
│  • OpenD 连通预检  • TRADE_ENV 安全闸  • 进程锁 fcntl                      │
│  • DI: EventStore / ExecutionEngine / RiskEngine / Notifier              │
│  • Boot: AgentBridge / BattlePlan / ConfigGov / DailyActivePool          │
│  • 后台: PositionSync (30s) / EmergencyInterceptor / IntradayAttribution │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   TradingScheduler (app/core/scheduler_core.py)          │
│  • _init_coordinator → Phase A (CapitalAllocator / SymbolLockManager /   │
│    SignalArbitrator / ExitDispatcher) — observe_only 灰度                │
│  • _load_scheduler_params → sandbox.overrides 优先                       │
│  • Watchdog 心跳 (scan_loop + monitor_loop)                              │
│  • scan_loop  ─→ app/pipeline/signal_pipeline.py                         │
│  • monitor_loop─→ app/pipeline/position_monitor.py                       │
└──────┬──────────────────┬───────────────────────────────────┬────────────┘
       │                  │                                   │
       ▼                  ▼                                   ▼
┌──────────────┐  ┌──────────────────────┐         ┌─────────────────────┐
│ Data 层      │  │ Signal Pipeline      │         │ Position Monitor    │
│ market_data_ │  │ (信号扫描 + 决策管线)│         │ (持仓监控 + 退出)   │
│ router       │  │  → voting_coordinator│         │  → position_exit_   │
│ ↳ futu_lv3   │  │  → debate / consensus│         │    monitor (6 规则) │
│ ↳ twelvedata │  │  → SmartRelevance    │         │  → ExitDispatcher   │
│ ↳ finnhub    │  │  → SignalScore       │         │    (按 owner_strategy│
│ ↳ alphavantg │  │  → RiskEngine        │         │     路由 ExitPolicy)│
│ news_v2:     │  │    .pre_trade_check  │         │  → SL monotonic     │
│ ↳ cailianpr. │  │  → SignalArbitrator  │         │    guard            │
│ ↳ wallstreetcn│ │    (priority+conf)   │         │  → SymbolDailyLoss  │
│ ↳ eastmoney  │  │  → CapitalAllocator  │         │    Guard            │
│ ↳ rsshub_us  │  │  → SymbolLockManager │         │  → PnLOutlier (5σ)  │
│ ↳ sec_edgar  │  │  → order_executor    │         │  → close → notifier │
└──────────────┘  └──────────┬───────────┘         └─────────────────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ Order Executor       │
                  │  → futu_integration  │
                  │  → thesis_inferer    │  (异步, fire-and-forget)
                  │  → shadow_mirror x4  │  (1:1 同源镜像)
                  │  → trade_attribution │  (30 列归因)
                  │  → notifier (飞书/钉)│
                  └──────────────────────┘
```

---

## 1. Entry & Boot 层

### 1.1 `main.py` (472 行)
**职责**: DI 容器装配 + 启动期守护 + 后台线程编排.
- **启动闸**:
  - `fcntl.flock` 进程锁 `/tmp/ftseek_us.lock` (单实例) — `main.py:48-75`
  - OpenD 连通性预检 (5s socket connect, 失败 sys.exit) — `main.py:78-92`
  - `TRADE_ENV` 强制 ==1 (模拟盘), `==0` 立即拒启 — `main.py:94-107`
- **DI 装配**: `app/core/di_container.py` 构建 `scheduler / execution_engine / risk_engine / notifier / event_store` — `main.py:109-167`
- **Boot 阶段**:
  1. AgentBridge (盘前/盘后 LLM 闭环) — `app/llm/agent_bridge.py`
  2. Hermes Worker 预检 (Python 3.11 子进程) — `app/llm/hermes_client.py`
  3. BattlePlan 加载 + F3 重启校验 (失败→ RISK_OFF) — `app/us_pipeline/battle_plan_manager.py`
  4. ConfigGov 三层防御:
     - Layer 1: `ConfigValidator.validate_all()` (启动校验)
     - Layer 1.5: `validate_exit_policy()` (退出规则覆盖)
     - Layer 1.6: `validate_short_config()` (做空配置完整性)
     - Layer 1.7: `print_config_audit()` (yaml vs CFG 默认值漂移)
     - Layer 2: `ConfigDriftDetector` (4h 巡检)
     - Layer 3: `ConfigHotReloader` (60s 冷却热重载)
  5. DailyActivePool v2 — `app/utils/daily_active_pool.py`
  6. EmergencyInterceptor 后台 thread — `app/us_pipeline/emergency_interceptor.py`
  7. PositionSync 30s loop (Futu broker → PositionStore) — `app/core/position_sync.py`
  8. P1-C: 关键告警通道检测 (DingTalk/Feishu webhook 至少 1 个)
  9. P0-X: SymbolDailyLossGuard `restore_from_db()` 当日状态恢复
  10. P0-E3: OrderTracker `restore_pending_on_startup()` 防孤儿
  11. IntradayAttribution 60min 报告
- **关闭流程** `_graceful_shutdown` — `main.py:114-154`: scheduler.stop → ConfigGov.stop → event_store.close → data_router.close → cleanup_lock → notifier.send_text → `os._exit(0)`

### 1.2 `app/bootstrap/app_bootstrap.py`
DI 容器辅助, 按需返回单例 (EventStore/Execution/Risk/Notifier).

### 1.3 `app/services/service_manager.py`
后台服务统一启停 (PositionSync / IntradayAttribution / EmergencyInterceptor 的注册中心).

---

## 2. 调度核心 (`app/core/scheduler_core.py`, 1531 行)

### 2.1 `PositionStore` — 内存持仓管理 (`scheduler_core.py:48-97`)
- `RLock` 保护 `_positions: Dict[str, Position]` (允许 sync 线程重入)
- 自动同步 `daily_active_pool` (add/remove)
- API: `add / get / remove / all / has / count`

### 2.2 `TradingScheduler` 主类 (`scheduler_core.py:100-1500+`)
**默认参数**:
- `SCAN_INTERVAL_MINUTES=10`, `MONITOR_INTERVAL_MINUTES=0.5`, `MAX_POSITIONS=5`
- `MIN_SIGNAL_STRENGTH=50`, `MIN_CONFIDENCE=0.50`, `MIN_CONSENSUS_LEVEL=0.20`

**关键方法**:
- `_init_coordinator()` — Phase A 协调层装配 (`scheduler_core.py:618-719`):
  ```
  CapitalAllocator(caps={main:0.65, scalping:0.25, llm_plan:0.0},
                   max_total_pct=0.90, observe_only=true,
                   bucket_map={fast_lane→main})
  SymbolLockManager(observe_only=true)
  SignalArbitrator(audit_db=decisions.db, audit_enabled=true)
  ExitDispatcher().register("main"|"scalping"|"fast_lane"|"llm_plan", ...)
  Watchdog.register("scan_loop"/"monitor_loop", stale=5×interval)
  ```
- `_load_scheduler_params()` — 配置加载 (sandbox.overrides 优先) (`scheduler_core.py:721-799`)
- `run_forever()` — 启动 scan_thread + monitor_thread (`scheduler_core.py:956`)
- `trigger_immediate_scan(symbol, reason)` — 突发事件即时扫描 (`scheduler_core.py:1126`)
- `force_scan(ignore_market_hours)` — CLI/手动触发
- `_is_market_open()` / `_is_hk_trading_hours()` — 美东+港股时段判断

### 2.3 `app/core/scheduler_tasks.py` (107 行) — DEPRECATED 桩
全部转发到 `app/pipeline/*`, 兼容旧导入.

### 2.4 `app/core/scheduler_triggers.py` (1039 行)
**MarketPhase**: `PRE_MARKET / OPEN / INTRADAY / PRE_CLOSE / AFTER_HOURS / CLOSED`
- `check_daily_review()` — 收盘后约 04:00 触发, 调 `_trigger_eod_agent_review` 把 daily_review 喂给 LLM
- `check_weekly_review()` / `check_monthly_review()`
- `check_position_snapshot()` — 持仓 snapshot 落库
- `_format_daily/weekly/monthly_review()` — 飞书 markdown 格式化

---

## 3. 数据层 (`app/data/`)

### 3.1 行情路由 `app/data/market_data_router.py`
Futu LV3 优先 → twelvedata+finnhub fallback → alphavantage 兜底.
- `subscription_manager.py` — 分时段订阅 (盘前 vs 盘中 vs 盘后)
- `adaptive_polling.py` — VIX 自适应轮询频率

### 3.2 行情 Provider (5 家)
| Provider | 用途 | 限速 | TTL |
|---------|-----|-----|-----|
| futu_lv3_provider | 1000 槽位 (与港股共享) | — | 实时 |
| twelvedata_provider | quote/timeseries/VIX/SPY | 8/min | 300/1800/600/900s |
| finnhub_provider | quote + news + sentiment | 60/min | 60/7200/3600s |
| alphavantage_provider | fallback | 5/min, 500/day | 300/1800s |
| yahoo_provider | 兜底 (无 key) | — | — |

### 3.3 财报日历 `app/data/earnings_calendar.py`
财报前后 3 日触发 `EarningsRiskGuard`.

### 3.4 拆股 `app/data/split_event_guard.py`
检测股息/拆股, 防止 stop-loss 跳变误平仓.

### 3.5 lv3 技术计算 `app/data/lv3_technical_calculator.py`
ATR / RSI / VWAP / Bollinger / MACD 实时计算.

### 3.6 新闻提供者
- `app/data/news_provider.py` — `USNewsProvider` 顶层
- `app/data/newsnow_us_news.py` — newsnow 聚合
- `app/data/rsshub_us_news.py` + `rsshub_health.py` — RSSHub 健康监控

---

## 4. 新闻 V2 (`app/news_v2/`)

### 4.1 Pipeline `app/news_v2/pipeline.py`
- `QualityGate.evaluate(item)` — DedupV2 → RelevanceFilter → ClassifierLLM
- `NewsPipelineV2.run_once()` — 多 source 聚合 + 阈值过滤
- 30s 轮询 / 异步 fetch

### 4.2 Source 适配器 (6 家)
| Source | 文件 | 类型 |
|-------|-----|-----|
| Cailianpress (财联社) | `sources/cailianpress_source.py` | 大陆友好 |
| WallstreetCN (华尔街见闻) | `sources/wallstreetcn_source.py` | 大陆友好 |
| Eastmoney US (东财美股) | `sources/eastmoney_us_source.py` | 大陆友好 |
| Finnhub | `sources/finnhub_news_source.py` | 海外 |
| RSSHub US | `sources/rsshub_us_source.py` | 海外 |
| SEC EDGAR | `sources/sec_edgar_source.py` | 监管文件 |

每个 source 实现 `async fetch() → List[UnifiedNewsItemV2]`, `_extract_tickers`, `_infer_urgency` (IMMEDIATE/HIGH/NORMAL/LOW).

### 4.3 质量层 `app/news_v2/quality/`
- `dedup_v2.py` — 标题相似度 + url 去重
- `embedding_matcher.py` — 向量近似匹配 (语义去重)
- `relevance_filter.py` — 关键词阈值
- `smart_relevance.py` — 智能相关性引擎 (语境消歧):
  - `TickerDictionary` (`ticker_dictionary.py`) — 8000+ ticker 词典 + 同义词 + 语境消歧 (例: "苹果汁" vs "苹果发布iPhone")
  - 行业传染传播 (sector propagation)
- 日语检测 (假名优先于汉字)

### 4.4 处理层 `app/news_v2/processor/`
- `llm_classifier.py` — L1 LLM 分类 (sentiment / urgency / impact)
- `event_signal_generator.py` — L9 新闻→ 信号 (依赖 L1)
- `news_attribution.py` — 把新闻关联到具体 trade

---

## 5. 信号生成与决策 (`app/pipeline/signal_pipeline.py`, 1397 行)

### 5.1 主循环 `scan_loop(scheduler)` (`signal_pipeline.py:38`)
- 轮询间隔 `_get_scan_wait_seconds()` (盘中=SCAN_INTERVAL_MINUTES, 其他时段长间隔)
- watchdog `_heartbeat("scan_loop")` 每轮一次
- 调 `_run_signal_scan_impl(scheduler)`

### 5.2 `_run_signal_scan_body()` (`signal_pipeline.py:298`)
1. 取 VIX / SPY context / market_news
2. `_get_dynamic_pool_filter()` — 当日动态池 tier 选择 (micro/small/mid/large)
3. `_apply_pool_filter()` 过滤 watchlist
4. 对每 symbol 并行 `_process_symbol_impl()`

### 5.3 `_process_symbol_impl()` (`signal_pipeline.py:720`, ~470 行)
**核心入口**, 完整管线:
1. **Sandbox universe 过滤** (mode.sandbox.enabled=true 时仅交易 universe 内 8 标的)
2. **冷却检查** `_is_in_cooldown(symbol)`
3. **行情快照** + `pre-flight cache check`
4. **News pull** 标的相关新闻 + `news_classifier` 分类
5. **VotingCoordinator** 多模型投票 — `app/agents/voting/voting_coordinator.py`
6. **Debate** (可选) — `app/llm/iterative_debate.py` / `dual_agent_debate.py`
7. **ConsensusEngine** — `app/decision/consensus.py`
8. **`_build_signal_from_vote()`** → `SignalScore` (with `position_pct`, `stop_loss_pct`, `metadata`)
9. **`scheduler.risk_engine.pre_trade_check(signal)`** ← 单一闸门 SoT (R1 修复, signal_pipeline.py:896,1165)
10. **SignalArbitrator** — 多 candidate 仲裁 (priority+confidence)
11. **CapitalAllocator.try_reserve()** + **SymbolLockManager.acquire()**
12. **`_execute_long/short/option`** — 路由到 `app/pipeline/order_executor.py`

### 5.4 `_build_signal_from_vote()` (`signal_pipeline.py:1188`)
- 把投票结果转为 `SignalScore` dataclass
- 注入 `metadata` (regime, news_summary, voting_result, _current_positions)
- F0 (`app/utils/datetime_utils.py`): `signal_expires_at()` 按 phase 设置过期时间

### 5.5 投票引擎 `app/agents/voting/`
- `voting_coordinator.py` — `VotingCoordinator.vote(context)` 协调 N 个 model agent
- `model_agent.py` — 单 agent, 调 `app/llm/multi_model_client.py` (qwen / deepseek / minimax)
- `voting_config.py` — yaml 配置

### 5.6 辩论引擎 `app/llm/`
- `iterative_debate.py` — 多轮辩论 (默认 3 轮)
- `dual_agent_debate.py` — 双 agent 对辩
- `disagreement_arbitrator.py` (L5) — 分歧>阈值时再调一个仲裁模型
- `debate_memory.py` — 历史辩论 reference

### 5.7 决策层 `app/decision/`
- `consensus.py` — `ConsensusEngine` 投票汇总
- `signal_dedup.py` — 60min 去重
- `signal_generator.py` / `signal_scorer.py` — 评分加权
- `cost_model.py` — slippage + fee 估算
- `strategy_orthogonalizer.py` — 多策略去相关
- `vote_logger.py` — 投票事件落库 (decisions.db)

---

## 6. 多策略协调层 (Phase A, `app/coordinator/`)

### 6.1 `StrategyCoordinator` (`strategy_coordinator.py`, 307 行)
**门面**: 把 4 个组件粘合, 暴露 `pre_trade_arbitrate / record_position_owner / on_position_closed / reconcile`.
- `enabled` / `observe_only` / `enforce` 三态
- `switch_to_enforce(position_registry)` — Phase B 切换前置必须 `reset_and_reconcile`
- `pre_trade_arbitrate(candidates)` — 主入口 (`strategy_coordinator.py:146`)

### 6.2 `CapitalAllocator` (`capital_allocator.py`, 250+ 行)
- 按 bucket cap 预占资金: `main 0.65 / scalping 0.25 / llm_plan 0.0`
- `bucket_map={fast_lane: main}` — fast_lane 不双计
- API: `try_reserve / release / force_set / snapshot / by_strategy_snapshot / reset_and_reconcile`
- `observe_only=True` 时不阻断仅记录 `BLOCKED_STRATEGY_CAP` 事件

### 6.3 `SymbolLockManager` (`symbol_lock.py`, 160 行)
- 单 symbol 一个 owner_strategy, 防多策略同时下单
- API: `acquire(symbol, strategy) / release / owner / reconcile_with_positions`
- `observe_only=True` 时记录 `OBSERVE_CONFLICT` 事件

### 6.4 `SignalArbitrator` (`signal_arbitrator.py`, 145 行)
- `arbitrate(candidates)` — priority 高者优先, 同 priority 比 confidence
- 写 `signal_arbitration` 表 (decisions.db) — `audit_enabled=True`

### 6.5 `ExitDispatcher` (`exit_dispatcher.py`, 67 行)
- `register(strategy, ExitPolicy)` — 4 个 policy
- `policy_for(owner_strategy)` — 路由
- 在 `position_exit_monitor.py:329-348` 被调用 (优先 dispatcher → fallback 6 规则)

### 6.6 4 个 ExitPolicy (`app/strategies/exit_policies.py`)
| Policy | thesis-aware 规则 |
|-------|------------------|
| `MainPipelineExitPolicy` | event+持仓>48h → CLOSE; breakout+跌0.5% → CLOSE; reversion+浮盈+1.5% → REDUCE 50% |
| `ScalpingExitPolicy` | (R3 SAFETY NET, 实际被 PositionExitMonitor:304 过滤) 持仓>30min → CLOSE |
| `FastLaneExitPolicy` | 浮盈≥1×ATR → REDUCE 50% (1ATR 立即保本) |
| `LLMPlanExitPolicy` | metadata.plan_target/plan_stop 命中 → CLOSE |

无 thesis 时 fallback `NO_ACTION`, 让 `position_exit_monitor` 6 规则接管.

### 6.7 4 个 StrategyAdapter (`app/strategies/adapters/`)
仅声明元数据 (name/priority/min_confidence), Phase A 不接管下单:
- `main_pipeline.py` — priority=5
- `scalping.py` — priority=10 (最高, 时效优先)
- `fast_lane.py` — priority=8 (require_entry_zone, min_confidence=0.7)
- `llm_plan.py` — priority=3

---

## 7. 短线引擎 (`app/scalping/`)

### 7.1 `ScalpingEngine` (`engine.py`, 2000+ 行)
**自管退出**: 不走 `position_exit_monitor` (C2 修复, 2026-05-13).
- `_init_submodules()` — `SignalDetector / MomentumDetector / ScalpingBudget / SymbolLock / TrendFilter / TimeGate / CircuitBreaker / DailyReport / StockSelector / Exits`
- `_run_loop()` 主循环 + `_capital_sync_loop()` 30s 同步 equity
- `_tick()` — 单轮: 选股 → SignalDetector → 风控 → 下单 → 退出监控
- `_check_global_risk() / _is_trending_day() / _can_trade_today()`
- **R1 修复 (2026-05-15)**: 入场前调 `risk_engine.pre_trade_check()` (与主线同闸门)

### 7.2 短线子模块
- `signals.py` — `SignalDetector` (RSI/VWAP/Bollinger/aggregate) + `MomentumDetector`
- `budget.py` — `ScalpingBudget` 单例 (日预算 + 持久化)
- `circuit_breaker.py` — 短线特有熔断
- `daily_report.py` — 短线日报
- `exits.py` — 短线退出 (1.5×ATR / 30min / 结构破位)
- `time_gate.py` — 09:30-10:30 / 14:30-15:30 高活跃时段
- `trend_filter.py` — 趋势日过滤 (避免被趋势打)
- `stock_selector.py` — 短线池筛选

### 7.3 `_ForcedTickerValidator` (`engine.py:30`)
强制纳入的 ticker 表现验证, 表现差自动剔除.

---

## 8. 风险层 (`app/risk/`)

### 8.1 `RiskEngine` 包装层 (`engine.py`, 417 行)
**单一闸门 SoT**. `pre_trade_check(signal)` 流程:
1. `system_breaker.reset_if_expired()` + `system_breaker.is_triggered()`
2. `strategy_breaker.is_triggered(strategy_id)`
3. `symbol_breaker.is_frozen(symbol)`
4. `self.pre_trade_checker.check(signal)` ← 内层 9 步
5. `drawdown_controller.is_daily_limit_breached()`
6. 仓位上限 `position_limits` (sector / total)

附加方法: `intra_trade_check / post_trade_check / check_circuit_breakers / should_exit / _record_rejection`

### 8.2 `PreTradeChecker` 9 步 (`pre_trade_check.py`, 318 行)
1. signal 过期检查 (`is_expired()`)
2. min_confidence + min_strength
3. cooldown
4. 黑名单
5. 涨跌停/价差
6. sector 限制 `_check_sector_limit`
7. 单笔风险 R 在预算内 `_check_trade_risk_within_budget`
8. system_breaker (双重保险)
9. 同向重复信号

### 8.3 熔断器 (`circuit_breaker.py`, 350+ 行)
- `StrategyCircuitBreaker` — 策略级 (连续 N 笔亏损 / 日内胜率<阈值)
- `SymbolCircuitBreaker` — 标的级冷冻
- `SystemCircuitBreaker` — 系统级 (大盘崩盘 / 数据源断 / 高错误率)
- `DataSourceHealth` — 数据源健康
- `TradingFrequencyLimiter` — 频率限速

### 8.4 `PositionExitMonitor` 6 规则 (`position_exit_monitor.py`, 1676 行)
**主线退出引擎**. `scan_positions()` 流程:
1. 过滤 scalping 仓位 (C2 修复, line 304)
2. ExitDispatcher 优先决断 (P0-2)
3. 6 规则 fallback:
   - `_check_flash_exit` — 急跌闪退 (1分钟内 -2%)
   - `_check_day_trade_exit` — 日内强平 (PDT 防护)
   - `_check_scale_out` — 分批减仓
   - `_check_time_stop` — 持仓时间止损
   - `_check_pre_close_exit` — 收盘前 30min 平仓
   - `_check_trailing_stop` — Chandelier 追踪止损
4. **CR-3 SL monotonic guard**: LONG 只能 max(_old, _new), SHORT 只能 min — 防 chandelier 保利被 ATR 重算放松
5. `_update_trail_state` — 更新 trail_state (高水位/低水位/锁利档)
6. `_execute_exit` — 写 `exit_reason` + 调 `execute_close_fn`

### 8.5 单标的日损守门 (`symbol_daily_loss_guard.py`, 250 行)
**P0-X**. 三触发器:
- `max_daily_loss_pct: 0.005` (净值 0.5%)
- `max_consecutive_losses: 2`
- `max_loss_rate: 0.66` (3 笔以上时)

API: `record_pnl / is_blocked / restore_from_db / set_equity_provider`. 重启恢复当日状态.

### 8.6 PnL 异常监控 (`pnl_outlier_monitor.py`, 145 行)
**Y5**. 30 天滚动 mean/std, 5σ 触发告警 (例如 ORCL +$17,882 单笔).

### 8.7 市场 Regime 检测 (`market_regime_detector.py`, 220 行)
**Y1**. ADX + VIX + SPY 联合分类:
- `EXTREME / TRENDING_UP / TRENDING_DOWN / RANGING / HIGH_VOL_CHOP / TRANSITION / UNKNOWN`
- `get_position_scale(regime)` — 仓位缩放系数

### 8.8 其他风控
| 模块 | 文件 | 用途 |
|-----|-----|-----|
| `DrawdownController` | `drawdown_control.py` | 日内/周/月回撤上限 |
| `EarningsRiskGuard` | `earnings_risk_guard.py` | 财报前后 3 日紧守 |
| `PDTGuard` | `pdt_guard.py` | 美股日内交易 PDT 规则 |
| `MarginGuard` | `us_margin_guard.py` | Reg-T margin 检查 |
| `OptionRiskMonitor` | `option_risk_monitor.py` | 期权 greek 监控 |
| `SectorLimits` | `sector_limits.py` | 行业敞口上限 |
| `SectorLeader` | `sector_leader.py` | 行业龙头加权 |
| `VIXMonitor` | `vix_monitor.py` | VIX 自适应 |
| `MarketCircuitBreaker` | `market_circuit_breaker.py` | SPY 大跌 → 全市场冻结 |
| `PriceMomentumGate` | `price_momentum_gate.py` | 进场前动能门 |
| `ATRStopEngine` | `atr_stop_engine.py` | ATR 止损计算 |
| `PositionDropAlert` | `position_drop_alert.py` | 持仓异常下跌告警 |
| `LLMEmergencyReview` | `llm_emergency_review.py` | L2 紧急 LLM 复核 |
| `OrderTracker` | `order_tracker.py` | pending 订单追踪 + 启动恢复 |
| `ExitPolicyGuardian` | `exit_policy_guardian.py` | exit_policy.yaml 启动校验 |
| `RiskMerger` | `risk_merger.py` | 多源风险合并 |
| `CapitalSafety` | `capital_safety.py` | 资金安全总闸 |

---

## 9. 执行层 (`app/execution/` + `app/pipeline/order_executor.py`)

### 9.1 `app/pipeline/order_executor.py` (2611 行)
**核心入场**:
- `_execute_long()` — 多头 (`order_executor.py:1249`)
- `_execute_short()` — 空头入口 (`order_executor.py:1063`) 路由到:
  - `_try_direct_short_only` / `_try_direct_short_with_fallback`
  - `_execute_stock_short` (融券做空)
  - `_submit_put_option` (Put 期权代替直接做空)
- `_close_position()` (`order_executor.py:2059`) — 主退出
- `_close_position_for_conflict` / `_close_short_for_long` / `_close_long_for_short`

**入场后 hook** (PB-2):
- `infer_thesis_async(symbol, signal_dict, context)` — L3 异步 fire-and-forget, 写 `trade_attribution.entry_thesis`
- `shadow_mirror.mirror_entry(real_trace_id, ...)` — 4 variant 镜像

**出场 hook**:
- `shadow_mirror.mirror_exit(real_trace_id, exit_price, reason)`
- `_update_trade_attribution()` — 30 列归因落库
- `_notify_exit()` — 飞书/钉钉

### 9.2 `app/execution/` 13 模块
| 模块 | 用途 |
|-----|-----|
| `engine.py` | `ExecutionEngine` 主类 |
| `broker_adapter.py` | Futu 抽象层 |
| `idempotency.py` | 订单幂等键 |
| `order_state_machine.py` | NEW→SENT→ACKED→FILLED 状态机 |
| `order_retry.py` | 失败重试 |
| `order_type_selector.py` | LIMIT/MARKET 自适应 |
| `order_verifier.py` | 下单后校验 |
| `slippage_control.py` | 滑点控制 |
| `smart_order_router.py` / `smart_router.py` | 智能路由 |
| `options_selector.py` | 期权合约选择 (delta/IV/到期) |
| `greeks_calculator.py` | Greeks 计算 |
| `short_checker.py` / `short_strategy.py` | 做空可行性 + 策略 |

### 9.3 `app/futu_integration.py`
Futu OpenD SDK 包装. 与港股共享 OpenD 实例 (1000 槽位全局).

### 9.4 `app/exit/option_quick_exit.py`
期权快速退出 (delta 跨阈值 / IV 异常).

---

## 10. 仓位生命周期

### 10.1 `app/pipeline/position_monitor.py`
- `monitor_loop(scheduler)` — 主循环 (interval=MONITOR_INTERVAL_MINUTES)
- `restore_positions_from_db()` — 启动从 db 恢复
- `_update_capital_safety()` — 净值更新
- `_check_positions()` — 调 `position_exit_monitor.scan_positions`
- `_monitor_option_positions()` — 期权独立监控

### 10.2 `app/core/position_recovery.py`
**Ghost 仓位恢复**: broker 端有 + 本地无 → 自动重建; 本地有 + broker 无 → 警报或清理.

### 10.3 `app/core/position_sync.py`
30s 后台 thread, broker (Futu) → PositionStore 单向同步.

### 10.4 数据库表 (主要)
| DB | 表 | 用途 |
|----|----|-----|
| `ftseek_us.db` | `positions` | 当前仓位 |
| `ftseek_us.db` | `trade_records` | 历史交易 |
| `ftseek_us.db` | `trade_attribution` | 30 列归因 (entry_thesis 等) |
| `ftseek_us.db` | `shadow_trades` | 旧 shadow (已被 ShadowMirror 替代) |
| `ftseek_us.db` | `shadow_daily_stats` | shadow 日统计 |
| `decisions.db` | `signal_arbitration` | Phase A 多 candidate 仲裁审计 |
| `decisions.db` | `vote_log` | 投票事件 |
| `decisions.db` | `tuning_proposals` | L8 daily_auto_tuner 提案 |
| `rejections.db` | `pre_trade_rejections` | 拒入场原因 |
| `exit_quality.db` | `exit_quality` | F13 退出质量评分 |
| `data/shadow_logs.db` | `market_ticks` | 0 行 (R3 保留, 未启用) |

### 10.5 `app/storage/` 11 模块
- `unified_trade_store.py` — 统一交易落库
- `decision_recorder.py` — 决策事件
- `trade_recorder.py` / `trade_store.py` — 交易历史
- `position_snapshot.py` — 持仓快照
- `daily_log_store.py` — 日志结构化
- `metrics_tables.py` / `vote_tables.py` — 指标表 schema
- `review_tables.py` — 复盘表
- `rejection_logger.py` — 拒入场记录
- `exit_quality_tracker.py` — F13 退出质量

---

## 11. Shadow & Evolution (`app/evolution/`)

### 11.1 `ShadowMirror` (Z1, `shadow_mirror.py`, 220 行)
**1:1 同源镜像**. 4 个 variant:
- `baseline` — 同实盘参数 (验证镜像准确性, 偏差<5% 即通过)
- `tighter_stop` — 止损收紧 50%
- `no_chandelier` — 禁用 chandelier 仅用 ATR stop
- `quick_exit` — 30min 强制平仓

API: `mirror_entry(real_trace_id, symbol, dir, entry_price, qty)` → 4 笔 shadow; `mirror_exit(real_trace_id, exit_price, reason)`:
- baseline 用 real 价格立即平仓
- 其他 variant 标记 real_exit_price, EOD batch 评估

### 11.2 `ShadowTracker` (`shadow_tracker.py`)
**Proposal-based** (旧). `SlippageModel + VirtualAccount + ShadowTracker` 影子测试 L8 提案.

### 11.3 `ShadowAccountDiagnostic` (`app/diagnostics/shadow_account.py`)
KMeans 聚类 + 五因子归因, 04:21 EOD 跑一次.

### 11.4 进化管线
- `improvement_pipeline.py` — 改进闭环
- `promotion_engine.py` — variant 表现优于 baseline → 推荐升级实盘 (走 ConfigWriter PARAM_MAP)

### 11.5 LLM 辅助进化
- `app/llm/daily_auto_tuner.py` (L8) — daily_review → `TuningProposal` → decisions.db
- `app/llm/weekly_proposer.py` — 周度 `ImprovementProposal`

---

## 12. LLM 子系统 (`app/llm/`, 18 模块)

### 12.1 LLM features 框架 (`app/llm/features/`)
- `feature_flags.py` — yaml `llm_features.*.enabled`
- `json_schema_guard.py` — 输出 schema 验证
- `audit_db.py` — LLM 调用审计 (cost / latency / success)
- `llm_helper.py` — 统一调用入口 (含预算守门 + 降级)

### 12.2 启用状态 (yaml `llm_features` SoT)
| Feature | 启用 | 模型 | 用途 |
|--------|-----|-----|-----|
| L1 `news_classifier` | ✅ true (P1-B) | minimax | 新闻情绪/紧急度/影响 |
| L3 `thesis_inferer` | ✅ true (PB-1) | deepseek | 入场理由 (5 类) |
| L10 `replay_dialog` | ✅ true | qwen | CLI 复盘对话 (无副作用) |
| L2 `emergency_review` | ❌ | qwen | 急跌/VIX 飙时 LLM 复核 |
| L4 `earnings_analyst` | ❌ | qwen | 财报后分析 |
| L5 `disagreement_arbitrator` | ❌ | qwen | 多模型分歧仲裁 |
| L6 `regime_classifier` | ❌ | qwen | 30min 一次场景分类 |
| L7 `large_position_review` | ❌ | qwen | 大仓位 LLM 确认 |
| L8 `daily_auto_tuner` | ❌ | deepseek | 日级自调参 |
| L9 `news_signal_gen` | ❌ | (依赖 L1) | 新闻直接生成信号 |
| L11 `sector_correlation` | ❌ | qwen | 行业关联推理 |
| L12 `news_multilang` | ❌ | (依赖 L1) | 中/日/英 prompt 适配 |

### 12.3 LLM 18 个能力模块
- `multi_model_client.py` — `DEFAULT_MODEL_FOR` SSOT (qwen / deepseek / minimax)
- `hermes_client.py` — Hermes worker (Python 3.11 子进程)
- `agent_bridge.py` — 盘前/盘后 Agent 闭环
- `attribution_engine.py` — 逐笔归因
- `production_monitor.py` — LLM 调用监控

### 12.4 LLM 预算 (yaml `llm_budget`)
- `daily_max_usd: 5.0` 总闸
- per_module: attribution=2.0, weekly_proposal=2.0, premarket=1.0
- 70% warn, 100% halt

---

## 13. 因子层 (`app/factors/`)

| 模块 | 用途 |
|-----|-----|
| `capital_flow.py` | 资金流入流出 |
| `execution_assessor.py` | 历史执行质量评分 |
| `geo_pre_filter.py` | 地缘政治预过滤 |
| `leader_pool.py` | 行业龙头池 |
| `news_prompt_templates.py` | 新闻 prompt 模板 |
| `pivotal_detector.py` | 关键点检测 |
| `regime_filter.py` | Regime 过滤器 |
| `trade_quality_gate.py` | 交易质量门 |

---

## 14. 复盘层 (`app/review/`, 16 模块)

### 14.1 主入口 `daily_review.py`
**JSON 段** (P1-D 后):
- `summary` — 总 PnL / 胜率
- `by_strategy` — 按 owner_strategy 分解
- `by_symbol` — 按标的
- `by_regime` — 按 RegimeDetector 分类
- `exit_reason_pnl` — F22 退出原因 × PnL
- `shadow_comparison` — Z4 shadow vs real 1:1
- `pnl_outliers` — Y5 PnL 异常 (5σ)
- `symbol_loss_guard_triggers` — P0-X 触发列表
- `entry_thesis_breakdown` — PB 入场理由分布

### 14.2 周期复盘
- `weekly_review.py` — 周度
- `monthly_review.py` — 月度
- `review_orchestrator.py` — 编排 daily/weekly/monthly

### 14.3 心理 + 经验
- `psychology_analyzer.py` — 交易心理 (是否冲动 / FOMO)
- `experience_accumulator.py` — 经验沉淀
- `lesson_injector.py` — 注入到 PreTradeChecker (历史教训)
- `improvement_generator.py` — 改进建议生成
- `suggestion_engine.py` — 行动建议

### 14.4 trade_analyzer / strategy_analysis / error_pattern_detector
按 trade / 策略 / 错误模式三维度分析.

### 14.5 双写 Obsidian
`obsidian_vault/` 存 markdown, 复盘人工查阅友好.

---

## 15. 观测层 (`app/observability/`, 16 模块)

| 模块 | 用途 |
|-----|-----|
| `watchdog.py` | scan_loop / monitor_loop 心跳 (P2-8 含 auto_ping) |
| `alert_manager.py` | 告警去重 + 路由 |
| `alert_debouncer.py` | 抖动抑制 |
| `audit_logger.py` | 审计日志结构化 |
| `data_flow_tracker.py` | 数据流追踪 |
| `error_handler.py` | 全局异常 |
| `failure_tracker.py` | 故障跟踪 |
| `feishu_alert.py` / `feishu_bot.py` / `feishu_client.py` | 飞书三层封装 |
| `dingtalk_alert.py` | 钉钉告警 |
| `metrics_api.py` / `metrics_collector.py` | 指标 |
| `performance_monitor.py` | 性能 |
| `engine.py` | observability 主入口 |

### 15.1 Notifier (`app/notifier/`)
- `feishu.py` — 飞书 webhook (含 `source_type` 标注)
- `dingtalk.py` — 钉钉 webhook (P1-C 至少 1 通道可用)
- 兜底: `data/critical_alerts.flag` 文件

### 15.2 Critical Alerts (`app/utils/critical_alerts.py`)
P0-3 修复. 5x 重试 + 多通道 + flag 兜底.

---

## 16. 工具层 (`app/utils/`)

| 模块 | 用途 |
|-----|-----|
| `atomic_write.py` | F0 atomic_write_text + atomic_read_with_mtime (原子写, 防部分写入) |
| `datetime_utils.py` | F0 tz-safe + signal_expires_at(phase) |
| `critical_alerts.py` | P0-3 关键告警 |
| `holiday_calendar.py` | 美股节假日 + 半日市 |
| `daily_active_pool.py` | 当日动态池 v2 |
| `dragonhead_pool.py` | 龙头股池 |
| `dynamic_pool.py` | tier 动态池 (micro/small/mid/large) |
| `performance_decay.py` | 历史表现时间衰减 |
| `rate_limiter.py` | API 限速 |
| `symbol_lock_registry.py` | symbol lock 共享注册表 |
| `watchlist_manager.py` | watchlist 管理 |

---

## 17. 配置治理 (`app/config_mgmt/`)

- `validator.py` — 启动校验 (Layer 1)
- `drift_detector.py` — 4h 漂移检测 (Layer 2)
- `hot_reloader.py` — 60s 冷却热重载 (Layer 3)

### 17.1 SSOT: `config/unified_config.yaml` (顶层段)
```
version 3.0.0
base / mode (sandbox) / paths / llm_budget / llm_models (DEPRECATED)
llm_features (L1-L12) / trading_hours / opend / data_sources / watchlist
decision / risk / options / dynamic_exit / symbol_cooldown / execution
service / observability / logging / database / agents / feature_flags
strategy_enhancements / llm / news_relevance / news_sources
debate_planner / debate_mode / strategy / strategies (coordinator+main+
scalping+fast_lane+llm_plan+defaults+dynamic_pool) / risk_limits
strategy_budgets / stop_loss / take_profit / signal_fusion / scheduler / portfolio
```

### 17.2 Sandbox 模式 (mode.sandbox)
启用时:
- universe: `[NVDA, TSLA, AAPL, MSFT, AMD, META, SPY, QQQ]`
- overrides:
  - `scan_interval_minutes: 10 → 3`
  - `monitor_interval_minutes: 0.5 → 0.25`
  - `signal_cooldown: 120 → 30`
  - `min_confidence: 0.50 → 0.45`
  - `min_strength: 25 → 20`
  - `MAX_DAILY_DRAWDOWN: 0.03 → 0.05`
  - `max_per_sector: 0.40 → 0.55`
- `keep_strict` (永不动): `kill_switch / per_trade_risk_pct / max_total_position / sector_circuit_breaker / critical_alerts`

---

## 18. CLI 工具 (`scripts/`)

### 18.1 状态查看
- `coordinator_status.py --all` — Phase A 状态 (capital / lock / arb / dispatcher)
- `sandbox_status.py` — 沙盒模式状态
- `phase_b_status.py` — Phase B 启动条件检查
- `news_sources_health.py` — 6 新闻源健康
- `opend_health_check.py` — Futu OpenD 健康

### 18.2 复盘工具
- `shadow_compare.py [--by-symbol|--variant-diff|--recent N]` — Z3 shadow vs real
- `shadow_verify_pipeline.py` — shadow 管线验证
- `exit_reason_pnl_report.py` — F22 退出原因×PnL
- `daily_alert_summary.py` — 当日告警汇总
- `post_market_audit.py` / `post_audit.py` — 盘后审计
- `daily_quality_validator.py` — 数据质量
- `why.py` — L10 LLM 复盘对话

### 18.3 运维
- `auto_position_reconcile.py` — 持仓自动对账
- `position_reconciler.py` — 手动对账
- `sync_positions_us.py` — 强制同步 broker
- `pre_market_check.py` / `preflight.py` / `preflight_check.sh` — 盘前检查
- `run_premarket.sh` / `run_postmarket.sh` — 盘前/盘后 batch
- `emergency_cleanup.py` — 紧急清理
- `check_config_drift.py` / `check_ssot.py` / `config_audit.py` — 配置漂移
- `watchdog.py` — 独立看门狗
- `whetstone_us.py` — 工具集合
- `validate_lessons_task.py` — 教训校验

### 18.4 测试 / POC
- `e2e_hermes_test.py` — Hermes 端到端
- `multi_agent_poc_test.py` — 多 agent POC
- `test_hybrid_arch.py` / `test_level2.py` / `test_structured_debate.py` — 架构测试
- `test_all_engines.sh` / `test_deployment.sh`
- `backtest_config_change.py` — 配置变更回测

---

## 19. 测试 (`tests/coordinator/`, 12 文件 ≈ 135 用例)

| 文件 | 用例数 | 主题 |
|-----|------|-----|
| `test_coordinator_phase_a.py` | ~25 | CapitalAllocator / SymbolLock / SignalArb / ExitDispatcher |
| `test_p0_audit_fixes.py` | ~15 | atomic write / holiday / ATR / sector limits |
| `test_p0_p1_p2_fixes.py` | ~10 | P0-P1-P2 综合 |
| `test_owner_strategy_persistence.py` | ~5 | owner_strategy 持久化 |
| `test_smart_relevance.py` | ~10 | 新闻智能相关性 |
| `test_llm_features.py` | ~12 | 12 个 LLM features 启用状态 |
| `test_cn_news_sources.py` | ~10 | 3 个大陆新闻源 (cailian/wallstreetcn/eastmoney) |
| `test_symbol_daily_loss_guard.py` | ~8 | 三触发器 + 重启恢复 |
| `test_y_data_driven_fixes.py` | ~12 | Y1-Y5 数据驱动修复 (regime/loss_guard/breakeven/outlier) |
| `test_shadow_mirror.py` | 7 | Z1 4 variant 入场 + baseline 平仓 + SHORT pnl |
| `test_phase_b_minimal.py` | 11 | PB 7 个 thesis 场景 + L3 启用 + ExitDispatcher 注册 |
| `test_core_logic_audit.py` | ~10 | CR-1/2/3 核心冲突修复 |

最近回归: **135/135 全绿** (排除 p0_audit / cn_news 两个慢测).

---

## 20. 端到端时序 (一笔 LONG 入场)

```
T+0    market_data_router 取 NVDA quote
T+1    SignalDetector / VotingCoordinator → vote_result
T+2    ConsensusEngine → SignalScore(direction=LONG, confidence=0.72,
                                     position_pct=0.10, stop_loss_pct=0.02)
T+3    risk_engine.pre_trade_check(signal):
         ↳ system_breaker → strategy_breaker → symbol_breaker
         ↳ PreTradeChecker.check (9 步)
         ↳ drawdown_controller / position_limits
         ✅ allowed=True
T+4    SignalArbitrator.arbitrate([nvda_signal])
         ↳ 写 signal_arbitration 表
T+5    CapitalAllocator.try_reserve("main", 0.10)
         ✅ main bucket 0.55 → 0.65
T+6    SymbolLockManager.acquire("NVDA", "main")
         ✅ owner=main
T+7    order_executor._execute_long(...)
         ↳ futu_integration.place_order(SIMULATE)
         ↳ position = Position(owner_strategy="main", trace_id=US-...)
         ↳ PositionStore.add(symbol, position)
T+8    [异步] thesis_inferer.infer_thesis_async()
         ↳ deepseek → "momentum"
         ↳ trade_attribution.entry_thesis = "momentum"
T+9    [异步] shadow_mirror.mirror_entry(trace, NVDA, LONG, 145.50, 100)
         ↳ baseline / tighter_stop / no_chandelier / quick_exit 各 1 笔
T+10   notifier.feishu / dingtalk 推送 "[NVDA] LONG ✅"

[每 30s monitor_loop]
       PositionExitMonitor.scan_positions:
         ↳ skip scalping (C2)
         ↳ ExitDispatcher.policy_for("main") → MainPipelineExitPolicy
         ↳ thesis="momentum" → fallback NO_ACTION → 走 6 规则
         ↳ _check_trailing_stop → SL=143.20 (chandelier)
         ↳ CR-3: SL = max(_old_sl, _new_sl) ≥ 之前
         ↳ SymbolDailyLossGuard.is_blocked("NVDA") → False

[价格触发 SL]
T+N    current_price=143.10 < SL=143.20
       _execute_exit(STOP_LOSS, qty=100)
         ↳ futu_integration.place_close
         ↳ position.exit_reason=STOP_LOSS, realized_pnl=-240
         ↳ shadow_mirror.mirror_exit(trace, 143.10, "STOP_LOSS")
         ↳ trade_attribution UPDATE 30 列
         ↳ CapitalAllocator.release("main", 0.10)
         ↳ SymbolLockManager.release("NVDA", "main")
         ↳ SymbolDailyLossGuard.record_pnl("NVDA", -240)
         ↳ PnLOutlierMonitor.check(-240)
         ↳ notifier.feishu "[NVDA] CLOSED -$240 STOP_LOSS"

[收盘后 04:00]
       check_daily_review → daily_review JSON
         ↳ summary / by_strategy / by_regime / shadow_comparison
         ↳ exit_reason_pnl / pnl_outliers / symbol_loss_guard_triggers
         ↳ entry_thesis_breakdown
         ↳ obsidian_vault/ 双写 markdown
       _trigger_eod_agent_review → LLM (deepseek) → 改进建议
```

---

## 21. 关键不变量 (Invariants)

1. **SoT 单一闸门**: 任何下单路径必经 `risk_engine.pre_trade_check` (主线 `signal_pipeline.py:896,1165` + 短线 `scalping/engine.py` R1 修复).
2. **TRADE_ENV=1 强制**: 实盘模式启动期拒启 (`main.py:94-107`).
3. **Scalping 自管退出**: `position_exit_monitor.py:304` 过滤, ScalpingEngine 独管 (C2).
4. **SL monotonic**: LONG 只能上移, SHORT 只能下移 (CR-3).
5. **owner_strategy 是 SoT**: `Position.strategy_id` DEPRECATED (R2), 用 `owner_strategy` 路由 ExitDispatcher + daily_review 分组.
6. **Coordinator observe_only=true**: Phase A 不阻断, 仅写 BLOCKED_* / OBSERVE_CONFLICT 事件. Phase B 切换需先 `reset_and_reconcile`.
7. **Position pre_trade**: 单笔风险 R 永不动 (sandbox.keep_strict).
8. **ConfigGov 三层**: 启动校验 + 4h 漂移 + 60s 热重载.
9. **进程单例**: `/tmp/ftseek_us.lock` fcntl 防多实例.
10. **关闭顺序**: scheduler.stop → ConfigGov → event_store → data_router → notifier → `os._exit`.

---

## 22. 当前阶段路线图

| 阶段 | 时间 | 状态 |
|-----|-----|-----|
| Phase A 协调层装配 | 5/14 | ✅ |
| Y1-Y5 数据驱动修复 | 5/14 | ✅ |
| Z1-Z4 ShadowMirror 1:1 | 5/14 | ✅ |
| L1 + L3 + L10 LLM features | 5/15 | ✅ 启用 |
| 大陆新闻源 (3 家) | 5/14 | ✅ |
| Smart Relevance + TickerDictionary | 5/14 | ✅ |
| Sandbox 模式 (8 universe) | 5/15 | ✅ |
| PB minimal (thesis-driven exit) | 5/15 | ✅ |
| CR-1/2/3 核心冲突修复 | 5/15 | ✅ |
| **R1-R4 重构 (PreTrade SoT 收敛)** | **5/15** | **✅ 本次** |
| Phase B.3-B.5 enforce 切换 | 7 天后看数据 | ⏸ |
| Phase C ExecutionMode 重构 | 1 月后 (有 baseline) | ⏸ |
| L8 daily_auto_tuner enforce | 30+ proposal 后 | ⏸ |

---

## 23. 已知技术债 (诚实清单)

1. **`Position.strategy_id` 仍写入** — R2 标 DEPRECATED 但保留向后兼容; 旧 trade_attribution 数据迁移待做.
2. **`metadata.source_type` 散落** — order_executor / dingtalk / feishu 各自填充, notifier 显示用. 未统一.
3. **`shadow_logs.db.market_ticks` 0 行** — ShadowLog daemon 未启用 (有意保留, tick 级数据量大).
4. **`ScalpingExitPolicy` 实际不被调用** — R3 标 SAFETY NET, PositionExitMonitor:304 过滤先行.
5. **`coordinator.unified_dispatcher_enabled: false`** — R4 灰度位声明, 代码未启用. 等 ExitDispatcher 与 PositionExitMonitor 决策一致率>95% 后切换.
6. **9 个 LLM features 默认 disabled** — 模拟盘逐项验证, 不全开避免预算爆炸.
7. **Phase C 重构推迟** — OrderGateway / ShadowExecutor / 多 coordinator 并行需 7+ 天 baseline 数据.

---

## 24. 部署与回滚

### 24.1 启动
```bash
systemctl start ftseek-us
journalctl -u ftseek-us -f
```

### 24.2 验证开盘后 30min
```bash
python scripts/coordinator_status.py --all
python scripts/phase_b_status.py
python scripts/news_sources_health.py
python scripts/sandbox_status.py
```

### 24.3 收盘后复盘
```bash
python scripts/post_market_audit.py
python scripts/shadow_compare.py --variant-diff
python scripts/exit_reason_pnl_report.py --days 1
```

### 24.4 一键回滚 PB
```bash
yq -i '.llm_features.thesis_inferer.enabled = false' config/unified_config.yaml
systemctl restart ftseek-us
# ExitPolicy 自动 fallback 到原 6 规则
```

### 24.5 关闭沙盒
```bash
yq -i '.mode.sandbox.enabled = false' config/unified_config.yaml
systemctl restart ftseek-us
```

---

## 25. 文档索引

| 文档 | 主题 |
|-----|-----|
| `MULTI_STRATEGY_ARCHITECTURE.md` | 多策略架构原始设计 |
| `COORDINATOR_PHASE_A_VERIFY.md` | Phase A 验证 |
| `LLM_AUDIT_AND_ENHANCEMENT_2026-05-14.md` | LLM 审计 |
| `LLM_FEATURES_DELIVERY_2026-05-14.md` | L1-L12 落地 |
| `LLM_ROLE_REDESIGN.md` | LLM 角色重设计 |
| `CN_NEWS_SOURCES_2026-05-14.md` | 大陆新闻源 |
| `CN_NEWS_SMART_RELEVANCE_2026-05-14.md` | 智能相关性 |
| `SHADOW_REVIEW_2026-05-15.md` | Shadow 模式诊断 + Z 修复 |
| `SHADOW_IMPLEMENTATION_REPORT.md` | Shadow 实施报告 |
| `PHASE_B_C_DECISION_2026-05-15.md` | Phase B/C 决策 + PB 实施 |
| `PIPELINE_AUDIT_2026-05-15.md` | 管线审计 |
| `SYSTEM_AUDIT_2026-05-14.md` | 系统审计 |
| `POST_TRADE_REVIEW_2026-05-14.md` | 交易复盘 |
| `AGENT_REVIEW_AND_Y_FIXES_2026-05-15.md` | Agent 审计 + Y 修复 |
| `P1_DEEP_FIXES_2026-05-15.md` | P1 深度修复 |
| `OPTIMIZATION_REPORT_2026-05-12.md` | 优化报告 |
| `ARCHITECTURE_OVERVIEW_2026-05-15.md` | **本文档** |

---

**结语**: FTSeek-US 已从单一管线演化为「多策略协调 + LLM 增强 + Shadow 1:1 镜像 + 沙盒加速」四位一体的模拟盘验证平台. 当前优先级是用 Phase A observe_only + ShadowMirror + Sandbox 收集 7+ 天 baseline 数据, 再决定 Phase B enforce 切换与 Phase C 重构. 本架构图随代码演进每周更新.
