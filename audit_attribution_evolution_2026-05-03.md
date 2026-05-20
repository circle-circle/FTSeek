# FTSeek-US 归因+进化层 全链路审计报告

> 审计时间: 2026-05-03
> 基准 Commit: 851c78c (2026-05-02 P0 link_trade_outcome + P1 weight 硬编码修复)
> 审计范围: 决策记录 / 复盘数据源 / Agent桥接 / 教训注入 / 配置一致性 / DB一致性 / 退出质量

---

## 一、系统架构概览

```
数据流:
  投票决策 → DecisionRecorder(decisions.db) → 4张表
    ↓
  开仓 → order_executor.py → link_trade_outcome(写入 trade_outcomes)
    ↓
  平仓 → order_executor._close_position → link_trade_outcome(更新 trade_outcomes)
    ↓
  model_performance 自动更新 (通过 _update_model_performance_for_decision)

  复盘层:
    daily_review.py ← ftseek_us.db(trade_records) + decisions.db + rejections.db
    weekly_review.py ← ftseek_us.db + decisions.db + rejections.db + daily_reviews/*.json

  进化层:
    lesson_injector.py ← ftseek_us.db(lessons表) → 注入盘前计划prompt
    agent_bridge.py ← ~/.hermes/ftseek/memory/us.json
    exit_quality_tracker.py ← exit_quality.db

  3个独立DB:
    decisions.db (投票/结果/模型表现/权重调整)
    ftseek_us.db (交易记录/资金状态/lessons/trade_attribution)
    rejections.db (拒绝日志)
    + exit_quality.db (退出质量)
    + trade_records.db (TradeRecorder — 旧版，已废弃)
```

---

## 二、P0 断裂 (必须修复)

### P0-1: lessons 表只有读取，没有写入 — 进化链断裂

**严重程度**: P0 (致命)
**文件**: `app/review/lesson_injector.py` + 全局搜索

**问题**:
- `LessonInjector.load_active_lessons()` 从 ftseek_us.db 的 `lessons` 表读取活跃教训
- `lesson_injector.py` 只有读取方法 (`load_active_lessons`, `generate_battle_plan_injection`, `generate_trade_constraints`)，**没有任何 INSERT 方法**
- `get_next_day_hints()` 引用了 `ExperienceAccumulator` 类，但 **该类在代码库中不存在** (grep 结果为 0)
- 复盘生成的教训 (daily_review `_extract_learnings_v2`) 仅输出到 JSON 文件，**不写入 lessons 表**
- agent_bridge `_update_memory` 只写 `us.json` 文件，**不写 lessons DB**

**影响**: 教训注入到盘前计划的内容 **永远为空**（除非手动INSERT）。进化层是空的。

**修复方案**:
1. 创建 `ExperienceAccumulator` 类或直接在 `daily_review.py` / `lesson_injector.py` 中添加 `save_lesson()` 方法
2. 在每日复盘生成后，将 key_learnings 自动写入 lessons 表
3. 或从 agent_bridge 的 EOD response 中提取 lessons 写入 DB

---

### P0-2: trade_records 数据分散在 3 个存储中，复盘读的是错误来源

**严重程度**: P0 (高)
**文件**: `app/storage/trade_store.py` vs `app/storage/trade_recorder.py` vs `app/pipeline/order_executor.py`

**问题**:

| 存储 | DB路径 | 表名 | 写入者 | 实际使用 |
|------|--------|------|--------|----------|
| TradeStore | ftseek_us.db | trade_records | order_executor.save_trade() | ✅ 主写入 |
| TradeRecorder | trade_records.db | trade_records | 无调用者 | ❌ 废弃 |
| DecisionRecorder | decisions.db | trade_outcomes | order_executor.link_trade_outcome() | ✅ 模型表现 |

**具体断裂**:
1. `order_executor.py` 开仓时同时写入:
   - `ftseek_us.db.trade_records` (via TradeStore.save_trade)
   - `decisions.db.trade_outcomes` (via link_trade_outcome, 仅 entry)
2. `order_executor.py` 平仓时写入:
   - `ftseek_us.db.trade_records` (UPDATE)
   - `decisions.db.trade_outcomes` (via link_trade_outcome with exit data)
3. `daily_review.py` 从 **ftseek_us.db** 读取 trade_records — 正确 ✅
4. **但** `TradeRecorder` (trade_records.db) 完全废弃，却仍在代码库中，可能被误引用
5. `weekly_review._analyze_risk()` 引用了 `stop_loss_planned` 和 `stop_loss_executed` — **这些列不存在于 ftseek_us.db.trade_records** (它们在 trade_records.db 中)

