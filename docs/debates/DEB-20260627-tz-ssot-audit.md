# Agent-vs-Agent Debate: 核心数据计算逻辑/时区/SSOT审计

---

# 议题：Playbook链路核心计算逻辑是否存在时区偏差、SSOT冲突或逻辑错误？

## 状态
- [x] 进行中

## 背景

参数已冻结, 链路已固化。固化前最后一轮审计: 核心计算逻辑(entry_price/SL/TP/position_size/profit_pct/peak_price)、时区一致性、配置SSOT。

## 已验证数据

### 时区问题

#### ET时区定义 — 27处, 2种实现
- **正确实现** (11处): `pytz.timezone("America/New_York")` — 处理夏令时
  - scheduler_core.py, scheduler_triggers.py, signal_pipeline.py, intraday_scorer.py, position_exit_monitor.py, market_regime_filter.py, timezone.py, datetime_utils.py(L17), economic_calendar.py, futu_lv3_provider.py
- **简化实现** (16处): `timezone(timedelta(hours=-4))` — 不处理夏令时(EDT only)
  - playbook/pipeline.py:1679, position_sync.py:656,1288, position_recovery.py:280, pre_trade_gate.py:164, pre_close.py:20, datetime_utils.py(L22), plan/pipeline.py:23, thesis.py, audit.py, executor.py, plan_feedback.py

**问题**: 11月EST切换为-5时, 16处简化实现仍用-4 → 会有1小时偏差。当前6月EDT=-4, 暂时正确。

#### date.today() 无时区 — ic_tracker.py:79
```python
trade_date = date.today().isoformat()  # 本地CST日期, 非ET日期
```
**问题**: CST比ET快12小时, CST凌晨(如CST 02:00 = ET 12:00前一天)时, `date.today()`返回CST日期, 而非ET日期。IC backfill用trade_date过滤指令, 可能差1天。

**pipeline.py**: 用`_today_et_str()`(正确), 但ic_tracker.py用`date.today()`(错误)。

#### SQLite date('now') — position_sync.py:748
```python
"AND entry_time >= date('now','start of day','-1 day')"
```
**问题**: SQLite `date('now')`返回UTC时间, 非ET。UTC比ET快4-5小时。

### SSOT配置冲突

#### max_position_pct — 6个不同值
- Playbook: 0.08 (instruction_generator.py)
- config_container: 0.25 (legacy)
- option_risk: 0.10
- daily_active_pool: 0.05
- ORB: 0.15
- LLM strategy: 0.05

**但**: 这些在不同模块, 不在同一条执行路径。Playbook链路只读`pb_cfg.get("max_position_pct", 0.08)`。不冲突, 但不SSOT。

#### rr_ratio — 2个来源
- instruction_generator.py: 从config读 `short=1.0, long=2.5`
- feedback_learner.py: 硬编码 `"rr_ratio"] = 2.5`

### 核心计算逻辑

#### entry_price (instruction_generator.py:151-155)
```python
entry_price = tech.ask or price or 0    # BUY用ask
entry_price = tech.bid or price or 0    # SELL用bid
entry_price = price or 0                # fallback
```
**逻辑正确**: BUY用ask(买入价), SELL用bid(卖出价)。

#### SL计算 (L173-182)
```python
_max_loss_pct = buy_fixed_stop_loss_pct(2.5) or sell_fixed_stop_loss_pct(2.0)
sl_distance = entry_price * (_max_loss_pct / 100.0)
stop_loss = entry_price - sl_distance (BUY) or entry_price + sl_distance (SELL)
```
**逻辑正确**: 固定%止损, 方向不对称。

#### TP计算 (L188-189)
```python
tp_distance = sl_distance * rr_ratio  # 1.0 for short
take_profit = entry_price + tp_distance (BUY) or entry_price - tp_distance (SELL)
```
**逻辑正确**: TP = SL距离 × RR ratio。

#### position_size (L211-221)
```python
position_size = int(risk_amount / per_share_risk)
# risk_amount = _risk_base($82K) * risk_pct(1%) * regime_mult * water_mult
# per_share_risk = abs(entry_price - stop_loss) = sl_distance
# max cap: _max_position_value = $82K * 0.08 = $6,560
```
**逻辑正确**: 风险预算 / 单股风险 = 仓位, 有上限。

#### profit_pct (pipeline.py:1461-1463)
```python
LONG: profit_pct = (current_price - entry_price) / entry_price * 100
SHORT: profit_pct = (entry_price - current_price) / entry_price * 100
```
**逻辑正确**: 方向调整。

