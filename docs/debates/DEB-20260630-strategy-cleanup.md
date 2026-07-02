# DEB-20260630: 策略梳理与优化方案

## 背景

FTSeek-US退出/信号/Guardrail策略经过多轮修改后变得混杂。今天修复了7个工程断点，又通过辩论重新启用了pre_close。但核心混乱未解决：存在两套退出系统、配置矛盾、功能开关不一致。需要梳理并给出简化方案。

## 目标

1. 梳理当前所有策略配置的实际生效状态
2. 识别矛盾和冗余
3. 提出简化方案（做减法，不新增复杂度）

## 数据与实证

### 两套退出系统并行

| 系统 | 配置源 | 监控对象 | 退出规则 |
|------|--------|---------|---------|
| **ExitMonitor** (scan_positions) | exit_policy.yaml | 所有owner=auto持仓(5个) | atr_stop(2.0×ATR,max2%) + pre_close(loss_flat=true,close_profit_atr=2.0) |
| **_check_playbook_exit_rules** (pipeline.py) | playbook_horizon.yaml | PLAYBOOK_SIM trade_records(BIDU/MSFT) | SL_FIXED_PCT + TAKE_PROFIT + OVERNIGHT_GAP + PRE_CLOSE + TIME_EXIT + MAX_HOLD |

**冲突点**：
- BIDU/MSFT同时被两套系统监控 → 可能双重触发
- ExitMonitor的pre_close刚改为"仅盈利仓"(loss_flat=true)
- _check_playbook_exit_rules的PRE_CLOSE是"收盘前8min全平"(cohort B盈利可跳过) → 和ExitMonitor的pre_close逻辑不同
- exit_policy.yaml禁用了breakeven/time_exit → 但_check_playbook_exit_rules的factor_exit_rules有这些
- _check_playbook_exit_rules有TAKE_PROFIT退出 → 但rr_ratio=999(TP=0) → 永不触发

### 配置生效状态

| 配置 | 文件 | 状态 | 是否生效 |
|------|------|------|---------|
| atr_stop 2.0×ATR max2% | exit_policy.yaml | enabled=true | ✅ ExitMonitor |
| pre_close loss_flat=true | exit_policy.yaml | enabled=true | ✅ ExitMonitor |
| breakeven | exit_policy.yaml | enabled=false | ✅ 禁用 |
| time_exit | exit_policy.yaml | enabled=false | ✅ 禁用 |
| progressive_trailing | exit_policy.yaml | enabled=false | ✅ 禁用 |
| vol_buckets | playbook_horizon.yaml | — | ✅ 盘前生成SL时用 |
| factor_exit_rules | playbook_horizon.yaml | — | ⚠️ 只被_check_playbook_exit_rules读 |
| rr_ratio=999 | playbook_horizon.yaml | — | ✅ TP=0, 禁用止盈 |
| horizon_selector | playbook_horizon.yaml | enabled=false | ✅ 所有指令默认short |
| market_state_overlay | playbook_horizon.yaml | enabled=false | ❌ VIX/regime调整不生效 |
| direction_mode | unified_config.yaml | llm_vote | ✅ LLM定方向 |
| guardrail skew=0.80 | pipeline.py硬编码 | — | ✅ 降仓不SKIP |
| guardrail min_rr=0.8 | pipeline.py硬编码 | — | ✅ |
| AUTO_TRADE_ACCOUNT | .env.us | sim | ✅ 模拟交易 |

### PF分解（剔除PHANTOM/ghost）

| 方向 | 笔数 | 胜 | 负 | PF | avg_win | avg_loss |
|------|------|----|----|----|---------|----------| 
| LONG | 161 | 44 | 117 | 0.73 | $377 | -$195 |
| SHORT | 200 | 86 | 114 | 2.00 | $596 | -$226 |

### 今日交易

| 持仓 | 方向 | exit_reason | PnL | PnL% | 退出系统 |
|------|------|------------|-----|------|---------|
| ARM | SHORT | HARD_STOP | -$21 | -6.3% | ExitMonitor |
| TSLA | SHORT | TRAILING_STOP | -$29 | -7.5% | ExitMonitor(滑点4.2%) |
| JPM | LONG | PRE_CLOSE_EXIT | -$27 | -0.4% | ? |
| LMT | SHORT | PRE_CLOSE_EXIT | +$32 | +0.5% | ? |

### 产出率

| 日期 | 总指令 | 执行 | SKIP | 产出率 |
|------|--------|------|------|--------|
| 6/30 | 37 | 8 | 29 | 21.6% |
| 6/29 | 38 | 15 | 23 | 39.5% |
| 6/24 | 25 | 25 | 0 | 100% |

## 讨论记录

### 第 1 轮：hermes

**核心问题：两套退出系统是策略混乱的根因**

