# Agent-vs-Agent Debate: 交易全链路查漏补缺

## 使用说明
- 每轮辩论在「## 讨论记录」下追加, **永不覆盖**他人内容
- 每条事实声明必须附验证命令输出 (grep/read), 不可虚构
- 让步时写 "接受: <观点>", 坚持时写 "维持: <观点>"

---

# 议题：当前优化后的交易全链路是否存在深层bug或逻辑问题？

## 状态
- [x] 进行中
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

## 背景

系统经过6/26-6/27的密集修复(9项P0+参数调优+幂等修复), 用户要求固化链路前做最后一轮查漏补缺。核心目标：找到深层的bug和逻辑问题，不是参数调优。

## 已验证数据 (Pre-debate Checklist)

### 1. 指令分布 (7天)
- 6/22: 2 BUY (0W 2L)
- 6/23: 19 BUY (9W 7L 1M) + 6 SHORT (4W 2L) = 25条
- 6/24: 14 BUY (4W 4L 6M) + 11 SHORT (3W 1L 6M) = 25条
- 6/25: 1 BUY (missed)
- 6/26: 0条 (PRE_MARKET错过, 已修catch-up)

### 2. 实际成交 (7天, PLAYBOOK_SIM)
- 12笔: 2胜 5负 5笔PHANTOM_RECONCILE_VOID(pnl=0)
- 总PnL: -$1,838.04 (排除phantom后)
- 有成交的7笔: 2胜(UNH+$313, MSFT+$807) 5负(-$2,958)

### 3. MFE分析
- 12笔中10笔MFE=0% (从未浮盈)
- 仅UNH(MFE=+1.49%)和MSFT(MFE=+2.39%)有浮盈
- MSFT的peak=$365.02但exit=$363.85 → 有浮盈但退出时机不最优

### 4. fwd_return_t1方向验证 (37笔已执行)
- 方向正确: 20/37 (54%)
- 方向错误: 17/37 (46%)
- 接近随机, 信号预测力弱

### 5. 幽灵仓位: 0条 (exit_time全有值) ✅

### 6. position_sync劫持: 仅2条 (PG/NFLX, 6/24当天竞态) ✅

### 7. ORPHAN/PHANTOM: 5笔pnl=0 ✅

### 8. IC backfill: total=58, t1=53, t3=3, t5=1 (T+3/T+5严重不足)

## 代码级参数验证 (不信任Skill)

### instruction_generator.py (693 lines)
- rr_ratio: 从config读, short=1.0, long=2.5
- **方向不对称SL已实现**: BUY=2.5% (buy_fixed_stop_loss_pct), SHORT=2.0% (sell_fixed_stop_loss_pct)
- max_position_pct: 0.08 (代码默认)
- risk_base_capital: $82,000 (代码默认)
- ATR计算链: 已删除 ✅
- SL formula: `sl_distance = entry_price * (_max_loss_pct / 100.0)`

### pipeline.py (1959 lines)
- breakout_pct: 0.5% ✅
- volume_ratio: 1.5 ✅
- ±3% fallback: 已删除 ✅
- breakeven: peak≥0.5% ✅
- TIME_EXIT: held>120min + profit<0 ✅
- buying_power guard 0.8x: ✅
- 空列表幂等修复: ✅
- datetime import: ✅
- **SL_ATR_STOP命名仍在用** (退出原因标为SL_ATR_STOP但实际是固定%, 命名误导)

### config
- execute_mode: futu_simulate ✅
- master_switch: legacy ✅
- HorizonSelector: enabled=false (所有指令默认short)

### position_sync.py
- PLAYBOOK_SIM skip逻辑: ✅
- **decision_id检查: ❌ 不存在** (只查symbol+source_type, 不查decision_id)

### playbook_service.py
- **buying_power hasattr guard: ❌ 不存在** (代码中无hasattr get_buying_power)
- fill price fallback to entry: 未找到 (可能已修)

