# 美股项目统一修复方案 (2026-05-21)

## 执行原则
- 架构级重构，非补丁式修复
- 每个决策回答"为什么"
- 渐进式交付，分阶段验证
- 以 broker 端真实持仓为准

---

## 架构问题诊断：3 个根本原因

### 根因 1：仓位生命周期无统一管理（P1-1, P1-2 部分）
**现状**：4 个独立模块各自决定仓位生死
- `scalping/engine.py` — _monitor_active_trades + _close_trade
- `core/position_sync.py` — ghost_cleaned + reconcile
- `scripts/position_reconciler.py` — 独立对账
- `pipeline/position_monitor.py` — 止损/止盈 + 期权管理

**后果**：同一笔交易被多个模块竞争处理，导致秒级清理、幽灵仓复活、止损被跳过。

**根因**：缺少统一的仓位状态机，每个模块独立判断"仓位是否存在"。

### 根因 2：价格获取失败 = 风控静默失效（P1-2, P2-2）
**现状**：`position_monitor.py:380-386` 价格获取失败时 `continue` 跳过
**后果**：903 次价格获取失败 → 903 次止损/止盈检查被跳过 → 风控完全失效
**根因**：`continue` 语义错误 — 应该是"记录失败 + 报警 + 使用 fallback"，而非"跳过整个检查循环"

### 根因 3：配置与实现脱节（P2-1, P1-3, P2-3）
**现状**：
- YAML delta_range=[-0.4, -0.3] → 代码硬编码 ATM (delta=-0.5)
- YAML shadow EOD → 无 cron 任务执行
- YAML signal_arbitration → DB 表为空

**后果**：YAML 配置成为文档而非契约，运行时行为与预期不一致
**根因**：代码不读取 YAML 配置，或配置读取逻辑缺失

---

## 统一修复方案：3 阶段 × 7 任务

### 阶段 1：止血 (P0，1-2 小时)
**目标**：防止当前损失扩大

#### 1.1 期权止损 fallback 机制
**文件**：`app/pipeline/position_monitor.py:380-386`
**修改**：
```python
# 当前代码：
if current_price <= 0:
    _opt_price_fail_count[option_code] = _opt_price_fail_count.get(option_code, 0) + 1
    if _opt_price_fail_count[option_code] % 10 == 1:
        logger.warning(...)
    continue  # ← 根因：跳过整个检查循环

# 修复后：
if current_price <= 0:
    _opt_price_fail_count[option_code] = _opt_price_fail_count.get(option_code, 0) + 1
    fail_count = _opt_price_fail_count[option_code]
    
    # 1. 报警升级
    if fail_count % 10 == 1:
        logger.warning(f"[期权监控] 价格获取失败 {fail_count}次 {position.symbol} {option_code}")
    
    # 2. Fallback 1: 使用 entry_price 估算 PnL (保守估计)
    # 期权时间价值衰减，用 entry_price * 0.9 作为 fallback (假设 10% 时间损耗)
    fallback_price = entry_price * 0.9
    pnl_ratio = fallback_price / entry_price
    pnl_pct = pnl_ratio - 1.0
    
    # 3. Fallback 2: 超过 50 次失败，强制平仓 (风控优先)
    if fail_count >= 50:
        logger.error(f"[期权·风控] {position.symbol} {option_code} 价格获取失败 {fail_count}次，强制平仓")
        _execute_option_close_order(scheduler, position, option_code, qty, 
                                    reason="PRICE_FAIL_FORCE_CLOSE")
        continue
    
    # 4. 使用 fallback 价格继续止损检查
    current_price = fallback_price
```

**验证**：重启服务后观察日志，确认止损检查不再被跳过

#### 1.2 期权 selector 配置对齐
**文件**：`app/execution/options_selector.py:457-492`
**修改**：`_get_from_futu` 方法增加 delta_range 筛选
```python
# 读取 YAML 配置
from app.execution.short_strategy import load_short_config
cfg = load_short_config()
delta_range = cfg.get("options", {}).get("delta_range", [-0.5, -0.3])
delta_min, delta_max = delta_range[0], delta_range[1]

# 在循环中筛选
for _, row in data.iterrows():
    # ...
    delta = float(row.get("delta", -0.5) or -0.5)
    
    # 按 delta_range 筛选 (而非纯 ATM)
    if not (delta_min <= delta <= delta_max):
        continue
    
    # 然后按 strike 差异排序
```

**验证**：观察日志确认期权选择按 OTM delta 范围筛选

---

### 阶段 2：架构加固 (P1，4-8 小时)
**目标**：解决仓位生命周期混乱

