# Agent-vs-Agent Debate: 双IC闭环焊接方案可行性审查

## 使用说明
- 每轮辩论在「## 讨论记录」下追加, **永不覆盖**他人内容
- 每条事实声明必须附验证命令输出 (grep/read), 不可虚构
- 让步时写 "接受: <观点>", 坚持时写 "维持: <观点>", 修改时写 "修正: 从X转向Y"

---

# 议题：双IC闭环焊接方案(8 Task)是否可行？应如何调整？

## 状态

- [ ] 进行中
- [ ] 待决策
- [x] 已决策
- [ ] 已验证
- [ ] 已关闭

## 背景

用户提交了一份「双IC系统闭环焊接 + 冗余消除方案」(task-409.md)，包含8个Task：
1. 新建 ICFeedbackBridge 模块
2. VotingCoordinator 注入 IC 权重反馈
3. PlaybookScreener 注入策略IC过滤
4. Signal Pipeline TRADING阶段瘦身
5. 新建 DailyCloseCache 共享日线缓存
6. 统一盘后回填调度
7. Feature Flags 配置
8. Shadow Mode 对比验证

Hermes 已完成代码级审查，发现以下关键事实（均附grep验证）：

## 数据与实证

### 事实1: Signal IC 已有3条消费路径（非"观测孤岛"）

```bash
# 消费路径1: order_executor IC门禁 (硬拦截 IC<-0.10)
grep -n 'get_ic_summary' app/pipeline/order_executor.py
# → L3351: from app.analysis.signal_ic_tracker import get_ic_summary

# 消费路径2: signal_scorer IC权重自适应 (IC<-0.05→权重置零, IC>0.08→×1.5)
grep -n '_apply_ic_adaptation' app/decision/signal_scorer.py
# → L225: weights = self._apply_ic_adaptation(weights)
# → L233: def _apply_ic_adaptation(self, weights)

# 消费路径3: strategy_health_monitor
grep -n 'get_ic_summary' app/risk/strategy_health_monitor.py
# → L161: ic_summary = get_ic_summary(days_back=30)

# 消费路径4: 盘后审计报告
grep -n 'get_ic_summary' app/harness/us_audit.py
# → L204: ic_summary = get_ic_summary(min_signals=20)
```

### 事实2: TRADING阶段LLM空转根因 — signal_stage.py L534未检查_skip

```bash
# _skip_execution_in_trading 设置位置
grep -n '_skip_execution_in_trading' app/pipeline/signal_pipeline.py
# → L172: _skip_execution_in_trading = True
# → L593: if getattr(scheduler, '_skip_execution_in_trading', False):  # 只跳过下单
# → L1223: if getattr(scheduler, '_skip_execution_in_trading', False):  # 只跳过下单

# signal_stage 中 LLM 投票调用 (无 _skip 检查)
sed -n '534,545p' app/pipeline/stages/signal_stage.py
# → if voting_coordinator and not has_position:
# →     future = executor.submit(voting_coordinator.vote, symbol, ctx)
# →     vote_result = future.result(timeout=15.0)
# 结论: _skip_execution_in_trading 只传导到 signal_pipeline 的下单层,
#        未传导到 signal_stage 的 LLM 投票层 → LLM 空转
```

### 事实3: get_ic_summary 已返回 by_strategy + by_direction IC

```bash
# get_ic_summary 返回结构 (signal_ic_tracker.py L369-525)
# 返回: {
#   'ic_mean': float, 'ic_p_value': float,
#   'by_strategy': {sid: {'ic': float, 'pval': float, 'n': int}},
#   'by_direction': {direction: {'ic': float, 'n': int}},
#   'status': 'ok' | 'insufficient_data'
# }
grep -n 'by_strategy\|by_direction' app/analysis/signal_ic_tracker.py
# → L389: 'by_strategy': dict
# → L390: 'by_direction': dict
# → L480-514: 完整计算逻辑
```

### 事实4: signal_snapshots 数据质量