#### peak_price tracking (pipeline.py:1467-1477)
```python
LONG: if current_price > peak_price: peak_price = current_price
SHORT: if peak_price == 0 or current_price < peak_price: peak_price = current_price
```
**问题**: SHORT的peak_price跟踪 — peak_price是最低价(对SHORT有利=价格下跌)。但`peak_profit_pct = (entry_price - peak_price) / entry_price * 100`。如果peak_price < entry_price, 结果为正(正确)。逻辑正确但命名混淆("peak"对SHORT是"trough")。

#### 退出监控时间 (pipeline.py:1682-1684)
```python
ET = timezone(timedelta(hours=-4))  # 简化, 不处理夏令时
now_et = datetime.now(ET)
now_minutes = now_et.hour * 60 + now_et.minute
minutes_to_close = 16 * 60 - now_minutes  # 距收盘(16:00 ET)
minutes_from_open = now_minutes - market_open_minutes  # 9*60+30=570
```
**问题**: `timezone(timedelta(hours=-4))`在EDT(夏令时)正确, EST(冬令时)应为-5。11月后会偏1小时。

## 关键分歧

1. **ic_tracker.py date.today()**: CST日期 vs ET日期, 影响IC backfill哪天的指令被处理
2. **16处timedelta(-4)**: 6月正确, 11月偏1小时
3. **position_sync SQLite date('now')**: UTC日期, 非ET日期
4. **feedback_learner.py rr_ratio=2.5硬编码**: 与config的1.0不一致

## 讨论记录

### 第 1 轮：hermes

我发现4个时区问题和1个SSOT问题:

**问题1 (P0): ic_tracker.py:79 date.today() 无时区**
`trade_date = date.today().isoformat()` 用服务器本地CST日期。CST比ET快12h, 当CST过零点(00:00-12:00 CST = ET前一天12:00-00:00)时, trade_date会多1天。IC backfill用trade_date判断"哪些指令需要回填T+1/T+3/T+5", 日期错误会导致:
- 指令日期 = 6/24(ET), 但trade_date = 6/25(CST) → `inst_date >= today`检查失败 → 跳过该指令
- 或者trade_date比ET日期多1天 → lookback窗口少覆盖1天

**修复**: `trade_date = datetime.now(ET).date().isoformat()`, 用ET日期。

**问题2 (P1): pipeline.py:1679 timedelta(-4) 不处理夏令时**
`ET = timezone(timedelta(hours=-4))` 在EDT(3月-11月初)正确, 但EST(11月初-3月)应为-5。
当前6月正确, 11月后会偏1小时 → 退出监控的minutes_from_open/minutes_to_close会偏1小时 → PRE_CLOSE_EXIT在15:00而非16:00触发, 或TIME_EXIT提前1小时。

**问题3 (P1): position_sync.py:748 SQLite date('now') 用UTC**
`date('now','start of day','-1 day')` 返回UTC日期。UTC 00:00 = ET 19:00/20:00前一天。PLAYBOOK_SIM skip检查的"当日"窗口会偏移。

**问题4 (P2): feedback_learner.py rr_ratio=2.5硬编码**
与config的rr_ratio=1.0(short)不一致。但feedback_learner是否在Playbook执行路径上需Claude Code验证。

维持: 问题1是P0必须修, 问题2/3在11月前修, 问题4待验证。

### 第 1 轮：claude-code

由 Claude Code 写入。

我对 Hermes 提出的 4 个问题进行了实际代码 grep/Read，结论如下。

#### 问题 1: ic_tracker.py:79 `date.today()` 无时区 — **REAL**

```python
# app/playbook/ic_tracker.py:78-80
if trade_date is None:
    trade_date = date.today().isoformat()
```

**影响**：IC backfill 用 `trade_date` 过滤指令，服务器默认时区(CST)下，凌晨 00:00-12:00 CST 对应 ET 前一天，会导致 `trade_date` 多一天，可能跳过应回填的指令或缩小 lookback 窗口。

**修复建议**：
```python
from app.core.scheduler_triggers import ET
...
if trade_date is None:
    trade_date = datetime.now(ET).date().isoformat()
```

#### 问题 2: pipeline.py:1679 `timedelta(-4)` 不处理夏令时 — **REAL**

```python
# app/playbook/pipeline.py:1679
ET = timezone(timedelta(hours=-4))
```

**影响**：当前 6 月 EDT 正确；11 月切换为 EST 后应为 -5，否则 `minutes_to_close`/`minutes_from_open` 偏 1 小时，可能导致 PRE_CLOSE_EXIT、TIME_EXIT 提前触发。

