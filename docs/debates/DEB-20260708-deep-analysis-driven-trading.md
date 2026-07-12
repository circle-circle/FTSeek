# DEB-20260708: Deep Analysis 驱动交易方案辩论

## 背景

用户提出: deep_analysis skill通过多LLM分析讨论输出高质量报告,FTSeek运行时去掉3模型投票,充分规划→执行。

方案文档: /opt/ftseek/FTSeek-US/docs/proposals/PROPOSAL-deep-analysis-driven-trading.md

辩论原则: Karpathy(最小改动/不过度设计/防加法思维) + Ponytail(最短路径/YAGNI/删除优先)

## Pre-debate 数据收集 (已验证, 2026-07-08 更新)

### 1. 当前交易链路 (grep验证)

```
_get_watchlist (27静态+factor_engine动态26=43合并)
  → screener.screen (质量门槛+因子打分)
  → _build_instructions_from_votes (pipeline.py:412)
    → voting_coordinator.vote (pipeline.py:487, 3模型LLM调用)
    → deep_analysis门禁 (pipeline.py:627-672, 读signal_snapshots.db方向一致性)
    → 多层过滤 (MACD/MA50已禁用/deep_analysis门禁/因子冲突)
    → Instruction生成
  → run_playbook_auto_execute (pipeline.py:1804)
    → _check_intraday_trigger (pipeline.py:2125, 方向穿越触发非突破)
    → execute_instruction (pipeline.py:2093, 下单)
```

### 2. 关键代码事实 (逐条grep验证)

1. **external_votes**: voting_coordinator.py:388,470 已实现注入。pipeline.py的_build_instructions_from_votes调vote()时不传external_votes(默认None)。signal_pipeline.py:787,1183两处显式=None。→ 接口存在但生产未接线。
2. **external_votes语义**: voting_coordinator.py:480-487 将external_votes包装为独立ModelScore参与scores[model_name]=score → aggregator加权平均。语义是"第4个模型投票"而非权重乘数。(DEB-20260629 IC反馈辩论已确认此语义)
3. **_build_instructions_from_votes**: pipeline.py:412, 含voting+多层过滤+Instruction生成。函数约400行。
4. **_check_intraday_trigger**: pipeline.py:2125, **不是突破当日high/low**。实际是方向穿越触发: BUY=current_price≤entry_price×1.01(回落买), SHORT=current_price≥entry_price×0.99(反弹做空)。方案文档§9.3声称"当前检查突破当日high/low"=**事实错误**。已有±1% touch zone+SL pre-entry guard。
5. **deep_analysis门禁**: pipeline.py:627-672, 非pipeline.py:642(642是SQL行)。读signal_snapshots.db WHERE strategy_id='deep_analysis' AND symbol=? AND trade_date=?。方向冲突→SKIP, 无deep_analysis→放行。**已存在方向一致性检查**。
6. **factor_engine._get_stock_pool**: factor_engine.py:295。core_pool + Finnhub异动(_discover_finnhub_movers) → 合并 → _apply_quality_gate。已有动态发现路径。
7. **llm_synth.synthesize()**: 在skill脚本 /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:561, 不在FTSeek app目录。已注入claim_ic_cache(llm_synth.py:578 _load_claim_ic_for_prompt)。产出bull_case/bear_case/verdict/execution_plan。
8. **ObsidianWriter.write_symbol_analysis**: app/core/obsidian_writer.py:457, 已存在。
9. **ic_writer.write_signal_snapshot**: deep_analysis skill脚本, 直接写signal_snapshots.db。
10. **voting_aggregator.aggregate**: voting_aggregator.py:16。已有P2.1准确率闭环(WeightTracker)+P1 IC反馈(FeatureFlag ic_feedback_to_voting默认False)。
11. **claim_ic_cache.json**: /root/.hermes/skills/deep-stock-analysis/scripts/claim_ic_cache.json。215样本, overall_ic_t1=0.0123, overall_ic_t5=0.2962。已在llm_synth prompt注入。

### 3. 方案核心改动

Phase 1 (外部脚本, 冻结期):
- market_scanner.py: 多渠道+四信号评分+板块叙事+操作分桶 (skill脚本, 不在app/)
- data_collector.py: 新增3个collect函数 (skill脚本)
- llm_synth.py: 新增审查官(阶段6)+最终裁定(阶段7) (skill脚本)
- pipeline.py(skill): 输出bridge JSON (skill脚本)

