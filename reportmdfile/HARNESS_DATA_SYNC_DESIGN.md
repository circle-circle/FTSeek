# Harness 数据同步方案设计

**分析时间**: 2026-03-30 12:15 CST  
**分析方式**: Harness 多模型协作  
**参与模型**: Strategist (代码分析) + RiskOfficer (风险评估) + ContextEngineer (方案设计)

---

## 一、现状深度分析

### 1.1 富途 API 限制

#### 支持的接口

| 接口 | 功能 | 状态 |
|------|------|------|
| `position_list_query` | 查询持仓 | ✅ 可用 |
| `order_list_query` | 查询订单 (需 order_id) | ✅ 可用 |
| `get_history_order_list` | 查询历史订单 | ⚠️ 需特定客户端 |
| `history_order_list_query` | 查询历史订单 | ❌ 参数不匹配 |
| `get_position_list` | 查询持仓 | ❌ 不存在 |
| `get_history_orders` | 查询历史成交 | ❌ 不存在 |

#### 核心问题

**富途模拟账户不支持标准成交查询接口**

```python
# 实盘接口 (可用)
ctx.position_list_query(trd_env=TrdEnv.REAL)  # ✅
ctx.order_list_query(order_id="xxx", trd_env=TrdEnv.REAL)  # ✅

# 模拟账户接口 (受限)
ctx.position_list_query(trd_env=TrdEnv.SIMULATE)  # ✅
ctx.get_history_order_list(filter_orders_by_status=[1, 2])  # ⚠️ 需特定客户端
```

---

### 1.2 现有代码分析

#### 订单执行流程

```python
# app/trader/executor.py
def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
    # 1. 下单
    order_id = self._place_order(signal)
    
    # 2. 查询成交价 (兜底逻辑)
    ret_order, order_data = trd_ctx.order_list_query(order_id=order_id, trd_env=trd_env)
    if ret_order == 0 and len(order_data) > 0:
        dealt_avg = float(order_data.iloc[0].get('dealt_avg_price', 0))
        if dealt_avg > 0:
            actual_entry_price = dealt_avg  # ✅ 使用真实成交价
    
    # 3. ❌ 缺失：未记录到数据库
    # self.record_trade_to_db(...)  # 未调用
```

**问题**:
- ✅ 下单成功
- ✅ 查询成交价
- ❌ **未保存到数据库**

#### 历史订单查询 (现有实现)

```python
# app/risk/risk_manager.py 第 876 行
from app.v4.trader.opend_executor import get_opend_executor
executor = get_opend_executor(simulate=True)

if executor.client:
    ret_code, ret_data = executor.client.get_history_order_list(
        filter_orders_by_status=[1, 2],  # 已成交
    )
    
    if ret_code == 0 and ret_data is not None:
        for order in ret_data.itertuples():
            today_trades.append({
                'code': order.code,
                'pnl': order.profit,
                'quantity': order.qty,
                'price': order.price,
            })
```

**关键发现**:
- ✅ 有查询历史订单的代码
- ✅ 使用 `get_opend_executor(simulate=True)`
- ✅ 过滤已成交订单 (`status=[1, 2]`)
- ❌ **仅用于周报，未用于实时同步**

---

### 1.3 数据库表结构

#### 主交易表 (trades)

```sql
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    code TEXT NOT NULL,
    name TEXT,
    action TEXT NOT NULL,  -- BUY/SELL
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity INTEGER NOT NULL,
    pnl REAL,
    pnl_percent REAL,
    fees REAL DEFAULT 0,
    strategy TEXT,
    entry_time DATETIME NOT NULL,
    exit_time DATETIME,
    exit_reason TEXT,
    order_id TEXT,
    status TEXT DEFAULT 'open'
);
```

#### 复盘表 (trades_review)

```sql
CREATE TABLE trades_review (
    id TEXT PRIMARY KEY,
    -- 基础信息 (同 trades)
    code TEXT, action TEXT, entry_price REAL, ...
    
    -- 决策复盘字段
    decision_reason TEXT,  -- technical/fundamental/sentiment/news
    decision_confidence INTEGER,  -- 1-10
    strategy_type TEXT,
    decision_quality_score INTEGER,  -- 1-10
    
    -- 时机复盘字段
    timing_score INTEGER,  -- 1-10
    entry_timing TEXT,  -- early/optimal/late
    
    -- 执行复盘字段
    execution_score INTEGER,  -- 1-10
    slippage REAL,  -- 滑点
    ...
);
```

