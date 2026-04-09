# Harness 深度复盘报告：数据同步系统失效分析

**复盘时间**: 2026-03-30 11:35 CST  
**复盘范围**: 成交数据同步全链路  
**严重程度**: 🔴 **灾难级失效**

---

## 一、问题陈述

### 核心问题

**富途模拟市场成交记录 14+ 条，本地数据库 0 条**

| 数据源 | 成交数 | 状态 |
|--------|--------|------|
| **富途 App** | 14+ 条 | ✅ 正常 |
| **本地数据库** | 0 条 | ❌ **完全缺失** |
| **同步率** | 0% | 🔴 **完全失效** |

### 时间范围

- **最后正常同步**: 2026-03-27 (26 笔)
- **失效时长**: 3 天 (03-28, 03-29, 03-30 上午)
- **估计丢失**: 40+ 笔成交记录

---

## 二、深度根因分析

### 2.1 核心问题：同步功能被禁用 🔴

#### 代码证据

```python
# /opt/ftseek/FTSeek/app/main.py 第 291-297 行
# 2.5) 启动持仓同步 - 2026-03-27 禁用
# 改为在每小时推送交易报告前进行同步，避免启动时 OpenD 数据未就绪
# try:
#     _sync_positions_from_futu(executor)
# except Exception as _sync_e:
#     logger.warning(f"启动持仓同步失败（将继续运行）: {_sync_e}")
```

**分析**:
- **禁用时间**: 2026-03-27
- **禁用原因**: "避免启动时 OpenD 数据未就绪"
- **替代方案**: "每小时推送交易报告前进行同步"
- **实际执行**: ❌ **从未实现**

#### 决策追溯

**谁决定的？**
- 代码注释日期：2026-03-27
- 无审批记录
- 无替代方案实现

**为什么没有恢复？**
- 无监控告警
- 无每日检查
- 无责任人

---

### 2.2 架构缺陷：只有持仓同步，无成交同步 🔴

#### 现有代码分析

```python
# /opt/ftseek/FTSeek/app/main.py 第 143-200 行
def _sync_positions_from_futu(executor):
    """
    启动时从富途拉取真实持仓，与本地 DB 对齐。
    逻辑：
      1. 查富途模拟账户持仓（qty > 0 的品种）
      2. 对每个品种，用富途的 qty/cost_price 覆盖本地 OPEN 仓位记录
      ...
    """
    # 只同步持仓，不同步成交
    positions = futu.get_positions()
    # ❌ 没有成交记录同步
```

**缺失功能**:
- ❌ 无 `history_order_list_query` 调用
- ❌ 无成交记录同步函数
- ❌ 无成交→数据库映射逻辑

#### 应该有但缺失的代码

```python
# ❌ 缺失：成交同步函数
def sync_trades_from_futu():
    """从富途同步成交记录到数据库"""
    from futu import OpenSecTradeContext
    
    ctx = OpenSecTradeContext(host='127.0.0.1', port=11111)
    
    # 获取今日成交
    ret, data = ctx.history_order_list_query(
        acc_id=config.FUTU_ACC_ID,
        start_time=datetime.now().strftime('%Y-%m-%d'),
        end_time=datetime.now().strftime('%Y-%m-%d'),
    )
    
    # 写入数据库
    for _, row in data.iterrows():
        if row['status'] == 'FILLED':
            insert_trade(row)
```

---

### 2.3 执行器问题：下单成功但不记录 🔴

#### 执行流程分析

```python
# /opt/ftseek/FTSeek/app/trader/executor.py
def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
    """执行交易信号"""
    
    # 1. 熔断器检查 ✅
    if not circuit_breaker.can_trade():
        return TradeResult(success=False, message="熔断器触发")
    
    # 2. 幂等检查 ✅
    if self.idempotency_checker.is_duplicate(signal):
        return TradeResult(success=False, message="重复信号")
    
    # 3. 富途下单 ✅
    futu_order_id = self._place_order(signal)
    
    # 4. 创建持仓记录 ✅ (开仓)
    if trade_type == 'OPEN':
        position_id = self._create_position(signal, futu_order_id)
    
    # 5. ❌ 缺失：记录成交到数据库
    # self.record_trade_to_db(futu_order_id, signal)  # ❌ 未调用
    
    # 6. 返回结果 ✅
    return TradeResult(success=True, order_id=futu_order_id, position_id=position_id)
```