**修复建议**：复用 `scheduler_triggers.ET`（已使用 `pytz.timezone("America/New_York")`）：
```python
from app.core.scheduler_triggers import ET
...
now_et = datetime.now(ET)
```

#### 问题 3: position_sync.py:748 SQLite `date('now')` 用 UTC — **REAL**

```python
# app/core/position_sync.py:746-749
_pb_row = cursor.execute(
    "SELECT 1 FROM trade_records WHERE symbol=? AND source_type='PLAYBOOK_SIM' "
    "AND entry_time >= date('now','start of day','-1 day') AND exit_time IS NULL LIMIT 1",
    (symbol,)).fetchone()
```

**影响**：SQLite `date('now')` 返回 UTC，UTC 00:00 对应 ET 前一天 19:00/20:00，导致 "当日" 判定窗口偏移约 4-5 小时，可能误判 Playbook SIM 仓位。

**修复建议**：在 Python 中计算 ET 日期字符串后传入：
```python
from app.core.scheduler_triggers import ET
et_today = datetime.now(ET).date().isoformat()
cursor.execute(
    "SELECT 1 FROM trade_records WHERE symbol=? AND source_type='PLAYBOOK_SIM' "
    "AND entry_time >= ? AND exit_time IS NULL LIMIT 1",
    (symbol, f"{et_today}T00:00:00")
)
```

#### 问题 4: feedback_learner.py `rr_ratio=2.5` 硬编码 — **REAL（但仅建议，不直接生效）**

```python
# app/playbook/feedback_learner.py:215-216
elif field == "take_profit" and direction == "widen" and count >= 3:
    adjustments["default_rr_ratio"] = 2.5
```

**影响**：`feedback_learner` 仅输出 `generator_adjustments` 建议，不会自动覆盖 `instruction_generator` 的 rr_ratio。但建议值 2.5 与 `instruction_generator.py` 中 short=1.0、long=2.5 的 config 不一致，存在 SSOT 风险。

**修复建议**：建议从 config 读取并作为建议边界，而不是硬编码：
```python
from app.core.config_container import get_config
pb_cfg = get_config().playbook
base_rr = pb_cfg.get("rr_ratio", {}).get("long", 2.5)
adjustments["default_rr_ratio"] = base_rr
```
或文档化说明 2.5 仅针对 long 方向。

---

#### 额外检查项

**(1) feedback_learner.py 是否在 Playbook 执行路径上？**

是。`app/playbook/pipeline.py:1028` 在周度复盘流程中导入并调用：

```python
# app/playbook/pipeline.py:1026-1030
try:
    from app.playbook.feedback_learner import FeedbackLearner
    learner = FeedbackLearner(db)
    feedback_report = learner.learn_from_feedback(days=7)
```

结论：`FeedbackLearner` 确实在周报复盘路径中，虽然不是每日生成指令路径。

**(2) pipeline.py 的 `_today_et_str()` 是否使用 ET 时区？**

是。`app/playbook/pipeline.py:52-54`：

```python
def _today_et_str() -> str:
    from datetime import datetime
    return datetime.now(_now_et()).date().isoformat()
```

其中 `_now_et()` 返回 `app.core.scheduler_triggers.ET`，即 `pytz.timezone("America/New_York")`，已正确处理夏令时。此实现正确。

**(3) Playbook 路径中是否还有其他 `date.today()` 调用（ic_tracker、shadow_tracker、factor_analyzer）？**

- `ic_tracker.py:79`：`trade_date = date.today().isoformat()` — **存在**
- `shadow_tracker.py:47`：`d = date.today()` — **存在**
- `shadow_tracker.py:87`：`trade_date = date.today().isoformat()` — **存在**
- `factor_analyzer.py`：无 `date.today()` / `datetime.today()` 调用 — **不存在**

此外 playbook 路径中还有 `playbook_service.py:636`、 `risk_circuit_breaker.py:94/103/104/125`、`feishu_push.py:46/79/208`、`weekly_report.py:55/185/216`、`data_collector.py:571` 也使用了 `date.today()`，同样需要按业务语义评估是否应改为 ET。

---

**总结**：Hermes 提出的 4 个问题均为 **REAL**，需要修复。优先级建议：P0 修复 `ic_tracker.py`，P1 修复 `pipeline.py:1679` 和 `position_sync.py:748`，P2 文档化/参数化 `feedback_learner.py` 的 rr_ratio 建议。