Phase 2 (运行时, 冻结期后):
- factor_engine.py: _get_stock_pool加Source 3读bridge (~10行)
- pipeline.py(FTSeek): _read_bridge_file + _build_instructions_from_bridge (~60行), 删除voting调用 (~-40行)
- pipeline.py: _check_intraday_trigger读entry_range (~10行)

### 4. 方案事实错误 (grep验证)

| 方案声称 | 实际代码 | 影响 |
|---------|---------|------|
| §9.3 "_check_intraday_trigger当前检查突破当日high/low" | pipeline.py:2125 方向穿越触发(entry_price±1%), 非突破 | 方案§9.3的"改后检查"已在当前代码中实现。改动量~10行→0行 |
| §5.3 "deep_analysis方向门禁在pipeline.py:642" | pipeline.py:627-672 (627是起始行, 642是SQL行) | 行号不准, 小问题 |
| §5.1 "factor_engine._get_stock_pool()加Source 3" | _get_stock_pool已有动态发现(_discover_finnhub_movers), 已有质量闸 | Source 3=在已有动态发现链上加一路径, 非全新 |
| §附录A "data_collector.collect_all()" | FTSeek app/无data_collector.py, 在skill脚本 | 方案混淆了skill脚本和app代码 |
| §附录A "llm_synth.synthesize()" | 在skill脚本不在app/ | 同上 |

### 5. Karpathy/Ponytail检查点

**Karpathy防加法思维**: 方案先想"加什么"(多渠道/四信号/审查官/裁定)还是先想"删什么"(3模型投票/post-vote过滤)?
- 方案删除了3模型投票 ✓
- 但新增了7个阶段(4个LLM调用类型) + 8个新函数 + bridge文件格式 ✗ (加法>减法)

**Ponytail最短路径**: bridge文件是最短路径吗?
- 替代方案A: 不用bridge文件, 直接用external_votes注入deep_analysis方向到voting_coordinator(已有接口, 需接线~5行)
- 替代方案B: 不删3模型投票, 只扩大候选池(factor_engine已有动态发现, 加Finnhub earnings/insider)
- 替代方案C: deep_analysis已写signal_snapshots, 门禁已读(627-672)。只需让deep_analysis产出更多标的的方向→自动进入门禁验证→通过则instruction产出。0行运行时改动。

**关键发现**: deep_analysis门禁(pipeline.py:627)已实现"deep_analysis方向一致性检查"。如果deep_analysis盘前产出N只标的的方向写入signal_snapshots, pipeline运行时voting产出方向后, 门禁自动做交叉验证。**deep_analysis已经接入了运行时决策链路**, 只是通过"门禁"而非"替代voting"。

## 讨论记录

### 第 1 轮：hermes

**立场: 方案过度设计。deep_analysis已通过门禁接入运行时。最小路径=扩大deep_analysis覆盖面, 不删voting不加bridge。**

**修正: 基于pre-debate验证更新立场。原R1部分论点保留但修正关键事实。**

**论点1: _check_intraday_trigger已实现方案§9.3的"改后检查"**

方案§9.3声称当前检查"突破当日high/low", 改后检查"实时价格∈entry.price_range"。

grep验证(pipeline.py:2125-2222): 当前代码已是方向穿越触发:
- BUY: current_price ≤ entry_price × 1.01 (回落买)
- SHORT: current_price ≥ entry_price × 0.99 (反弹做空)
- SL pre-entry guard: 触发后检查是否越过stop_loss

方案声称的"改后检查"已在当前代码中实现。改动量~10行→0行。方案的事实前提错误。

**论点2: deep_analysis已通过门禁接入运行时决策链路**

方案核心假设: "deep_analysis只写signal_snapshots做IC追踪,不注入候选/方向"。

grep验证(pipeline.py:627-672): deep_analysis门禁**已存在**。读signal_snapshots WHERE strategy_id='deep_analysis', 做方向一致性检查:
- voting BUY + deep LONG → 放行(交叉验证)
- voting BUY + deep SHORT → SKIP(冲突不赌)
- 无deep_analysis → 放行(不阻断)

**deep_analysis已经接入运行时决策链路**, 通过"门禁"模式而非"替代voting"模式。方案要改成"替代voting"=从"交叉验证"升级为"唯一决策源"。这是一个质的跳跃, 不是从0到1的接入。

Karpathy: "不重构没坏的东西"。门禁模式=安全网(voting错时deep拦, deep缺时不阻断)。替代模式=单点故障(deep错时无交叉验证)。

**论点3: 7阶段4次LLM = 过度设计 (保留原R1)**

阶段6审查官: 如果阶段5的llm_synth已产出verdict+execution_plan, IC反馈已在prompt注入(llm_synth.py:578), 审查官=第二次调同LLM看同样数据。