```bash
sqlite3 data/signal_snapshots.db "SELECT COUNT(*) FROM signal_snapshots"
# → 1757

sqlite3 data/signal_snapshots.db "SELECT strategy_id, COUNT(*) FROM signal_snapshots GROUP BY strategy_id"
# → TECHNICAL_MULTI_FACTOR|561
# → EARNINGS_REVISION_MOMENTUM|441
# → NULL|383  (22%无strategy_id)
# → voting_llm|328
# → BATTLE_PLAN_FAST_PATH|21
# → trend_first|21
# → orb|2

sqlite3 data/signal_snapshots.db "SELECT realized, COUNT(*) FROM signal_snapshots GROUP BY realized"
# → 0|348  (pending, 未回填)
# → 1|420  (T1 only)
# → 2|674  (T1+T5)
# → 3|315  (full T1+T5+T20)
# T20完成率: 315/1757 = 18%
```

### 事实5: MarketDataRouter.get_daily_bars 无缓存

```bash
# get_daily_bars 实现 (market_data_router.py L160-196)
# 直接调 self.futu.get_daily_kline() 或 self.td.get_daily_bars()
# 无任何缓存层

# TwelveData provider 内部有300秒缓存 (twelvedata_provider.py L96, L114)
# 但两个backfill用不同router实例 → 不共享缓存

# get_daily_closes (backfill_forward_returns.py) 走自己的 _fetch_router_daily()
# → 独立的 backtest_cache + 独立 router 调用
# ic_tracker.backfill_forward_returns 走 scheduler.market_data.get_daily_bars()
# → 不同代码路径, 不同实例
```

### 事实6: 两个backfill接口签名不兼容

```bash
# Playbook IC backfill
grep -n 'def backfill_forward_returns' app/playbook/ic_tracker.py
# → L76: def backfill_forward_returns(db, market_data, trade_date: str = None) -> int
# 操作 playbook_instructions 表

# Signal IC backfill
grep -n 'def backfill_forward_returns' scripts/backfill_forward_returns.py
# → L301: def backfill_forward_returns(dry_run: bool = False) -> Dict[str, int]
# 操作 signal_snapshots 表
# 在 app 层直接 import scripts 层违反分层
```

### 事实7: 方案引用的路径/函数名有误

```bash
# 方案声称: app/analysis/signal_scorer.py → 实际在 app/decision/signal_scorer.py
# 方案声称: _generate_technical_fallback → 实际叫 _generate_technical_signal (L753)
# 方案声称: pipeline.py L1134 → 实际 L1136-1140
```

### 事实8: signal_stage voting 调用已有high-confidence skip机制

```bash
sed -n '536,538p' app/pipeline/stages/signal_stage.py
# → if candidates and any(c.confidence >= 0.70 for c in candidates):
# →     logger.info(f"[SignalStage] Skipping LLM vote for {symbol} - already have high-confidence candidates")
# 结论: 已有条件跳过LLM的机制, 但阈值太高(0.70)且不看_skip_execution_in_trading
```

## 可选方案

### 方案 A: 原方案8个Task全量实施

- 提出者：方案文档(task-409.md)
- 核心思路：新建 ICFeedbackBridge + DailyCloseCache + shadow_log 3个模块，改造 VotingCoordinator + Screener + SignalPipeline + pipeline.py
- 优点：架构完整，IC闭环 formally 接入所有决策点
- 缺点：ICFeedbackBridge 与现有 get_ic_summary 重复；external_votes 机制语义错误；新建3个模块=加法；383条NULL strategy_id未修就接入决策=垃圾进垃圾出
- 适用条件：样本充足 + 数据质量过关 + 现有消费路径不够用

### 方案 B: 做减法精简版 — 修根因 + 复用现有基础设施

- 提出者：Hermes 审查报告
- 核心思路：
  1. P0: signal_stage.py L534 加 _skip_execution_in_trading 检查 (1行代码消灭LLM空转)
  2. P0: 修复383条NULL strategy_id (数据质量)
  3. P1: 在 aggregator.aggregate() 内做IC权重调整 (非external_votes)
  4. P1: 新建 DailyCloseCache (验证确认router无缓存, Task5必要)
  5. P2: 将signal IC backfill入口提到app层, 统一调度
  6. P2: 用strategy级IC做screener加减分 (非symbol级)
  7. 暂缓: ICFeedbackBridge (与get_ic_summary重复)
  8. 暂缓: shadow_log新模块 (用decision_recorder扩展)
- 优点：最小改动解决核心问题，复用现有基础设施，不引入冗余抽象
- 缺点：不formally "闭环"，IC消费路径扩展较慢
- 适用条件：当前阶段(20天采集期)，样本不足时不应接入决策