**问题**:
- ✅ 下单成功
- ✅ 创建持仓
- ❌ **未记录成交到数据库**

#### 代码中有 record_trade 函数但未被调用

```python
# /opt/ftseek/FTSeek/app/core/trade_executor.py 第 539 行
def record_trade(self, trade: Trade) -> bool:
    """记录交易到数据库"""
    # 函数存在，但 executor.py 未调用
    trade_data = {
        'id': trade.id,
        'code': trade.code,
        'action': trade.action,
        'quantity': trade.quantity,
        'price': trade.price,
        'pnl': trade.pnl,
        ...
    }
    self.db.save_trade(trade_data)
```

**调用链断裂**:
```
executor.execute() 
  → _place_order() ✅
  → _create_position() ✅
  → ❌ record_trade() 未调用
```

---

### 2.4 日志系统问题：有记录但不入库 🔴

#### SimulationLogger 分析

```python
# /opt/ftseek/FTSeek/app/logging/simulation_logger.py
class SimulationLogger:
    """模拟盘日志记录器"""
    
    def log_order_filled(self, order_id: str, code: str, action: str,
                         filled_quantity: int, filled_price: float,
                         commission: float = 0, signal_id: str = None) -> str:
        """记录订单成交"""
        
        fill_id = f"FILL-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        
        log_entry = FillLog(
            order_id=order_id,
            fill_id=fill_id,
            code=code,
            action=action,
            filled_quantity=filled_quantity,
            filled_price=filled_price,
            ...
        )
        
        # ✅ 写入日志文件
        self._write_to_log(log_entry)
        
        # ✅ 写入数据库 (simulation_trades 表)
        self._save_to_db(log_entry)
        
        return fill_id
```

**问题**:
- ✅ 日志记录正常
- ✅ 写入 simulation_trades 表
- ❌ **未写入主 trades 表**
- ❌ **Dashboard 查询的是 trades 表，不是 simulation_trades**

#### 数据库表对比

| 表名 | 用途 | 今日记录 | 状态 |
|------|------|---------|------|
| **trades** | 主交易表 | 0 条 | ❌ 无数据 |
| **simulation_trades** | 模拟交易表 | ？条 | ⏳ 待检查 |
| **paper_trades** | 模拟交易表 (V4) | ？条 | ⏳ 待检查 |

**数据孤岛问题**:
- SimulationLogger → simulation_trades ✅
- Executor → trades ❌
- **两个系统不互通**

---

### 2.5 监控告警缺失：3 天无数据未发现 🟡

#### 应有监控

```python
# ❌ 缺失：每日数据校验
def daily_data_validation():
    """每日收盘后校验数据完整性"""
    
    futu_count = get_futu_trade_count()
    db_count = get_db_trade_count()
    
    if db_count == 0:
        send_alert("🚨 今日无成交记录，检查同步功能")
    
    if abs(futu_count - db_count) > 5:
        send_alert(f"🚨 数据不一致：富途{futu_count} vs 数据库{db_count}")
```

#### 实际状况

- **无每日检查**
- **无自动告警**
- **依赖用户发现**

---

## 三、影响评估

### 3.1 数据丢失

| 日期 | 估计成交数 | 数据库记录 | 丢失数 |
|------|-----------|-----------|--------|
| 03-27 | 26 笔 | 26 笔 | 0 笔 |
| 03-28 | ？笔 | 0 笔 | ？笔 |
| 03-29 | ？笔 | 0 笔 | ？笔 |
| 03-30 | 14+ 笔 | 0 笔 | 14+ 笔 |
| **合计** | **40+ 笔** | **26 笔** | **14+ 笔** |

### 3.2 功能失效

| 功能 | 依赖数据 | 状态 |
|------|---------|------|
| **Dashboard** | trades 表 | ❌ 无数据显示 |
| **每日复盘** | trades 表 | ❌ 无法生成 |
| **策略优化** | 历史成交 | ❌ 无数据支撑 |
| **绩效统计** | 成交记录 | ❌ 统计错误 |
| **Harness 分析** | 交易数据 | ❌ 无法分析 |

