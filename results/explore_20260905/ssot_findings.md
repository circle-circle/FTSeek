# SSOT 深潜发现 — Position Single Source of Truth Deep-Dive Findings

**HEAD**: `fb162af0`  
**Date**: 2026-09-05  
**Author**: Explore Agent  
**Scope**: ② _exit_cooldowns 吞退出 ③ tombstone 竞态 ④ version 乐观锁合规 ⑤ 裸 symbol 跨库归一  
**Dedup**: `results/explore_20260905/prior_confirmed_0904.md` — 文件不存在; `bug_hunt/deb20260904/prior_confirmed_baseline.md` — 文件不存在。无已知重复。  
**第 1 点 (PositionStore / broker_adapter / trade_records / ExitMonitor / web_dashboard / SQLite schema SSOT trace)**: 见 `results/explore_20260905/ssot_trace_ref.md` (协调员已核验落锤)。

---

## ② `_exit_cooldowns` 类级 dict — 同 symbol <120s 快速重开吞退出

### 机制

`ExitMonitor` 有两层防振荡冷却:

| 层级 | 存储位置 | 生命周期 | 文件:行 |
|------|----------|----------|---------|
| trail_state 级 | `position.trail_state["last_trigger_time"]` | 绑定 Position 对象，对象销毁即失 | `position_exit_monitor.py:1148-1150` |
| 类级 `_exit_cooldowns` | `ExitMonitor._exit_cooldowns: Dict[str, float]` | 进程级单例，跨 Position 对象重建存活 | `position_exit_monitor.py:1152-1155` |

**写入点**: `_execute_exit()` 在触发平仓时写入类级冷却:
```python
# position_exit_monitor.py:2004-2005
# 类级别冷却记录：防止position对象重建后丢失trail_state导致重复退出
self._exit_cooldowns[position.symbol] = now_ts
```

**检查点**: `scan_positions()` 每轮对每个 position 先查 trail_state 级再查类级:
```python
# position_exit_monitor.py:1148-1155
last_trigger = trail_state.get("last_trigger_time", 0)
if last_trigger > 0 and (now_ts - last_trigger) < self.COOLDOWN_SECONDS:
    continue
last_exit = self._exit_cooldowns.get(symbol, 0)
if (now_ts - last_exit) < self.COOLDOWN_SECONDS:
    logger.debug(f"[退出监控] {symbol} 类级别冷却中...")
    continue
```

**COOLDOWN_SECONDS = 120** (`position_exit_monitor.py:223`)

### 设计意图 (合理)

注释明说: "防止 position 对象重建后 trail_state 丢失导致重复退出"。即如果同一个 symbol 的 Position 对象被销毁又重建 (trail_state 清零)，类级 dict 兜底防止对已平仓的幽灵残留再发一次平仓单。

### 事故剧本 A: Playbook 反手 / 日内再入 <120s 吞退出

**前提**: Playbook SIM/LIVE 仓位由 `monitor_playbook_sim_exits()` 自管退出 (`pipeline.py:7944`)，**不走 ExitMonitor**。但主线 (AUTO_MAIN/AUTO_ORB) 仓位由 ExitMonitor 管。两者共享 `position_store` 和 broker。

**场景**:
1. `T+0s`: 主线仓位 `US.AAPL` LONG 触发 SL，ExitMonitor 调 `_execute_exit()` → broker 平仓成功 → `position_store.remove("US.AAPL")` → `_exit_cooldowns["US.AAPL"] = T`
2. `T+30s`: 新信号到达 (或 playbook 反手指令)，order_executor 开 `US.AAPL` SHORT → `position_store.add("US.AAPL", new_position)` → `upsert_open_position()` 写入 DB
3. `T+30s ~ T+120s`: ExitMonitor 下一轮 `scan_positions()` 拿到新 `US.AAPL` SHORT position:
   - trail_state 级: 新 Position 的 `trail_state` 为空 → `last_trigger = 0` → **通过**
   - 类级: `_exit_cooldowns["US.AAPL"] = T`，`now_ts - T = 30~120s < 120s` → **continue (跳过)**