#### 2.1 统一仓位生命周期状态机
**新增文件**：`app/core/position_lifecycle.py`
```python
"""
统一仓位生命周期管理

状态流转：
  pending → broker_confirmed → active → closing → closed
  
规则：
  1. 下单后 status=pending，等待 broker 确认
  2. broker 确认后 status=broker_confirmed（由 position_sync 更新）
  3. 监控开始 status=active
  4. 触发止损/止盈/时间 status=closing（锁定状态，禁止其他模块修改）
  5. 平仓完成 status=closed

核心原则：
  - broker 确认是唯一真实来源
  - 一旦进入 closing 状态，其他模块禁止处理
  - 所有状态变更必须记录 trace_id
"""
from enum import Enum
from typing import Optional, Dict, Any
import sqlite3
from datetime import datetime

class LifecycleStatus(Enum):
    PENDING = "pending"
    BROKER_CONFIRMED = "broker_confirmed"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"

class PositionLifecycle:
    """统一仓位生命周期管理器"""
    
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._ensure_column()
    
    def _ensure_column(self):
        """确保 open_positions 表有 lifecycle_status 列"""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("ALTER TABLE open_positions ADD COLUMN lifecycle_status TEXT DEFAULT 'active'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # 列已存在
        finally:
            conn.close()
    
    def transition(self, trace_id: str, from_status: LifecycleStatus, 
                   to_status: LifecycleStatus, reason: str = "") -> bool:
        """
        状态转换
        
        Returns:
            True 如果转换成功，False 如果状态冲突
        """
        conn = sqlite3.connect(self._db_path)
        try:
            # 检查当前状态
            cur = conn.execute(
                "SELECT lifecycle_status FROM open_positions WHERE trace_id = ?",
                (trace_id,)
            )
            row = cur.fetchone()
            
            if not row:
                # 可能已在 closed_positions 表中
                return False
            
            current = LifecycleStatus(row[0] or 'active')
            
            # 状态冲突检测
            if current == LifecycleStatus.CLOSING and to_status != LifecycleStatus.CLOSED:
                return False  # closing 状态只能转到 closed
            
            if current == LifecycleStatus.CLOSED:
                return False  # closed 状态不可再转换
            
            # 执行转换
            conn.execute(
                "UPDATE open_positions SET lifecycle_status = ? WHERE trace_id = ?",
                (to_status.value, trace_id)
            )
            conn.commit()
            return True
        finally:
            conn.close()
    
    def is_active(self, trace_id: str) -> bool:
        """检查仓位是否处于活跃状态（非 closing/closed）"""
        conn = sqlite3.connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT lifecycle_status FROM open_positions WHERE trace_id = ?",
                (trace_id,)
            )
            row = cur.fetchone()
            if not row:
                return False
            status = row[0] or 'active'
            return status not in ('closing', 'closed')
        finally:
            conn.close()
```

**集成点**：
- `scalping/engine.py:_close_trade` — 平仓前检查 `lifecycle_status`
- `position_sync.py:reconcile` — 清理前检查 `lifecycle_status`
- `position_monitor.py:_check_positions` — 止损前设置 `lifecycle_status=closing`

#### 2.2 Shadow EOD Cron 任务
**文件**：新增 `/etc/systemd/system/ftseek-shadow-eod.service` + `.timer`
```ini
# ftseek-shadow-eod.service
[Unit]
Description=FTSeek US Shadow EOD Replay
After=ftseek-us.service

[Service]
Type=oneshot
WorkingDirectory=/opt/ftseek/FTSeek-US
ExecStart=/opt/ftseek/FTSeek-US/venv/bin/python scripts/shadow_eod_replay.py --days 1
Environment=PYTHONPATH=/opt/ftseek/FTSeek-US

# ftseek-shadow-eod.timer
[Unit]
Description=FTSeek US Shadow EOD Daily Timer

[Timer]
OnCalendar=Mon..Fri 16:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

**验证**：`systemctl start ftseek-shadow-eod.service` 手动执行一次，确认 194 条未关闭记录被处理

#### 2.3 LLM 链路日志补全
**文件**：`app/strategy/llm_strategy_engine.py`
**修改**：`audit_position_async` 增加错误处理和日志
```python
def audit_position_async(self, symbol: str, context: Dict, rsshub=None, timeout: int = 15):
    """异步审计仓位 — 增加完整日志链路"""
    import time
    start = time.time()
    
    try:
        # 1. 记录调用开始
        logger.info(f"[LLM Audit] {symbol} 开始审计 context={context}")
        
        # 2. 调用 LLM
        result = self._call_llm(symbol, context, rsshub, timeout)
        
        # 3. 记录结果
        logger.info(f"[LLM Audit] {symbol} 完成 耗时={time.time()-start:.1f}s result={result}")
        
        # 4. 写入 llm_audit.db
        self._record_audit_call(symbol, context, result, time.time()-start)
        
        return result
    except Exception as e:
        logger.error(f"[LLM Audit] {symbol} 审计失败: {e}")
        self._record_audit_call(symbol, context, {"error": str(e)}, time.time()-start)
        return None