ExitMonitor和_check_playbook_exit_rules各自独立计算退出，配置源不同，规则不同。BIDU/MSFT同时被两套系统监控，可能出现：
1. ExitMonitor先触发atr_stop → 平仓 → _check_playbook_exit_rules也想平仓 → 重复
2. _check_playbook_exit_rules先触发PRE_CLOSE → 平仓 → ExitMonitor的pre_close也想平仓 → 重复
3. SL计算不同：ExitMonitor用atr_stop(2.0×ATR)，_check_playbook_exit_rules用SL_FIXED_PCT(initial_sl)

**论点1: 统一到ExitMonitor，废弃_check_playbook_exit_rules**

理由：
- ExitMonitor是唯一有registry+互斥+冷却+grace period的成熟系统
- _check_playbook_exit_rules是硬编码if/elif链，无互斥无冷却
- 两套系统并行=维护成本2×，故障面2×
- ExitMonitor已经监控全部5个持仓(包括BIDU/MSFT)

**风险**: _check_playbook_exit_rules有TAKE_PROFIT和OVERNIGHT_GAP_STOP，ExitMonitor没有。但rr_ratio=999(TP=0)→TAKE_PROFIT永不触发；OVERNIGHT_GAP_STOP只在cohort B+long horizon→当前所有指令都是short horizon+cohort A→也不触发。所以废弃_check_playbook_exit_rules不丢失任何活跃退出规则。

**论点2: market_state_overlay应该启用**

VIX/regime调整仓位和SL是重要风控。当前enabled=false → VIX=30时仍满仓+不加宽SL。启用后：
- VIX>25 → 仓位×0.5 + SL加宽0.8%
- VIX>30 → 仓位×0 + SL加宽1.5%（实际不开仓）

**论点3: factor_exit_rules应该和exit_policy.yaml对齐**

当前矛盾：
- exit_policy.yaml: breakeven=disabled, time_exit=disabled
- factor_exit_rules: above_ma20 trailing_enabled=true, time_exit_hours=6

但factor_exit_rules只被_check_playbook_exit_rules读取。如果废弃_check_playbook_exit_rules，factor_exit_rules自动失效，矛盾消除。

**行动项**:
- [P0] 废弃_check_playbook_exit_rules: monitor_playbook_sim_exits直接return 0，让ExitMonitor统一管理
- [P0] 启用market_state_overlay: enabled=true
- [P1] factor_exit_rules标记为"仅文档，不执行"(等ExitMonitor接入后再启用)
- [P2] 不改direction_mode(维持llm_vote)、不改guardrail(降仓已修复)

### 第 2 轮：hermes（自验证修正）

**验证发现: BIDU的TP=117.63只在_check_playbook_exit_rules中检查**

BIDU metadata: take_profit=117.63(+3.9%)。废弃_check_playbook_exit_rules后:
- ExitMonitor没有TAKE_PROFIT退出规则 → BIDU的TP=117.63失效
- 但: rr_ratio=999(TP禁用) → 新生成的instruction TP=0
- BIDU的TP=117.63是盘前reprice时算的(metadata中有) → 但rr_ratio=999应该让TP=0

**验证: BIDU的TP=117.63是怎么来的?**
trade_records metadata: take_profit=117.63 → 但instruction的take_profit=0
→ 可能是盘前生成instruction时rr_ratio还不是999, 后改的
→ BIDU是旧指令, TP=117.63是遗留值

**修正论点1: 废弃_check_playbook_exit_rules, 但保留SL检查作为fallback**

更安全的方案: 不是直接return 0, 而是只保留SL_FIXED_PCT检查(用initial_sl), 删除PRE_CLOSE/TIME_EXIT/MAX_HOLD/TAKE_PROFIT(这些ExitMonitor已有或不需要)。

这样:
- ExitMonitor负责: atr_stop + pre_close(盈利落袋+亏损平掉)
- _check_playbook_exit_rules只负责: SL_FIXED_PCT(用instruction的stop_loss)
- 两套系统不重叠: ExitMonitor的atr_stop用2.0×ATR, _check_playbook_exit_rules用initial_sl(更紧的)

**修正论点2: market_state_overlay启用是安全的**

验证: _apply_market_state_overlay在instruction_generator.py中, 盘前生成instruction时调用。VIX从data.sentiment.vix获取(有默认值20.0)。仓位乘数=vix_pos_mult×regime_pos_mult → 不会double-reduce(是乘法不是加法)。

但: overlay只影响**新指令的SL和仓位**, 不影响已有持仓。当前5个持仓的SL不会被改变。

**修正论点3: factor_exit_rules矛盾消除确认**

废弃_check_playbook_exit_rules的PRE_CLOSE/TIME_EXIT后, factor_exit_rules的trailing_enabled/time_exit_hours不再被执行。exit_policy.yaml的breakeven=disabled/time_exit=disabled是唯一生效的。矛盾消除。

### 收敛结论（等CRITIC返回后补充）

