# 方案: 规划-执行分离架构 (Plan-Execute Separation)

> 状态: 草案
> 日期: 2026-07-08
> 关联: DEB-20260708-deep-analysis-driven-trading.md, PROPOSAL-deep-analysis-driven-trading.md
> 原则: Karpathy(最小改动/防加法) + Ponytail(最短路径/复用已有)

---

## 1. 现状诊断: 三个断裂点

### 当前链路 (grep验证)

```
FTSeek运行时 (盘中):
  _get_watchlist (27静态+factor_engine动态)
    → screener.screen
    → _build_instructions_from_votes
      → voting_coordinator.vote()          ← 3模型LLM, 盘中调, 30-60s/标的
      → record_signal_snapshot()           ← 写signal_snapshots (strategy_id=voting_llm)
      → deep_analysis门禁 (pipeline.py:627) ← 读signal_snapshots, 方向一致性检查
      → compute_instruction_values()       ← FTSeek自己算entry/SL/TP/position_size
      → Instruction → save_instruction → DB
    → run_playbook_auto_execute
      → _check_intraday_trigger            ← 方向穿越触发 (entry_price±1%)
      → execute_instruction → 下单
    → monitor_playbook_sim_exits           ← SL/breakeven/trailing/PRE_CLOSE/EOD

deep_analysis skill (盘前):
  pipeline.py → scan → analyze → ic_writer → signal_snapshots.db
    → signal_gate (评估, 不执行)
    → adapter.to_voting_result()           ← 输出4种格式, 但无人消费
    → ObsidianWriter.write_symbol_analysis ← 归档
```

### 断裂点

| # | 断裂 | 现状 | 影响 |
|---|------|------|------|
| 1 | **adapter输出无人消费** | deep_analysis的adapter输出4种格式(to_voting_result/to_signal_score/to_exit_meta/to_ic_snapshot), FTSeek运行时不读 | deep_analysis的entry/SL/TP/conditions/strategy_type全被FTSeek重算覆盖 |
| 2 | **分桶/退出策略脱节** | deep_analysis有strategy_type(quality_breakout/oversold_bounce/trend_short)和position_size_hint, FTSeek用conditions→factor_exit_rules映射退出, 两者不连通 | deep_analysis的"超跌反弹型应小仓+快止盈"无法传递到FTSeek退出策略 |
| 3 | **反馈环路断裂** | feedback_loop更新strategy_memory+claim_ic_cache, 但只回写deep_analysis的LLM prompt, 不回写FTSeek的退出参数 | 交易结果学到"LONG+SL_ATR_STOP亏钱"但FTSeek退出参数不变 |

### 为什么不是"全删voting, deep_analysis直接定方向"?

辩论已收敛(DEB-20260708):
- voting LONG IC=-0.067(负预测), 但voting有3模型交叉验证+VotingAccuracyTracker闭环
- deep_analysis IC=+0.62(n=11), 但样本量不足
- **最短路径**: 不是删voting, 是让deep_analysis的规划输出真正影响FTSeek执行参数

---

## 2. 方案: 三个最小改动打通断裂

### 设计原则

**Ponytail**: 不建bridge文件, 不加新模块, 不删voting。复用已有的3个数据通道:
1. signal_snapshots.db (已有, deep_analysis写入, FTSeek门禁读取)
2. playbook_instructions表 (已有, FTSeek写入+执行)
3. claim_ic_cache.json (已有, feedback_loop写入, LLM prompt读取)

**Karpathy**: 每个改动<20行, 只改消费端(FTSeek), 不改生产端(deep_analysis skill)。

### 改动1: deep_analysis输出strategy_type到signal_snapshots (skill侧, ~5行)

deep_analysis的ic_writer.write_signal_snapshot已经写signal_snapshots.db, 只需在factors_json里加strategy_type和position_size_hint。

```python
# ic_writer.py: write_signal_snapshot() 的factors_json里追加
factors_json = json.dumps({
    ...  # 现有claim_type列表
    "strategy_type": analysis.get("strategy_type", ""),  # 新增
    "position_size_hint": analysis.get("position_size_hint", ""),  # 新增
    "signal_scores": analysis.get("signal_scores", {}),  # 新增
    "bucket_reason": analysis.get("bucket_reason", ""),  # 新增
})
```

**FTSeek侧消费** (冻结期后, ~10行): pipeline.py:627门禁查询时, 同时读factors_json里的strategy_type, 写入Instruction的metadata。compute_instruction_values用position_size_hint调整仓位乘数。