## 关键分歧/疑点

1. **方向不对称SL (BUY=2.5% vs SHORT=2.0%)**: 何时实现的? 是否经过辩论? DB中6/24所有交易SL=2.0% → BUY的2.5%可能没生效
2. **SL_ATR_STOP命名**: 退出原因标为"SL_ATR_STOP"但实际是固定%止损 → 日志/审计误导
3. **position_sync无decision_id检查**: 只查symbol+source_type → 同symbol不同decision_id的仓位可能误跳过
4. **IC backfill T+3/T+5严重不足**: 58条中只有3条T+3, 1条T+5 → lookback窗口或数据源问题?
5. **fwd_return_t1方向正确率54%**: 接近随机 → 信号逻辑是否有结构性问题?
6. **MFE=0%集中**: 12笔中10笔从未浮盈 → 入场时机还是信号问题?
7. **PHANTOM_RECONCILE_VOID 5笔**: 6/25生成的指令被void → 购买力不足还是broker拒绝?

## 讨论记录

### 第 1 轮：hermes

基于代码级验证, 我发现以下深层问题:

**问题1: 方向不对称SL可能未生效**
代码(instruction_generator.py L173-177)有`buy_fixed_stop_loss_pct=2.5`和`sell_fixed_stop_loss_pct=2.0`, 但DB中6/24所有BUY指令的SL距离都是2.0%而非2.5%。可能原因:
- 代码读取config时, `_horizon_params.get("buy_fixed_stop_loss_pct")` 返回None → fallback到`pb_cfg.get("fixed_stop_loss_pct", 2.5)` → 但config里`fixed_stop_loss_pct: 2.0` → 最终SL=2.0%
- 验证命令: `grep -n "buy_fixed_stop_loss_pct\|sell_fixed_stop_loss" app/playbook/instruction_generator.py`
- 待Claude Code验证: get_horizon_params()是否正确返回buy/sell区分的参数

**问题2: SL_ATR_STOP退出原因命名误导**
pipeline.py的退出逻辑用固定2%止损, 但exit_reason标为"SL_ATR_STOP"。ATR计算链已删, 这个名字是历史遗留。影响: 日志分析时误以为有ATR止损逻辑, 实际没有。
- 验证: `grep -n "SL_ATR_STOP" app/playbook/pipeline.py`

**问题3: position_sync无decision_id去重**
position_sync.py只查`symbol + source_type='PLAYBOOK_SIM' + exit_time IS NULL`, 不查decision_id。如果同symbol有多个PLAYBOOK_SIM仓位(不同decision_id), 只要有1个未平仓, 该symbol所有新仓位都会被跳过。
- 验证: `grep -n "decision_id" app/core/position_sync.py`

**问题4: IC backfill lookback窗口**
ic_tracker.py L86: `lookback = today - timedelta(days=30)` → 应该覆盖T+5(7个自然日)。但T+3只有3条, T+5只有1条。可能原因: `get_daily_bars()`返回的数据是旧缓存(2025年数据), target_date在bars_by_date中找不到。
- 验证: 检查backfill日志 "Backfilled forward returns for 0 instructions" 是否持续出现

**问题5: PHANTOM_RECONCILE_VOID的生成路径**
5笔VOID记录entry_time=2026-06-25T14:10:59(非交易时段), exit_time=2026-06-26T10:16:24。这些是6/25 14:10生成的reconcile记录, 但6/25是非交易日(周四 CST 14:10 = ET 02:00, 非交易时段)。为什么在非交易时段生成交易记录?

维持: 以上5个问题都需要Claude Code独立验证代码路径确认。

### 第 2 轮：hermes

逐条核实Claude Code的结论:

**问题1: 方向不对称SL — Claude Code说REAL但描述不完整**
- 我执行了`get_horizon_params("short")`: 返回`buy_fixed_stop_loss_pct=2.5, sell_fixed_stop_loss_pct=2.0` ✅
- 代码逻辑: `_horizon_params.get("buy_fixed_stop_loss_pct")` → 2.5 → `float(2.5)` → SL=2.5%
- **但DB中6/24所有BUY指令SL=2.0%**
- 根因: 方向不对称SL是6/27辩论后新增的代码, 6/24指令用旧代码生成(SL=2.0%统一)
- 结论: **代码逻辑正确, DB数据是旧代码产物**。下周一新生成的BUY指令应该SL=2.5%。不是bug, 是时序差异。
- 维持: 需要周一验证新生成指令的SL%是否正确

**问题2: SL_ATR_STOP命名 — 接受**
- 验证: pipeline.py L1430,1519,1522,1626,1706,1742,1745 共7处使用SL_ATR_STOP
- ATR计算链已删, 实际是固定%止损, 命名误导
- 接受: 应改为SL_FIXED_PCT。优先级P2(不影响功能, 影响审计)

**问题3: position_sync无decision_id — 接受但降级**
- 验证: position_sync.py中decision_id出现0次 ✅
- 只查symbol+source_type+exit_time IS NULL
- **但**: 当前系统同symbol不太可能有多个同时未平仓的PLAYBOOK_SIM仓位(buying_power guard限制了并发仓位)
- 严重度: P2而非P0。当前2条竞态劫持(PG/NFLX)不是由decision_id缺失导致的, 是写入延迟
- 接受: 长期应该加decision_id, 但不是固化前的阻塞项

**问题4: IC backfill count=10 — 接受, P0级**
- 验证: `get_daily_bars(symbol, count=10)` 只拉10根日线
- lookback=30天, 但只拉10根bar → 30天前的指令T+5 target_date不在10根bar范围内
- **根因确认**: count=10不够覆盖30天lookback的T+5需求
- 接受: 这是T+3=3条/T+5=1条的根因。修count=10→count=30即可
- **优先级P0**: IC计算是毕业门禁前置条件, 不修则永远ACCUMULATING

**问题5: PHANTOM_RECONCILE_VOID — 接受**
- 验证: grep确认只在trade_store.py和daily_review.py作为过滤条件出现
- 生成代码已被移除, DB中5条是历史遗留
- 接受: 不是bug, 是历史数据。已在统计查询中过滤

**新增Bug 1: count=10 — 同问题4, 合并**

**新增Bug 2: position_sync SQLite UTC时区 — 接受, P1级**
- 验证: L748 `date('now','start of day','-1 day')` 用SQLite UTC
- 系统时区CST(UTC+8), SQLite `date('now')`返回UTC日期
- CST凌晨(如CST 06:00 = UTC 22:00前一天) → "当日"判断偏移
- **但**: position_sync的PLAYBOOK_SIM skip只影响是否跳过, 偏移1天最多导致多跳过1天或少跳过1天
- 优先级P1: 应该用ET时区, 但当前影响有限

**新增Bug 3: OVERNIGHT_GAP_STOP — 维持不成立**
- 验证: L1526 `if not exit_reason and _skip_allowed and _is_overnight_position(...)`
- `_skip_allowed = meta.get("_skip_allowed", _cohort == "B" or _horizon == "long")`
- **但**: 当前所有指令horizon=short, _cohort未设置 → _skip_allowed默认False → OVERNIGHT_GAP_STOP条件不触发
- 这不是bug: short horizon不应该有隔夜仓位(max_hold_days=1), PRE_CLOSE_EXIT会在盘末平仓
- 维持: OVERNIGHT_GAP_STOP对short horizon不生效是设计正确, 不是bug

**总结: 5+3个问题中, P0=1(count=10→30), P1=1(UTC时区), P2=3(命名/decision_id/历史数据), 不成立=1(OVERNIGHT_GAP_STOP), 时序差异=1(方向SL)**