### 方案 C: 分阶段实施 — 先数据质量再闭环

- 提出者：辩论中可能涌现
- 核心思路：Phase 0 只做数据质量修复(NULL strategy_id + backfill积压)；Phase 1 等20天采集完成后，再评估是否需要ICFeedbackBridge
- 优点：最保守，不引入任何新抽象
- 缺点：可能过度保守，Task4 LLM空转每多一天=浪费$2.7

## 关键分歧

1. **ICFeedbackBridge 是否必要？**
   - 方案A: 必要，提供标准化接口供Playbook消费
   - 方案B: 不必要，get_ic_summary已返回所有数据，各消费者已自行实现缓存
   - 可验证: grep get_ic_summary的所有调用方是否已各自实现缓存

2. **Task4 瘦身方式：新建_run_lightweight_ic_collection vs 修signal_stage一行？**
   - 方案A: 新建轻量模式函数，完整重构TRADING阶段信号路径
   - 方案B: 在signal_stage.py L534加_skip检查，根因修复
   - 可验证: signal_stage中LLM投票是否有其他_skip守卫

3. **VotingCoordinator注入机制：external_votes vs aggregator内部调整？**
   - 方案A: 用external_votes注入"IC advisor第4票"
   - 方案B: external_votes语义是独立model score，IC权重应走aggregator内部
   - 可验证: grep aggregator.aggregate()的实现，看external_votes如何处理

4. **Screener用symbol级IC vs strategy级IC？**
   - 方案A: get_symbol_ic_bonus(symbol)，per-symbol IC
   - 方案B: 用strategy级IC(561/441/328条样本)，per-strategy IC更可靠
   - 可验证: 查signal_snapshots per-symbol样本分布

## 决策

- 选择方案：方案B精简版（做减法 — 修根因 + 复用现有基础设施）
- 决策依据：3轮Agent辩论收敛，双方一致。Claude Code独立验证3个核心事实：①get_ic_summary无内建缓存但各调用方已自行缓存 ②signal_stage L534加1行skip守卫即可消灭LLM空转 ③external_votes机制语义不适合IC权重注入。原方案8 Task中6个新建文件→0个新建文件。
- 保留空间：T20回填率达30%+后，可重新评估ICFeedbackBridge是否需要（当前18%不足）
- 负责人：Hermes
- 截止时间：2026-06-30

## 执行项

- [ ] **P0-1**: signal_stage.py L534 加 `_skip_execution_in_trading` 守卫，跳过LLM投票 (1行代码)
- [ ] **P0-2**: 回填383条NULL strategy_id（按decision_id关联decisions.db补全）+ record_signal_snapshot加默认值"unknown"防再生
- [ ] **P1-1**: MarketDataRouter 加 `_daily_bars_cache` (key=`{symbol}:{trade_date}`, TTL=当日有效) 替代新建DailyCloseCache
- [ ] **P1-2**: aggregator.aggregate() 内部加IC权重调整 (乘数[0.0, 1.5], 不用external_votes) + FeatureFlag `ic_feedback_to_voting`
- [ ] **P2-1**: signal IC backfill 入口从scripts/提到app/analysis/层，统一在run_playbook_track中调度
- [ ] **P2-2**: screener._calculate_score() 加strategy级IC加减分 (min_samples=30, 各消费者自行缓存) + FeatureFlag `ic_feedback_to_screener`
- [ ] **暂缓**: ICFeedbackBridge新模块 (与get_ic_summary重复)
- [ ] **暂缓**: shadow_log新模块 (用decision_recorder扩展)

## 验证项

- [ ] P0-1: 验证TRADING阶段不再调用voting_coordinator.vote (grep日志确认无LLM API调用)
- [ ] P0-2: 验证signal_snapshots中NULL strategy_id=0 (sqlite3 COUNT)
- [ ] P1-1: 验证同标的盘后backfill只调一次API (日志中get_daily_bars调用次数减半)
- [ ] P1-2: 验证aggregator输出包含ic_adjusted字段 (grep审计日志)
- [ ] P2-2: 验证screener加减分有min_samples守卫 (不足30的策略不加分)
- [ ] 全局: T20回填率达到30%+后重新评估是否需要ICFeedbackBridge

## 讨论记录

### 第 1 轮：hermes

**立场: 支持方案B（做减法精简版），反对方案A全量实施**

核心理由：