#### 模拟交易日志 (simulation_trades)

```python
# app/logging/simulation_logger.py
@dataclass
class FillLog:
    order_id: str
    fill_id: str
    code: str
    action: str
    filled_quantity: int
    filled_price: float
    commission: float
    timestamp: str
```

**数据孤岛问题**:
- `trades` 表：主交易表，Dashboard 查询此表 ❌ 无数据
- `trades_review` 表：复盘表，含 6 大维度分析字段
- `simulation_trades` 表：模拟交易日志 ✅ 有数据
- **三个表不互通**

---

### 1.4 事件存储机制

#### SimulationLogger

```python
class SimulationLogger:
    def log_order_filled(self, order_id, code, action, 
                         filled_quantity, filled_price, ...):
        # 1. 创建 FillLog
        fill_id = f"FILL-{timestamp}-{uuid}"
        log_entry = FillLog(...)
        
        # 2. 写入日志文件
        self._write_to_log(log_entry)
        
        # 3. 写入 simulation_trades 表
        self._save_to_db(log_entry)
```

**问题**:
- ✅ 记录成交事件
- ✅ 写入 simulation_trades
- ❌ **未同步到 trades 表**

#### OrderLogger

```python
class OrderLogger:
    def log_order_status_change(self, order_id, status, ...):
        # 记录订单状态变化
        # 保存到 order_logs 表
```

**状态字段**:
- `pending` - 待提交
- `submitted` - 已提交
- `filled` - 已成交
- `cancelled` - 已取消
- `failed` - 失败

---

## 二、数据同步方案设计

### 2.1 核心思路

**三层数据同步架构**:

```
┌─────────────────────────────────────────────────────────┐
│                    数据源层                               │
├─────────────────────────────────────────────────────────┤
│  富途 OpenD                                              │
│  • position_list_query (持仓)                           │
│  • get_history_order_list (历史订单)                     │
│  • order_list_query (单笔订单查询)                       │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                    同步层                                 │
├─────────────────────────────────────────────────────────┤
│  TradeSyncEngine                                        │
│  • 持仓同步：每 5 分钟                                     │
│  • 成交同步：每 5 分钟                                     │
│  • 订单状态同步：实时 (事件驱动)                          │
│  • 数据清洗：去重/校验/补全                              │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│                    存储层                                 │
├─────────────────────────────────────────────────────────┤
│  • trades 表 (主交易表) ← Dashboard 查询                  │
│  • trades_review 表 (复盘表)                            │
│  • simulation_trades 表 (模拟日志)                       │
│  • sync_logs 表 (同步日志)                              │
└─────────────────────────────────────────────────────────┘
```

---

### 2.2 数据获取方案

#### 方案 A: 使用 OpenD Executor (推荐)

**优势**:
- ✅ 已有成熟代码
- ✅ 支持模拟账户
- ✅ 可获取完整订单信息

**实现**:
```python
from app.v4.trader.opend_executor import get_opend_executor

def sync_trades_from_futu():
    # 1. 获取 OpenD 客户端
    executor = get_opend_executor(simulate=True)
    
    # 2. 查询今日已成交订单
    ret_code, ret_data = executor.client.get_history_order_list(
        filter_orders_by_status=[1, 2],  # FILLED
    )
    
    # 3. 过滤今日订单
    today = datetime.now().strftime('%Y-%m-%d')
    today_orders = [
        order for order in ret_data.itertuples()
        if order.create_time and order.create_time.startswith(today)
    ]
    
    # 4. 转换为 Trade 对象
    trades = []
    for order in today_orders:
        trade = {
            'id': f"trade_{order.order_id}",
            'code': order.code,
            'action': 'BUY' if order.trd_side == 1 else 'SELL',
            'quantity': order.qty,
            'price': order.price,
            'pnl': order.profit,
            'entry_time': order.create_time,
            'order_id': order.order_id,
            'status': 'CLOSED' if order.qty == order.dealt_qty else 'OPEN',
        }
        trades.append(trade)
    
    # 5. 写入数据库
    save_trades_to_db(trades)
```