4. **结果**: 新 SHORT 仓位在 120s 冷却窗口内**不受 ExitMonitor 任何退出规则保护**。如果在这 90s 内价格暴跌穿越 SL，ExitMonitor 不会触发止损。

**严重度**: **P1** — 120s 内无 SL 保护是真实风险敞口。美股快市 120s 可以走 1-2%。

### 事故剧本 B: Playbook 日内再入 (intraday_reentry) 被吞

**路径**: `app/playbook/intraday_reentry.py` 生成的 `intraday_reentry-` 前缀指令 (`pipeline.py:3999`) 走 playbook 管线开仓。Playbook 仓位标 `PLAYBOOK_SIM` source_type，reconciler 会将其标 `PROTECTED` → ExitMonitor 不管 (R-7c 过滤)。

但 playbook 平仓走 `dual.close_position()` → `_close_trade_record()` → **DELETE open_positions** (`pipeline.py:6433`)，**不调 tombstone** (与 order_executor 不同)。如果同一 symbol 随后被主线信号再开 (非 playbook):

1. `T+0s`: Playbook 平 `US.AAPL` (DELETE open_positions，无 tombstone)
2. `T+10s`: 主线信号开 `US.AAPL` → `position_store.add()` → `upsert_open_position()`
3. ExitMonitor 下一轮: 新 position 的 `_exit_cooldowns` 查的是 **ExitMonitor 类级 dict** — 但 playbook 平仓路径从未写过 `_exit_cooldowns` → **此处不触发吞退出**

**结论**: Playbook→主线路径不触发冷却吞退出 (playbook 不写 ExitMonitor 冷却)。但主线→主线 (同 symbol SL 后快速再开) 和主线→playbook (如果 playbook 也读 ExitMonitor 冷却，实际不读) 才是真问题。

**真正的风险面**: 主线 SL 后 120s 内同 symbol 再开 → 新仓无 SL 保护 120s。

### 修复建议

- `_execute_exit()` 写入 `_exit_cooldowns` 时应带 `direction` 键: `_exit_cooldowns[(symbol, direction)]`
- 或在 `position_store.add()` 时清除对应 symbol 的冷却: `ExitMonitor._exit_cooldowns.pop(symbol, None)`
- 最小改动: 在 `scan_positions()` 检查类级冷却时，对比新 position 的 `entry_time` 与冷却时间戳 — 如果 position 是冷却标记之后创建的，跳过冷却

---

## ③ Tombstone 60-300s vs 部分成交 / 结算延迟竞态

### 机制

```
order_executor._close_position()
  ├─ broker 确认成交
  ├─ position_store.remove(symbol)           # L5070
  ├─ trade_store.upsert_open_position(...)   # L5080 (action="pending_close")
  ├─ mark_closed_tombstone(symbol)           # L5091 → 60s TTL
  ├─ confirm_tombstone(symbol)               # L5093 → 延长到 300s
  └─ request_immediate_sync()                # L5095 → 5s debounce
```

**Tombstone 定义**: `position_sync.py:165-166`
```python
self._closed_tombstones: Dict[str, tuple] = {}  # symbol → (close_at_ts, ttl_sec)
self._tombstone_ttl_sec = 60.0
```

**检查点**: `_is_tombstoned()` 仅在 `_sync_once()` 的 **"new_symbols" 路径** (Futu 有、DB 无) 被调用:
```python
# position_sync.py:1300 (approx — new_symbols path)
if self._is_tombstoned(symbol):
    logger.info(f"[US Sync] 跳过 tombstoned {symbol}")
    continue
```