1. **ICFeedbackBridge是冗余抽象。** get_ic_summary()已返回by_strategy(by_direction IC map，包含ic/pval/n三个字段。order_executor已用_ic_summary_cache做进程级TTL缓存(L3354-3358)。signal_scorer已用_ic_adapt_cache做5分钟缓存(L251-256)。新建ICFeedbackBridge的4个接口全是get_ic_summary结果的薄包装，不提供任何新信息。应直接在新消费者中调get_ic_summary + 自行缓存。

2. **Task4根因是一行代码，不是新函数。** _skip_execution_in_trading=True在L172设置，但只传导到L593(组合下单)和L1223(直接下单)。signal_stage.py L534的voting_coordinator.vote调用完全不检查_skip → LLM空转。在L534前加`if getattr(scheduler, '_skip_execution_in_trading', False): skip LLM vote`即可。新建_run_lightweight_ic_collection是过度工程——技术信号已通过_generate_technical_signal(L753)产出，signal_snapshots已通过decision_recorder记录。

3. **external_votes机制语义错误。** L459-481的external_votes将注入项作为独立ModelScore参与聚合(`scores[model_name] = score`)。IC不是模型投票，是权重乘数。把IC当"第4票"会让aggregator把IC值当作一个strength参与多数投票，而不是调整其他投票的权重。正确做法是在aggregator.aggregate()内部，根据IC map对scores中各ModelScore的strength/confidence做乘数调整。

4. **383条NULL strategy_id是前置条件。** 22%的快照无strategy_id，ICFeedbackBridge的should_suppress_strategy()和get_strategy_weight_multiplier()对这22%完全无效。接入决策前必须先修数据质量。

5. **Task5 DailyCloseCache确实必要。** 验证确认MarketDataRouter.get_daily_bars无缓存(L160-196)，两个backfill走不同router实例。但应简化：不新建DailyCloseCache类，而是在MarketDataRouter中加一个_daily_bars_cache dict + TTL。

维持: 方案B的7步优先级排序。接受: Task5的必要性(已验证router无缓存)。维持: ICFeedbackBridge暂缓。

### 第 1 轮：claude-code

赞同方案B。验证结果：1) `signal_ic_tracker.py:get_ic_summary()` 每次直接查询 `signal_snapshots` 并实时计算 Spearman，函数内无任何缓存逻辑；各调用方需自行缓存。2) `signal_stage.py` 第534行 `voting_coordinator.vote()` 调用前，仅有 high-confidence candidates 跳过（阈值0.70）且未检查 `_skip_execution_in_trading`，因此在该行之前增加 skip 守卫即可根因消除 LLM 空转。3) `voting_aggregator.py` 将 `external_scores` 作为独立"llm_strategy"模型参与加权平均（L159-177），而非用 IC 作为乘数调整其他模型权重；把 IC 注入 `external_votes` 会改变聚合语义。维持方案B。

### 第 2 轮：hermes

**接受: Claude Code 对3个核心事实的独立验证。** 修正: 从"get_ic_summary已有缓存"修正为"get_ic_summary本身无缓存，缓存由各调用方自行实现（order_executor用_ic_summary_cache，signal_scorer用_ic_adapt_cache）"。

**但 Claude Code 的验证暴露了一个需要深化的点：** "各调用方需自行缓存"意味着 VotingCoordinator 和 Screener 两个新消费者都需要各自实现缓存。这引出一个判断：2个新消费者各自实现缓存(2×~10行代码) vs 共享一个ICFeedbackBridge(~60行代码 + 5min TTL)，哪个更好？

验证现有模式的一致性：
```bash
# order_executor 的缓存模式 (L68注释: "同一进程周期内 get_ic_summary() 只算一次")
grep -n '_ic_summary_cache\|_ic_summary_cache_ts\|_IC_CACHE_TTL' app/pipeline/order_executor.py
# → L68-69: 模块级变量 _ic_summary_cache + _ic_summary_cache_ts
# → L3354-3358: if not cache or expired → refresh

# signal_scorer 的缓存模式
grep -n '_ic_adapt_cache\|_ic_adapt' app/decision/signal_scorer.py
# → L251-256: 实例属性 self._ic_adapt_cache = {'ts':, 'ic_map':}
```