### 改动2: FTSeek用deep_analysis的conditions替代自己映射 (冻结期后, ~15行)

当前FTSeek的conditions来自voting_coordinator的factors(voting_llm的粗粒度), deep_analysis的adapter._map_conditions已经做了更精细的映射(bull_case→bullish conditions, bear_case→bearish conditions, 方向一致)。

在pipeline.py:627门禁通过后, 如果deep_analysis有conditions, 用deep的conditions替代voting的:

```python
# pipeline.py:627 门禁通过后 (~15行)
if _snap_row is not None and _deep_dir == _expected:
    # deep_analysis背书 → 用deep的conditions替代voting的
    import json as _json
    _deep_factors = _json.loads(_snap_row["factors_json"] or "{}")
    if _deep_factors.get("conditions"):
        conditions = _deep_factors["conditions"]  # deep_analysis的精细映射
        logger.info(f"[playbook] {symbol} 使用deep_analysis conditions: {conditions}")
```

**效果**: FTSeek的factor_exit_rules根据deep_analysis的conditions选择退出策略。deep_analysis说"quality_breakout+above_ma20"→FTSeek用trailing stop让利润奔跑; deep_analysis说"oversold_bounce+rsi_oversold"→FTSeek用快止盈。

### 改动3: feedback_loop回写FTSeek退出参数 (skill侧, ~20行)

当前feedback_loop只更新strategy_memory和claim_ic_cache。增加: 如果某pattern持续亏损, 写一个exit_param_override.json, FTSeek盘前读取调整factor_exit_rules。

```python
# feedback_loop.py: extract_lessons() 里, 如果pattern PnL持续为负
if pattern_pnl < 0 and pattern_count >= 10:
    overrides[pattern_key] = {
        "time_exit_hours": current_value * 1.5,  # 放宽时间止损
        "trailing_enabled": False,  # 关闭trailing(亏钱pattern不适合让利润奔跑)
    }
# 写 exit_param_override.json
```

**FTSeek侧消费** (冻结期后, ~5行): pipeline.py启动时读exit_param_override.json, 覆盖factor_exit_rules的对应字段。

---

## 3. 良性循环闭环

```
盘前 (deep_analysis skill):
  scan(多渠道) → analyze(LLM) → ic_writer(signal_snapshots + strategy_type + conditions)
                                        ↓
盘前 (FTSeek):  读signal_snapshots → deep方向背书 → deep conditions注入instruction
                                        ↓
盘中 (FTSeek):  voting(3模型) → 门禁(deep交叉验证) → instruction(deep conditions)
  → trigger → execute → monitor_playbook_sim_exits(deep conditions → factor_exit_rules)
                                        ↓
盘后 (deep_analysis skill):
  feedback_loop → trade_records → strategy_memory + claim_ic_cache
    → exit_param_override (持续亏损pattern → 调整退出参数)
                                        ↓
次日盘前:  claim_ic_cache → LLM prompt(论据有效性反馈)
           exit_param_override → FTSeek factor_exit_rules(退出参数优化)
           strategy_memory → signal_gate(策略推荐)
```

**循环的3个反馈通道**:

| 通道 | 数据流 | 已有? | 改动 |
|------|--------|-------|------|
| IC反馈 | claim_ic_cache → llm_synth prompt | ✅ 已有 | 0行 |
| 退出参数反馈 | trade_records → exit_param_override → FTSeek factor_exit_rules | ❌ 断裂 | ~25行(skill 20 + FTSeek 5) |
| 策略记忆 | trade_records → strategy_memory → signal_gate | ✅ 已有 | 0行 |

---

## 4. 与辩论方案D+的关系

| 辩论Action Item | 本方案对应 | 状态 |
|----------------|-----------|------|
| P0-1: 3个collect函数 | 已完成 | ✅ |
| P0-2: 四信号评分 | 已完成 | ✅ |
| P1-1: NEUTRAL fallback (~15行) | 改动2的子集(门禁通过时用deep conditions) | 冻结后 |
| P1-2: fallback监控计数器 | 改动2附带 | 冻结后 |
| P1-3: cron传watchlist标的 | 独立项 | 冻结期可做 |
| **新增**: strategy_type注入 | 改动1 | ~5行(skill) + ~10行(FTSeek) |
| **新增**: conditions替代 | 改动2 | ~15行(FTSeek) |
| **新增**: 退出参数反馈 | 改动3 | ~25行 |