**Tombstone 不检查的路径**:
- Ghost 清理 (DB 有、Futu 无): `position_sync.py:721+` — 无 tombstone 检查
- 已有持仓更新: `position_sync.py:966+` — 无 tombstone 检查
- 自管持仓隔离: `position_sync.py:934+` — 无 tombstone 检查

### 事故剧本 A: 部分成交 + 结算延迟 > 300s

**场景**:
1. `T+0s`: order_executor 发平仓单 100 股 `US.AAPL`，broker 确认成交 (dealt_qty=100)
2. `T+0s`: tombstone 标记 → 300s TTL
3. `T+60s`: broker 异步结算 — 但实际只成交了 50 股 (部分成交 / partial fill)
4. `T+300s`: tombstone 过期
5. `T+330s`: position_sync `_sync_once()` — Futu 仍报 50 股 AAPL (未全部成交), DB 已 pending_close
6. **Futu 有、DB 无/已 pending_close** → new_symbols 路径 → tombstone 已过期 → sync 认为这是"broker 新发现" → 写入 open_positions 恢复仓位
7. **结果**: 已部分平仓的仓位被 sync 恢复为全仓，ExitMonitor 恢复监控，可能对已平的那 50 股再发平仓单 → **"持仓不足" 错误** 或更糟的 **反向开仓**

**严重度**: **P1** — 部分成交 + tombstone 过期 + sync 恢复 = 双重平仓风险

### 事故剧本 B: Ghost 路径无 tombstone 保护

**场景**:
1. `T+0s`: order_executor 平仓成功 → position_store.remove + tombstone 300s
2. `T+10s`: sync 运行 → Ghost 路径 (DB 有 open_positions 行 [pending_close 但未 DELETE]、Futu 无)
3. Ghost 路径检查: pending_close → "✅ 确认平仓" → DELETE open_positions
4. **正常情况**: 这没问题

**但**: 如果平仓单是 LIMIT 挂单 (非立即成交), 30s 内 Futu 仍报有仓位:
1. `T+0s`: order_executor 发 LIMIT 平仓单 → 标记 `_close_order_unconfirmed` → 保留 position_store 和 open_positions
2. `T+30s`: LIMIT 成交 → Futu 不再报该仓位
3. `T+60s`: sync 运行 → Ghost 路径 → 180s 保护窗内 → 跳过
4. `T+180s`: sync 运行 → Ghost 路径 → pending_close 检查 → 如果没有 pending_close 标记 (比如 unmark 了) → 当做 ghost → DELETE
5. **正常但有风险**: 如果 broker 结算延迟导致 Futu 暂时报无仓位 (结算中), sync 可能过早清理

**严重度**: **P2** — 180s 保护窗 + pending_close 双保险，大部分场景安全

### 事故剧本 C: Playbook 平仓不调 tombstone

**发现**: `monitor_playbook_sim_exits()` 平仓后:
- 调 `_close_trade_record()` (pipeline.py:6001) → DELETE open_positions (pipeline.py:6433)
- **不调 `mark_closed_tombstone()`**
- **不调 `confirm_tombstone()`**

**场景**:
1. Playbook 平 `US.AAPL` → DELETE open_positions → 无 tombstone
2. `T+30s`: position_sync 运行 → Futu 延迟仍报 AAPL (broker 结算中)
3. Futu 有、DB 无 → new_symbols 路径 → `_is_tombstoned("US.AAPL")` = False (playbook 没标)
4. sync 重新写入 open_positions → 标为 MANUAL/AUTO → **仓位复活**
5. ExitMonitor 恢复监控 → 可能对已平仓发平仓单

**严重度**: **P0** — Playbook 是高频路径 (每日数十笔), 无 tombstone = 仓位复活风险。实际被两个因素缓解:
- sync 的 `_today_orders` 检查可能阻止标 AUTO
- playbook 平仓后 Futu 通常秒级更新 (结算延迟罕见)

### 修复建议

