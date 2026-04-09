# 🔴 紧急数据不一致报告

**发现时间**: 2026-03-30 14:55 CST  
**严重程度**: 🔴 **严重**

---

## 一、问题发现

### 钉钉推送 vs 数据库对比

| 数据源 | 订单 ID | 方向 | 数量 | 价格 | 时间 |
|--------|--------|------|------|------|------|
| **钉钉推送** | 7538053 | **买入** | 100 股 | 149.45 | 14:50:06 |
| **trades 表** | 7538053 | **SELL** | 100 股 | 149.45 | 14:50:06 |
| **positions 表** | - | **BUY** | 200 股 | 149.45 | 14:50:06 |

**核心问题**:
- ✅ positions 表正确 (BUY - 开仓)
- ❌ trades 表错误 (SELL - 记录为平仓)
- ❌ 钉钉推送显示"买入成交"

---

## 二、根因分析

### 2.1 trades 表记录错误

**数据库记录**:
```sql
SELECT * FROM trades WHERE order_id='7538053';
-- trade_7538053|HK.09992|SELL|100|149.45|CLOSED
```

**问题**: action 字段为 `SELL`，应该是 `BUY`

### 2.2 server.py 修复代码未触发

**无 DEBUG 日志**:
```bash
tail -500 ftseek.log | grep "DEBUG:"
# 输出：(空)
```

**说明**: server.py 第 229 行的代码**未被执行**

### 2.3 可能原因

**推测**:
1. trades 表的 SELL 记录是**之前错误的遗留数据**
2. 14:50 的开仓交易**未触发 server.py 修复代码**
3. 可能存在**其他代码路径**写入 trades 表

---

## 三、数据验证

### 3.1 positions 表 (正确)

```sql
SELECT * FROM positions WHERE code='HK.09992' AND status='OPEN';
-- 155|HK.09992|BUY|200|149.45|14:50:06|OPEN
```

✅ **开仓记录正确**

### 3.2 trades 表 (错误)

```sql
SELECT * FROM trades WHERE order_id='7538053';
-- trade_7538053|HK.09992|SELL|100|149.45|CLOSED
```

❌ **方向错误 (应为 BUY)**

### 3.3 今日 trades 表统计

```sql
SELECT action, COUNT(*) FROM trades WHERE date(entry_time) = date('now') GROUP BY action;
-- SELL|16
-- BUY|0
```

❌ **无 BUY 记录** (应该有开仓记录)

---

## 四、修复方案

### 4.1 立即修复

**修正 trades 表错误记录**:
```sql
UPDATE trades SET action='BUY', status='OPEN', exit_price=NULL, exit_time=NULL, exit_reason=''
WHERE order_id='7538053';
```

### 4.2 根因调查

**需要确认**:
1. server.py 修复代码为何未触发
2. trades 表的 SELL 记录从何而来
3. 是否有其他代码路径写入 trades 表

### 4.3 调试增强

**添加更多日志**:
```python
# server.py /api/confirm 路由开始处
logger.info(f"🔍 确认信号：signal_id={signal_id}, action={signal.action.value}")
```

---

## 五、影响评估

| 影响项 | 严重程度 | 说明 |
|--------|---------|------|
| **数据不一致** | 🔴 高 | positions vs trades 不一致 |
| **复盘分析** | 🔴 高 | 错误的交易方向导致分析错误 |
| **盈亏计算** | 🔴 高 | SELL 记录导致盈亏计算错误 |
| **钉钉推送** | ✅ 正常 | 推送内容正确 |

---

## 六、下一步

### 立即执行

1. ✅ **修正 trades 表记录** - UPDATE action='BUY'
2. ⏳ **调查根因** - 确认为何修复代码未触发
3. ⏳ **增强调试** - 添加更多日志

### 待验证

- [ ] 下次开仓交易验证
- [ ] DEBUG 日志输出确认
- [ ] trades 表记录正确性

---

**状态**: 🔴 **严重 - 数据不一致**  
**下一步**: ⏳ **立即修正 trades 表记录 + 调查根因**