#### 方案 B: 事件驱动同步

**思路**: 在 executor.execute() 成功后立即记录

**修改点**:
```python
# app/trader/executor.py
def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
    # ... 下单成功 ...
    
    # ✅ 新增：记录成交到数据库
    if futu_order_id:
        self.record_trade_to_db(futu_order_id, signal)
    
    return TradeResult(success=True, order_id=futu_order_id)
```

**优势**:
- ✅ 实时记录
- ✅ 不依赖轮询
- ✅ 数据准确

**劣势**:
- ❌ 需要修改核心代码
- ❌ 历史数据需补录

#### 方案 C: 混合方案 (最佳)

**结合 A + B**:
- **实时**: executor.execute() 成功后立即记录 (方案 B)
- **补漏**: 每 5 分钟从 OpenD 同步 (方案 A)
- **校验**: 每日收盘后数据一致性检查

---

### 2.3 数据表设计优化

#### 新增同步日志表

```sql
CREATE TABLE sync_logs (
    id TEXT PRIMARY KEY,
    sync_type TEXT NOT NULL,  -- position/trade/order
    sync_time DATETIME NOT NULL,
    source TEXT NOT NULL,  -- futu/manual/auto
    records_synced INTEGER DEFAULT 0,
    status TEXT NOT NULL,  -- success/failed/partial
    error_message TEXT,
    details TEXT  -- JSON
);

CREATE INDEX idx_sync_logs_time ON sync_logs(sync_time);
```

#### 统一数据视图

```sql
-- 创建统一视图，合并三个交易表
CREATE VIEW all_trades_view AS
SELECT 
    id, code, action, quantity, entry_price, exit_price,
    pnl, pnl_percent, entry_time, exit_time, status,
    'main' as source
FROM trades
UNION ALL
SELECT 
    id, code, action, quantity, entry_price, exit_price,
    pnl, pnl_percent, entry_time, exit_time, status,
    'review' as source
FROM trades_review
UNION ALL
SELECT 
    fill_id as id, code, action, filled_quantity as quantity,
    filled_price as entry_price, NULL as exit_price,
    0 as pnl, 0 as pnl_percent, timestamp as entry_time,
    NULL as exit_time, 'FILLED' as status,
    'simulation' as source
FROM simulation_trades;
```

---

### 2.4 数据关联方案

#### 订单状态追踪

```python
# 订单状态机
ORDER_STATUS_FLOW = {
    'pending': ['submitted', 'cancelled'],
    'submitted': ['filled', 'cancelled', 'failed'],
    'filled': [],  # 终态
    'cancelled': [],  # 终态
    'failed': [],  # 终态
}

# 状态变更日志
def log_order_status_change(order_id, old_status, new_status):
    """记录订单状态变更"""
    log = {
        'order_id': order_id,
        'old_status': old_status,
        'new_status': new_status,
        'timestamp': datetime.now().isoformat(),
    }
    save_to_order_status_logs(log)
```

#### 交易 - 信号关联

```python
# 关联关系
signal → order → trade → review

# 外键约束
CREATE TABLE trades (
    ...
    signal_id TEXT,
    order_id TEXT,
    review_id TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id),
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (review_id) REFERENCES trades_review(id)
);
```

---

## 三、实施计划

### 3.1 P0 紧急修复 (今日)

#### 任务 1: 启用 OpenD 同步

```python
# /opt/ftseek/FTSeek/scripts/sync_trades_from_futu.py
# 使用 get_opend_executor(simulate=True)

from app.v4.trader.opend_executor import get_opend_executor

def sync_today_trades():
    executor = get_opend_executor(simulate=True)
    
    ret_code, ret_data = executor.client.get_history_order_list(
        filter_orders_by_status=[1, 2],
    )
    
    # 过滤今日订单并导入数据库
    ...
```

**验收标准**:
- ✅ 成功获取今日成交
- ✅ 导入数据库 trades 表
- ✅ 数据与富途 App 一致