- playbook 平仓路径 (`_close_trade_record` 后) 应调 `mark_closed_tombstone(symbol)` + `confirm_tombstone(symbol)`
- tombstone 检查应扩展到 ghost 路径 (DB 有 Futu 无 时也应检查)
- 部分成交场景: 在 `upsert_open_position()` 中标 `is_partially_reduced=True` 后，sync 应感知部分成交不恢复

---

## ④ `open_positions` version 乐观锁 — 全写方合规审计

### 设计

`open_positions` 表有 `version INTEGER DEFAULT 1` 列 (`trade_store.py:229` 附近 schema)。
`upsert_open_position()` (主线写入方法) 实现了"读-增-写"模式:

```python
# trade_store.py:1018-1029
row = conn.execute(
    "SELECT version, ... FROM open_positions WHERE symbol = ?",
    (position.symbol,)
).fetchone()
current_version = (row[0] if row else 0) + 1
# ... 后续 INSERT OR REPLACE 写入 current_version
```

### 全写方审计表

| 写入方 | 文件:行 | version 处理 | WHERE version=? | 合规 |
|--------|---------|-------------|-----------------|------|
| `trade_store.upsert_open_position()` | `trade_store.py:1084` | 读→+1→写 | ❌ 无 WHERE | ⚠️ 半合规 |
| `trade_store.upsert_position_simple()` | `trade_store.py:974` | ❌ 不触碰 version | ❌ | ❌ 不合规 |
| `trade_store.update_position_field()` | `trade_store.py:1360` | 不读不改 version | ❌ | ❌ 不合规 |
| `trade_store.mark_sync_pending()` | `trade_store.py:1295` | 不读不改 version | ❌ | ❌ 不合规 |
| `trade_store.mark_sync_complete()` | `trade_store.py:1309` | 不读不改 version | ❌ | ❌ 不合规 |
| `trade_store.mark_anomalous()` | `trade_store.py:1323` | 不读不改 version | ❌ | ❌ 不合规 |
| `trade_store.unmark_anomalous()` | `trade_store.py:1348` | 读 version 但未用 | ❌ | ❌ 不合规 |
| `trade_store.remove_open_position()` | `trade_store.py:1165` | 直接 DELETE | N/A | N/A (删除) |
| `position_sync._sync_once()` 新仓 INSERT | `position_sync.py:1135` | 不触碰 version | ❌ | ❌ 不合规 |
| `position_sync._sync_once()` 更新 UPDATE | `position_sync.py:983` | 不触碰 version | ❌ | ❌ 不合规 |
| `position_sync._sync_once()` 自管 INSERT | `position_sync.py:956` | 不触碰 version | ❌ | ❌ 不合规 |
| `position_sync._sync_once()` 自管 UPDATE | `position_sync.py:946` | 不触碰 version | ❌ | ❌ 不合规 |
| `position_sync` ghost DELETE | `position_sync.py:762` | 直接 DELETE | N/A | N/A |
| `web_dashboard/order/executor.py` SQL_TAKEOVER | `executor.py:42` | 不触碰 version | ❌ | ❌ 不合规 |
| `web_dashboard/order/executor.py` SL update | `executor.py:1207` | 不触碰 version | ❌ | ❌ 不合规 |
| `web_dashboard/order/executor.py` SL+TP | `executor.py:1583,1719` | 不触碰 version | ❌ | ❌ 不合规 |
| `web_dashboard/order/executor.py` OCO | `executor.py:1807` | 不触碰 version | ❌ | ❌ 不合规 |
| `web_dashboard/routes/dashboard.py` SL | `dashboard.py:374` | 不触碰 version | ❌ | ❌ 不合规 |
| `web_dashboard/data_service.py` ghost mark | `data_service.py:933` | 不触碰 version | ❌ | ❌ 不合规 |
| `lifecycle/engine.py` UPDATE | `engine.py:304` | 不触碰 version | ❌ | ❌ 不合规 |
| `pipeline/position_monitor.py` DELETE | `position_monitor.py:1343` | 直接 DELETE | N/A | N/A |
| `playbook/pipeline.py` DELETE | `pipeline.py:6433` | 直接 DELETE | N/A | N/A |
| `web_dashboard/order/oco_handler.py` | `oco_handler.py:109` | 不触碰 version | ❌ | ❌ 不合规 |

