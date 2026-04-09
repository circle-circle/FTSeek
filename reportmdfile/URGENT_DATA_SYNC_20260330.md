# 🚨 紧急数据同步问题报告

**发现时间**: 2026-03-30 11:12 CST  
**严重程度**: 🔴 **严重**

---

## 一、问题发现

### 数据对比

| 数据源 | 今日成交数 | 最近成交日期 | 状态 |
|--------|-----------|-------------|------|
| **富途 App** | 10+ 笔 | 今日 (03-30) | ✅ 正常 |
| **本地数据库** | 0 笔 | 03-27 (26 笔) | ❌ 异常 |

### 根本原因

**成交同步功能被禁用!**

```python
# /opt/ftseek/FTSeek/app/main.py 第 291 行
# 2.5) 启动持仓同步 - 2026-03-27 禁用
# 改为在每小时推送交易报告前进行同步，避免启动时 OpenD 数据未就绪
# try:
#     _sync_positions_from_futu(executor)
# except Exception as _sync_e:
#     logger.warning(f"启动持仓同步失败（将继续运行）: {_sync_e}")
```

---

## 二、影响分析

### 当前影响

1. **本地数据库无今日成交** - 无法进行复盘分析
2. **Harness 工作流无数据** - 无法生成日报
3. **Dashboard 无数据显示** - 无法监控
4. **策略优化无依据** - 无法改进

### 历史数据

- **最后正常日期**: 2026-03-27 (26 笔成交)
- **中断天数**: 1 天 (03-28 至 03-30)
- **估计丢失**: 10+ 笔今日成交

---

## 三、紧急修复方案

### 3.1 P0: 手动同步今日成交 (立即执行)

**步骤**:
1. 从富途 App 导出今日成交记录
2. 手动插入数据库
3. 验证数据完整性

**SQL 模板**:
```sql
INSERT INTO trades (
    id, signal_id, code, name, action, 
    entry_price, quantity, pnl, pnl_percent,
    strategy, entry_time, status, order_id
) VALUES (
    'trade_20260330_001',
    'signal_xxx',
    'HK.00981',
    '中芯国际',
    'BUY',
    49.50,
    1000,
    0,
    0,
    'confluence_v2',
    '2026-03-30 10:30:00',
    'CLOSED',
    'futu_order_xxx'
);
```

### 3.2 P1: 恢复自动同步 (今日收盘后)

**修改 main.py**:
```python
# 取消注释并优化
try:
    # 增加 OpenD 就绪检查
    if opend_ready():
        _sync_positions_from_futu(executor)
    else:
        logger.warning("OpenD 未就绪，跳过启动同步")
except Exception as _sync_e:
    logger.warning(f"启动持仓同步失败（将继续运行）: {_sync_e}")
```

### 3.3 P2: 增加成交同步定时任务 (本周内)

**Crontab 配置**:
```bash
# 每 5 分钟同步成交记录
*/5 9-16 * * 1-5 /opt/ftseek/FTSeek/venv/bin/python /opt/ftseek/FTSeek/scripts/sync_trades_from_futu.py
```

---

## 四、数据恢复脚本

### 4.1 手动导入脚本

```python
#!/usr/bin/env python3
"""
手动导入富途成交记录到数据库
"""

import sqlite3
import json
from datetime import datetime

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"

def import_trades(trades_file: str):
    """从 JSON 文件导入成交记录"""
    
    with open(trades_file, 'r') as f:
        trades = json.load(f)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for trade in trades:
        cursor.execute("""
            INSERT OR REPLACE INTO trades (
                id, signal_id, code, name, action,
                entry_price, exit_price, quantity,
                pnl, pnl_percent, fees,
                strategy, entry_time, exit_time,
                exit_reason, order_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.get('id'),
            trade.get('signal_id'),
            trade.get('code'),
            trade.get('name'),
            trade.get('action'),
            trade.get('entry_price'),
            trade.get('exit_price'),
            trade.get('quantity'),
            trade.get('pnl'),
            trade.get('pnl_percent'),
            trade.get('fees', 0),
            trade.get('strategy'),
            trade.get('entry_time'),
            trade.get('exit_time'),
            trade.get('exit_reason'),
            trade.get('order_id'),
            trade.get('status', 'CLOSED')
        ))
    
    conn.commit()
    conn.close()
    print(f"✅ 成功导入 {len(trades)} 条成交记录")

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("用法：python3 import_trades.py <trades.json>")
        sys.exit(1)
    
    import_trades(sys.argv[1])
```

### 4.2 数据验证脚本

```python
#!/usr/bin/env python3
"""
验证数据库成交记录完整性
"""

import sqlite3
from datetime import datetime

DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"

def validate_trades():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 今日成交数
    cursor.execute("""
        SELECT COUNT(*) FROM trades 
        WHERE date(entry_time) = date('now')
    """)
    today_count = cursor.fetchone()[0]
    print(f"今日成交数：{today_count} 笔")
    
    # 本周成交数
    cursor.execute("""
        SELECT COUNT(*) FROM trades 
        WHERE date(entry_time) >= date('now', '-7 days')
    """)
    week_count = cursor.fetchone()[0]
    print(f"本周成交数：{week_count} 笔")
    
    # 总成交数
    cursor.execute("SELECT COUNT(*) FROM trades")
    total_count = cursor.fetchone()[0]
    print(f"总成交数：{total_count} 笔")
    
    # 最新成交时间
    cursor.execute("""
        SELECT entry_time FROM trades 
        ORDER BY entry_time DESC LIMIT 1
    """)
    result = cursor.fetchone()
    if result:
        print(f"最新成交时间：{result[0]}")
    else:
        print("⚠️ 无成交记录")
    
    conn.close()

if __name__ == '__main__':
    validate_trades()
```

---

## 五、执行计划

| 时间 | 任务 | 优先级 | 状态 |
|------|------|--------|------|
| **11:15** | 从富途 App 导出今日成交 | P0 | ⏳ 待执行 |
| **11:20** | 手动导入数据库 | P0 | ⏳ 待执行 |
| **11:25** | 验证数据完整性 | P0 | ⏳ 待执行 |
| **16:00** | 恢复自动同步功能 | P1 | ⏳ 待执行 |
| **17:00** | 配置定时同步任务 | P2 | ⏳ 待执行 |

---

## 六、验收标准

| 验收项 | 目标 | 验证方式 |
|--------|------|---------|
| 今日成交导入 | ≥10 笔 | 数据库查询 |
| 数据字段完整 | 100% | 字段检查 |
| 富途 App 一致 | 100% | 对比验证 |
| 自动同步恢复 | 正常 | 日志验证 |

---

## 七、后续优化

### 7.1 增加同步监控

```python
# 每小时检查同步状态
if last_sync_time > 1 hour:
    send_alert("⚠️ 成交同步超时 1 小时")
```

### 7.2 增加数据校验

```python
# 每日收盘后校验
futu_count = get_futu_trade_count()
db_count = get_db_trade_count()

if abs(futu_count - db_count) > 5:
    send_alert(f"🚨 数据不一致：富途{futu_count} vs 数据库{db_count}")
```

---

**状态**: 🔴 **紧急**  
**下一步**: ⏳ 从富途 App 导出今日成交记录

---

*发现严重数据同步问题！本地数据库无今日成交记录，需立即手动同步并恢复自动同步功能。* 🚨
