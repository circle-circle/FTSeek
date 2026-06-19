# PositionStore = Broker 镜像 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 消除 PositionStore 与 Broker 之间的状态偏差，使内存持仓永远 = Broker 现实。

**Architecture:** 不创建新 Gateway 类（违反 "增量>重构"），而是在现有 `position_sync` 基础上收拢写路径。核心改动：(1) 消灭 DB 启动恢复的独立写路径，(2) 订单成交后即时触发 sync 而非等 30s，(3) 所有 add/remove 操作前校验 broker 状态。

**Tech Stack:** Python 3.8, SQLite, Futu OpenD SDK

---

## 现状分析

```
PositionStore 写入路径 (30 个, 7 个文件):
┌─────────────────────────────────────────────────────────┐
│ 启动恢复 (7): position_recovery ×4, position_monitor ×2,│
│              reconciler ×1                               │
│ 运行时同步 (3): position_sync ×2, position_monitor ×2    │
│ 订单成交 (3): order_executor ×3 (LONG/SHORT/OPTION)      │
│ 平仓清理 (6): order_executor ×2, position_monitor ×2,    │
│              dashboard ×1, conflict ×1                    │
│ 冲突解决 (2): order_executor ×2                           │
│ 订单失败 (2): order_executor ×2                           │
│ 手动平仓 (1): dashboard ×1                                │
└─────────────────────────────────────────────────────────┘

读取路径 (55 个, 20 个文件): 信号扫描/退出监控/风控/Dashboard/LLM
```

**核心矛盾**: 多个写入源各自独立操作 → 与 Broker 偏差 → 幽灵仓/假亏损。

---

## Phase 1: 消灭启动恢复的独立写路径

**原理**: 启动时从 DB 恢复持仓是幽灵仓的主要来源。DB 可能有过期记录（手动平仓但 exit_price=0、外部平仓但系统未感知）。Broker 才是唯一真相源。

### Task 1: 启动恢复改为 Broker-gated

**Objective:** 启动时不再盲目从 DB 恢复持仓到 PositionStore。改为：查 Broker → 有则恢复元数据 → 无则跳过。

**Files:**
- Modify: `app/core/position_recovery.py:289` (stock restore)
- Modify: `app/core/position_recovery.py:478` (option restore)
- Modify: `app/core/position_recovery.py:585` (P0 backfill)
- Modify: `app/pipeline/position_monitor.py:987` (main pipeline restore)
- Modify: `app/pipeline/position_monitor.py:1050` (option restore)

**Step 1: 在 `position_recovery.restore_positions_from_db()` 添加 Broker 校验**

```python
# 在 position_store.add() 调用前插入 Broker gate:
# 查询 Broker 是否确实持有此 symbol
broker_positions = _query_broker_positions_safe(scheduler)
if broker_positions is not None:  # 查询成功
    if symbol not in broker_positions:
        logger.info(
            f"[启动·Broker-gated] {symbol} DB 有记录但 Broker 无持仓 → 跳过恢复 (避免幽灵仓)"
        )
        continue
    # Broker 有 → 用 Broker 的 qty/cost/direction 覆盖 DB 值
    broker_info = broker_positions[symbol]
    position.size = broker_info['qty']
    position.avg_entry_price = broker_info['cost']
    position.direction = Direction.SHORT if broker_info['direction'] == 'SHORT' else Direction.LONG
```

**Step 2: 同理修改 position_monitor.py 的 restore_positions_from_db()**

同样的 Broker-gated 模式：恢复前校验 Broker，Broker 无则跳过。

**Step 3: 添加 `_query_broker_positions_safe()` 公共函数**

```python
def _query_broker_positions_safe(scheduler) -> Optional[Dict[str, Dict]]:
    """安全查询 Broker 持仓, 失败返回 None (不阻断启动)"""
    try:
        from app.pipeline.order_executor import _refresh_broker_position_qty
        # 复用 position_sync 的 query_broker_positions
        from app.core.position_sync import USPositionSync
        # ... 或直接从 broker_adapter.get_positions()
        adapter = getattr(getattr(scheduler, 'execution_engine', None), 'broker_adapter', None)
        if adapter and hasattr(adapter, 'get_positions'):
            raw = adapter.get_positions() or []
            return {
                p['symbol']: {
                    'qty': abs(float(p.get('qty', 0))),
                    'cost': float(p.get('cost_price', 0)),
                    'direction': 'SHORT' if float(p.get('qty', 0)) < 0 else 'LONG'
                }
                for p in raw if p.get('symbol')
            }
    except Exception as e:
        logger.warning(f"[启动·Broker查询] 失败 (降级为传统恢复): {e}")
    return None  # 查询失败 → 不阻断, 降级为原有行为
```