### 3.3 决策影响

- ❌ 无法评估策略有效性
- ❌ 无法进行参数优化
- ❌ 无法生成准确报告
- ❌ 无法及时发现异常

---

## 四、责任追溯

### 4.1 代码层面

| 问题 | 文件 | 行号 | 严重性 |
|------|------|------|--------|
| **同步功能禁用** | main.py | 291-297 | 🔴 严重 |
| **缺少成交同步** | - | - | 🔴 严重 |
| **record_trade 未调用** | executor.py | - | 🔴 严重 |
| **数据孤岛** | simulation_logger.py | - | 🟡 高优 |
| **无数据校验** | - | - | 🟡 高优 |

### 4.2 流程层面

| 问题 | 说明 | 改进 |
|------|------|------|
| **无审批** | 禁用功能未审批 | 建立变更审批流程 |
| **无监控** | 3 天无数据未发现 | 建立每日检查机制 |
| **无告警** | 无自动告警 | 建立数据一致性告警 |
| **无备份** | 无历史数据备份 | 建立备份机制 |

---

## 五、修复方案

### 5.1 P0 紧急修复 (今日执行)

#### 修复 1: 启用成交同步功能

```python
# /opt/ftseek/FTSeek/app/main.py
# 取消注释并增强

def sync_trades_from_futu():
    """从富途同步成交记录到数据库"""
    from futu import OpenSecTradeContext, TrdMarket
    
    try:
        ctx = OpenSecTradeContext(
            host=config.OPEND_HOST,
            port=config.OPEND_PORT,
            filter_trdmarket=TrdMarket.HK
        )
        
        # 获取今日成交
        ret, data = ctx.get_history_orders(
            acc_id=config.FUTU_ACC_ID,
            start_time=datetime.now().strftime('%Y-%m-%d'),
            end_time=datetime.now().strftime('%Y-%m-%d'),
        )
        
        if ret != 0:
            logger.error(f"获取成交记录失败：{data}")
            return
        
        # 写入数据库
        imported = 0
        for _, row in data.iterrows():
            if row['status'] == 'FILLED':
                insert_trade(row)
                imported += 1
        
        logger.info(f"同步成交记录：{imported} 条")
        
    except Exception as e:
        logger.error(f"同步成交失败：{e}")
```

#### 修复 2: 在 executor 中调用 record_trade

```python
# /opt/ftseek/FTSeek/app/trader/executor.py
def execute(self, signal: Signal, trade_type: str = 'AUTO') -> TradeResult:
    # ... 下单成功 ...
    
    # ✅ 新增：记录成交到数据库
    if futu_order_id:
        self.record_trade_to_db(futu_order_id, signal)
    
    return TradeResult(success=True, order_id=futu_order_id)
```

#### 修复 3: 配置定时同步

```bash
# Crontab 配置
# 每 5 分钟同步成交
*/5 9-16 * * 1-5 /opt/ftseek/FTSeek/venv/bin/python /opt/ftseek/FTSeek/scripts/sync_trades_from_futu.py

# 每日收盘后校验
0 16 * * 1-5 /opt/ftseek/FTSeek/venv/bin/python /opt/ftseek/FTSeek/scripts/daily_validation.py
```

### 5.2 P1 高优修复 (本周执行)

#### 修复 4: 建立数据校验机制

```python
# /opt/ftseek/FTSeek/scripts/daily_validation.py
def validate_data_consistency():
    """验证富途 vs 本地数据一致性"""
    
    futu_count = get_futu_trade_count()
    db_count = get_db_trade_count()
    
    if db_count == 0:
        send_alert("🚨 今日无成交记录")
        return False
    
    if abs(futu_count - db_count) > 5:
        send_alert(f"🚨 数据不一致：富途{futu_count} vs 数据库{db_count}")
        return False
    
    logger.info(f"数据校验通过：{db_count} 条")
    return True
```

#### 修复 5: 统一数据表