```

**验证**：重启后观察 `llm_audit.db` 是否有新记录写入

---

### 阶段 3：防御加固 (P2，2-4 小时)
**目标**：防止同类问题再次发生

#### 3.1 仓位同步单一入口
**文件**：`app/core/position_sync.py`
**修改**：所有仓位清理操作必须通过 `PositionLifecycle` 检查
```python
def reconcile(self, broker_positions: List[Dict]) -> List[str]:
    """
    对账 — 增加生命周期检查
    
    规则：
      1. 只有 lifecycle_status=active 的仓位可以被清理
      2. closing/closed 状态的仓位跳过
      3. pending 状态仓位等待 broker 确认（保护窗口）
    """
    for pos in self._position_store.positions:
        # 生命周期检查
        if not self._lifecycle.is_active(pos.trace_id):
            logger.debug(f"[对账] {pos.symbol} {pos.trace_id} 状态非活跃，跳过")
            continue
        
        # 保护窗口
        if self._is_in_protection_window(pos):
            continue
        
        # ... 现有对账逻辑
```

#### 3.2 配置验证启动检查
**文件**：`app/core/scheduler_core.py`
**修改**：启动时验证 YAML 配置与代码行为一致性
```python
def _validate_config_alignment(self):
    """启动时验证关键配置与代码行为一致"""
    cfg = load_short_config()
    
    # 检查 1: options delta_range
    options_cfg = cfg.get("options", {})
    if options_cfg.get("enabled", False):
        delta_range = options_cfg.get("delta_range", [])
        if delta_range:
            logger.info(f"[启动验证] options delta_range={delta_range}")
            # 确保 selector 读取此配置
    
    # 检查 2: shadow EOD
    if not self._shadow_eod_timer_exists():
        logger.warning("[启动验证] shadow EOD timer 未配置，数据将无法自动关闭")
```

#### 3.3 风控失效报警升级
**文件**：`app/pipeline/position_monitor.py`
**修改**：价格获取失败时发送报警（钉钉/飞书）
```python
# 新增报警阈值
PRICE_FAIL_WARNING = 10
PRICE_FAIL_CRITICAL = 30
PRICE_FAIL_FORCE_CLOSE = 50

if fail_count >= PRICE_FAIL_CRITICAL:
    # 发送飞书/钉钉报警
    self._send_alert(
        level="CRITICAL",
        message=f"期权 {symbol} 价格获取失败 {fail_count}次，风控已失效 {fail_count - PRICE_FAIL_CRITICAL} 分钟"
    )
```

---

## 交付计划

| 阶段 | 任务 | 预计时间 | 风险 |
|------|------|----------|------|
| 1.1 | 期权止损 fallback | 30min | 低 — 仅修改 position_monitor.py |
| 1.2 | 期权 selector delta_range | 30min | 低 — 仅修改 options_selector.py |
| 2.1 | 仓位生命周期状态机 | 2h | 中 — 涉及多模块集成 |
| 2.2 | Shadow EOD cron | 30min | 低 — 仅添加 systemd 配置 |
| 2.3 | LLM 链路日志 | 30min | 低 — 仅修改 llm_strategy_engine.py |
| 3.1 | 仓位同步单一入口 | 1h | 中 — 需要测试对账逻辑 |
| 3.2 | 配置验证启动检查 | 30min | 低 — 仅添加启动逻辑 |
| 3.3 | 风控失效报警 | 30min | 低 — 仅添加报警逻辑 |

**总计**：6-8 小时

---

## 验证清单

### 阶段 1 验证
- [ ] 重启服务后日志无 "止损止盈暂不生效" 警告
- [ ] 期权 selector 日志显示 delta_range 筛选生效
- [ ] DB `trade_records` 无新的 `ghost_cleanup_cruise` 记录

### 阶段 2 验证
- [ ] `open_positions` 表新增 `lifecycle_status` 列
- [ ] Shadow EOD 执行后 `shadow_trades.closed=1` 数量增加
- [ ] `llm_audit.db` 有新记录写入

### 阶段 3 验证
- [ ] 启动日志显示配置验证结果
- [ ] 价格获取失败时报警发送成功
- [ ] 对账逻辑不再清理 `closing/closed` 状态仓位

---

## 回滚计划

每个阶段独立可回滚：
- 阶段 1：`git checkout HEAD -- app/pipeline/position_monitor.py app/execution/options_selector.py`
- 阶段 2：`git checkout HEAD -- app/core/position_lifecycle.py` + 禁用 systemd timer
- 阶段 3：`git checkout HEAD -- app/core/position_sync.py app/core/scheduler_core.py`

---

## 风险说明

1. **仓位生命周期状态机**：新增列可能影响现有查询，需要确保向后兼容
2. **Shadow EOD cron**：首次执行可能处理大量历史数据，建议先用 `--dry-run` 验证
3. **LLM 链路日志**：增加日志量可能影响性能，建议异步写入

---

## 后续建议

1. **监控仪表板**：为 `lifecycle_status` 添加 Grafana 仪表板
2. **自动化测试**：为状态机转换添加单元测试
3. **配置即代码**：将 YAML 配置纳入 CI 检查，确保与代码行为一致