**Step 4: 运行测试**

```bash
cd /opt/ftseek/FTSeek-US
python3 -m py_compile app/core/position_recovery.py
python3 -m py_compile app/pipeline/position_monitor.py
pytest tests/ -k "restore" -x --tb=short 2>&1 | tail -20
```

**Step 5: 验证**

重启服务，检查日志：
```
grep "Broker-gated" /opt/ftseek/FTSeek-US/logs/ftseek_us.log
```
预期：DB 有但 Broker 无的持仓被跳过。

---

### Task 2: 订单成交后即时触发 position_sync

**Objective:** 订单成交确认后，不再等 30s 的 sync 周期，立即触发一次 position_sync 使 PositionStore 与 Broker 对齐。

**Files:**
- Modify: `app/pipeline/order_executor.py:4752` (close_position success)
- Modify: `app/pipeline/order_executor.py:2954` (execute_long success)
- Modify: `app/pipeline/order_executor.py:2433` (execute_short success)
- Modify: `app/pipeline/position_monitor.py:600` (partial_close success)

**Step 1: 在 `_close_position` 成功后触发即时 sync**

```python
# order_executor.py, 在 position_store.remove() 之后:
# ── P1: 成交后即时 sync (不等 30s 周期) ──
try:
    if hasattr(scheduler, 'position_sync') and scheduler.position_sync:
        scheduler.position_sync.request_immediate_sync()
except Exception:
    pass  # sync 失败不影响已完成的平仓
```

**Step 2: 在 USPositionSync 添加 `request_immediate_sync()` 方法**

```python
def request_immediate_sync(self):
    """请求立即执行一次 sync (非阻塞, 由 sync 线程处理)"""
    self._immediate_sync_requested = True
```

在 sync 主循环中检查此标记：
```python
# position_sync.py 主循环, 在 time.sleep() 之前:
if self._immediate_sync_requested:
    self._immediate_sync_requested = False
    self._sync_once()  # 立即执行
```

**Step 3: 同理在 open position 成功后触发**

`_execute_long` 和 `_execute_short` 成功后同样调用 `request_immediate_sync()`。

**Step 4: 运行测试 + 语法检查**

```bash
python3 -m py_compile app/core/position_sync.py
python3 -m py_compile app/pipeline/order_executor.py
```

---

## Phase 2: 写入前校验 Broker

### Task 3: PositionStore.add() 添加 Broker 一致性断言

**Objective:** 每次 add 操作前，验证 Broker 确实持有此 symbol。Broker 无 → 拒绝写入 + 告警。

**Files:**
- Modify: `app/core/scheduler_core.py:58` (PositionStore.add)

**Step 1: 给 PositionStore 注入 broker_validator**

```python
class PositionStore:
    def __init__(self):
        self._positions: Dict[str, Position] = {}
        self._lock = threading.RLock()
        self._broker_validator: Optional[Callable] = None  # 延迟注入
    
    def set_broker_validator(self, validator: Callable[[str], bool]):
        """注入 Broker 校验函数: symbol → bool (Broker 是否持有)"""
        self._broker_validator = validator
    
    def add(self, symbol: str, position: Position, skip_broker_check: bool = False):
        """添加持仓 (Broker-gated)"""
        if not skip_broker_check and self._broker_validator:
            try:
                if not self._broker_validator(symbol):
                    logger.warning(
                        f"[PositionStore·拒绝] {symbol} Broker 无此持仓, 拒绝 add()"
                    )
                    return False
            except Exception:
                pass  # 校验失败不阻断 (fail open for adds, less dangerous)
        
        with self._lock:
            self._positions[symbol] = position
            # ... existing daily_active_pool logic
        return True
```

**Step 2: 启动时注入 validator**

在 scheduler 初始化完成后，将 `_refresh_broker_position_qty` 包装为 validator 注入 PositionStore。

