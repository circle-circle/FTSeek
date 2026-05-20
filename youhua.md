 3.2 主要问题

  P0-L：三套出场逻辑共存

  - DynamicExitManager.check_exit_priority（dynamic_exit_manager.py:481-762）
  - HKExitAggregator.evaluate（hk_exit_aggregator.py:263-361）
  - ExitRuleEngine.evaluate（exit_rule_engine.py:108-126）
  - 注释里写"双跑模式仅记录差异不改变行为"，但实际 data_subscriber.py
  调用哪一个未明示，三套规则、三个优先级、三套参数（DEM 用 yaml 7
  regime、HKAggregator 用自身硬编码、ExitRuleEngine 用
  config.get("atr_hard_stop.hard_multiplier", 1.5) 路径）。
  - 修复方案：明确 cutover：
    - 第一步：在 data_subscriber.py 加日志，记录每笔实际触发的引擎名 + 决策
  priority 7 天。
    - 第二步：选定 HKExitAggregator 为 SSOT（理由：聚合最紧线 + Cost Buffer
  更稳，最贴近港股成本），废弃 DEM 的 check_exit_priority 优先级链；保留 DEM
  仅作"参数容器"。
    - 第三步：ExitRuleEngine 作为单元测试 / 双跑监控工具，不直接驱动平仓。
  - 配套：把 chandelier_activation_atr / breakeven_buffer_pct 等 HKAggregator
  硬编码全部迁入 unified_config.yaml.dynamic_exit_params.<regime>。

  P0-M：止损止盈值散落 5 处

  - unified_config.yaml:
    - line 124-189：旧的 dynamic_exit_params（首段）
    - line 1256-1604：新版 dynamic_exit_params（含 _id002~_id008 锚点）
    - line 408-462：profit_loss.dynamic_stop_loss / partial_take_profit /
  exit_by_quality（已 deprecated 但仍被部分代码读）
    - line 488-510：risk_limits.fusion_v2.stop_loss_atr_multiplier=2.5 /
  take_profit_rr_ratio=2.0
    - line 685-690：stop_loss.atr_multiplier=2.5 / default_percent=0.03 /
  max_holding_hours=48（已 deprecated）
    - line 756-758：strategy.confluence_v2.stop_loss_pct=0.05 /
  take_profit_pct=0.1
  - 修复：保留唯一 SSOT dynamic_exit_params.<regime>.*；删除所有标 deprecated
  的字段，并在 config_center.py 加 validate_no_deprecated_keys() 启动时校验。

  P0-N：hard_stop_guard 8% 永远触发不到

  - 因为 DEM 的 ATR 硬止损（1.5×ATR ≤
  5%）、结构止损（3.5%）、日内止损（2%）都更紧 —— SmartStop 兜底永远不会触发。
  - 修复：保留 8% 作为"DEM 全部失败时的最后防线"，但前提是 DEM
  异常时把它当兜底；目前 data_subscriber.py 是否有 try/except → fallback 到
  hard_stop_guard 逻辑未明。需要在 DEM 任何 exception 路径都强制调用
  hard_stop_guard.check_hard_stop。

  P0-O：breakeven_buffer_pct 配置 vs 代码不一致

  - unified_config.yaml:1285
  dynamic_exit_params.default.breakeven_stop.buffer_pct=0.006
  - hk_exit_aggregator.py:225 buffer_pct = config.get("breakeven_buffer_pct",
  0.005)
  - 键名都不一样 → HKAggregator 永远拿不到 yaml 的 buffer_pct，永远用 0.5%。
  - 修复：统一键名为 breakeven_buffer_pct（或两边都改成 buffer_pct），并通过
  regime_params_loader 注入。

  P0-P：partial_take_profit 已执行层级未正确传入 DEM

  - dynamic_exit_manager.py:725-752 通过解析 exit_state="PARTIAL_TP1"
  来跳过已执行层级。但 exit_state 谁来维护？trade_exit_manager.py
  已被简化为只做冷却（line 9-14：「实际离场逻辑已由
  DynamicExitManager.check_exit_priority() 统一承担」）。
  - 实际上 exit_state 是从 data_subscriber._on_quote_check_exit 传给
  dem.check_exit_priority(exit_state=position.exit_state) —— 但
  position_manager.Position 的 exit_state
  字段是否每次部分平仓后被正确写回？需要审计。
  - 修复：①Position 数据类显式增加 exit_state
  列；②TradeExecutor.execute_partial_close 在订单成交回调内 position.exit_state
  = f"PARTIAL_TP{level_num}" 持久化到 SQLite；③加单元测试
  test_partial_tp_no_double_trigger。

  P0-Q：time_stop 修复了周末缺口但没扣午休

  - dynamic_exit_manager.py:587-606 _trading_hours_between 周一至周五
  5.5h/天，但代码同一个 current 日内用同一个 t_start/t_end，午休 12:00-13:00
  是被两段 (am/pm) 自然剔除，逻辑是对的。但若持仓跨天，eff_start = max(t_start
  if current == t_start.date() else seg_start, seg_start) —— t_start.date() 比较
   current.replace(hour=0) 失败（前者是 date，后者是
  datetime），结果第一天午盘和第二天起所有时段都按"非首日"算。
  - 修复：line 600 改为 current.date() == t_start.date()
  比较；增加单元测试覆盖跨周持仓。

  P0-R：突发新闻止盈与 DEM 主链未集成

  - stop_loss.py:272 _breaking_news_exit + check_breaking_news_exit
  只在外部代码主动构造 breaking_news_config={'is_breaking': True, ...} 调用
  StopLossCalculator.calculate 才生效。
  - 但 dynamic_exit_manager.check_exit_priority 完全不调用 stop_loss.py，DEM
  用自身 priority 链，所以突发新闻路径永远不被触发。
  - 修复：在 DEM check_exit_priority 优先级 1 之前插入"breaking news
  exit"分支：通过 news_interceptor 标记 position.breaking_news_active=True +
  breaking_news_config，DEM 检测到则直接走 stop_loss._breaking_news_exit +
  check_breaking_news_exit。

  P1-I：trailing_stop 在 range_bound / strong_trend_down / weak_trend_down 全
  disabled

  - unified_config.yaml:1411 / 1455 / 1489 trailing_stop.enabled: false
  - 震荡市无追踪止盈合理，但 trend_down
  时若开了多头（短线反弹）就完全没有追踪保护，只能等固定止盈或止损。
  - 修复：weak_trend_down / strong_trend_down 的 trailing_stop.enabled=true
  但用更紧的 trail_atr_mult=1.0。

  P1-J：min_hold_minutes=30 在反向止损时也阻挡

  - risk_pipeline.py MinHoldTimeCheck（line 618-668）：开仓 30 分钟内反向信号且
  pnl_rate > -stop_pct 时拒绝。
  - 漏洞：盈利 0.1% 时反向信号（如重大利空）不能提前止盈 → 等价于 30
  分钟禁止任何 close 信号（除非已经亏损）。
  - 修复：加例外：①reason 包含"政策利空 / 重大事件"绕过；②盈利 ≥ 1×ATR
  时绕过；③exit_state == 'PARTIAL_TP*' 绕过（部分止盈不应被禁）。

  P1-K：跳空缺口未有专门保护

  - 港股每日 09:30 和 13:00（午后开盘）会跳空。gap_pct = (open - prev_close) /
  prev_close > 5% 时应触发"开盘不立即止损 5 分钟"或"动态扩大 ATR multiplier
  1.5x"。
  - 现状：auction_manager 在盘前竞价过滤，但盘中跳空 + 持仓亏损会立即触发 ATR
  硬止损 → 最坏情况开盘瞬间被打到最低点。
  - 修复：在 DEM 增加 _check_gap_protection：开盘 5 分钟内若 gap >
  1.5×ATR%，止损线临时放宽至 2.5×ATR，或暂停 5 分钟硬止损（盘末退出除外）。

  P1-L：连续亏损后没有"该标的限制时段"机制

  - SmartStopGuard 是全账户连败计数，不分标的。
  - symbol_loss_cooldown（yaml line 899）启用了，但代码 SymbolLossCooldownCheck
  实现需要核对（risk_pipeline 后续部分未读）。
  - 修复：保证 SymbolLossCooldownCheck 实际生效，且参数 consecutive_loss_limit=2
   / cooldown_hours=24 从 yaml 读取，不硬编码。

  P1-M：profit_loss.exit_by_quality.class_a/b/c 已废弃但代码仍可能在
  quality_scorer 路径调用

  - 现状：line 425-446 标 _deprecated，但仍占用配置位。
  - 修复：grep 全代码 exit_by_quality，确认已无消费方后从 yaml 删除。

  P2-D：volatility_risk_adapter 与 dynamic_exit_manager 之间没有联动

  - VolatilityRiskAdapter.get_adjusted_params 返回的 stop_loss / take_profit
  没有传给 DEM。
  - 双 regime 系统：DEM 看 regime_type（confluence_v2
  detect），VolatilityAdapter 看 VIX 直接读 vix_monitor。
  - 修复：DEM update_regime 时同时调用
  volatility_risk_adapter.get_adjusted_params()，对 stop_loss/take_profit
  做二次乘法叠加（因子限定 [0.7, 1.3]）。

  P2-E：add_position 加仓后止损止盈不重算

  - unified_config.yaml:569-575 定义了加仓参数，但加仓后
  position_manager.Position.entry_price 是平均成本，DEM 用平均成本计算 ATR 止损
  → 加仓被高位套牢概率上升。
  - 修复：加仓时记录 add_position_history（list of (price, qty)），DEM
  计算止损时使用"分批批次的最差成本"或"加权 1.2×平均"。

  3.3 止盈止损 8 大改进点

  1. 三套出场引擎并存 → 选定 HKExitAggregator 为 SSOT（P0-L）
  2. 止损值 5 处定义 → 删 deprecated（P0-M）
  3. 突发新闻止盈未与 DEM 集成（P0-R）
  4. partial_take_profit exit_state 持久化（P0-P）
  5. 跳空缺口动态 ATR 放宽（P1-K）
  6. 配置键名 buffer_pct 不一致（P0-O）
  7. min_hold_minutes 反向止损豁免（P1-J）
  8. trailing_stop 在 trend_down 禁用导致追涨保护缺失（P1-I）

  ---
  4. 复盘 / 归因 / 自动化优化（Review / Attribution / Self-Healing）

  4.1 现状定位

  ┌───────────────────────────────────┬─────────────────────────────────────┐
  │               文件                │              关键作用               │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/daily_review.py        │ 6 维度 EOD 报告（决策/时机/仓位/风  │
  │                                   │ 控/心理/学习）                      │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/plan_vs_actual.py      │ 计划 vs 实际执行对比                │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/improvement_generator. │ 8 模板生成改进 actions（写          │
  │ py                                │ review_db）                         │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/suggestion_engine.py   │ 13 条规则触发建议                   │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/pattern_miner.py       │ 4 类模式（stock-time / stock-signal │
  │                                   │  / stop_loss / strategy）           │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/hypothesis_engine.py   │ 假设生成 → verifying ≥3 → confirmed │
  │                                   │  ≥5 → confirmed_rules               │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/hk_feedback_relay.py   │ EOD JSON ↔ 次日盘前                 │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/review/hk_memory_injector.py  │ 写入 LLM Prompt 上下文              │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/attribution/pnl_loop.py       │ 6 维度归因 + param_adjustments 字段 │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/self_healing/engine.py        │ 11 个 IssueHandler                  │
  ├───────────────────────────────────┼─────────────────────────────────────┤
  │ app/self_healing/optimizer.py     │ 周期性"优化建议"推送钉钉            │
  └───────────────────────────────────┴─────────────────────────────────────┘

  4.2 主要问题

  P0-S：自优化闭环根本性断裂

  - pnl_loop.py:263 _suggest_param_adjustments 输出 {"position_size_multiplier":
   0.8, "signal_threshold": 0.1, ...} —— 写到 attribution.db
  后没有任何代码读取并应用。
  - improvement_generator.py:337 _save_to_database 写到 improvement_actions 表
  —— 次日盘前不读取，也不修改 yaml。
  - hypothesis_engine.get_confirmed_rules() 仅在
  premarket_analyst._load_patterns_context 中拼成 LLM Prompt —— LLM
  自己决定听不听。
  - self_healing.optimizer.SelfHealingOptimizer.analyze 推送钉钉，仅仅给人看。

  ▎ 结论：FTSeek 当前没有真正的自优化，所有"优化"都是给 LLM 喂上下文。

  - 修复方案（关键）：建立显式的参数热更新通道：
    a. 新建 app/optimizer/param_writer.py：暴露 apply_adjustments(adjustments:
  Dict[str, float], source: str, scope: str = 'next_session')；写入
  config/runtime_overrides.yaml（不是直接覆盖 unified_config.yaml）。
    b. 配置加载层 config/config_center.py：先加载 unified_config.yaml，再叠加
  runtime_overrides.yaml；并暴露 expire_overrides_older_than(hours=24)
  自动过期。
    c. pnl_loop._suggest_param_adjustments → 调
  param_writer.apply_adjustments，但只对单笔调整 < 5%（防过拟合）。
    d. hypothesis_engine.confirmed_rules 中类型 block 的规则 → 自动写入
  runtime_overrides.symbol_blacklist[code]={start,end}；类型 adjust_param → 写入
   runtime_overrides.dynamic_exit_params.<regime>.<key>。
    e. 审核闸门：所有 param_writer.apply_adjustments 同时发钉钉 ActionCard，2
  小时内人工"否决"则回滚（默认通过）。
    f. A/B 双跑：runtime_overrides 仅对 50% 标的生效（按 hash），剩余 50%
  保留原参数；24h 后比较 PnL 决定是否扩大覆盖。

  P0-T：daily_review.py 6 维度依赖大量未填字段

  - 文件依赖：trade.decision_reason / decision_confidence /
  decision_quality_score / entry_timing_score / exit_timing_score /
  position_notes / risk_notes / emotion_state / emotion_intensity /
  confidence_entry / confidence_exit / lessons_learned / review_notes /
  psychological_notes。
  - 这些字段在 position_manager.py / executor.py
  自动入库时不会填充，需要交易者手动写。结果除了"PnL / 持仓时间"外其他维度全空。
  - 修复：自动评分由系统填充：
    - entry_timing_score：min(10, 5 + (entry_price 到 5min 后最优价的距离 /
  ATR))
    - exit_timing_score：相对 60min 内最优价
    - decision_quality_score：信号 confidence × MTF align
    - position_size_actual：从 quantity × price / portfolio_value 计算
    - decision_reason：信号的 agree_strategies + scenario
    - emotion_state / psychological_notes：删除（自动系统不填心理字段）。
  - 修改 app/storage/trade_recorder.py 入库时自动计算这些。

  P0-U：PnLAttributionEngine 与 app.evidence.attribution_engine 重复

  - daily_review.py:485 from app.evidence.attribution_engine import
  AttributionEngine
  - app/attribution/pnl_loop.py PnLAttributionEngine 是另一套
  - 两套 schema 不同，没有互相同步
  - 修复：合并到 app/attribution/，删除
  app/evidence/attribution_engine.py（如不存在则确认，daily_review
  导入路径需统一）。

  P0-V：HypothesisEngine._matches_hypothesis 太粗

  - hypothesis_engine.py:287-319 完全靠字符串匹配 (code in statement, '止损' in
  statement)。
  - 例如："HK.02269 午后(RSI>70)追高" → 一笔 HK.02269
  任意时段任意原因的交易都会匹配这个假设。证据收集严重失真。
  - 修复：把 trigger_conditions 升级为结构化字段（dict），如：
  {"code":"HK.02269", "time_bucket":"afternoon", "rsi_min":70,
  "exit_reasons":["止损"]}
  - 然后 _matches_hypothesis 用结构化匹配，每个条件都需满足。

  P0-W：PatternMiner 阈值硬编码 + 标尺粗

  - pattern_miner.py:34 MIN_EVIDENCE = 3、win_rate < 0.40 阈值固定。
  - 3 笔交易就形成"模式"过于敏感。
  - 港股个股每日 ≤ 1 笔交易，3 个月才 60 笔，每个 (code, time_bucket) 组合很少 ≥
   3 → 模式覆盖严重不足。
  - 修复：
    - MIN_EVIDENCE 从 yaml 读，默认 5；
    - 加入"统计显著性"：scipy.stats.binom_test(wins, n, p=0.5) < 0.05；
    - 模式失效自动废弃：连续 5 次反例 → 自动删除（hypothesis_engine 已有
  REFUTE_THRESHOLD=3，pattern_miner 也加上）。

  P1-N：improvement_generator 模板字符串匹配建议

  - improvement_generator.py:186-201 用 if '冲动' in rec / if '止损' in rec
  决定模板，太脆弱。
  - 修复：让 psychology_analyzer 直接产出 {"template_id":
  "stop_loss_discipline", "evidence": ...} 结构化建议。

  P1-O：SelfHealingEngine 不处理"策略级"问题

  - 11 个 Handler
  都是系统问题（log_error/config_drift/risk_circuit_breaker/lot_size_mismatch/ad
  x_anomaly/news_source_failure/risk_check_failure/performance_degradation）。
  - 没有："信号胜率突然下降 30%"、"止损过紧（连续 5 次开仓后立刻止损）"、"某
  regime 在最近 2 周表现劣化"。
  - 修复：新增 4 个 strategy-level handler:
    - signal_win_rate_drop：detect 24h win_rate < 7d win_rate × 0.7 → fix: 触发
  risk_pipeline min_confidence 临时 +0.1
    - quick_stop_pattern：detect 30 分钟内止损率 > 40% → fix: 临时调宽 ATR
  multiplier × 1.3
    - regime_degradation：detect strategy in regime X over 14d underperform →
  fix: 在 runtime_overrides 暂停该策略
    - liquidity_dry_up：detect 标的成交量 < 20day avg × 0.5 → fix: 加入
  restricted 名单 24h
  - 这些 fix 走 §P0-S 的 param_writer 通道。

  P1-P：SelfHealingOptimizer.analyze 不消化交易数据

  - 仅看 self_healing_history.json，不读取 attribution.db。
  - 修复：把 attribution、PnL、win_rate 三类指标拉入 7
  天分析；输出"策略层级优化建议 + 系统层级优化建议"两类。

  P2-F：复盘报告不区分港股/美股 PnL

  - daily_review._generate_summary 没有 market 字段，HK 和 US 同时统计。
  - 修复：所有方法增加 market: str = "all" 参数，filter t.market。

  P2-G：pnl_loop.AttributionReport.success_factors / failure_factors
  是字符串列表

  - _analyze_exit_reason 把分析结论塞为人类可读 string，难以下游聚合。
  - 修复：改为 {"code": "STOP_TOO_TIGHT", "evidence": {...}} 结构化对象，便于
  pattern_miner 二次聚合。

  P2-H：HKMemoryInjector Token 预算不可见

  - 注释提到"Token 预算控制"但没看到实际预算配置。
  - 修复：把 max_tokens_per_section / total_max_tokens 暴露在 yaml；超出时按
  priority 截断。

  4.3 自闭环优化系统 6 大缺口

  1. 没有"参数热更新通道" —— 所有建议只到 DB / 钉钉，不回写配置（P0-S）
  2. 6 维度报告依赖未自动填充字段 —— 复盘只剩 PnL（P0-T）
  3. 两套 attribution 引擎 —— app.evidence vs app.attribution（P0-U）
  4. HypothesisEngine 字符串匹配 —— 证据严重失真（P0-V）
  5. self_healing 不解决策略问题 —— 仅修系统问题（P1-O）
  6. SelfHealingOptimizer 不读 attribution.db —— 优化建议与交易归因脱钩（P1-P）

  ---
  5. 跨模块系统性问题

  5.1 配置 SSOT 不统一

  止损 5 处定义、cutoff_time 2 处、min_score 在 weak_signal 硬编码
  1.2、breakeven_buffer_pct 键名不一致。

  统一治理：
  - 启动时 ConfigCenter.validate_no_orphan_keys()：扫描代码 grep
  config.get(/cc.get(/getattr(config,，找出"被读但 yaml 未定义"或"yaml
  定义但无人读"。
  - 所有止盈止损读取走单一函数 get_exit_param(key, regime, time_gate)。
  - 新建 config/SCHEMA.yaml 描述每个键的类型/范围/owner/deprecated 标记。

  5.2 双轨规模：ConfluenceV2 vs 6 因子、DEM vs HKAggregator vs
  ExitRuleEngine、AttributionEngine 双套、Hermes vs Debate 双轨

  根因：每次需求迭代都"加新模块"而非"替换旧模块"。

  治理：建立 docs/architecture-decisions/，每个新模块上线前必须列出"它替换了谁、
  何时清理旧代码、验证 cutover 的指标是什么"，否则不合并。

  5.3 LLM 调用没有缓存层

  - premarket_analyst 每次补执行重跑同一事件包；sentiment_strategy 缓存 10
  分钟但需要预热；debate 按辩论 id 没有缓存。
  - 修复：新建 app/llm/cache.py，统一 key = hash(model + messages + task_type) →
   (response, ttl)。

  5.4 时间处理散乱

  - 多处用 datetime.now() 无 tz；只有 LateSessionGate 用
  pytz.timezone("Asia/Hong_Kong")。
  - 修复：在 app/utils/timing.py 提供 now_hkt() / now_et()，全代码 grep
  datetime.now\(\) 替换。

  ---
  6. 总优先级修复清单（按文件）

  ▎ 这是 agent 直接可执行的清单。每条标注：文件 / 函数 / 改动类型 / 验证方式。

  #: 1
  优先级: P0
  文件: app/pipeline/premarket_analyst.py
  函数 / 行号: line 836-865
  改动: 删除重复 _validate_assessment
  验证: grep 验证仅一份
  ────────────────────────────────────────
  #: 2
  优先级: P0
  文件: app/pipeline/premarket_analyst.py
  函数 / 行号: _make_fallback
  改动: 在 return 前调用 self._validate_assessment(assessment)
  验证: 单测：fallback 时 confidence=0 → direction=NO_TRADE
  ────────────────────────────────────────
  #: 3
  优先级: P0
  文件: app/pipeline/battle_plan_generator.py
  函数 / 行号: _save_fallback_plan
  改动: direction 改为 WATCH；并在 plan 加 reason_kind="fallback"
  验证: 单测：fallback plan 加载后 check_open_trade 全部拒绝
  ────────────────────────────────────────
  #: 4
  优先级: P0
  文件: app/pipeline/battle_plan.py
  函数 / 行号: check_open_trade
  改动: 当 plan.reason_kind=="fallback" → return False
  验证: 同上
  ────────────────────────────────────────
  #: 5
  优先级: P0
  文件: app/pipeline/orchestrator.py
  函数 / 行号: _stage_analyze line 343-376
  改动: 全部缩进进 if should_generate: 块
  验证: 静态检查 + 跑 dry-run pipeline
  ────────────────────────────────────────
  #: 6
  优先级: P0
  文件: app/pipeline/premarket_analyst.py
  函数 / 行号: SYSTEM_PROMPT + _parse_response + generate_battle_plan_json
  改动: 增加 position_pct / stop_loss_pct / take_profit_pct / entry_zone /
    invalidation_price 结构化字段
  验证: 单测：解析含/不含字段两种 LLM 输出
  ────────────────────────────────────────
  #: 7
  优先级: P0
  文件: app/pipeline/battle_plan_schema.py（新增 schema）
  函数 / 行号: -
  改动: 扩展 BattlePlan target 字段
  验证: -
  ────────────────────────────────────────
  #: 8
  优先级: P0
  文件: app/pipeline/premarket_scheduler.py
  函数 / 行号: _execute_pipeline line 208
  改动: 第一行加节假日判断
  验证: 节假日跑 catchup 不会执行
  ────────────────────────────────────────
  #: 9
  优先级: P0
  文件: config/unified_config.yaml
  函数 / 行号: strategy.confluence_v2.enabled
  改动: 改为 false（只用 6 因子）；或删除 confluence_v2 在 weights
    中的依赖（保留主路径）
  验证: grep 验证
  ────────────────────────────────────────
  #: 10
  优先级: P0
  文件: app/strategy/signal_fusion.py
  函数 / 行号: _build_sell_result
  改动: 接 structural_votes / event_votes 参数；evaluate_consensus 调用时也传
  验证: 对称单测：buy/sell confidence 一致
  ────────────────────────────────────────
  #: 11
  优先级: P0
  文件: app/strategy/signal_scorer.py
  函数 / 行号: calculate_sell_confidence
  改动: 接 structural_votes / event_votes
  验证: 同上
  ────────────────────────────────────────
  #: 12
  优先级: P0
  文件: app/risk/risk_pipeline.py
  函数 / 行号: WeakSignalCheck.check line 467
  改动: 改 score_abs < 1.2 为读
    cc.get('risk_pipeline.checks.weak_signal.params.min_score',
    cc.get('strategy.consensus.threshold', 1.0))
  验证: 单测
  ────────────────────────────────────────
  #: 13
  优先级: P0
  文件: app/strategy/signal_fusion.py
  函数 / 行号: line 184
  改动: 删除 MIN_AGREE_VOTES = 1 块
  验证: 回归测
  ────────────────────────────────────────
  #: 14
  优先级: P0
  文件: app/strategy/confluence_v2.py
  函数 / 行号: _apply_ashare_linkage
  改动: 阈值改 ±50；调整 ±0.05；TTL 由 5min 改为可配置 southbound_ttl_sec
  验证: 单测
  ────────────────────────────────────────
  #: 15
  优先级: P0
  文件: config/unified_config.yaml
  函数 / 行号: line 894 / 971
  改动: 删除 strategy.late_session_gate.cutoff_time，只保留
    risk_pipeline.checks.late_session_gate.params.cutoff_time
  验证: 加载校验
  ────────────────────────────────────────
  #: 16
  优先级: P0
  文件: app/risk/dynamic_exit_manager.py + app/risk/hk_exit_aggregator.py
  函数 / 行号: check_exit_priority 入口
  改动: 选定 HKExitAggregator 为主路径，DEM 仅作参数容器；ExitRuleEngine 只跑
    shadow
  验证: 双跑日志 + cutover 文档
  ────────────────────────────────────────
  #: 17
  优先级: P0
  文件: config/unified_config.yaml
  函数 / 行号: line 124-189, 408-462, 488-510, 685-690, 756-758
  改动: 删除所有标 _deprecated 的止损止盈段；启动时 validate_no_deprecated_keys
  验证: grep _deprecated 为空
  ────────────────────────────────────────
  #: 18
  优先级: P0
  文件: app/risk/hk_exit_aggregator.py
  函数 / 行号: BreakevenGuardRule.calculate_stop_line line 225
  改动: 改 config.get("breakeven_buffer_pct", 0.005) 为
    config.get("breakeven_stop.buffer_pct", 0.005)
  验证: 单测
  ────────────────────────────────────────
  #: 19
  优先级: P0
  文件: app/trader/position_manager.py + app/trader/executor.py
  函数 / 行号: Position 数据类
  改动: 新增 exit_state 列、持久化到 SQLite；executor 部分平仓后写回
  验证: 单测：分批止盈不重复触发
  ────────────────────────────────────────
  #: 20
  优先级: P0
  文件: app/risk/dynamic_exit_manager.py
  函数 / 行号: _trading_hours_between line 600
  改动: current.date() == t_start.date() 比较
  验证: 跨周持仓单测
  ────────────────────────────────────────
  #: 21
  优先级: P0
  文件: app/risk/dynamic_exit_manager.py
  函数 / 行号: check_exit_priority 优先级 0.5
  改动: 新增 breaking_news_exit 分支，调用 stop_loss._breaking_news_exit
  验证: 集成测
  ────────────────────────────────────────
  #: 22
  优先级: P0
  文件: 新建 app/optimizer/param_writer.py
  函数 / 行号: -
  改动: 实现 apply_adjustments(adjustments, source, scope='next_session') 写入
    config/runtime_overrides.yaml
  验证: 单测 + 回滚测
  ────────────────────────────────────────
  #: 23
  优先级: P0
  文件: config/config_center.py
  函数 / 行号: get
  改动: 加载 unified + runtime_overrides 并合并；过期清理
  验证: 单测
  ────────────────────────────────────────
  #: 24
  优先级: P0
  文件: app/attribution/pnl_loop.py
  函数 / 行号: _suggest_param_adjustments 之后
  改动: 调用 param_writer.apply_adjustments，调整幅度限制 ±5%
  验证: 集成测
  ────────────────────────────────────────
  #: 25
  优先级: P0
  文件: app/review/hypothesis_engine.py
  函数 / 行号: Hypothesis.trigger_conditions
  改动: 字段升级为结构化 dict (code/time_bucket/rsi_min/...)
  验证: 数据迁移脚本
  ────────────────────────────────────────
  #: 26
  优先级: P0
  文件: app/review/hypothesis_engine.py
  函数 / 行号: _matches_hypothesis
  改动: 改为结构化匹配
  验证: 单测
  ────────────────────────────────────────
  #: 27
  优先级: P0
  文件: app/review/hypothesis_engine.py
  函数 / 行号: get_confirmed_rules 之后
  改动: confirmed 规则自动 param_writer.apply_adjustments 或
    runtime_overrides.symbol_blacklist
  验证: 集成测
  ────────────────────────────────────────
  #: 28
  优先级: P0
  文件: app/storage/trade_recorder.py
  函数 / 行号: save()
  改动: 自动填充 entry_timing_score / exit_timing_score / decision_quality_score

    / position_size_actual
  验证: 单测
  ────────────────────────────────────────
  #: 29
  优先级: P0
  文件: app/review/daily_review.py
  函数 / 行号: line 485
  改动: 统一改用 app.attribution.pnl_loop.PnLAttributionEngine
  验证: 删除 evidence 模块（如存在）
  ────────────────────────────────────────
  #: 30
  优先级: P1
  文件: config/unified_config.yaml
  函数 / 行号:
  dynamic_exit_params.weak_trend_down/strong_trend_down.trailing_stop
  改动: 改 enabled=true，trail_atr_mult=1.0
  验证: 回测验证
  ────────────────────────────────────────
  #: 31
  优先级: P1
  文件: app/risk/risk_pipeline.py
  函数 / 行号: MinHoldTimeCheck.check
  改动: 增加豁免：reason 含"政策利空" / 盈利 ≥ 1×ATR / exit_state PARTIAL_TP
  验证: 单测
  ────────────────────────────────────────
  #: 32
  优先级: P1
  文件: app/risk/dynamic_exit_manager.py
  函数 / 行号: 新增 _check_gap_protection
  改动: 开盘 5 分钟内 gap > 1.5×ATR% 临时放宽止损
  验证: 单测
  ────────────────────────────────────────
  #: 33
  优先级: P1
  文件: app/strategy/sentiment_strategy.py
  函数 / 行号: 模块外部
  改动: 新增后台 SentimentRefresher 线程，每 15 分钟预热
  验证: 验证 on_quote 不再 miss
  ────────────────────────────────────────
  #: 34
  优先级: P1
  文件: app/strategy/regime_detector.py + app/strategy/market_regime.py
  函数 / 行号: 合并
  改动: 统一为 6 态 SSOT，所有调用方一致
  验证: 单测
  ────────────────────────────────────────
  #: 35
  优先级: P1
  文件: app/strategy/factor_votes.py
  函数 / 行号: VolumeAmplifierStrategy line 414
  改动: 反向方向也加 600s 冷却
  验证: 单测
  ────────────────────────────────────────
  #: 36
  优先级: P1
  文件: app/risk/risk_pipeline.py
  函数 / 行号: MultiTimeframeCheck.check
  改动: 加 mtf_warmup_seconds 豁免
  验证: 单测
  ────────────────────────────────────────
  #: 37
  优先级: P1
  文件: app/self_healing/engine.py
  函数 / 行号: 注册 4 个新 handler
  改动: signal_win_rate_drop / quick_stop_pattern / regime_degradation /
    liquidity_dry_up
  验证: 集成测
  ────────────────────────────────────────
  #: 38
  优先级: P1
  文件: app/self_healing/optimizer.py
  函数 / 行号: analyze
  改动: 引入 attribution.db 数据；产出 strategy-level + system-level 双类报告
  验证: 报告对比
  ────────────────────────────────────────
  #: 39
  优先级: P1
  文件: app/strategy/confluence_v2.py
  函数 / 行号: 因子相关性折扣
  改动: 新增 rsi vs macd / vol_factor vs macd 两组动态折扣
  验证: 单测
  ────────────────────────────────────────
  #: 40
  优先级: P1
  文件: app/risk/volatility_risk_adapter.py ↔ DEM
  函数 / 行号: 联动
  改动: DEM update_regime 时叠加 VIX 调整
  验证: 单测
  ────────────────────────────────────────
  #: 41
  优先级: P2
  文件: app/llm/cache.py（新增）
  函数 / 行号: -
  改动: 统一 LLM 缓存层
  验证: 集成测
  ────────────────────────────────────────
  #: 42
  优先级: P2
  文件: app/utils/timing.py（新增）
  函数 / 行号: now_hkt()/now_et()
  改动: 全代码替换 datetime.now()
  验证: grep
  ────────────────────────────────────────
  #: 43
  优先级: P2
  文件: config/SCHEMA.yaml（新增）
  函数 / 行号: -
  改动: 描述每个 key 的 owner/type/range
  验证: CI 校验
  ────────────────────────────────────────
  #: 44
  优先级: P2
  文件: app/review/pattern_miner.py
  函数 / 行号: mine
  改动: 阈值从 yaml 读；统计显著性
  验证: 单测
  ────────────────────────────────────────
  #: 45
  优先级: P2
  文件: app/review/daily_review.py
  函数 / 行号: 所有方法
  改动: 增加 market 参数过滤
  验证: 单测

  ---
  7. 关键回归测试清单（agent 实施完毕后必跑）

  1. tests/test_battle_plan_fallback_blocked.py：LLM 全失败时 fallback plan
  拒绝所有 BUY
  2. tests/test_signal_buy_sell_symmetry.py：相同因子结构下 BUY/SELL confidence
  镜像一致
  3. tests/test_consensus_threshold_consistency.py：consensus 通过的信号必然通过
   weak_signal
  4. tests/test_partial_tp_no_double_trigger.py：分批止盈层级不重复
  5. tests/test_time_stop_cross_week.py：周末持仓时间扣除
  6. tests/test_breaking_news_exit_in_dem.py：突发新闻路径在 DEM 中触发
  7. tests/test_param_writer_rollback.py：runtime_overrides 24h 自动过期
  8. tests/test_hypothesis_structured_match.py：结构化 trigger_conditions
  精准匹配
  9. tests/test_confluence_v2_disabled.py：6 因子模式下不出现
  strategy="confluence_v2" 投票
  10. tests/test_southbound_linkage_symmetric.py：南向调整买卖对称

  ---
  8. 美股链路同构问题速记

  - app/us_pipeline/、app/strategy/us_overnight_monitor.py、config/unified_confi
  g.yaml.profit_loss.us（line 169-189）等大概率有相同问题：
    - 同样的 fallback 全市场放行
    - 同样的双计票
    - 美股盘后 4:00 PM ET 时间窗与港股 catchup 互斥未明
  - 建议：清单 #1-#45 完成 HK 路径后，把每条修复在 us_* 等价路径再做一次。

  ---
  9. 实施次序建议（给执行 agent）

  阶段 1（第 1 天，P0 安全修复）：清单 #1, #2, #3, #4, #5, #8, #18, #20 ——
  仅修补缺陷，不改架构
  阶段 2（第 2-3 天，P0 信号对称）：清单 #9, #10, #11, #12, #13, #14, #15
  阶段 3（第 4-5 天，P0 出场架构 cutover）：清单 #16, #17, #19, #21
  阶段 4（第 6-8 天，P0 自优化闭环）：清单 #22, #23, #24, #25, #26, #27, #28,
  #29
  阶段 5（第 9-12 天，P1 增强）：清单 #30-#40
  阶段 6（第 13-15 天，P2 体系化）：清单 #41-#45 + 跨市场同构

  每阶段完成后跑回归测试（§7），并以 runtime_overrides.yaml = empty
  为基线确认主路径不退化。

  ---

  ▎ 总结：FTSeek 在每个站点都有"完整功能"的实现，问题集中在 契约一致性 / 配置
  ▎ SSOT / 真闭环（不是 prompt 闭环）/ 双轨清理。本报告 45
  ▎ 条改动按优先级与文件落地，agent 可逐项实施，每条都给出了文件 / 函数 /
  ▎ 验证方式三要素，无需再做二次拆解。

⏺ 报告已完成。共 45 条精准修复点，按文件 × 函数 × 改动类型 ×
  验证方式四要素组织，下游 agent 可按"实施次序建议"逐项落地。

  核心结论：
  - 盘前：LLM fallback 全市场放行 + _validate_assessment 重复定义 + orchestrator
   控制流 bug 是最致命的 3 处。
  - 盘中：confluence_v2 与 6 因子双计票、买卖 confidence
  不对称、阈值多处不一致是信号识别失真主因。
  - 止盈止损：DEM/HKExitAggregator/ExitRuleEngine 三套并存 + 配置 5 处定义 +
  突发新闻路径未集成进 DEM 主链。
  - 复盘自优化：根本性断裂 —— 所有"建议"只到 DB / 钉钉 / LLM
  Prompt，没有任何代码回写配置；改进重点是新增 param_writer +
  runtime_overrides.yaml 通道，把 hypothesis_engine 与 pnl_loop
  的输出真正闭环到运行时配置。

✻ Cogitated for 12m 47s

※ recap: 目标是为 FTSeek 量化系统输出一份盘前/盘中/止盈止损/复盘自优化的全链路改
  进报告，已完成分析并交付包含 45 条精准修复点的 Markdown
  报告。下一步等你确认报告，决定是否开始按优先级实施修复。 (disable recaps in
  /config)

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts                         Image in clipboard · ctrl+v to paste

  核心结论：
  - 盘前：LLM fallback 全市场放行 + _validate_assessment 重复定义 + orchestrator
  控制流 bug 是最致命的 3 处。
  - 盘中：confluence_v2 与 6 因子双计票、买卖 confidence
  不对称、阈值多处不一致是信号识别失真主因。
  - 止盈止损：DEM/HKExitAggregator/ExitRuleEngine 三套并存 + 配置 5 处定义 +
  突发新闻路径未集成进 DEM 主链。
  - 复盘自优化：根本性断裂 —— 所有"建议"只到 DB / 钉钉 / LLM
  Prompt，没有任何代码回写配置；改进重点是新增 param_writer +
  runtime_overrides.yaml 通道，把 hypothesis_engine 与 pnl_loop
  的输出真正闭环到运行时配置。

✻ Cogitated for 12m 47s

※ recap: 目标是为 FTSeek 量化系统输出一份盘前/盘中/止盈止损/复盘自优化的全链路改进
  报告，已完成分析并交付包含 45 条精准修复点的 Markdown
  报告。下一步等你确认报告，决定是否开始按优先级实施修复。 (disable recaps in
  /config)

──────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts                           Image in clipboard · ctrl+v to paste
  优先级: P2
  文件: config/SCHEMA.yaml（新增）
  函数 / 行号: -
  改动: 描述每个 key 的 owner/type/range
  验证: CI 校验
  ────────────────────────────────────────
  #: 44
  优先级: P2
  文件: app/review/pattern_miner.py
  函数 / 行号: mine
  改动: 阈值从 yaml 读；统计显著性
  验证: 单测
  ────────────────────────────────────────
  #: 45
  优先级: P2
  文件: app/review/daily_review.py
  函数 / 行号: 所有方法
  改动: 增加 market 参数过滤
  验证: 单测

  ---
  7. 关键回归测试清单（agent 实施完毕后必跑）

  1. tests/test_battle_plan_fallback_blocked.py：LLM 全失败时 fallback plan
  拒绝所有 BUY
  2. tests/test_signal_buy_sell_symmetry.py：相同因子结构下 BUY/SELL confidence
  镜像一致
  3. tests/test_consensus_threshold_consistency.py：consensus 通过的信号必然通过
  weak_signal
  4. tests/test_partial_tp_no_double_trigger.py：分批止盈层级不重复
  5. tests/test_time_stop_cross_week.py：周末持仓时间扣除
  6. tests/test_breaking_news_exit_in_dem.py：突发新闻路径在 DEM 中触发
  7. tests/test_param_writer_rollback.py：runtime_overrides 24h 自动过期
  8. tests/test_hypothesis_structured_match.py：结构化 trigger_conditions 精准匹配
  9. tests/test_confluence_v2_disabled.py：6 因子模式下不出现
  strategy="confluence_v2" 投票
  10. tests/test_southbound_linkage_symmetric.py：南向调整买卖对称

  ---
  8. 美股链路同构问题速记

  - app/us_pipeline/、app/strategy/us_overnight_monitor.py、config/unified_config.
  yaml.profit_loss.us（line 169-189）等大概率有相同问题：
    - 同样的 fallback 全市场放行
    - 同样的双计票
    - 美股盘后 4:00 PM ET 时间窗与港股 catchup 互斥未明
  - 建议：清单 #1-#45 完成 HK 路径后，把每条修复在 us_* 等价路径再做一次。

  ---
  9. 实施次序建议（给执行 agent）

  阶段 1（第 1 天，P0 安全修复）：清单 #1, #2, #3, #4, #5, #8, #18, #20 ——
  仅修补缺陷，不改架构
  阶段 2（第 2-3 天，P0 信号对称）：清单 #9, #10, #11, #12, #13, #14, #15
  阶段 3（第 4-5 天，P0 出场架构 cutover）：清单 #16, #17, #19, #21
  阶段 4（第 6-8 天，P0 自优化闭环）：清单 #22, #23, #24, #25, #26, #27, #28, #29
  阶段 5（第 9-12 天，P1 增强）：清单 #30-#40
  阶段 6（第 13-15 天，P2 体系化）：清单 #41-#45 + 跨市场同构

  每阶段完成后跑回归测试（§7），并以 runtime_overrides.yaml = empty
  为基线确认主路径不退化。

  ---

  ▎ 总结：FTSeek 在每个站点都有"完整功能"的实现，问题集中在 契约一致性 / 配置 SSOT
  ▎  / 真闭环（不是 prompt 闭环）/ 双轨清理。本报告 45
  ▎ 条改动按优先级与文件落地，agent 可逐项实施，每条都给出了文件 / 函数 /
  ▎ 验证方式三要素，无需再做二次拆解。

⏺ 报告已完成。共 45 条精准修复点，按文件 × 函数 × 改动类型 × 验证方式四要素组织，下游 agent 可按"实施次序建议"逐项落地。

  核心结论：
  - 盘前：LLM fallback 全市场放行 + _validate_assessment 重复定义 + orchestrator 控制流 bug 是最致命的 3 处。
  - 盘中：confluence_v2 与 6 因子双计票、买卖 confidence 不对称、阈值多处不一致是信号识别失真主因。
  - 止盈止损：DEM/HKExitAggregator/ExitRuleEngine 三套并存 + 配置 5 处定义 + 突发新闻路径未集成进 DEM 主链。
  - 复盘自优化：根本性断裂 —— 所有"建议"只到 DB / 钉钉 / LLM Prompt，没有任何代码回写配置；改进重点是新增 param_writer + runtime_overrides.yaml 通道，把
  hypothesis_engine 与 pnl_loop 的输出真正闭环到运行时配置。

✻ Cogitated for 12m 47s

※ recap: 目标是为 FTSeek 量化系统输出一份盘前/盘中/止盈止损/复盘自优化的全链路改进报告，已完成分析并交付包含 45 条精准修复点的 Markdown
  报告。下一步等你确认报告，决定是否开始按优先级实施修复。 (disable recaps in /config)

─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
  ---

  ▎ 总结：FTSeek 在每个站点都有"完整功能"的实现，问题集中在 契约一致性 / 配置 SSOT / 真闭环（不是 prompt 闭环）/ 双轨清理。本报告 45 条改动按优先级与文件落地，agent
  ▎ 可逐项实施，每条都给出了文件 / 函数 / 验证方式三要素，无需再做二次拆解。

⏺ 报告已完成。共 45 条精准修复点，按文件 × 函数 × 改动类型 × 验证方式四要素组织，下游 agent 可按"实施次序建议"逐项落地。

  核心结论：
  - 盘前：LLM fallback 全市场放行 + _validate_assessment 重复定义 + orchestrator 控制流 bug 是最致命的 3 处。
  - 盘中：confluence_v2 与 6 因子双计票、买卖 confidence 不对称、阈值多处不一致是信号识别失真主因。
  - 止盈止损：DEM/HKExitAggregator/ExitRuleEngine 三套并存 + 配置 5 处定义 + 突发新闻路径未集成进 DEM 主链。
  - 复盘自优化：根本性断裂 —— 所有"建议"只到 DB / 钉钉 / LLM Prompt，没有任何代码回写配置；改进重点是新增 param_writer + runtime_overrides.yaml 通道，把
  hypothesis_engine 与 pnl_loop 的输出真正闭环到运行时配置。

✻ Cogitated for 12m 47s

※ recap: 目标是为 FTSeek 量化系统输出一份盘前/盘中/止盈止损/复盘自优化的全链路改进报告，已完成分析并交付包含 45 条精准修复点的 Markdown
  报告。下一步等你确认报告，决定是否开始按优先级实施修复。 (disable recaps in /config)

─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
  3. tests/test_consensus_threshold_consistency.py：consensus 通过的信号必然通过 weak_signal
  4. tests/test_partial_tp_no_double_trigger.py：分批止盈层级不重复
  5. tests/test_time_stop_cross_week.py：周末持仓时间扣除
  6. tests/test_breaking_news_exit_in_dem.py：突发新闻路径在 DEM 中触发
  7. tests/test_param_writer_rollback.py：runtime_overrides 24h 自动过期
  8. tests/test_hypothesis_structured_match.py：结构化 trigger_conditions 精准匹配
  9. tests/test_confluence_v2_disabled.py：6 因子模式下不出现 strategy="confluence_v2" 投票
  10. tests/test_southbound_linkage_symmetric.py：南向调整买卖对称

  ---
  8. 美股链路同构问题速记

  - app/us_pipeline/、app/strategy/us_overnight_monitor.py、config/unified_config.yaml.profit_loss.us（line 169-189）等大概率有相同问题：
    - 同样的 fallback 全市场放行
    - 同样的双计票
    - 美股盘后 4:00 PM ET 时间窗与港股 catchup 互斥未明
  - 建议：清单 #1-#45 完成 HK 路径后，把每条修复在 us_* 等价路径再做一次。

  ---
  9. 实施次序建议（给执行 agent）

  阶段 1（第 1 天，P0 安全修复）：清单 #1, #2, #3, #4, #5, #8, #18, #20 —— 仅修补缺陷，不改架构
  阶段 2（第 2-3 天，P0 信号对称）：清单 #9, #10, #11, #12, #13, #14, #15
  阶段 3（第 4-5 天，P0 出场架构 cutover）：清单 #16, #17, #19, #21
  阶段 4（第 6-8 天，P0 自优化闭环）：清单 #22, #23, #24, #25, #26, #27, #28, #29
  阶段 5（第 9-12 天，P1 增强）：清单 #30-#40
  阶段 6（第 13-15 天，P2 体系化）：清单 #41-#45 + 跨市场同构

  每阶段完成后跑回归测试（§7），并以 runtime_overrides.yaml = empty 为基线确认主路径不退化。

  ---

  ▎ 总结：FTSeek 在每个站点都有"完整功能"的实现，问题集中在 契约一致性 / 配置 SSOT / 真闭环（不是 prompt 闭环）/ 双轨清理。本报告 45 条改动按优先级与文件落地，agent 可逐项实施，每条都给出了文件 / 函数 /
  ▎ 验证方式三要素，无需再做二次拆解。

⏺ 报告已完成。共 45 条精准修复点，按文件 × 函数 × 改动类型 × 验证方式四要素组织，下游 agent 可按"实施次序建议"逐项落地。

  核心结论：
  - 盘前：LLM fallback 全市场放行 + _validate_assessment 重复定义 + orchestrator 控制流 bug 是最致命的 3 处。
  - 盘中：confluence_v2 与 6 因子双计票、买卖 confidence 不对称、阈值多处不一致是信号识别失真主因。
  - 止盈止损：DEM/HKExitAggregator/ExitRuleEngine 三套并存 + 配置 5 处定义 + 突发新闻路径未集成进 DEM 主链。
  - 复盘自优化：根本性断裂 —— 所有"建议"只到 DB / 钉钉 / LLM Prompt，没有任何代码回写配置；改进重点是新增 param_writer + runtime_overrides.yaml 通道，把 hypothesis_engine 与 pnl_loop 的输出真正闭环到运行时配置。

✻ Cogitated for 12m 47s

※ recap: 目标是为 FTSeek 量化系统输出一份盘前/盘中/止盈止损/复盘自优化的全链路改进报告，已完成分析并交付包含 45 条精准修复点的 Markdown 报告。下一步等你确认报告，决定是否开始按优先级实施修复。 (disable recaps in
  /config)

──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
❯
──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
  1. tests/test_battle_plan_fallback_blocked.py：LLM 全失败时 fallback plan 拒绝所有 BUY
  2. tests/test_signal_buy_sell_symmetry.py：相同因子结构下 BUY/SELL confidence 镜像一致
  3. tests/test_consensus_threshold_consistency.py：consensus 通过的信号必然通过 weak_signal
  4. tests/test_partial_tp_no_double_trigger.py：分批止盈层级不重复
  5. tests/test_time_stop_cross_week.py：周末持仓时间扣除
  6. tests/test_breaking_news_exit_in_dem.py：突发新闻路径在 DEM 中触发
  7. tests/test_param_writer_rollback.py：runtime_overrides 24h 自动过期
  8. tests/test_hypothesis_structured_match.py：结构化 trigger_conditions 精准匹配
  9. tests/test_confluence_v2_disabled.py：6 因子模式下不出现 strategy="confluence_v2" 投票
  10. tests/test_southbound_linkage_symmetric.py：南向调整买卖对称

  ---
  8. 美股链路同构问题速记

  - app/us_pipeline/、app/strategy/us_overnight_monitor.py、config/unified_config.yaml.profit_loss.us（line 169-189）等大概率有相同问题：
    - 同样的 fallback 全市场放行
    - 同样的双计票
    - 美股盘后 4:00 PM ET 时间窗与港股 catchup 互斥未明
  - 建议：清单 #1-#45 完成 HK 路径后，把每条修复在 us_* 等价路径再做一次。

  ---
  9. 实施次序建议（给执行 agent）

  阶段 1（第 1 天，P0 安全修复）：清单 #1, #2, #3, #4, #5, #8, #18, #20 —— 仅修补缺陷，不改架构
  阶段 2（第 2-3 天，P0 信号对称）：清单 #9, #10, #11, #12, #13, #14, #15
  阶段 3（第 4-5 天，P0 出场架构 cutover）：清单 #16, #17, #19, #21
  阶段 4（第 6-8 天，P0 自优化闭环）：清单 #22, #23, #24, #25, #26, #27, #28, #29
  阶段 5（第 9-12 天，P1 增强）：清单 #30-#40
  阶段 6（第 13-15 天，P2 体系化）：清单 #41-#45 + 跨市场同构

  每阶段完成后跑回归测试（§7），并以 runtime_overrides.yaml = empty 为基线确认主路径不退化。

  ---

  ▎ 总结：FTSeek 在每个站点都有"完整功能"的实现，问题集中在 契约一致性 / 配置 SSOT / 真闭环（不是 prompt 闭环）/ 双轨清理。本报告 45 条改动按优先级与文件落地，agent 可逐项实施，每条都给出了文件 / 函数 /
  ▎ 验证方式三要素，无需再做二次拆解。

⏺ 报告已完成。共 45 条精准修复点，按文件 × 函数 × 改动类型 × 验证方式四要素组织，下游 agent 可按"实施次序建议"逐项落地。

  核心结论：
  - 盘前：LLM fallback 全市场放行 + _validate_assessment 重复定义 + orchestrator 控制流 bug 是最致命的 3 处。
  - 盘中：confluence_v2 与 6 因子双计票、买卖 confidence 不对称、阈值多处不一致是信号识别失真主因。
  - 止盈止损：DEM/HKExitAggregator/ExitRuleEngine 三套并存 + 配置 5 处定义 + 突发新闻路径未集成进 DEM 主链。
  - 复盘自优化：根本性断裂 —— 所有"建议"只到 DB / 钉钉 / LLM Prompt，没有任何代码回写配置；改进重点是新增 param_writer + runtime_overrides.yaml 通道，把 hypothesis_engine 与 pnl_loop 的输出真正闭环到运行时配置。

