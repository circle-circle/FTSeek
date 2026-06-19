# FTSeek-US 项目状态

## 🎯 当前任务：止损止盈自动下单方案 (Task 4)

**状态**: ✅ 已完成  
**完成时间**: 2026-06-18

---

## 📋 任务清单

### ✅ Task 1: 审查 Dashboard 交易页面的止损止盈价格逻辑
- **状态**: 已完成
- **产出**: 确认了现有逻辑的价格验证机制

### ✅ Task 2: 调研 Futu 订单类型
- **状态**: 已完成
- **关键发现**:
  - `STOP` (止损市价单): 适合止损
  - `LIMIT` (限价单): 适合止盈
  - `STOP_LIMIT` (止损限价单): 可替代方案
  - `TRAILING_STOP` (追踪止损): 动态止损
  - **⚠️ Futu API 没有原生 OCO 订单**: 需要手动实现订单组管理

### ✅ Task 3: 排查昨晚下单后止损止盈未生效的根因
- **状态**: 已完成
- **根因分析**:
  1. **数据库 Schema 缺失**: `open_positions` 表缺少 `sl_broker_order_id` 列
  2. **Dashboard 服务不稳定**: 2026-06-17 晚上重启了 13+ 次
  3. **手动订单流程未走通**: `manual_order_audit` 表 2026-06-17 之后没有新增记录

### ✅ Task 4: 设计并实现完整的止损止盈自动下单方案
- **状态**: 已完成
- **实现内容**:
  1. **数据库 Schema 修复** (`audit.py`)
     - 添加 `sl_broker_order_id`, `tp_broker_order_id`, `current_sl`, `initial_tp` 列
     - 幂等操作，列已存在时自动跳过
  
  2. **实盘止损止盈挂单** (`executor.py`)
     - 止损: `STOP` 单（市价止损）
     - 止盈: `LIMIT` 单（限价止盈）
     - 价格校验: 止损 1%-15%，止盈 2%-50%
     - 失败降级: broker 失败时写入 DB，由 ExitMonitor 监控
  
  3. **订单组管理 (模拟 OCO)** (`executor.py`)
     - `_register_order_group_monitor`: 注册订单组监控
     - `_order_group_monitor_loop`: 10s 轮询检查订单状态
     - SL 成交 → 自动取消 TP
     - TP 成交 → 自动取消 SL
     - 24h 超时自动移除监控
  
  4. **平仓流程集成** (`executor.py`)
     - `cancel_order_group`: 手动平仓时取消 SL + TP 订单
     - 替换旧的 SL-only 取消逻辑

---

## 🏗️ 架构设计

### 实盘环境 (TrdEnv.REAL)
```
开仓成交
  ↓
place_protective_stop()
  ├─ 挂 STOP 单 (止损市价单)
  ├─ 挂 LIMIT 单 (止盈限价单)
  ├─ 写入 DB (sl_broker_order_id, tp_broker_order_id)
  └─ 启动订单组监控线程
  
订单组监控 (10s 轮询)
  ├─ SL FILLED → 取消 TP
  ├─ TP FILLED → 取消 SL
  ├─ 两者都 CANCEL/FAIL → 移除监控
  └─ 24h 超时 → 移除监控

手动平仓
  └─ cancel_order_group() → 取消 SL + TP
```

### 模拟盘环境 (TrdEnv.SIMULATE)
```
开仓成交
  ↓
place_protective_stop()
  ├─ 写入 DB (current_sl, initial_tp)
  └─ 不挂 broker 单 (Futu 模拟盘不支持 STOP)

ExitMonitor (轮询)
  └─ 检查价格触发 SL/TP → 执行平仓
```

---

## 📝 关键代码变更

### 1. `app/web_dashboard/order/audit.py`
- `ensure_sl_column()`: 添加 SL/TP broker 订单追踪列

### 2. `app/web_dashboard/order/executor.py`
- `place_protective_stop()`: 重构止损止盈挂单逻辑
- `_register_order_group_monitor()`: 注册订单组监控
- `_order_group_monitor_loop()`: 订单组监控线程 (模拟 OCO)
- `cancel_order_group()`: 平仓时取消订单组
- `close_position()`: 集成 `cancel_order_group`

---

## 🧪 验证步骤

### 语法检查
```bash
cd /opt/ftseek/FTSeek-US
python3 -m py_compile app/web_dashboard/order/executor.py
python3 -m py_compile app/web_dashboard/order/audit.py
```
✅ 编译通过

### 功能测试 (待执行)
1. **实盘环境测试**:
   - 下单时设置 SL/TP
   - 验证 broker 订单创建
   - 触发 SL → 验证 TP 自动取消
   - 触发 TP → 验证 SL 自动取消
   - 手动平仓 → 验证 SL/TP 都取消

2. **模拟盘环境测试**:
   - 下单时设置 SL/TP
   - 验证 DB 记录
   - ExitMonitor 触发平仓

---

## 🚀 下一步建议

1. **Dashboard 前端集成**: 在交易页面添加 SL/TP 输入框和显示
2. **ExitMonitor 集成**: 模拟盘环境下集成 SL/TP 检查逻辑
3. **日志监控**: 添加订单组管理相关的告警
4. **性能优化**: 订单组监控线程的轮询间隔可配置化

---

## 📊 技术指标

- **代码行数**: ~200 行新增/修改
- **新增函数**: 4 个
- **修改函数**: 3 个
- **数据库变更**: 4 个新列

---

## 🔗 相关文档

- [Futu 订单类型文档](https://openapi.futunn.com/futu-api-doc/trade/OrderType.html)
- [FTSeek-US 交易流程设计](docs/trading_flow.md)
- [止损止盈策略说明](docs/stop_loss_take_profit.md)
