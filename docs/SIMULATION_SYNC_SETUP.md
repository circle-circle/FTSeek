# FTSeek 模拟盘数据同步 - 配置与验证指南

**更新日期**: 2026-03-31 18:06  
**执行状态**: ✅ 代码修复完成，⚠️ 待配置 OpenD 密码

---

## 📊 当前状态

### 数据库状态

| 表名 | 记录数 | 说明 |
|------|--------|------|
| `simulation_account` | 1 | ✅ 账户记录 (已清理重复) |
| `simulation_positions` | 2 | ⚠️ 测试数据 (腾讯 + 阿里) |
| `simulation_trades` | 2 | ⚠️ 测试数据 |

### Systemd 服务状态

```bash
● ftseek-simulation-sync.timer - FTSeek Simulation Data Sync Timer
   Active: active (waiting)
   Trigger: Wed 2026-04-01 16:30:56 CST (每日 16:30)
```

### API 验证

```bash
# Metrics API - ✅ 正常
curl "http://39.97.58.4:8899/api/simulation/metrics?token=xxx"
# 返回：胜率 100%, 持仓 2 个

# Positions API - ✅ 正常
curl "http://39.97.58.4:8899/api/simulation/positions?token=xxx"
# 返回：腾讯 200 股，阿里 500 股

# Trades API - ✅ 正常
curl "http://39.97.58.4:8899/api/simulation/trades?token=xxx"
# 返回：2 条交易记录
```

---

## ⚠️ 待配置项

### 1. 配置 OpenD 交易密码

**文件**: `/opt/ftseek/FTSeek/app/config.py`

**步骤**:
```bash
# 1. 编辑配置文件
vim /opt/ftseek/FTSeek/app/config.py

# 2. 找到第 43 行
FUTU_LOGIN_PWD = os.getenv("FUTU_LOGIN_PWD", "")

# 3. 修改为 (二选一):
# 方式 A: 直接配置 (不推荐)
FUTU_LOGIN_PWD = "your_trading_password"

# 方式 B: 使用环境变量 (推荐)
# 在 .env 文件中添加:
echo "FUTU_LOGIN_PWD=your_trading_password" >> /opt/ftseek/FTSeek/.env
```

**验证**:
```bash
# 检查配置
grep FUTU_LOGIN_PWD /opt/ftseek/FTSeek/.env
```

---

### 2. 配置钉钉告警 Token (可选)

**步骤**:
```bash
# 在 .env 文件中添加
echo "DINGTALK_TOKEN=your_dingtalk_token" >> /opt/ftseek/FTSeek/.env
```

---

## 🚀 执行同步

### 手动同步

```bash
cd /opt/ftseek/FTSeek

# 方式 1: 使用模块运行
python3 -m app.simulation.sync_service

# 方式 2: 直接运行脚本
python3 scripts/simulation_sync_cron.py
```

### 查看同步日志

```bash
# 实时查看日志
tail -f /opt/ftseek/FTSeek/data/simulation_logs/sync_*.json

# 查看最新同步结果
cat /opt/ftseek/FTSeek/data/simulation_logs/sync_$(date +%Y%m%d)_*.json | tail -1
```

### 验证同步结果

```bash
# 1. 检查数据库
sqlite3 /opt/ftseek/FTSeek/data/simulation.db \
  "SELECT COUNT(*) FROM simulation_positions;"

# 2. 检查 API
curl "http://39.97.58.4:8899/api/simulation/positions?token=xxx"

# 3. 访问前端
curl "http://39.97.58.4:8899/simulation/"
```

---

## 📈 绩效分析

### 查看胜率

```bash
curl "http://39.97.58.4:8899/api/simulation/metrics?token=xxx" | jq '.data.win_rate'
# 返回：胜率 (0-1 之间)
```

### 查看交易记录

```bash
curl "http://39.97.58.4:8899/api/simulation/trades?token=xxx" | jq '.data[] | {code, pnl, status}'
# 返回：股票代码、盈亏、状态
```

### 查看持仓盈亏

```bash
curl "http://39.97.58.4:8899/api/simulation/positions?token=xxx" | jq '.data[] | {code, unrealized_pnl}'
# 返回：股票代码、未实现盈亏
```

---

## 🔧 故障排查

### 问题 1: OpenD 连接失败

**错误信息**: `无法连接富途 OpenD (交易)`

**解决步骤**:
```bash
# 1. 检查 OpenD 服务状态
ps aux | grep OpenD

# 2. 检查 OpenD 配置
cat /opt/ftseek/FTSeek/.env | grep OPEND

# 3. 测试 OpenD 连接
telnet 127.0.0.1 11111

# 4. 重启 OpenD (如需要)
/opt/ftseek/Futu_OpenD_*/FutuOpenD restart
```

### 问题 2: 同步无数据

**可能原因**:
- OpenD 密码错误
- 无真实交易记录
- 数据库路径错误

**解决步骤**:
```bash
# 1. 检查同步日志
cat /opt/ftseek/FTSeek/data/simulation_logs/sync_*.json | jq '.error'

# 2. 检查数据库
sqlite3 /opt/ftseek/FTSeek/data/simulation.db \
  "SELECT * FROM simulation_account;"

# 3. 手动测试同步
cd /opt/ftseek/FTSeek && python3 -c "
from app.simulation.sync_service import run_sync
result = run_sync()
print(result)
"
```

### 问题 3: 前端显示不一致

**解决步骤**:
```bash
# 1. 清除浏览器缓存
# 2. 检查 API 返回
curl "http://39.97.58.4:8899/api/simulation/positions?token=xxx"

# 3. 检查前端代码
grep -n "API_BASE" /opt/ftseek/FTSeek/app/dashboard/simulation_dashboard.html
```

---

## 📅 定时任务管理

### 查看定时任务状态

```bash
systemctl list-timers | grep ftseek
```

### 手动触发同步

```bash
systemctl start ftseek-simulation-sync.service
```

### 查看同步历史

```bash
journalctl -u ftseek-simulation-sync.service -n 20
```

### 禁用定时任务

```bash
systemctl disable ftseek-simulation-sync.timer
systemctl stop ftseek-simulation-sync.timer
```

---

## 📊 同步日志示例

**成功同步**:
```json
{
  "success": true,
  "timestamp": "2026-03-31T18:30:00",
  "positions_synced": 5,
  "trades_synced": 12,
  "discrepancies": []
}
```

**失败同步**:
```json
{
  "success": false,
  "timestamp": "2026-03-31T18:30:00",
  "positions_synced": 0,
  "trades_synced": 0,
  "error": "无法连接富途 OpenD (交易)"
}
```

---

## 📝 执行报告

**详细执行报告**: `/opt/ftseek/FTSeek/data/simulation_logs/execution_report_20260331.md`

**同步日志目录**: `/opt/ftseek/FTSeek/data/simulation_logs/`

---

**最后更新**: 2026-03-31 18:06  
**状态**: ✅ 代码修复完成，⚠️ 待配置 OpenD 密码