**修复方案**:
- 删除 `app/storage/trade_recorder.py` (已废弃)
- 修复 `weekly_review._analyze_risk()` 改用实际存在的列

---

### P0-3: 开仓时 link_trade_outcome 只写入 entry，exit_price=None 导致 outcome_correct=-1 但 model_performance 仍被更新

**严重程度**: P0
**文件**: `app/storage/decision_recorder.py` line 307, `app/pipeline/order_executor.py` line 1498

**问题**:

开仓时 (order_executor.py:1498):
```python
recorder.link_trade_outcome(
    decision_id=decision_id,
    symbol=symbol,
    direction=signal.direction.value,
    entry_price=price,
    quantity=quantity,
    # exit_price=None, realized_pnl=0 → outcome_correct = -1
)
```

在 `link_trade_outcome` 中 (decision_recorder.py:281-308):
```python
outcome_correct = -1  # open (因为 exit_price=None)
# ... 写入 trade_outcomes ...
# 同步更新模型表现
if outcome_correct >= 0:   # -1 >= 0 为 False ✅ 此处正确跳过
    self._update_model_performance_for_decision(...)
```

**但实际上**，这里有个微妙问题：开仓时创建了一条 trade_outcomes 记录 (outcome_correct=-1)，平仓时又创建了一条 **新的** trade_outcomes 记录 (有 exit_price, outcome_correct=0/1)。这导致 **同一个 decision_id 在 trade_outcomes 中有两条记录**！

**正确做法**: 平仓时应该 UPDATE 开仓时创建的那条记录，而不是 INSERT 新记录。

**影响**:
- trade_outcomes 表中有大量 outcome_correct=-1 的孤立记录
- 模型表现更新只在平仓时触发，但关联的是新记录而非开仓记录
- `learn_factor_weights()` 统计时可能会重复计算或遗漏

---

## 三、P1 问题 (重要但非阻断)

### P1-1: outcome_correct 计算逻辑过于简化

**文件**: `decision_recorder.py:280-286`

```python
if direction == "LONG":
    outcome_correct = 1 if exit_price > entry_price else 0
else:
    outcome_correct = 1 if exit_price < entry_price else 0
```

**问题**:
- 没有考虑手续费/滑点 (COST_MODEL 已配置但未使用)
- 盈亏比例 < 0.5% 的"平手"交易也被算作 wrong
- 没有时间衰减 (持仓 3 天和 3 小时同等对待)
- 与 trade_store 中 pnl_pct 的计算不一致 (trade_store 可能考虑了更多因素)

---

### P1-2: weekly_review._analyze_risk 引用幽灵列

**文件**: `weekly_review.py:399-400`

```python
sl_trades = [t for t in trades if t.get('stop_loss_planned') and t['stop_loss_planned'] > 0]
executed = len([t for t in sl_trades if t.get('stop_loss_executed', True)])
```

`ftseek_us.db.trade_records` 表中 **没有** `stop_loss_planned` 或 `stop_loss_executed` 列。这些列只存在于 `trade_records.db` (TradeRecorder)。daily_review 已经在 v2.0 修复了这个问题，但 weekly_review 还没修。

---

### P1-3: config_loader EXECUTION_CONFIG 路径不匹配

**文件**: `config/config_loader.py:294-311`

```python
"MARKET_ORDER_MIN_SCORE": self.get('execution.order_type_selection.market_order_min_score', 80),
"LIMIT_ORDER_MAX_SCORE": self.get('execution.order_type_selection.limit_order_max_score', 60),
"TWAP_ORDER_MIN_VALUE": self.get('execution.order_type_selection.twap_order_min_value', 100000),
"ORDER_STATE_CHECK_INTERVAL": self.get('execution.order_tracking.check_interval', 2),
"ORDER_TIMEOUT_SECONDS": self.get('execution.order_tracking.timeout_seconds', 300),
```

但 `unified_config.yaml` 中对应的路径是:
```yaml
execution:
  order_type:          # 不是 order_type_selection
    market_min_score: 80
    limit_max_score: 60
    twap_min_value: 100000
  order_state:         # 不是 order_tracking
    check_interval: 2
    timeout_seconds: 300
```

**结果**: 所有 EXECUTION_CONFIG 的值都是默认 fallback，不从 yaml 读取。

---

### P1-4: OBSERVABILITY_CONFIG 路径部分不匹配

**文件**: `config/config_loader.py:317`

```python
"METRICS_COLLECT_INTERVAL": self.get('observability.metrics_collect_interval', 10),
```

但 yaml 中是:
```yaml
observability:
  metrics:
    collect_interval: 10
```

路径应该是 `observability.metrics.collect_interval`，不是 `observability.metrics_collect_interval`。

---

### P1-5: RejectionLogger 未启用 WAL 模式 + 无连接池

