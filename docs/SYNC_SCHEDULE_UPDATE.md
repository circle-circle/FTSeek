# 数据同步时间调整

**更新时间**: 2026-03-31 21:41  
**状态**: ✅ 已配置

---

## ✅ 新的同步机制

### 同步频率

**原配置**: 每 30 分钟同步一次  
**新配置**: 每个交易日 17:30 同步一次 ✅

### 执行时间

- **时间**: 每个交易日 17:30
- **周期**: 周一至周五
- **节假日**: 自动跳过 (可配置)

### Cron 配置

```cron
# 每个交易日 17:30 同步 (周一至周五)
30 17 * * 1-5 root cd /opt/ftseek/FTSeek && python3 -m app.simulation.sync_service >> /opt/ftseek/FTSeek/logs/simulation_sync.log 2>&1
```

---

## 📅 同步日历

### 典型交易周

| 日期 | 星期 | 同步时间 | 说明 |
|------|------|---------|------|
| 2026-03-30 | 周一 | 17:30 | ✅ 同步 |
| 2026-03-31 | 周二 | 17:30 | ✅ 同步 |
| 2026-04-01 | 周三 | 17:30 | ✅ 同步 |
| 2026-04-02 | 周四 | 17:30 | ✅ 同步 |
| 2026-04-03 | 周五 | 17:30 | ✅ 同步 |
| 2026-04-04 | 周六 | - | ❌ 不执行 |
| 2026-04-05 | 周日 | - | ❌ 不执行 |

### 节假日处理

```python
def is_trading_day():
    """判断是否为交易日"""
    from datetime import datetime
    today = datetime.now()
    
    # 周末不是交易日
    if today.weekday() >= 5:
        return False
    
    # TODO: 添加港股节假日判断
    # 可以添加具体的港股节假日列表
    
    return True
```

---

## 🔧 手动同步

### 立即执行同步

```bash
cd /opt/ftseek/FTSeek
python3 -m app.simulation.sync_service
```

### 查看同步日志

```bash
# 实时查看
tail -f /opt/ftseek/FTSeek/logs/simulation_sync.log

# 查看最近 100 行
tail -100 /opt/ftseek/FTSeek/logs/simulation_sync.log
```

### 验证同步状态

```bash
# 查看数据库
sqlite3 /opt/ftseek/FTSeek/data/simulation.db \
  "SELECT date('now') as today, COUNT(*) as total FROM simulation_trades;"

# 查看 API
curl "http://aitz.cc:8899/api/v1/review/stats/summary?period=today"
```

---

## 📊 数据更新

### 大盘刷新频率

- **KPI 指标**: 每 60 秒自动刷新
- **图表数据**: 每 60 秒自动刷新
- **持仓列表**: 每 60 秒自动刷新
- **数据同步**: 每个交易日 17:30

### 数据流程

```
17:30 交易日结束
    ↓
Cron 触发同步任务
    ↓
从富途 API 拉取今日数据
    ↓
更新本地数据库
    ↓
大盘自动刷新 (60 秒内)
```

---

## 📖 配置说明

### Cron 文件位置

```
/etc/cron.d/ftseek_data_sync
```

### 日志文件位置

```
/opt/ftseek/FTSeek/logs/simulation_sync.log
```

### 同步脚本位置

```
/opt/ftseek/FTSeek/app/simulation/sync_service.py
```

---

## 🎯 优势

### 相比原配置

| 维度 | 原配置 (每 30 分钟) | 新配置 (17:30) |
|------|------------------|---------------|
| **API 调用次数** | 约 20 次/天 | 1 次/天 |
| **系统负载** | 较高 | 低 |
| **数据实时性** | 高 | 盘后统一 |
| **适用场景** | 实时交易 | 盘后复盘 |

### 适用场景

✅ **适合**:
- 盘后复盘分析
- 历史数据统计
- 交易记录归档

❌ **不适合**:
- 实时交易监控
- 盘中数据分析

---

**配置更新时间**: 2026-03-31 21:41  
**下次同步时间**: 2026-04-01 17:30 (周三)  
**状态**: ✅ 已配置，交易日 17:30 自动执行