### 关键发现

**1. `INSERT OR REPLACE` 不是乐观锁**: `upsert_open_position()` 用 `INSERT OR REPLACE INTO open_positions ... VALUES (...)` 无任何 `WHERE version = ?` 子句。这是 **last-writer-wins** 语义，不是 compare-and-swap。两个并发写者可能:
   - Writer A 读 version=5
   - Writer B 读 version=5
   - Writer A 写 version=6 (INSERT OR REPLACE)
   - Writer B 写 version=6 (INSERT OR REPLACE) — **两个 writer 都认为自己是 v6，B 覆盖 A**

**2. `upsert_position_simple()` 完全不碰 version**: playbook 下单后立即写入 (`trade_store.py:944`)，使用 `ON CONFLICT DO UPDATE` 但不触碰 version 列。如果 sync 随后用 `upsert_open_position()` 写入，读到的是旧 version → +1 → 可能比 simple 写入的版本号还低。

**3. position_sync 裸 SQL 全部绕过 version**: 6 处 UPDATE/INSERT 全部不碰 version。sync 每 30s 运行一次，是最高频的写者。

**4. Dashboard executor SQL_TAKEOVER 绕过 version**: 手动开仓 (`executor.py:42`) 的 `ON CONFLICT DO UPDATE` 不碰 version。

### 事故剧本: 并发写导致 version 回退

1. `T+0s`: `upsert_open_position()` 写入 US.AAPL v=10 (含 SL/TP/trail_state)
2. `T+5s`: `position_sync` UPDATE US.AAPL size/direction (不碰 version → DB 仍 v=10)
3. `T+10s`: dashboard executor SL update (不碰 version → DB 仍 v=10)
4. `T+15s`: `upsert_open_position()` 再写 → 读 v=10 → +1 → v=11 → INSERT OR REPLACE

**这个序列是安全的** (version 单调递增)。但:

1. `T+0s`: `upsert_position_simple()` 写 US.AAPL (不碰 version → DB 保留旧 v=5)
2. `T+2s`: `upsert_open_position()` 读 v=5 → +1 → v=6 → INSERT OR REPLACE 覆盖 simple 的写入
3. **结果**: simple 写入的字段 (direction/size/stop_loss) 被 `_preserved_*` 旧值覆盖

**严重度**: **P2** — version 列目前是**装饰性**的，没有实际的并发保护效果。所有写者都用 SQLite 的 serial 写入 (单连接 + `_lock`) 避免了真正的并发冲突，但 version 号不可信赖。

### 修复建议

- **短期**: 要么在所有 UPDATE/INSERT 路径统一 `version = COALESCE(open_positions.version, 0) + 1`，要么删除 version 列 (承认它是装饰)
- **中期**: 如果需要真正的乐观锁，改为 `UPDATE ... WHERE symbol=? AND version=? → 检查 affected_rows`
- **长期**: 用 `upsert_open_position()` 统一所有写入，废弃 `upsert_position_simple()` 和裸 SQL

---

## ⑤ 裸 symbol 旧 TradeRecorder 跨库归一漏洞

### 两个数据库

| 数据库 | 路径 | 写入者 | symbol 格式 |
|--------|------|--------|------------|
| **ftseek_us.db** (主库) | `data/ftseek_us.db` | `TradeStore.save_trade()`, `upsert_open_position()` | `"US.AAPL"` |
| **trade_records.db** (旧库) | `data/trade_records.db` | `TradeRecorder.record()` | `"AAPL"` (裸) |

### TradeRecorder symbol 格式