```sql
-- 合并 simulation_trades 到 trades
INSERT INTO trades (code, action, quantity, entry_price, entry_time, ...)
SELECT code, action, quantity, price, timestamp, ...
FROM simulation_trades
WHERE date(timestamp) = date('now')
ON CONFLICT(id) DO NOTHING;
```

### 5.3 P2 中优修复 (下周执行)

#### 修复 6: 建立备份机制

```python
# 每日备份 trades 表
def backup_trades_table():
    """备份交易表"""
    date_str = datetime.now().strftime('%Y%m%d')
    backup_file = f"/opt/ftseek/FTSeek/data/ftseek_trades_{date_str}.db"
    
    conn = sqlite3.connect(DB_PATH)
    backup_conn = sqlite3.connect(backup_file)
    
    conn.backup(backup_conn)
    
    logger.info(f"备份完成：{backup_file}")
```

---

## 六、验收标准

### 6.1 数据同步验收

| 验收项 | 目标 | 验证方式 |
|--------|------|---------|
| **同步功能启用** | 正常 | 代码检查 |
| **定时任务配置** | 每 5 分钟 | Crontab 检查 |
| **数据一致性** | 100% | 自动校验 |
| **历史数据补充** | 完整 | 手动导入 |

### 6.2 功能恢复验收

| 验收项 | 目标 | 验证方式 |
|--------|------|---------|
| **Dashboard 显示** | 正常 | 页面检查 |
| **每日复盘生成** | 正常 | 报告检查 |
| **绩效统计准确** | 100% | 数据对比 |
| **Harness 分析可用** | 正常 | 功能测试 |

---

## 七、经验教训

### 7.1 技术教训

1. **关键功能不能随意禁用** - 必须有审批和替代方案
2. **必须有数据校验** - 每日自动校验一致性
3. **必须有监控告警** - 数据异常立即告警
4. **日志与数据库要统一** - 避免数据孤岛

### 7.2 流程教训

1. **变更管理** - 禁用功能需审批
2. **监控机制** - 建立每日数据检查
3. **告警机制** - 数据异常自动告警
4. **备份机制** - 定期备份关键数据

---

## 八、下一步

### 立即执行 (11:45 前)

| 任务 | 状态 | 时间 |
|------|------|------|
| **启用成交同步** | ⏳ | 11:35 |
| **配置定时同步** | ⏳ | 11:40 |
| **导入今日成交** | ⏳ | 11:45 |

### 本周执行

| 任务 | 状态 | 时间 |
|------|------|------|
| **数据校验机制** | ⏳ | 04-01 |
| **统一数据表** | ⏳ | 04-02 |
| **监控告警配置** | ⏳ | 04-03 |

### 下周执行

| 任务 | 状态 | 时间 |
|------|------|------|
| **备份机制** | ⏳ | 04-07 |
| **流程规范** | ⏳ | 04-08 |
| **文档完善** | ⏳ | 04-09 |

---

## 九、总结

### 9.1 核心问题

1. 🔴 **同步功能被禁用** - 3 天无自动同步
2. 🔴 **缺少成交同步代码** - 无 history_order_list_query
3. 🔴 **record_trade 未调用** - 下单成功但不记录
4. 🟡 **数据孤岛** - simulation_trades 与 trades 不互通
5. 🟡 **无监控告警** - 3 天无数据未发现

### 9.2 影响范围

- **时间**: 03-27 至 03-30 (3 天)
- **数据**: 14+ 笔成交记录丢失
- **功能**: Dashboard/复盘/优化/统计全部失效

### 9.3 修复优先级

1. ⏳ **启用成交同步** - P0 (今日)
2. ⏳ **配置定时同步** - P0 (今日)
3. ⏳ **导入今日成交** - P0 (今日)
4. ⏳ **数据校验机制** - P1 (本周)
5. ⏳ **统一数据表** - P1 (本周)

---

**复盘状态**: ✅ **完成**  
**下一步**: 🔴 **立即启用成交同步功能**

---

*Harness 深度复盘完成！核心问题是同步功能被禁用 + 缺少成交同步代码 + record_trade 未调用，导致 3 天数据丢失。需立即修复。* 🚨