Ponytail: 阶段6/7的增量价值是什么? 如果审查官能发现阶段5的错误, 说明阶段5的prompt不够好——修prompt比加一层LLM更短。如果审查官发现不了, 就是冗余。

**论点4: bridge文件 vs external_votes (修正)**

原R1说external_votes是"0行新代码"。修正: external_votes需接线(~5行, pipeline.py调vote()时传参)。但比bridge的60行新代码+40行删除更短。

external_votes语义问题: DEB-20260629已确认external_votes="第4个模型投票"参与聚合, 不是权重乘数。如果把deep_analysis当external_vote注入, aggregator会把deep的方向当1个模型vs 3个实时模型→少数服从多数→deep被outvoted。**这削弱了deep_analysis的决策权重。**

**收敛立场**: 

最佳方案不是方案原版(bridge替代voting), 也不是external_votes(语义不对), 而是:

**方案D (做减法版)**: 
- Phase 1: deep_analysis盘前扩大覆盖面(多渠道采集+四信号评分筛标的), 产出N只标的方向写入signal_snapshots(已有)。不加审查官/裁定/bridge。
- Phase 2: 0行运行时改动。deep_analysis门禁已存在(627), voting产出方向后自动交叉验证。
- 效果: deep覆盖面扩大→更多标的通过门禁→更多instruction产出。voting保留(3模型实时判断+deep交叉验证=双保险)。

改动量: Phase 1约6h(3个collect+四信号, 只改skill脚本), Phase 2 = 0h。总6h vs方案19h。

**让步**: 如果用户认为voting的3模型NEUTRAL问题是根因(不只是候选池窄), 那么方案D不够。需要替代voting。但替代方案应该是"deep_analysis单模型直接定方向"(1次LLM), 不是"7阶段4次LLM"(过度设计)。

### 第 1 轮：claude-code

**立场: CRITIC — 方案D有结构性盲区，门禁模式解决不了voting瓶颈。**

**反驳1: NEUTRAL问题未解决，门禁永远触达不了30%的候选。**

代码事实: pipeline.py:524 `is_neutral→SKIP+continue`，在deep_analysis门禁(line 627)之前。signal_snapshots实证: voting_llm产出80 NEUTRAL / 263总信号 = **30.4%候选在门禁前被杀**。deep_analysis门禁只对BUY/SELL_SHORT生效(line 634)，永远看不到NEUTRAL。Hermes说"扩大deep覆盖面→更多标的通过门禁"，但门禁的前提是voting先给出方向。voting说NEUTRAL→deep_analysis根本没机会发言。这是**结构性盲区**，不是覆盖面问题。

**反驳2: deep_analysis自身也有NEUTRAL问题，且覆盖面极窄。**

signal_snapshots数据: deep_analysis共57条 vs voting_llm 763条 = 7.5%覆盖率。且deep_analysis自身产出13 NEUTRAL / 57总 = **22.8%无方向**。即使方案D把deep扩到200只，其中~46只是NEUTRAL无方向，且voting仍会在上游杀掉30%。instruction吞吐瓶颈是voting NEUTRAL，不是deep覆盖面。

**反驳3: 方案D "0行运行时改动" = 接受现状。**

方案D声称"Phase 2 = 0h"。但现状是: voting_llm LONG IC=-0.0674(注释line 628)，即**voting做多方向是负预测**。3模型NEUTRAL+负IC = 系统性产出低质量方向信号。门禁只能拦voting错的(deep≠voting→SKIP)，不能救voting沉默的(NEUTRAL→SKIP)。Hermes的"双保险"实际是**单向过滤**: 只减不增。方案D的真正效果=扩大deep覆盖面来多做几个交叉验证，但instruction产出仍受voting方向+NEUTRAL率双重约束。

**让步**: Hermes正确指出7阶段过度设计。但方案D不够——需要在NEUTRAL路径上增加deep_analysis fallback（voting NEUTRAL时查deep方向作为替代决策），而非仅在方向路径上做门禁。改动~15行，仍远小于bridge方案。

### 第 2 轮：hermes

**接受: Claude Code的反驳1——NEUTRAL结构性盲区是真实的。**

pipeline.py:524 `is_neutral→continue` 确实在deep_analysis门禁(627)之前。grep验证确认。voting说NEUTRAL→候选直接SKIP→deep_analysis永远没机会做交叉验证。这是我R1方案D的核心盲区: 我以为"扩大deep覆盖面→更多标的通过门禁", 但门禁的前提是voting先给出非NEUTRAL方向。