**文件**: `app/storage/rejection_logger.py`

每次 `record()`、`get_today_summary()`、`get_today_rejections()` 都创建新连接，无 WAL 模式，无 row_factory (在 record 中)。虽然数据量不大，但在高拒绝率时可能成为瓶颈。

---

### P1-6: Agent Bridge 的盘后 EOD 响应与 lessons DB 断裂

**文件**: `app/llm/agent_bridge.py`

`generate_eod_review()` 发送请求，Hermes Agent 返回的 JSON 中包含 `"lessons"` 字段。`_check_eod_response()` 读取响应后只调用 `_update_memory()`，更新的是 `us.json` 文件。**lessons 不写入 ftseek_us.db 的 lessons 表**，导致 lesson_injector 永远读不到 AI 提取的教训。

---

## 四、P2 问题 (优化点)

### P2-1: 5 张表中有 3 张缺少 WAL 模式

| 文件 | DB | WAL? | 问题 |
|------|-----|------|------|
| decision_recorder.py | decisions.db | ✅ | 正常 |
| trade_store.py | ftseek_us.db | ✅ | 正常 |
| trade_recorder.py | trade_records.db | ❌ | 无WAL + 已废弃 |
| rejection_logger.py | rejections.db | ❌ | 无WAL |
| exit_quality_tracker.py | exit_quality.db | ❌ | 无WAL |
| agent_bridge.py | hermes_queue.db | ✅ | 正常 |

rejections.db 和 exit_quality.db 在多写场景下可能锁死。

### P2-2: exit_quality_tracker evaluate_pending 使用两个独立连接

`exit_quality_tracker.py:120-127` 读取用一个连接，`166-177` 写入用另一个连接。虽然不会出错（SQLite 串行化），但增加了 IO 开销。应该复用同一连接。

### P2-3: trade_outcomes 缺少 trace_id 列

`decisions.db.trade_outcomes` 没有 trace_id 列，无法与 `ftseek_us.db.trade_attribution` 关联。`order_executor._lookup_decision_id_by_trace` 使用 trace_id 反查 decision_id 时走的是 trade_attribution 表，但如果 trade_attribution 写入失败（用 INSERT OR IGNORE），反查就永远为空。

### P2-4: RiskStateMachine 的 _confirm_market_recovery 创建新的 MarketDataRouter

`risk_state_machine.py:215-217`:
```python
from app.data.market_data_router import MarketDataRouter
router = MarketDataRouter()
vix_data = router.get_vix()
```

每次 auto_recovery 调用都创建新的 Router 实例。应该复用 scheduler 已有的 data_router 实例。

### P2-5: 配置中 risk.position.max_per_direction YAML=0.65 vs config_loader default=0.50

**文件**: `config/config_loader.py:268`

```python
'MAX_POSITION_PER_DIRECTION': self.get('risk.position.max_per_direction', 0.65),
```

注释说 "04/30: 从0.50提升"，但 PositionRisk dataclass 默认值仍是 0.50 (line 92)。如果 YAML 未加载或加载失败，fallback 值是 0.50 不是 0.65，**注释误导但代码正确**（fallback 写的是 0.65）。

### P2-6: daily_review._analyze_per_trade 引用可能不存在的 TradeAnalyzer

```python
from app.review.trade_analyzer import TradeAnalyzer
analyzer = TradeAnalyzer()
```

如果 `trade_analyzer.py` 不存在，整个逐笔分析失败并被静默吞掉。

### P2-7: agent_bridge post_market 没有被 scheduler 定期调用

搜索结果显示 `generate_eod_review` 和 `_check_eod_response` 在 agent_bridge 中定义，但没有在 scheduler 的 cron/trigger 中找到调用点。盘后归因 **可能从未自动执行**。

---

## 五、配置不一致汇总

| 配置项 | YAML 值 | config_loader 读取路径 | 实际读取结果 |
|--------|---------|----------------------|-------------|
| execution.order_type.market_min_score | 80 | execution.order_type_selection.market_order_min_score | ❌ fallback 80 |
| execution.order_type.limit_max_score | 60 | execution.order_type_selection.limit_order_max_score | ❌ fallback 60 |
| execution.order_state.check_interval | 2 | execution.order_tracking.check_interval | ❌ fallback 2 |
| observability.metrics.collect_interval | 10 | observability.metrics_collect_interval | ❌ fallback 10 |
| risk.signal_quality.min_confidence | 0.6 | risk.signal_quality.min_confidence | ✅ 0.6 |
| risk.position.max_per_direction | 0.65 | risk.position.max_per_direction | ✅ 0.65 |

---

## 六、DB 数据一致性汇总

