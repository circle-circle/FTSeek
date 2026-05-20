# 交易记录查询指南

**更新时间**: 2026-03-30  
**数据库**: `/opt/ftseek/FTSeek-US/data/ftseek_us.db`

---

## 一、数据库表结构

### 1.1 核心表

| 表名 | 说明 | 主要字段 |
|------|------|---------|
| **trade_records** | 交易记录表 | trade_date, symbol, direction, quantity, entry_price, exit_price, realized_pnl, status |
| **open_positions** | 当前持仓表 | symbol, direction, size, avg_entry_price, initial_sl, current_sl, unrealized_pnl |
| **signals** | 信号记录表 | signal_date, symbol, direction, signal_strength, confidence, status |
| **events** | 事件记录表 | event_type, symbol, description, timestamp |

### 1.2 trade_records 表结构

```sql
CREATE TABLE trade_records (
    id INTEGER PRIMARY KEY,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time TEXT,
    exit_time TEXT,
    entry_price REAL,
    exit_price REAL,
    quantity INTEGER,
    realized_pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    holding_hours REAL,
    signal_strength INTEGER,
    signal_confidence REAL,
    futu_order_id TEXT,
    created_at TEXT,
    voting_result TEXT,
    final_strength INTEGER,
    consensus_level REAL,
    model_scores TEXT,
    strategy_id TEXT,
    decision_reason TEXT,
    max_drawdown REAL
);
```

---

## 二、统一查询脚本

### 2.1 脚本位置

```bash
/opt/ftseek/FTSeek-US/scripts/query_trades.py
```

### 2.2 使用方法

```bash
# 查询今日交易
python3 query_trades.py --today

# 查询本周交易
python3 query_trades.py --week

# 查询本月交易
python3 query_trades.py --month

# 查询指定股票
python3 query_trades.py --symbol AAPL

# 查询指定状态
python3 query_trades.py --status CLOSED

# JSON 格式输出
python3 query_trades.py --today --json

# 组合查询
python3 query_trades.py --week --symbol TSLA --status CLOSED
```

### 2.3 输出示例

```
====================================================================================================
交易记录 (共 7 条)
====================================================================================================

日期         代码       方向   数量     入场       出场       盈亏         盈亏率      状态      
----------------------------------------------------------------------------------------------------
2026-03-30  US.TSLA   SHORT  100      $384.48    $380.00    $448.00      1.16%      CLOSED    
2026-03-30  US.AAPL   SHORT  100      $253.11    $250.00    $311.00      1.23%      CLOSED    

====================================================================================================

统计:
  总交易数：7
  盈利交易：5
  亏损交易：2
  胜率：71.4%
  总盈亏：$1250.00
```

---

## 三、SQL 查询示例

### 3.1 基础查询

```sql
-- 查询所有交易记录
SELECT * FROM trade_records ORDER BY created_at DESC LIMIT 10;

-- 查询今日交易
SELECT * FROM trade_records WHERE date(created_at) = date('now');

-- 查询本周交易
SELECT * FROM trade_records WHERE date(created_at) >= date('now', '-7 days');

-- 查询指定股票
SELECT * FROM trade_records WHERE symbol = 'US.AAPL';

-- 查询指定状态
SELECT * FROM trade_records WHERE status = 'CLOSED';
```

### 3.2 统计查询

```sql
-- 总交易数
SELECT COUNT(*) as total_trades FROM trade_records;

-- 胜率统计
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
    ROUND(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) as win_rate
FROM trade_records;

-- 总盈亏
SELECT SUM(realized_pnl) as total_pnl FROM trade_records;

-- 按股票统计
SELECT 
    symbol,
    COUNT(*) as trades,
    SUM(realized_pnl) as total_pnl,
    ROUND(AVG(pnl_pct), 2) as avg_pnl_pct
FROM trade_records
GROUP BY symbol;

-- 按方向统计
SELECT 
    direction,
    COUNT(*) as trades,
    SUM(realized_pnl) as total_pnl,
    ROUND(AVG(pnl_pct), 2) as avg_pnl_pct
FROM trade_records
GROUP BY direction;
```

