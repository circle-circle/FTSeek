# 🔴 紧急数据同步分析报告

**分析时间**: 2026-03-30 14:22 CST  
**严重程度**: 🔴 **严重**

---

## 一、核心发现

### 数据对比

| 数据表 | 记录数 | 状态 |
|--------|--------|------|
| **positions** | 22 条 | ✅ 正常 |
| **trades** | 7 条 | ❌ **缺失 15 条 (68%)** |

### 最新 trades 记录

```
trade_7537943 | HK.03690 | SELL | 100  | 84.2  | 14:20:51
trade_7537937 | HK.09992 | SELL | 400  | 149.4 | 14:20:02
trade_7537936 | HK.09992 | SELL | 100  | 149.35| 14:20:02
trade_7537917 | HK.09992 | SELL | 100  | 149.95| 14:09:12
trade_7537899 | HK.00981 | SELL | 500  | 51.25 | 14:04:11
trade_7537898 | HK.00981 | SELL | 100  | 51.25 | 14:04:07
trade_7537825 | HK.03690 | SELL | 100  | 84.7  | 13:33:06
```

**关键发现**:
- ✅ **trades 表有 7 条记录** (全部是 SELL/平仓)
- ❌ **无 BUY/开仓记录**
- ❌ **positions 表有 22 条记录**
- ❌ **缺失率 68%**

---

## 二、根因分析

### 问题 1: record_trade_to_db 方法仍未生效

**日志错误**:
```
❌ 记录平仓成交失败：'TradeExecutor' object has no attribute 'record_trade_to_db'
❌ 记录成交失败：'TradeExecutor' object has no attribute 'record_trade_to_db'
```

**状态**:
- ✅ 方法已添加到 executor.py (第 1051 行)
- ❌ **但服务仍在报方法缺失错误**

**原因推测**:
1. 服务重启后未正确加载新代码
2. Python 缓存了旧模块
3. 有多个 Python 进程在运行

### 问题 2: 只有平仓记录，无开仓记录

**trades 表记录**:
- 7 条全部是 `SELL` (平仓)
- 0 条 `BUY` (开仓)

**positions 表记录**:
- 应该有开仓和平仓记录

**说明**:
- 开仓时 `record_trade_to_db()` 未被调用或失败
- 平仓时可能有其他代码路径写入 trades

### 问题 3: order_id 关联异常

**检查**:
```sql
SELECT id, order_id FROM trades WHERE date(entry_time) = date('now');
```

**预期**: `id = 'trade_' + order_id`

**待验证**: 需要检查实际数据

---

## 三、数据一致性对比

### positions vs trades

| 股票代码 | positions | trades | 缺失 |
|---------|-----------|--------|------|
| **HK.00981** | ? 条 | 2 条 | ❌ ? |
| **HK.03690** | ? 条 | 2 条 | ❌ ? |
| **HK.09992** | ? 条 | 3 条 | ❌ ? |

### 成交量对比

**positions 表**:
```sql
SELECT code, action, SUM(quantity) FROM positions 
WHERE date(entry_time) = date('now') 
GROUP BY code, action;
```

**trades 表**:
```sql
SELECT code, action, SUM(quantity) FROM trades 
WHERE date(entry_time) = date('now') 
GROUP BY code, action;
```

---

## 四、紧急修复方案

### P0: 立即执行

#### 1. 强制重启服务

```bash
# 停止所有 FTSeek 进程
pkill -9 -f "python.*app.main"
sleep 2

# 清除 Python 缓存
find /opt/ftseek/FTSeek -name "*.pyc" -delete
find /opt/ftseek/FTSeek -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

# 重启服务
systemctl start ftseek
sleep 5

# 验证服务状态
systemctl status ftseek
```

#### 2. 验证方法存在性

```bash
python3 << 'EOF'
import sys
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 强制重新加载
if 'app.trader.executor' in sys.modules:
    del sys.modules['app.trader.executor']

from app.trader.executor import TradeExecutor
te = TradeExecutor()
print('✅ record_trade_to_db 方法存在:', hasattr(te, 'record_trade_to_db'))
EOF
```

#### 3. 数据补录

```bash
sqlite3 /opt/ftseek/FTSeek/data/ftseek.db << 'EOF'
-- 从 positions 表回补缺失的 trades 记录
INSERT OR IGNORE INTO trades (
    id, signal_id, code, name, action,
    entry_price, quantity, entry_time,
    order_id, status
)
SELECT 
    'trade_' || id,
    signal_id,
    code,
    '',
    action,
    entry_price,
    quantity,
    entry_time,
    id,
    'OPEN'
FROM positions
WHERE date(entry_time) = date('now')
AND action = 'BUY'
AND id NOT IN (SELECT substr(id, 7) FROM trades);
EOF
```

---

## 五、验证计划

| 时间 | 任务 | 验收标准 |
|------|------|---------|
| **14:25** | 强制重启 | 服务正常运行 |
| **14:27** | 方法验证 | hasattr 返回 True |
| **14:30** | 数据补录 | positions vs trades 一致 |
| **14:35** | 新交易验证 | 日志有"成交记录成功" |

---

## 六、监控命令

```bash
# 实时监控成交记录日志
tail -f /opt/ftseek/FTSeek/logs/ftseek.log | grep "成交记录成功"

# 预期输出:
# ✅ [数据库] 成交记录成功：HK.09992 BUY 400 股 @ 146.80
```

---

**状态**: 🔴 **严重 - 缺失率 68%**  
**下一步**: ⏳ **立即执行强制重启**
