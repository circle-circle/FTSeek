# 统一决策引擎运维指南

> 🔧 FTSeek V4 交易决策系统运维手册  
> 版本：1.0.0 | 最后更新：2026-03-12

---

## 📖 目录

- [系统架构](#系统架构)
- [配置文件说明](#配置文件说明)
- [监控和日志](#监控和日志)
- [故障排查](#故障排查)
- [备份和恢复](#备份和恢复)

---

## 🏗️ 系统架构

### 整体架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    FTSeek V4 统一决策引擎                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   信号接入层                          │   │
│  │  策略信号 (fusion_v2, rsi_rebound, ma_cross)          │   │
│  └──────────────────────────────────────────────────────┘   │
│                           ↓                                   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   决策引擎层                          │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │   │
│  │  │ RateLimiter │  │ SessionMgr  │  │ ModeRouter  │   │   │
│  │  │ (限流)      │  │ (时段)      │  │ (模式路由)   │   │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘   │   │
│  └──────────────────────────────────────────────────────┘   │
│                           ↓                                   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   交易执行层                          │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │   │
│  │  │ PaperTrader │  │ LiveTrader  │  │ ExitManager │   │   │
│  │  │ (模拟)      │  │ (实盘)      │  │ (止盈止损)   │   │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘   │   │
│  └──────────────────────────────────────────────────────┘   │
│                           ↓                                   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                   报告推送层                          │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │   │
│  │  │ DailyReport │  │ WeeklyReport│  │ DingTalk    │   │   │
│  │  │ (日报)      │  │ (周报)      │  │ (推送)      │   │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘   │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
├─────────────────────────────────────────────────────────────┤
│                        数据持久化                            │
│  ┌─────────────────┐  ┌─────────────────┐                   │
│  │ SQLite Database │  │   Log Files     │                   │
│  │ trading_decision│  │  logs/*.log     │                   │
│  │ .db             │  │                 │                   │
│  └─────────────────┘  └─────────────────┘                   │
└─────────────────────────────────────────────────────────────┘
```

### 组件说明

| 组件 | 文件路径 | 职责 |
|------|---------|------|
| **决策引擎** | `app/v4/core/decision_engine.py` | 信号处理、模式路由 |
| **限流器** | `app/v4/core/rate_limiter.py` | 信号频率控制 |
| **时段管理器** | `app/v4/core/session_manager.py` | 港股/美股时段管理 |
| **模拟交易引擎** | `app/v4/trader/paper_trader.py` | 纸面交易执行 |
| **退出管理器** | `app/v4/core/exit_manager.py` | 止盈止损决策 |
| **调度器** | `app/v4/scheduler/trading_scheduler.py` | 定时任务调度 |
| **报告生成器** | `app/v4/report/report_generator.py` | 日报/周报生成 |
| **API 服务器** | `app/v4/api/decision_api.py` | 人工干预接口 |

### 进程架构

```
┌─────────────────────────────────────────────┐
│            部署脚本执行                      │
│         deploy_trading_engine.sh            │
└─────────────────────────────────────────────┘
                    ↓
    ┌───────────────┴───────────────┐
    ↓                               ↓
┌─────────────┐              ┌─────────────┐
│  调度器进程  │              │  API 服务器  │
│  (后台运行)  │              │  (后台运行)  │
│  scheduler   │              │  api_server  │
│  PID 文件    │              │  PID 文件    │
│  logs/scheduler.pid         │  logs/api_server.pid
│  logs/scheduler.log         │  logs/api_server.log
└─────────────┘              └─────────────┘
    ↓                               ↓
定时任务：                    REST API:
- 日报推送                    - /api/v2/positions
- 周报推送                    - /api/v2/performance
- 绩效检查                    - /api/v2/mode/{code}
                              - /api/v2/manual_exit/{code}
```

---

## ⚙️ 配置文件说明

### 环境变量 (.env)

```bash
# ========== 安全配置 ==========
# API 认证密钥 (必需)
export API_TOKEN="your-secret-token-here"

# ========== 富途配置 ==========
# 富途牛牛配置 (必需)
export FUTU_PASSWORD="your-password"
export FUTU_HOST="127.0.0.1"
export FUTU_PORT="11111"
export FUTU_ENV="SIMULATE"  # SIMULATE/REAL

# ========== 钉钉推送 (可选) ==========
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxx"
export DINGTALK_SECRET="your-secret"

# ========== 决策引擎配置 ==========
# 限流配置
export MAX_SIGNALS_PER_HOUR="2"
export COOLDOWN_MINUTES="15"

# 退出策略
export TARGET_PROFIT="0.05"    # 5% 止盈
export STOP_LOSS="0.03"        # 3% 止损
export TIMEOUT_HOURS="24"      # 24 小时超时

# 滑点配置 (模拟交易)
export SLIPPAGE="0.005"        # 0.5% 滑点

# ========== API 配置 ==========
export API_PORT="8900"
export API_HOST="0.0.0.0"
```

### 数据库配置

数据库文件位置：`/opt/ftseek/FTSeek/data/trading_decision.db`

**数据表结构**:

```sql
-- 交易模式表
CREATE TABLE trading_modes (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE,
    mode TEXT,  -- PAPER/HYBRID/LIVE/DISABLED
    updated_at DATETIME
);

-- 模拟持仓表
CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY,
    code TEXT,
    quantity INTEGER,
    avg_cost REAL,
    entry_time DATETIME,
    last_update DATETIME
);

-- 交易记录表
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY,
    code TEXT,
    action TEXT,  -- BUY/SELL
    quantity INTEGER,
    price REAL,
    timestamp DATETIME,
    pnl REAL,
    return_pct REAL
);

-- 绩效统计表
CREATE TABLE performance_stats (
    id INTEGER PRIMARY KEY,
    code TEXT,
    date DATE,
    daily_return REAL,
    cumulative_return REAL,
    trade_count INTEGER,
    win_count INTEGER,
    created_at DATETIME
);
```

**查看数据库**:

```bash
cd /opt/ftseek/FTSeek
sqlite3 data/trading_decision.db

# 常用查询
SELECT * FROM trading_modes;
SELECT * FROM paper_positions;
SELECT * FROM paper_trades ORDER BY timestamp DESC LIMIT 10;
SELECT * FROM performance_stats WHERE code='0700.HK' ORDER BY date DESC LIMIT 7;
```

---

## 📊 监控和日志

### 日志文件位置

| 日志文件 | 路径 | 说明 |
|---------|------|------|
| 调度器日志 | `logs/scheduler.log` | 定时任务执行日志 |
| API 服务器日志 | `logs/api_server.log` | API 请求日志 |
| 决策引擎日志 | `logs/decision_engine.log` | 信号处理日志 |
| 交易执行日志 | `logs/trading.log` | 交易执行日志 |

### 查看实时日志

```bash
# 实时监控所有日志
tail -f /opt/ftseek/FTSeek/logs/*.log

# 只看调度器日志
tail -f /opt/ftseek/FTSeek/logs/scheduler.log

# 只看错误日志
grep -i error /opt/ftseek/FTSeek/logs/*.log | tail -50
```

### 关键日志指标

**正常日志示例**:

```
[INFO] DecisionEngine V3 初始化完成，滑点=0.50%
[INFO] 已调度港股日报任务：0700.HK (周一至周五 16:30 HKT)
[INFO] 已调度绩效检查任务 (每小时一次)
[INFO] 信号处理：0700.HK BUY @ 380.5 (PAPER 模式)
[INFO] 模拟交易执行成功：0700.HK BUY 100 股
[INFO] 日报推送成功：0700.HK
```

**警告日志示例**:

```
[WARNING] 信号限流：0700.HK 已达每小时上限 (2 个)
[WARNING] 非交易时段：0700.HK，信号加入等待队列
[WARNING] 日报推送失败：0700.HK - 网络超时，将重试
```

**错误日志示例**:

```
[ERROR] 数据库迁移失败：table already exists
[ERROR] API 服务器启动失败：端口 8900 已被占用
[ERROR] 交易执行失败：0700.HK - 资金不足
```

### 监控脚本

创建监控脚本 `scripts/monitor_trading_engine.sh`:

```bash
#!/bin/bash
# 统一决策引擎监控脚本

PROJECT_ROOT="/opt/ftseek/FTSeek"
SCHEDULER_PID_FILE="${PROJECT_ROOT}/logs/scheduler.pid"
API_PID_FILE="${PROJECT_ROOT}/logs/api_server.pid"
LOG_FILE="${PROJECT_ROOT}/logs/monitor.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# 检查调度器进程
if [ -f "$SCHEDULER_PID_FILE" ]; then
    PID=$(cat "$SCHEDULER_PID_FILE")
    if ! kill -0 $PID 2>/dev/null; then
        log "ERROR: 调度器进程异常 (PID: $PID)"
        # 可在此添加重启逻辑
    else
        log "OK: 调度器运行正常 (PID: $PID)"
    fi
else
    log "ERROR: 调度器 PID 文件不存在"
fi

# 检查 API 服务器进程
if [ -f "$API_PID_FILE" ]; then
    PID=$(cat "$API_PID_FILE")
    if ! kill -0 $PID 2>/dev/null; then
        log "ERROR: API 服务器进程异常 (PID: $PID)"
        # 可在此添加重启逻辑
    else
        log "OK: API 服务器运行正常 (PID: $PID)"
    fi
else
    log "ERROR: API 服务器 PID 文件不存在"
fi

# 检查磁盘空间
DISK_USAGE=$(df -h /opt/ftseek | awk 'NR==2 {print $5}' | sed 's/%//')
if [ "$DISK_USAGE" -gt 80 ]; then
    log "WARNING: 磁盘使用率 ${DISK_USAGE}%"
fi

# 检查日志文件大小
for log in "${PROJECT_ROOT}"/logs/*.log; do
    SIZE=$(stat -f%z "$log" 2>/dev/null || stat -c%s "$log" 2>/dev/null)
    if [ "$SIZE" -gt 104857600 ]; then  # 100MB
        log "WARNING: 日志文件过大：$log ($(echo $SIZE | awk '{printf "%.1fMB", $1/1024/1024}'))"
    fi
done
```

**添加到 crontab**:

```bash
# 每 5 分钟检查一次
*/5 * * * * bash /opt/ftseek/FTSeek/scripts/monitor_trading_engine.sh
```

---

## 🔧 故障排查

### 问题 1: 服务无法启动

**症状**: 运行部署脚本后，服务未启动

**排查步骤**:

```bash
# 1. 检查端口占用
netstat -tlnp | grep 8900
# 如果端口被占用，停止旧进程或修改 API_PORT

# 2. 检查 Python 依赖
python3 -c "import apscheduler, flask, pytz"
# 如果报错，安装缺失依赖：pip3 install apscheduler flask pytz

# 3. 查看启动日志
tail -100 /opt/ftseek/FTSeek/logs/scheduler.log
tail -100 /opt/ftseek/FTSeek/logs/api_server.log

# 4. 手动启动测试
cd /opt/ftseek/FTSeek
python3 -m app.v4.api.decision_api
```

**解决方案**:

```bash
# 清理旧进程
kill $(cat /opt/ftseek/FTSeek/logs/scheduler.pid) 2>/dev/null
kill $(cat /opt/ftseek/FTSeek/logs/api_server.pid) 2>/dev/null

# 重新部署
bash scripts/deploy_trading_engine.sh
```

### 问题 2: 信号被限流

**症状**: 策略生成信号，但未执行交易

**排查步骤**:

```bash
# 1. 查看限流日志
grep "限流" /opt/ftseek/FTSeek/logs/decision_engine.log | tail -20

# 2. 检查当前限流状态
sqlite3 /opt/ftseek/FTSeek/data/trading_decision.db \
  "SELECT code, action, timestamp FROM signal_log ORDER BY timestamp DESC LIMIT 10;"
```

**解决方案**:

- 等待限流窗口过去 (1 小时)
- 调整限流配置：`MAX_SIGNALS_PER_HOUR`
- 优化策略，减少信号频率

### 问题 3: 钉钉推送失败

**症状**: 日报/周报未推送

**排查步骤**:

```bash
# 1. 检查 webhook 配置
echo $DINGTALK_WEBHOOK

# 2. 测试 webhook
curl -X POST "$DINGTALK_WEBHOOK" \
  -H "Content-Type: application/json" \
  -d '{"msgtype":"text","text":{"content":"测试消息"}}'

# 3. 查看推送日志
grep "推送" /opt/ftseek/FTSeek/logs/scheduler.log | tail -20
```

**解决方案**:

- 检查 webhook URL 是否正确
- 检查网络连通性
- 检查钉钉机器人配置

### 问题 4: 数据库锁死

**症状**: 数据库操作超时或失败

**排查步骤**:

```bash
# 1. 检查数据库文件
ls -lh /opt/ftseek/FTSeek/data/trading_decision.db

# 2. 检查是否有锁文件
ls -la /opt/ftseek/FTSeek/data/*.db-*

# 3. 测试数据库连接
sqlite3 /opt/ftseek/FTSeek/data/trading_decision.db "SELECT 1;"
```

**解决方案**:

```bash
# 1. 停止所有服务
kill $(cat /opt/ftseek/FTSeek/logs/scheduler.pid) 2>/dev/null
kill $(cat /opt/ftseek/FTSeek/logs/api_server.pid) 2>/dev/null

# 2. 清理锁文件
rm -f /opt/ftseek/FTSeek/data/trading_decision.db-*

# 3. 重启服务
bash scripts/deploy_trading_engine.sh
```

### 问题 5: 止盈止损未触发

**症状**: 持仓收益已达阈值，但未自动卖出

**排查步骤**:

```bash
# 1. 检查绩效检查任务是否运行
grep "绩效检查" /opt/ftseek/FTSeek/logs/scheduler.log | tail -10

# 2. 检查持仓数据
sqlite3 /opt/ftseek/FTSeek/data/trading_decision.db \
  "SELECT code, quantity, avg_cost FROM paper_positions;"

# 3. 检查退出管理器配置
echo "TARGET_PROFIT=$TARGET_PROFIT"
echo "STOP_LOSS=$STOP_LOSS"
```

**解决方案**:

- 等待下一次绩效检查 (每小时)
- 手动触发平仓：`POST /api/v2/manual_exit/{code}`
- 检查退出管理器配置

### 问题 6: API 返回 401

**症状**: API 请求返回认证失败

**排查步骤**:

```bash
# 1. 检查 API Key
echo "API_TOKEN=$API_TOKEN"

# 2. 测试 API
curl -v http://localhost:8900/api/v2/health
curl -v -H "X-API-Key: wrong-key" http://localhost:8900/api/v2/positions

# 3. 检查 API 服务器日志
tail -50 /opt/ftseek/FTSeek/logs/api_server.log
```

**解决方案**:

- 确保请求头包含正确的 API Key：`-H "X-API-Key: your-key"`
- 检查环境变量 `API_TOKEN` 是否正确设置
- 重启 API 服务器以加载新配置

---

## 💾 备份和恢复

### 数据备份

**自动备份脚本** `scripts/backup_trading_data.sh`:

```bash
#!/bin/bash
# 统一决策引擎数据备份脚本

PROJECT_ROOT="/opt/ftseek/FTSeek"
BACKUP_DIR="${PROJECT_ROOT}/data/backups"
DATE=$(date +%Y%m%d_%H%M%S)

# 创建备份目录
mkdir -p "$BACKUP_DIR"

# 备份数据库
cp "${PROJECT_ROOT}/data/trading_decision.db" \
   "${BACKUP_DIR}/trading_decision.db.${DATE}"

# 备份日志 (最近 7 天)
tar -czf "${BACKUP_DIR}/logs.${DATE}.tar.gz" \
   -C "${PROJECT_ROOT}" \
   logs/

# 清理旧备份 (保留 30 天)
find "$BACKUP_DIR" -name "*.db.*" -mtime +30 -delete
find "$BACKUP_DIR" -name "logs.*.tar.gz" -mtime +30 -delete

echo "备份完成：${BACKUP_DIR}"
ls -lh "${BACKUP_DIR}" | tail -5
```

**添加到 crontab**:

```bash
# 每天凌晨 2 点备份
0 2 * * * bash /opt/ftseek/FTSeek/scripts/backup_trading_data.sh
```

### 数据恢复

**恢复数据库**:

```bash
# 1. 停止服务
kill $(cat /opt/ftseek/FTSeek/logs/scheduler.pid) 2>/dev/null
kill $(cat /opt/ftseek/FTSeek/logs/api_server.pid) 2>/dev/null

# 2. 备份当前数据库 (以防万一)
cp /opt/ftseek/FTSeek/data/trading_decision.db \
   /opt/ftseek/FTSeek/data/trading_decision.db.backup.before_restore

# 3. 恢复数据库
cp /opt/ftseek/FTSeek/data/backups/trading_decision.db.20260312_020000 \
   /opt/ftseek/FTSeek/data/trading_decision.db

# 4. 重启服务
bash scripts/deploy_trading_engine.sh
```

**恢复日志**:

```bash
# 解压日志备份
tar -xzf /opt/ftseek/FTSeek/data/backups/logs.20260312_020000.tar.gz \
   -C /opt/ftseek/FTSeek/
```

### 灾难恢复

**完整系统恢复流程**:

```bash
# 1. 检查项目文件
ls -la /opt/ftseek/FTSeek/

# 2. 恢复数据库
cp /opt/ftseek/FTSeek/data/backups/trading_decision.db.LATEST \
   /opt/ftseek/FTSeek/data/trading_decision.db

# 3. 检查依赖
pip3 install -r /opt/ftseek/FTSeek/requirements.txt

# 4. 重新部署
bash /opt/ftseek/FTSeek/scripts/deploy_trading_engine.sh

# 5. 验证服务
curl http://localhost:8900/api/v2/health
curl -H "X-API-Key: your-key" http://localhost:8900/api/v2/positions
```

---

## 📞 应急联系

**紧急停机**:

```bash
# 停止所有服务
kill $(cat /opt/ftseek/FTSeek/logs/scheduler.pid) 2>/dev/null
kill $(cat /opt/ftseek/FTSeek/logs/api_server.pid) 2>/dev/null

# 禁用所有交易
sqlite3 /opt/ftseek/FTSeek/data/trading_decision.db \
  "UPDATE trading_modes SET mode='DISABLED';"
```

**快速重启**:

```bash
cd /opt/ftseek/FTSeek
bash scripts/deploy_trading_engine.sh
```

---

*最后更新：2026-03-12 | 版本：1.0.0*