```python
# trade_recorder.py:139-154
def record(self, stock_code: str, ...):
    # 文档示例: stock_code="AAPL"
    conn.execute("INSERT INTO trade_records (..., symbol, ...) VALUES (...)", (stock_code, ...))
```

`stock_code` 参数直存入 `symbol` 列，无 "US." 前缀归一。

### 复盘脚本跨库漏洞清单

| 脚本 | 文件:行 | 读取哪个 DB | symbol 归一 | 漏洞 |
|------|---------|------------|------------|------|
| `daily_review.py` | `review/daily_review.py:42` | `get_trade_records_db()` → ftseek_us.db | ✅ (主库本身 US.XXX) | 无 |
| `weekly_review.py` | `review/weekly_review.py:5` | 同 daily_review | ✅ | 无 |
| `monthly_review.py` | `review/monthly_review.py:25-27` | `get_trade_recorder()` → **override** 到 `get_trade_records_db()` | ✅ (override 后读主库) | 无 (已修) |
| `cohort_ab.py` | `review/cohort_ab.py:20-31` | 双库搜索 (ftseek_us.db 优先, trade_records.db fallback) | ⚠️ | **有漏洞** |
| `strategy_analysis.py` | `review/strategy_analysis.py:19` | `get_trade_recorder()` → **默认 trade_records.db** | ❌ | **已退役 2026-11-01** |
| `psychology_analyzer.py` | `review/psychology_analyzer.py:19` | `get_trade_recorder()` → **默认 trade_records.db** | ❌ | **已退役 2026-11-01** |
| `suggestion_engine.py` | `review/suggestion_engine.py:58` | `get_trade_recorder()` → **默认 trade_records.db** | ❌ | **已退役 2026-11-01** |
| `improvement_generator.py` | `review/improvement_generator.py:29` | `get_trade_recorder()` → **默认 trade_records.db** | ❌ | **已退役 2026-11-01** |
| `weekly_optimizer.py` | `backtest/weekly_optimizer.py:61-67` | 候选列表: ftseek_us.db 优先 | ⚠️ | **已修 (部分)** |
| `edge_weekly_report.py` | `decision/edge_weekly_report.py:28-29` | 硬编码 ftseek_us.db | ✅ | 无 (已修) |

### 详细漏洞分析

#### `cohort_ab.py` — 双库 fallback 静默空数据

```python
# cohort_ab.py:20-31
def _get_db() -> Path:
    try:
        from app.core.paths import get_trade_records_db
        p = Path(get_trade_records_db())
        if p.exists():
            return p
    except Exception:
        pass
    root = Path(__file__).resolve().parent.parent.parent
    for cand in (root / "data" / "ftseek_us.db", root / "data" / "trade_records.db"):
        if cand.exists():
            return cand
```

**现状**: `get_trade_records_db()` 返回 `ftseek_us.db`，所以 cohort_ab 实际读主库 (US.XXX 格式)。但如果 `get_trade_records_db()` 异常或返回的路径不存在，fallback 到 `trade_records.db` (裸 symbol) — 查询不报错但可能返回 0 行 (因为 symbol 格式不匹配任何过滤条件)。

**严重度**: **P3** — 实际不影响生产 (ftseek_us.db 总存在)，但如果有人手动清理主库后跑复盘，会静默切到空壳旧库。

#### `strategy_analysis.py` / `psychology_analyzer.py` / `suggestion_engine.py` / `improvement_generator.py` — 默认旧库

```python
# strategy_analysis.py:19
self.trade_recorder = get_trade_recorder()
# → TradeRecorder.__init__() → get_trade_records_legacy_db() → trade_records.db
# → 该文件是 4 月遗留空壳 (0 行/28KB, weekly_optimizer.py:61-63 注释)
```