两种缓存模式不同：order_executor用模块级变量(进程级)，signal_scorer用实例属性(对象级)。VotingCoordinator是单例(每scheduler一个)，用实例属性更自然。Screener也是单例。各自实现缓存只需3行代码(template: check cache → miss → refresh → return)。

**结论: 2×3行 < 60行新模块。** 两个新消费者各自实现缓存遵循现有模式(order_executor和signal_scorer已验证的模式)，比新建ICFeedbackBridge更简单、更一致。ICFeedbackBridge的唯一增量价值是"统一缓存"，但5min TTL的Spearman计算本身只需50ms级(1757条数据)，缓存优化的是DB查询不是计算，两个消费者不同时调所以共享缓存命中率为零。

**收敛提议:**
- P0: signal_stage.py L534 加 `_skip_execution_in_trading` 守卫 (1行)
- P0: 修复383条NULL strategy_id (数据回填)
- P1: DailyCloseCache → 简化为 MarketDataRouter 加 `_daily_bars_cache` (不新建类)
- P1: aggregator.aggregate() 内部加IC权重调整 (不用external_votes)
- P2: signal IC backfill 入口提到 app 层
- P2: screener 用 strategy 级 IC 加减分 (各自缓存)
- 暂缓: ICFeedbackBridge, shadow_log 新模块

### 第 2 轮：claude-code

接受 Hermes 的收敛提议。它准确识别了根因：signal_stage LLM 空转只需一行守卫；IC 应作为聚合器内权重乘数而非 `external_votes`；DailyCloseCache 可下沉到 MarketDataRouter。这比原方案 A 更轻量、风险更低。

剩余顾虑：
1. NULL `strategy_id` 的根因未完全定位——是写入缺失还是历史数据？修复后需加约束/校验防再生。
2. T20 回填完成率仅 18%，strategy 级 IC 样本偏少，直接接入 screener 可能过拟合；建议先设 `min_samples` 阈值，shadow 观察后再调。
3. MarketDataRouter 缓存需明确失效策略（按 symbol+trade_date+TTL），避免跨日 stale data。
4. aggregator IC 乘数应有上下限并记录 audit，防止异常 IC 放大错误信号。

### 第 3 轮：hermes（收敛总结）

**接受: Claude Code 的4项剩余顾虑，全部纳入执行项。** 回应：

1. NULL strategy_id根因：验证写入路径。
```bash
grep -n 'record_signal_snapshot\|strategy_id' app/storage/decision_recorder.py | head -10
# → L277: from app.analysis.signal_ic_tracker import record_signal_snapshot
# 需查 record_signal_snapshot 调用时 strategy_id 参数来源
grep -n 'strategy_id' app/analysis/signal_ic_tracker.py | head -10
# → L150: strategy_id: Optional[str] = None
# → L205-210: INSERT 时 strategy_id 可选, 部分调用方不传→NULL
```
修复：回填383条NULL（按decision_id关联decisions.db补strategy_id）+ 在record_signal_snapshot中加默认值"unknown"防再生。

2. T20样本不足：接受。P2 screener注入时设min_samples=30（strategy级），不足30的策略不加减分。当前TECHNICAL_MULTI_FACTOR(561)和EARNINGS_REVISION_MOMENTUM(441)够，voting_llm(328)够但T20完成率低，先只看T1 IC。

3. 缓存失效策略：接受。MarketDataRouter._daily_bars_cache key=`{symbol}:{trade_date}`，TTL=当日有效(跨日自动miss)。

4. IC乘数边界：接受。乘数范围[0.0, 1.5]，与signal_scorer._apply_ic_adaptation的边界一致(L268-274)。加`logger.info("[IC-Adjust] ...")`审计日志。

**收敛声明: 双方一致收敛到方案B精简版。** 原方案8 Task中Task1(ICFeedbackBridge)和Task8(shadow_log)暂缓，Task4简化为1行守卫，Task2改为aggregator内部调整，Task5简化为router内置缓存。净效果：6个新建文件→0个新建文件，8 Task→7项执行(含4项P0/P1)。

### 第 3 轮：claude-code

收敛。接受方案 B 精简版：1 行守卫修复 LLM 空转、回填 NULL strategy_id、MarketDataRouter 缓存、aggregator 内部 IC 权重。无新模块。

保留顾虑：T20 回填率仅 18%，strategy 级 IC 样本仍偏少；建议 shadow 阶段持续观测，min_samples=30 达标后再全量接入。