#### 任务 2: 修改 executor 记录成交

```python
# /opt/ftseek/FTSeek/app/trader/executor.py
def execute(self, signal, trade_type='AUTO'):
    # ... 下单成功 ...
    
    # ✅ 新增
    if futu_order_id and status == 'FILLED':
        self.record_trade_to_db(futu_order_id, signal)
```

**验收标准**:
- ✅ 下单成功后自动记录
- ✅ 数据完整 (价格/数量/时间)

#### 任务 3: 配置定时同步

```bash
# Crontab
*/5 9-16 * * 1-5 /opt/ftseek/FTSeek/venv/bin/python /opt/ftseek/FTSeek/scripts/sync_trades_from_futu.py
0 16 * * 1-5 /opt/ftseek/FTSeek/venv/bin/python /opt/ftseek/FTSeek/scripts/daily_validation.py
```

---

### 3.2 P1 高优修复 (本周)

#### 任务 4: 统一数据表

```sql
-- 合并 simulation_trades 到 trades
INSERT INTO trades (code, action, quantity, entry_price, entry_time, source)
SELECT code, action, filled_quantity, filled_price, timestamp, 'simulation'
FROM simulation_trades
WHERE date(timestamp) = date('now')
ON CONFLICT(id) DO NOTHING;
```

#### 任务 5: 建立数据校验机制

```python
# /opt/ftseek/FTSeek/scripts/daily_validation.py
def validate_data_consistency():
    """验证富途 vs 本地数据一致性"""
    
    # 1. 获取富途成交数
    futu_count = get_futu_trade_count()
    
    # 2. 获取数据库成交数
    db_count = get_db_trade_count()
    
    # 3. 比较
    if abs(futu_count - db_count) > 5:
        send_alert(f"🚨 数据不一致：富途{futu_count} vs 数据库{db_count}")
        return False
    
    return True
```

---

### 3.3 P2 中优修复 (下周)

#### 任务 6: 建立备份机制

#### 任务 7: 完善监控告警

#### 任务 8: 流程规范文档

---

## 四、推荐方案

### 4.1 最佳实践

**混合方案 (方案 C)**:

1. **实时记录**: executor.execute() 成功后立即记录
2. **定时同步**: 每 5 分钟从 OpenD 同步补漏
3. **每日校验**: 收盘后数据一致性检查
4. **手动导入**: 异常时手动补录

### 4.2 代码修改清单

| 文件 | 修改内容 | 优先级 |
|------|---------|--------|
| `executor.py` | 增加 record_trade_to_db 调用 | P0 |
| `sync_trades_from_futu.py` | 使用 OpenD Executor | P0 |
| `main.py` | 取消注释同步功能 | P1 |
| `daily_validation.py` | 新建校验脚本 | P1 |

### 4.3 数据补录方案

**历史数据补录**:
1. 从富途 App 导出历史成交 (Excel)
2. 使用导入脚本批量导入
3. 验证数据完整性

---

## 五、总结

### 5.1 核心问题

1. 🔴 **同步功能被禁用** - 3 天无自动同步
2. 🔴 **缺少成交同步代码** - 未调用 record_trade
3. 🟡 **数据孤岛** - 三个表不互通
4. 🟡 **无监控告警** - 数据丢失未发现

### 5.2 解决方案

1. ✅ **使用 OpenD Executor** - 获取历史订单
2. ✅ **executor 实时记录** - 下单成功后立即保存
3. ✅ **定时同步补漏** - 每 5 分钟同步
4. ✅ **每日校验** - 数据一致性检查

### 5.3 实施优先级

1. ⏳ **P0**: OpenD 同步脚本 (今日)
2. ⏳ **P0**: executor 修改 (今日)
3. ⏳ **P1**: 定时任务配置 (本周)
4. ⏳ **P1**: 数据校验机制 (本周)

---

**分析状态**: ✅ **完成**  
**推荐方案**: **混合方案 (实时 + 定时 + 校验)**  
**下一步**: ⏳ **执行 P0 修复**

---

*Harness 多模型协作分析完成！通过深入分析现有代码和富途 API 限制，设计了三层数据同步架构和混合方案。* 🚀