### 3.3 高级查询

```sql
-- 查询最佳交易
SELECT * FROM trade_records ORDER BY realized_pnl DESC LIMIT 5;

-- 查询最差交易
SELECT * FROM trade_records ORDER BY realized_pnl ASC LIMIT 5;

-- 查询持仓时间最长的交易
SELECT * FROM trade_records ORDER BY holding_hours DESC LIMIT 5;

-- 查询高置信度交易
SELECT * FROM trade_records WHERE signal_confidence > 0.8;

-- 查询高信号强度交易
SELECT * FROM trade_records WHERE signal_strength > 50;
```

---

## 四、持仓查询

### 4.1 当前持仓

```sql
-- 查询所有持仓
SELECT * FROM open_positions WHERE direction IS NOT NULL;

-- 查询多头持仓
SELECT * FROM open_positions WHERE direction = 'LONG';

-- 查询空头持仓
SELECT * FROM open_positions WHERE direction = 'SHORT';

-- 查询未实现盈亏
SELECT 
    symbol,
    direction,
    size,
    avg_entry_price,
    current_price,
    unrealized_pnl,
    unrealized_pnl / (avg_entry_price * size) * 100 as pnl_pct
FROM open_positions
WHERE direction IS NOT NULL;
```

### 4.2 持仓统计

```sql
-- 总持仓数
SELECT COUNT(*) as total_positions FROM open_positions WHERE direction IS NOT NULL;

-- 多头/空头持仓数
SELECT 
    direction,
    COUNT(*) as count,
    SUM(size * avg_entry_price) as total_value
FROM open_positions
WHERE direction IS NOT NULL
GROUP BY direction;

-- 总未实现盈亏
SELECT SUM(unrealized_pnl) as total_unrealized_pnl FROM open_positions WHERE direction IS NOT NULL;
```

---

## 五、信号查询

### 5.1 信号记录

```sql
-- 查询所有信号
SELECT * FROM signals ORDER BY created_at DESC LIMIT 10;

-- 查询今日信号
SELECT * FROM signals WHERE date(created_at) = date('now');

-- 查询高置信度信号
SELECT * FROM signals WHERE confidence > 0.8;

-- 查询信号状态
SELECT 
    status,
    COUNT(*) as count
FROM signals
GROUP BY status;
```

---

## 六、Dashboard 查询

### 6.1 Web Dashboard

访问：`http://localhost:8899/health`

显示内容:
- 系统指标 (CPU/Memory/Disk)
- 交易指标 (今日交易数/胜率/盈亏)
- API 调用统计
- 持仓状态

### 6.2 简易 Dashboard

```bash
# 使用查询脚本
python3 query_trades.py --today
python3 query_trades.py --week
```

---

## 七、数据导出

### 7.1 导出为 CSV

```bash
sqlite3 -header -csv /opt/ftseek/FTSeek-US/data/ftseek_us.db \
  "SELECT * FROM trade_records WHERE date(created_at) >= date('now', '-7 days');" \
  > trades_last_7days.csv
```

### 7.2 导出为 JSON

```bash
python3 query_trades.py --week --json > trades_last_week.json
```

---

## 八、常见问题

### Q1: 如何查询某只股票的所有交易？

```bash
python3 query_trades.py --symbol AAPL --all
```

### Q2: 如何查询胜率？

```sql
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) as win_rate
FROM trade_records;
```

### Q3: 如何查询当前持仓？

```bash
sqlite3 /opt/ftseek/FTSeek-US/data/ftseek_us.db \
  "SELECT symbol, direction, size, avg_entry_price, unrealized_pnl FROM open_positions WHERE direction IS NOT NULL;"
```

---

## 九、相关文档

- [数据源配置指南](DATA_SOURCE_GUIDE.md)
- [监控解决方案](MONITORING_SOLUTION_SUMMARY.md)
- [部署指南](DEPLOYMENT_GUIDE.md)

---

**最后更新**: 2026-03-30  
**维护者**: FTSeek-US Team