**与D+的区别**: D+只做NEUTRAL fallback(救回被voting杀掉的候选)。本方案更进一步: 即使voting非NEUTRAL, 也让deep_analysis的conditions/strategy_type影响FTSeek退出策略。这是"规划影响执行"而不只是"规划替代决策"。

---

## 5. 冻结期可做的事 (不改FTSeek运行时)

1. **ic_writer写strategy_type+position_size_hint到factors_json** (~5行, 只改skill)
2. **更新cron prompt传FTSeek watchlist标的** (--symbols参数, 只改cron)
3. **feedback_loop写exit_param_override.json** (~20行, 只改skill)
4. **adapter输出加strategy_type字段** (~3行, 只改skill)

冻结期后做:
1. pipeline.py:627门禁读factors_json的strategy_type+conditions (~15行)
2. pipeline.py启动读exit_param_override.json (~5行)

---

## 6. 补充: 5个遗漏的断裂点

### 补充1: avoid列表 → FTSeek global_blacklist

deep_analysis扫描产出avoid桶(falling knife: 当日跌>5%且5日跌>15%), FTSeek有global_blacklist机制(risk_stage.py:58, 读unified_config.yaml)。但blacklist是静态配置(QCOM/LCID/TME等), deep_analysis的动态avoid列表无法注入。

**解法** (skill侧~10行, 冻结期可做): deep_analysis pipeline结束后, 把avoid标的写入`data/dynamic_blacklist.json`。FTSeek risk_stage的`_check_global_blacklist`加一行读这个文件(冻结后~3行)。比改unified_config.yaml更短(不改配置文件, 只加一个文件读取)。

### 补充2: deep_analysis scan候选 → daily_active_pool.json

FTSeek factor_engine._get_stock_pool()已经读`data/daily_active_pool.json`(当前34只, 含POSITION+MANUAL+PLAN)。deep_analysis scan发现的候选(多渠道39只, 含watchlist外的OXY/COP/NET等)可以写入这个文件。

**解法** (skill侧~15行, 冻结期可做): deep_analysis pipeline结束后, 把scan top_bullish+top_bearish的symbol追加到daily_active_pool.json的entries里(source="SCAN", expires_at=当日收盘)。FTSeek factor_engine自动读(已有逻辑, 0行改动)。

**效果**: FTSeek候选池从34→50+只, 且包含deep_analysis发现的新标的(OXY/COP等)。不需要改factor_engine代码。

### 补充3: sector_rotation → plan_validator

deep_analysis的_collect_sector_data产出`{strongest: [...], weakest: [...], sector_changes: {...}}`。FTSeek plan_validator.py:227-248已经用`sector_rotation.get("strongest"/"weakest")`做板块方向检查(LONG不在最弱, SHORT不在最强)。格式兼容!

但plan_validator的sector_rotation来自premarket_scanner(FTSeek自己的11个SPDR ETF扫描), 不是deep_analysis。

**解法** (skill侧~5行, 冻结期可做): deep_analysis把sector_rotation写入`data/deep_sector_rotation.json`。FTSeek plan_validator加一行读这个文件作为fallback(如果premarket_scanner失败, 用deep的sector_rotation; 冻结后~3行)。

**效果**: 即使premarket_scanner出问题, 板块方向检查仍有数据。且deep_analysis的sector_changes更丰富(有个股→板块映射)。

### 补充4: 多批次入场 → FTSeek instruction

deep_analysis LLM prompt要求"入场至少分2批, 每批有触发条件"(llm_synth.py)。LLM输出execution_plan.entry_strategy含分批描述。但FTSeek的Instruction dataclass只有一个entry_price, 不支持多批次。

**Ponytail判断**: 多批次入场是过度设计。FTSeek已有`_check_intraday_trigger`(方向穿越触发, entry_price±1%), 实质是限价单逻辑——价格回落到entry附近才触发。分批入场=多个entry_price=多个trigger check。复杂度翻倍但收益有限(FTSeek是日内交易, max_hold=1天, 分批节省的滑点<0.1%)。

**建议**: 不做。LLM的分批建议存到thesis/reasoning里供人工参考, FTSeek执行用单entry_price+方向穿越触发。

### 补充5: 持仓已存在时的deep_analysis退出建议

deep_analysis分析某标的时, 如果FTSeek已有持仓, deep_analysis的方向可能与持仓方向矛盾(持多但deep说SHORT)。当前没有机制让deep_analysis影响已有持仓的退出。