**修正: 从方案D转向方案D+ (NEUTRAL fallback)**

接受Claude Code的让步方向。最小改动: 在pipeline.py:524的`is_neutral→continue`之前, 加deep_analysis fallback:

```python
# pipeline.py:524 之前, ~15行
if is_neutral:
    # deep_analysis fallback: voting NEUTRAL时查deep方向
    _deep_snap = _query_deep_snapshot(symbol, today)  # 已有查询逻辑(627-646)
    if _deep_snap and _deep_snap["direction"] in ("LONG", "SHORT"):
        vote_direction = "BUY" if _deep_snap["direction"] == "LONG" else "SELL_SHORT"
        is_neutral = False
        logger.info(f"[playbook] {symbol} voting NEUTRAL→deep fallback {_deep_snap['direction']}")
    # deep无方向或NEUTRAL→维持SKIP (原逻辑)
```

**为什么这是最短路径**:
1. 查询逻辑已有(627-646的SQL), 提取为helper复用
2. 不删voting(保留IC记录链路)
3. 不加bridge文件(0新接口)
4. 不加审查官/裁定(0新LLM调用)
5. ~15行改动 vs bridge方案100行改动

**对Claude Code反驳2的回应**: deep_analysis覆盖窄(7.5%)和自身NEUTRAL(22.8%)是数据问题不是架构问题。Phase 1扩大候选池(多渠道采集)→覆盖面自然扩大。不需要改架构来解覆盖率问题。

**对Claude Code反驳3的回应**: voting_llm LONG IC=-0.0674是事实, 但这不是"必须删voting"的理由。voting有3模型交叉验证+VotingAccuracyTracker闭环(P2.1已接通, aggregator.py:56-89)。问题不是voting机制本身, 是voting在NEUTRAL时太保守。deep_analysis fallback解决了"voting沉默时deep补位", 保留了voting非NEUTRAL时的交叉验证价值。

**维持: 7阶段4次LLM过度设计**。Claude Code也同意(让步中)。收敛点: 不需要bridge文件, 不需要审查官/裁定, 只需要NEUTRAL fallback(~15行)+Phase 1扩大候选池(只改skill脚本)。

**新增关键分歧**: 方案D+的deep_analysis fallback vs 方案原版的bridge替代voting。核心tradeoff:
- D+: deep作为fallback(仅voting NEUTRAL时补位), 保留voting主路径, ~15行改动
- 原版: deep作为唯一决策源(替代voting), 删voting, ~100行改动, 需bridge新接口

D+更短更安全。但Claude Code可能认为"voting IC为负→voting主路径不可靠→fallback不够, 需替代"。这是下一轮的焦点。

### 第 2 轮：claude-code

**部分接受D+，但指出三个残留问题。**

**1. D+解决NEUTRAL盲区，但不解决负IC主路径。** 同意fallback救回30%被NEUTRAL杀掉的候选。但voting LONG IC=-0.0674意味着：非NEUTRAL的70%路径上，voting方向本身是负预测。D+保留voting做主路径=保留一个负IC信号做多数决策。吞吐提升有，质量提升存疑。**建议**: D+落地时加监控——对比fallback路径 vs voting主路径的后续收益，用数据决定下一步是扩fallback还是替代voting。

**2. 15行比bridge简单，但引入条件分叉。** `_query_deep_snapshot`提取为helper后，pipeline关键路径变成两条分支：voting驱动 + deep-fallback驱动。每条需独立测试/监控/日志。维护成本高于15行暗示的，但远低于bridge的100行+新文件格式。**可接受**。

**3. 候选发现问题未解，Hermes说"Phase 1扩池"但D+代码没包含。** D+的fallback只在screener已通过的~43只上生效。deep_analysis看不到watchlist之外的标的。Hermes的Phase 1(多渠道采集扩池)是独立工作，D+不等价于Phase 1。**需明确**: D+和Phase 1是并列交付，不是包含关系。

**收敛立场**: 接受D+作为最小可行改动（~15行+监控），但附带条件：(a) 加fallback vs 主路径的收益对比监控；(b) Phase 1扩候选池作为独立交付项同步推进；(c) 若监控显示voting主路径持续负IC，下一步是替代voting而非继续修补。分歧收窄到：Hermes认为D+足够，我认为D+是第一步但不够——负IC主路径终需面对。

### 第 3 轮：hermes (收敛)

**接受: Claude Code的收敛立场。D+是第一步, 负IC主路径终需面对。**

三个残留问题的回应:

**1. 负IC监控**: 同意。D+落地时加_filter_audit计数器(fallback触发次数)+盘后收益对比(fallback vs voting主路径PnL)。数据说话, 不猜测。如果3周内fallback路径PnL显著优于voting主路径→Phase 2考虑"deep优先,voting fallback"(反转)。代码量=加2行计数器+1行日志。

**2. 条件分叉维护**: 同意可接受。`_query_deep_snapshot`提取为helper后, 两条分支共用同一个deep查询逻辑, 维护点=1个helper函数+1个if分支。远低于bridge的_read_bridge_file+_build_instructions_from_bridge+bridge JSON schema。

**3. Phase 1和D+并列交付**: 同意。明确:
- Phase 1 (skill脚本, 冻结期可做): 多渠道采集(3个collect函数)+四信号评分→扩大deep_analysis覆盖面→更多标的写入signal_snapshots
- Phase 2 (运行时, 冻结期后, ~15行): NEUTRAL fallback + 监控计数器
- Phase 3 (条件触发, 数据驱动): 如果监控显示voting持续负IC→替代voting

**最终收敛**:

| 维度 | 原方案 | 辩论后收敛 |
|------|--------|-----------|
| LLM调用 | 7阶段4次类型(市况+个股+审查+裁定) | 1次(llm_synth.synthesize, 已有) |
| 新接口 | bridge JSON文件格式 | 无(复用signal_snapshots) |
| 运行时改动 | ~100行(60新+40删) | ~15行(NEUTRAL fallback+计数器) |
| 删除代码 | voting_coordinator调用删除 | 不删(保留IC记录链路) |
| 候选池 | bridge注入新候选 | Phase 1 skill脚本扩池(不改运行时) |
| 触发位 | bridge entry_range | 已有(pipeline.py:2125方向穿越触发) |
| 安全机制 | 保留 | 保留(门禁627+voting交叉验证+SL guard) |

**消除的冲突链路**:
1. bridge文件 vs signal_snapshots: 收敛到signal_snapshots(已有), 不加bridge
2. 删voting vs 保留voting: 收敛到保留+fallback, 不删
3. 审查官 vs llm_synth: 收敛到llm_synth(已有verdict), 不加审查官
4. entry_range vs 方向穿越: 收敛到方向穿越(已有), 不改trigger

**并行策略消除**:
- 原方案: bridge(盘前) + voting(盘中) = 两条决策路径并行→冲突
- 收敛后: voting主路径 + deep fallback(仅NEUTRAL时) = 一条路径+补位→无冲突

## Action Items

- [x] **P0-1**: Phase 1 — deep_analysis skill新增3个collect函数 ✅ 已完成 (collect_earnings_surprise/insider_movers/after_hours_movers)
- [x] **P0-2**: Phase 1 — deep_analysis skill新增四信号评分(_four_signal_score) ✅ 已完成 (catalyst+momentum+sentiment+value+featured override+value trap)
- [x] **P0-补充**: Phase 1 — 板块轮动叙事(_sector_narrative) + 操作分桶(_bucketing) + 质量闸(_quality_gate) ✅ 已完成
- [x] **P0-bugfix**: 修复change_rate=0 bug (富途snapshot无change_rate字段, 改用last/prev计算) ✅ 已完成+验证
- [ ] **P1-1**: Phase 2 — pipeline.py:524 NEUTRAL fallback (~15行, 提取_query_deep_snapshot helper+if分支) (冻结期后)
- [ ] **P1-2**: Phase 2 — 加_filter_audit计数器(fallback_trigger_count) + 盘后收益对比日志 (冻结期后)
- [ ] **P1-3**: Phase 2 — 更新deep_analysis cron prompt传FTSeek watchlist标的(--symbols参数), 确保deep覆盖面≥voting覆盖面 (冻结期可做, 不改运行时)
- [ ] **P2-1**: Phase 3 — 3周后评估voting主路径IC, 如果持续负IC→考虑deep优先+voting fallback反转 (数据驱动, 条件触发)
- [ ] **删除**: 不实施原方案§4.2(阶段6审查官)、§4.3(阶段7裁定)、§3(bridge JSON格式)、§5.1(_read_bridge_file/_build_instructions_from_bridge)、§5.4(删voting调用)

## 验证项

- [ ] Phase 1完成后: deep_analysis覆盖标的数从57→150+, signal_snapshots写入数翻倍
- [ ] Phase 2完成后: 当日voting NEUTRAL的标的中, ≥30%被deep fallback救回→非SKIP instruction
- [ ] 3周后: fallback路径PnL vs voting主路径PnL对比, 数据决定是否反转