```
decisions.db:
  ✅ decisions (投票主表)
  ✅ model_votes (投票明细)
  ⚠️ trade_outcomes (开仓+平仓各一条，应合并)
  ✅ model_performance (自动更新)
  ✅ weight_adjustments (权重调整)

ftseek_us.db:
  ✅ trade_records (主交易表)
  ✅ open_positions (持仓快照)
  ✅ stoploss_events (止损事件)
  ✅ capital_state (资金状态)
  ✅ trade_attribution (归因数据)
  ✅ lessons (教训表 — 只有读没有写)

rejections.db:
  ✅ rejections (拒绝记录)

exit_quality.db:
  ✅ exit_quality (退出质量)
```

**核心断裂**:
1. `decisions.db.decisions` ↔ `decisions.db.trade_outcomes` 通过 decision_id 关联 ✅
2. `ftseek_us.db.trade_records` ↔ `decisions.db.decisions` 通过 trace_id 关联 ⚠️ (trade_records 无 decision_id 列)
3. `ftseek_us.db.trade_attribution` ↔ `decisions.db.decisions` 通过 decision_id 关联 ✅
4. `ftseek_us.db.lessons` ↔ 任何写入源 **❌ 无写入**
5. `decisions.db.model_performance` ↔ `decisions.db.trade_outcomes` 通过 _update_model_performance_for_decision 关联 ✅
6. `~/.hermes/ftseek/memory/us.json` ↔ `ftseek_us.db.lessons` **❌ 不同步**

---

## 七、退出质量追踪评估

| 维度 | 评分 | 说明 |
|------|------|------|
| 数据记录 | ⚠️ | record_exit() 逻辑完整，但调用方(ExitMonitor)需验证 |
| 事后评估 | ✅ | evaluate_pending() 有合理的 15min 延迟和质量判定 |
| 聚合报告 | ✅ | get_session_quality_report() / get_reason_quality_report() 完整 |
| 校准建议 | ✅ | suggest_session_vol_adjustments() 有合理的偏差映射 |
| 实际应用 | ⚠️ | apply_calibration() 修改的是内存变量 DEFAULT_SESSION_VOL_RATIO, **不持久化** |
| 集成到盘前计划 | ✅ | agent_bridge.generate_battle_plan 注入了 session_exit_quality |

**P2 问题**: apply_calibration 修改内存变量，服务重启后丢失。应该写入配置文件或 DB。

---

## 八、风险状态机评估

| 维度 | 评分 | 说明 |
|------|------|------|
| 状态定义 | ✅ | NORMAL/CAUTION/RISK_OFF/HALT 四种状态 |
| 动作矩阵 | ✅ | 各状态允许的动作定义清晰 |
| 审计日志 | ✅ | 文件锁 + 原子写入 |
| 状态恢复 | ✅ | 启动时从 audit log 恢复 |
| 自动恢复 | ✅ | 30min cooldown + 市场二次确认 + 渐进强制恢复 |
| 防抖 | ✅ | is_in_cooldown() |
| 失败降级 | ✅ | fail-open 策略化 |

**P2 问题**: `_confirm_market_recovery()` 每次都创建新的 MarketDataRouter/YahooFinanceProvider 实例。

---

## 九、按优先级汇总的修复清单

### 立即修复 (P0)
1. **[P0-1]** 创建 lessons 表的写入链路: daily_review 或 EOD review 后自动 INSERT lessons
2. **[P0-1]** 删除废弃的 `app/storage/trade_recorder.py`
3. **[P0-2]** 修复 weekly_review._analyze_risk 的幽灵列引用
4. **[P0-3]** link_trade_outcome 改为 UPSERT 模式 (开仓INSERT, 平仓UPDATE)

### 重要修复 (P1)
5. **[P1-1]** outcome_correct 加入手续费阈值 (如 COST_MODEL 中的 default_stock_spread_bps)
6. **[P1-3]** 修复 config_loader EXECUTION_CONFIG 的路径映射
7. **[P1-4]** 修复 config_loader OBSERVABILITY_CONFIG 的路径映射
8. **[P1-5]** RejectionLogger 启用 WAL 模式
9. **[P1-6]** EOD review 响应中的 lessons 写入 ftseek_us.db.lessons

### 优化 (P2)
10. **[P2-1]** exit_quality.db + rejections.db 启用 WAL
11. **[P2-2]** exit_quality_tracker evaluate_pending 复用连接
12. **[P2-3]** trade_outcomes 增加 trace_id 列
13. **[P2-4]** RiskStateMachine 复用 scheduler.data_router
14. **[P2-7]** 在 scheduler triggers 中添加 EOD review 自动调用
15. **[P2]** apply_calibration 持久化 vol_ratio 调整