**Ponytail判断**: 不做。已有持仓的退出由FTSeek ExitMonitor管(SL/TP/EOD/breakeven), 这是安全机制不该被外部覆盖。deep_analysis对新仓的影响通过conditions+门禁实现, 对老仓的影响通过feedback_loop(盘后学教训→次日调参)间接实现。直接干预老仓退出=引入新的故障面。

---

## 7. 更新后的完整方案

| 改动 | 位置 | 行数 | 冻结期? | 效果 |
|------|------|------|---------|------|
| 1. ic_writer写strategy_type+conditions到factors_json | skill ic_writer.py | ~5 | ✅ | deep_analysis的规划数据进入signal_snapshots |
| 2. 门禁读factors_json用deep conditions | FTSeek pipeline.py:627 | ~15 | ❌ | deep的conditions影响FTSeek退出策略选择 |
| 3. NEUTRAL fallback | FTSeek pipeline.py:524 | ~15 | ❌ | voting沉默时deep补位 |
| 4. fallback监控计数器 | FTSeek pipeline.py | ~3 | ❌ | 数据驱动决定是否反转 |
| 5. avoid列表→dynamic_blacklist.json | skill pipeline.py | ~10 | ✅ | falling knife标的阻止开仓 |
| 6. scan候选→daily_active_pool.json | skill pipeline.py | ~15 | ✅ | FTSeek候选池扩大, 0行运行时改动 |
| 7. sector_rotation→deep_sector_rotation.json | skill pipeline.py | ~5 | ✅ | 板块方向检查有fallback数据 |
| 8. feedback_loop写exit_param_override.json | skill feedback_loop.py | ~20 | ✅ | 持续亏损pattern→调整退出参数 |
| 9. FTSeek读exit_param_override | FTSeek pipeline.py启动 | ~5 | ❌ | 退出参数优化 |
| **不做** | | | | |
| 多批次入场 | — | — | — | 过度设计, 日内交易分批收益<0.1% |
| deep干预老仓退出 | — | — | — | ExitMonitor是安全机制不该被外部覆盖 |
| bridge文件 | — | — | — | 辩论已否决, 复用signal_snapshots |
| 审查官/裁定 | — | — | — | 辩论已否决, llm_synth已有verdict |

**冻结期可做**: 改动1+5+6+7+8 = ~55行, 全在skill侧, 不碰FTSeek运行时。
**冻结期后**: 改动2+3+4+9 = ~38行, 在FTSeek pipeline.py。

---

## 8. 良性循环闭环 (更新)

```
盘前 (deep_analysis skill):
  scan(6渠道) → 四信号评分 → 分桶(quality/oversold/trend_short/avoid/watch)
    → analyze(LLM 8维数据+IC反馈+交易经验注入)
    → ic_writer(signal_snapshots + strategy_type + conditions + signal_scores)
    → avoid列表 → data/dynamic_blacklist.json          ← 补充1
    → scan候选 → data/daily_active_pool.json            ← 补充2
    → sector_rotation → data/deep_sector_rotation.json  ← 补充3
                                        ↓
盘前 (FTSeek, 不改运行时):
  factor_engine读daily_active_pool.json → 候选池扩大(自动, 0行改动)
  risk_stage读dynamic_blacklist.json → avoid标的拦截(冻结后加3行)
  plan_validator读deep_sector_rotation.json → 板块方向检查(冻结后加3行)
                                        ↓
盘中 (FTSeek, 冻结后改):
  voting(3模型) → 门禁(deep背书+deep conditions注入) → instruction
    → NEUTRAL fallback(deep补位) → trigger → execute
    → monitor_playbook_sim_exits(deep conditions → factor_exit_rules)
                                        ↓
盘后 (deep_analysis skill):
  feedback_loop → trade_records → strategy_memory + claim_ic_cache
    → exit_param_override (持续亏损pattern → 调整退出参数)
    → claim_ic_cache → llm_synth prompt (论据有效性反馈, 已有)
                                        ↓
次日盘前:  全部数据通道刷新, 循环重启
```

**5个反馈通道** (3个已有+2个新增):

| 通道 | 数据流 | 状态 |
|------|--------|------|
| IC反馈 | claim_ic_cache → llm_synth prompt | ✅ 已有 |
| 策略记忆 | trade_records → strategy_memory → signal_gate | ✅ 已有 |
| 退出参数 | trade_records → exit_param_override → FTSeek factor_exit_rules | 新增(~25行) |
| 候选池 | scan结果 → daily_active_pool.json → factor_engine | 新增(~15行, 0行FTSeek) |
| 黑名单 | avoid桶 → dynamic_blacklist.json → risk_stage | 新增(~13行, 3行FTSeek) |