**这 4 个脚本全部读空壳旧库**:
- `get_trade_recorder()` 返回 `TradeRecorder` 实例
- 默认 `db_path = get_trade_records_legacy_db()` → `data/trade_records.db`
- 该文件存在但 **0 行数据** (所有真实交易写入 ftseek_us.db)
- `monthly_review.py` 已修 (line 27: override 到 `get_trade_records_db()`)
- 其余 4 个未修

**事故**: 用户跑 `strategy_analysis.py` 得到 "无交易记录" → 误以为策略无交易 → 做出错误决策。`psychology_analyzer.py` 返回空数据 → 心理学分析缺失 → 复盘报告不完整。

**严重度**: **P2** — 影响复盘质量，不影响实盘交易。

#### `weekly_optimizer.py` — 已修但有残留

```python
# weekly_optimizer.py:61-67
# 候选兜底顺序: 主库优先. 历史教训 (2026-08-29): trade_records.db 排首位,
# 该文件是 4 月遗留空壳 (0 行/28KB) 却因"存在且非零字节"永远赢得选择
DEFAULT_TRADE_DB_CANDIDATES = [
    _DATA_DIR / "ftseek_us.db",        # 主库优先 ✅
    _DATA_DIR / "trade_records.db",    # fallback
]
```

**已修** (2026-08-29): 主库排首位。但如果 ftseek_us.db 不存在 (极端情况)，仍会 fallback 到空壳旧库。

### 修复建议

> ⚠️ **2026-11-01 退役批 (取代下面 P0)**: `strategy_analysis.py` / `psychology_analyzer.py` /
> `suggestion_engine.py` / `improvement_generator.py` **四个模块已整体删除**。
> 依据: 全仓零生产调用方 (唯一入口是 `app/review/__init__.py` 的 re-export)、数据源为 0 行遗留库、
> 最后实质改动 2026-05-20、无 cron/动态导入/台账登记、能力已被
> `daily_review._generate_suggestions_v2` / `weekly_review._analyze_strategies` 替代。
> **P0 因此作废, 不要再去给这 4 个模块加 override** (文件已不存在)。下方 P1/P2 仍有效。

- **P0 (作废, 见上方退役说明)**: 给 `strategy_analysis.py`, `psychology_analyzer.py`, `suggestion_engine.py`, `improvement_generator.py` 加 `monthly_review.py` 同样的 override:
  ```python
  self.trade_recorder = get_trade_recorder()
  self.trade_recorder.db_path = get_trade_records_db()
  ```
- **P1**: 统一 `get_trade_recorder()` 的默认路径到 ftseek_us.db (消除 legacy 路径)
- **P2**: 删除 `data/trade_records.db` 空壳文件 (消除 fallback 诱惑)
- **P3**: 在所有读取 trade_records 的脚本中加 symbol 格式归一 (`if not sym.startswith("US."): sym = f"US.{sym}"`)

---

## 汇总: 风险矩阵

| 编号 | 问题 | 严重度 | 影响面 | 触发概率 | 修复复杂度 |
|------|------|--------|--------|---------|-----------|
| ②-A | 主线 SL 后 120s 内同 symbol 再开 → 无 SL 保护 | P1 | 主线仓位 | 低 (需 <120s 再入) | 低 (加 direction 键或清除冷却) |
| ③-A | 部分成交 + tombstone 300s 过期 + sync 恢复 | P1 | 所有平仓路径 | 低 (部分成交罕见) | 中 (扩展 tombstone 检查) |
| ③-C | Playbook 平仓不调 tombstone → 仓位复活 | P0 | Playbook 高频路径 | 中 (broker 延迟常见) | 低 (加 tombstone 调用) |
| ④ | version 列装饰性 — 无写方合规 | P2 | 全 DB 写入 | N/A (设计问题) | 中 (统一写入路径) |
| ⑤ | 4 个复盘脚本读空壳旧库 | P2 | 复盘质量 | 高 (每次运行) | 低 (加 override) |

---

*Generated by Explore Agent — SSOT Deep-Dive — 2026-09-05*