**Step 3: 测试**

- 添加 `skip_broker_check=True` 的现有测试不改
- 新增测试: validator 返回 False 时 add() 被拒绝

---

### Task 4: 熔断计算排除手动亏损

**Objective:** 日亏损熔断只计算自动交易产生的亏损。手动平仓的亏损单独统计，不影响自动交易能力。

**Files:**
- Modify: `app/risk/engine.py` (日亏损计算逻辑)
- Modify: `app/web_dashboard/order/validator.py` (手动订单校验)

**Step 1: 分离手动/自动日亏损**

在 trade_records 查询中增加 `source_type` 过滤：
```python
# 自动交易亏损 (影响熔断)
auto_loss = sum(pnl for pnl, src in trades if src != 'MANUAL' and pnl < 0)
# 手动交易亏损 (不影响熔断, 仅供展示)
manual_loss = sum(pnl for pnl, src in trades if src == 'MANUAL' and pnl < 0)
```

**Step 2: Dashboard 手动订单校验也改为仅计自动亏损**

```python
# validator.py 中的日亏损熔断检查:
if auto_daily_loss >= threshold:  # 只用自动亏损
    reject("日亏损熔断: 自动交易亏损 ${auto_daily_loss:.0f} ({pct:.1%}) ≥ 阈值")
```

---

## Phase 3: trade_records 写入校验

### Task 5: exit_price=0 的记录拒绝写入

**Objective:** trade_records INSERT 时，exit_price ≤ 0 的记录被拒绝（而非写入假数据）。

**Files:**
- Modify: `app/storage/trade_store.py:607` (save_trade method)

**Step 1: 添加 exit_price 校验**

```python
def save_trade(self, record: dict):
    exit_price = record.get("exit_price", 0)
    if exit_price <= 0:
        logger.error(
            f"[TradeStore·拒绝] {record.get('symbol', '?')} exit_price={exit_price} "
            f"≤ 0, 拒绝写入 (防止假亏损污染熔断)"
        )
        # 不写入, 但记录审计
        self._audit_rejected_trade(record, reason="exit_price_zero")
        return False
    
    # ... existing INSERT logic
    return True
```

**Step 2: 添加审计表记录被拒绝的交易**

```python
def _audit_rejected_trade(self, record: dict, reason: str):
    """记录被拒绝的交易到审计表 (不影响主流程)"""
    try:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.execute("""
            INSERT INTO rejected_trades (symbol, direction, exit_price, reason, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            record.get('symbol', ''),
            record.get('direction', ''),
            record.get('exit_price', 0),
            reason,
            json.dumps(record, default=str),
            datetime.now().isoformat(),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"[TradeStore] audit write failed: {e}")
```

---

## 决策记录 (2026-06-16)

| # | 问题 | 决策 | 理由 |
|---|------|------|------|
| Q1 | Broker 不可用时怎么办 | **空仓等待** | 宁可空仓不冒幽灵仓风险 |
| Q2 | 手动大亏损是否暂停自动 | **不暂停** | 简单直接，不增加复杂度 |
| Q3 | 即时 sync 是否 debounce | **5s debounce** | 减少 API 压力，合并连续成交 |

---

## Implementation Order

```
Phase 1 (高优先级, 低风险):
  Task 1: 启动恢复 Broker-gated     → 1-2h
  Task 2: 成交后即时 sync            → 1h

Phase 2 (中优先级, 中风险):
  Task 3: PositionStore.add() 校验   → 2h
  Task 4: 熔断分离手动/自动          → 1h

Phase 3 (高优先级, 低风险):
  Task 5: exit_price=0 拒绝写入     → 30min

总计: ~5-6h 实施 + 1h 测试验证
```

## Verification Strategy

1. **语法检查**: 每个 Task 完成后 `py_compile` 所有修改文件
2. **单元测试**: 新增 test_ghost_convergence.py 覆盖 Broker-gated 场景
3. **集成验证**: 重启服务 → 检查日志无幽灵仓 → 模拟手动平仓 → 验证 exit_price 非零
4. **回归测试**: `pytest tests/ -x` 确保现有测试不被破坏
5. **开盘观察**: 首次开盘时监控日志，确认 PositionStore 与 Broker 一致