**已验证的事实**:
1. ✅ BIDU/MSFT确实被两套退出系统双重监控(ExitMonitor + _check_playbook_exit_rules)
2. ✅ BIDU的trade_records metadata有TP=117.63, 但instruction TP=0(rr_ratio=999) → 遗留值
3. ✅ ExitMonitor监控全部5个持仓(positions=[IWM,UNH,SNDK,BIDU,MSFT])
4. ✅ _check_playbook_exit_rules只监控PLAYBOOK_SIM trade_records(BIDU/MSFT)
5. ✅ broker SIM=5持仓, DB=5持仓, 完全对齐
6. ✅ market_state_overlay在instruction_generator.py中, 只影响新指令
7. ✅ factor_exit_rules只被_check_playbook_exit_rules读取

**行动项**:
- [P0] _check_playbook_exit_rules只保留SL_FIXED_PCT, 删除PRE_CLOSE/TIME_EXIT/MAX_HOLD/TAKE_PROFIT
  - ExitMonitor已有pre_close(更优: loss_flat=true+close_profit_atr=2.0)
  - TAKE_PROFIT不需要(TP=0, rr_ratio=999)
  - TIME_EXIT已禁用(exit_policy.yaml)
  - MAX_HOLD不需要(short horizon, 不持夜)
  - 开盘冷却期(15min SL放宽到4%)保留 → 和ExitMonitor的grace period(30min)互补
- [P0] 启用market_state_overlay: enabled=true
- [P1] factor_exit_rules标记为"仅文档"(等废弃_check_playbook_exit_rules的其他规则后自然失效)
- [P2] 不改direction_mode/guardrail/vol_buckets(今天已修复)

**策略梳理后的最终架构**:
```
信号层:
  - direction_mode: llm_vote (3模型投票定方向)
  - vol_buckets: 波动率分桶计算SL/TP
  - market_state_overlay: VIX/regime调整仓位+SL
  - guardrail: 方向偏斜降仓(不SKIP) + min_rr=0.8

退出层(统一):
  - ExitMonitor: atr_stop(2.0×ATR,max2%) + pre_close(loss_flat=true,close_profit_atr=2.0)
  - _check_playbook_exit_rules: 只保留SL_FIXED_PCT(用initial_sl) + 开盘冷却期(15min,4%)
  - grace period: 30min, 只允许hard_stop

禁用的规则:
  - breakeven, time_exit, progressive_trailing, chandelier, scale_out
  - factor_exit_rules的trailing/time_exit(随_check_playbook_exit_rules简化而失效)
  - horizon_selector(全short)
  - TAKE_PROFIT(rr_ratio=999)
```

### 第 1 轮：claude-code (CRITIC, delegate_task)

**论点1修正**: 不能直接return 0。monitor_playbook_sim_exits走DualBrokerAdapter.close_position(target_env="sim")真实平仓, ExitMonitor的execute_close_fn是否路由到sim broker未验证。

**论点2反驳**: market_state_overlay已enabled=true(过时数据)。风险: total_mult=vix_pos_mult×regime_pos_mult, 而compute_instruction_values另有regime_multiplier参数 → 可能double-reduce。且overlay不区分多空, SHORT PF=2.00也被缩减。

**论点3修正**: 废弃_check后breakeven保护完全消失, 应先迁移到exit_policy再废弃。

### 第 2 轮：hermes (接受部分修正)

**修正1验证: ExitMonitor→sim broker链路已验证✅**
ARM close_futu_order_id=7909750, TSLA close_futu_order_id=7909763 → ExitMonitor通过order_executor→broker_adapter→Futu SIM成功下单。链路通畅。

**修正2接受: 需检查double-reduce** ⚠️
instruction_generator.py line 225: total_mult=vix_pos_mult×regime_pos_mult
line 383: risk_amount=risk_base×risk_pct×regime_multiplier×water_mult
→ 如果regime_multiplier和regime_pos_mult都来自regime_context → double-reduce!
→ 需要确认regime_multiplier的来源, 如果重复则去掉一个

**修正3拒绝: breakeven不应迁移** 
历史数据: BREAKEVEN_STOP 2笔 -$6,125(SHORT) → 巨亏
breakeven保护消失是正确的, 不应启用。

**收敛: 双方一致**
1. ✅ _check_playbook_exit_rules简化为只SL(已执行)
2. ⚠️ market_state_overlay需检查double-reduce(待验证)
3. ✅ breakeven不迁移(数据证明亏损)

### 最终收敛（含CRITIC修正）

**CRITIC的3个修正处理**:
1. ✅ ExitMonitor→sim broker链路已验证(ARM/TSLA成功平仓, futu_order_id有值)
2. ✅ double-reduce已修复: regime_position_mult全部改为1.0, regime调整只由pipeline.py:369的regime_mult负责, overlay只做VIX调整
3. ✅ breakeven不迁移(历史2笔-$6,125, 巨亏, 不应启用)

**最终改动清单**:
- pipeline.py: _check_playbook_exit_rules禁用5个退出规则, 只保留SL+开盘冷却期
- playbook_horizon.yaml: market_state_overlay enabled=true + regime_position_mult=1.0(防double-reduce)
- exit_policy.yaml: pre_close enabled=true, loss_flat=true(上一场辩论)
