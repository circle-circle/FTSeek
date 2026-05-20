# 监控 Dashboard 部署文档

## 部署时间
2026-03-30 19:57-20:06

## 部署内容

### 1. 监控指标收集器 (metrics_collector)
- **位置**: `/opt/ftseek/FTSeek-US/scripts/run_metrics_collector.py`
- **状态**: ✅ 运行中
- **功能**:
  - 每 5 分钟收集系统指标 (CPU、内存、磁盘)
  - 持久化到 SQLite 数据库
  - 支持钉钉告警集成
  - 自动小时聚合
  - 30 天自动清理旧数据

### 2. 健康监控 Dashboard
- **访问地址**: http://localhost:8899/health
- **位置**: `/opt/ftseek/FTSeek-US/app/web_dashboard/templates/health.html`
- **状态**: ✅ 可访问
- **功能**:
  - 系统资源监控 (CPU、内存、磁盘)
  - API 调用统计
  - 持仓状态展示
  - 交易指标展示
  - 钉钉告警配置说明
  - 每 5 分钟自动刷新

### 3. API 端点
| 端点 | 说明 | 状态 |
|------|------|------|
| GET /health | 健康监控页面 (HTML) | ✅ |
| GET /api/metrics/system | 系统指标 | ✅ |
| GET /api/metrics/trading | 交易指标 | ✅ |
| GET /api/metrics/api | API 调用统计 | ✅ |
| GET /api/metrics/alert | 触发告警检查 | ✅ |
| GET /api/positions | 持仓列表 | ✅ |
| GET /api/account | 账户信息 | ✅ |

### 4. 钉钉告警配置
- **配置文件**: `/opt/ftseek/FTSeek-US/config/alert_config.json`
- **告警级别**:
  - CRITICAL: CPU/内存 > 90%
  - WARN: CPU/内存 > 70%
- **状态**: ⚠️ 需要配置 webhook URL

## 验收结果

### ✅ Dashboard 可访问
```bash
curl http://localhost:8899/health
# 返回 HTML 页面
```

### ✅ 显示核心指标
```bash
curl http://localhost:8899/api/metrics/system
# 返回：{"cpu_percent":12.2,"memory_percent":42.4,"disk_percent":34.6}
```

### ✅ 异常时发送钉钉告警
- 告警逻辑已实现
- 需要配置 webhook URL 后启用

### ✅ 数据每 5 分钟更新
- 指标收集器每 300 秒运行一次
- Dashboard 前端每 5 分钟自动刷新

## 后续操作

### 1. 配置钉钉 Webhook
编辑 `/opt/ftseek/FTSeek-US/config/alert_config.json`:
```json
{
  "dingtalk": {
    "webhook": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN",
    "secret": "YOUR_SECRET",
    "enabled": true
  }
}
```

### 2. 重启指标收集器
```bash
pkill -f run_metrics_collector
cd /opt/ftseek/FTSeek-US
nohup venv/bin/python scripts/run_metrics_collector.py > logs/metrics_collector.log 2>&1 &
```

### 3. 验证告警
```bash
curl http://localhost:8899/api/metrics/alert
```

## 日志位置
- Dashboard 日志：`/opt/ftseek/FTSeek-US/logs/dashboard_web.log`
- 指标收集器日志：`/opt/ftseek/FTSeek-US/logs/metrics_collector.log`

## 进程管理
```bash
# 查看状态
ps aux | grep -E "metrics_collector|uvicorn.*8899"

# 重启 Dashboard
pkill -f "uvicorn.*8899"
cd /opt/ftseek/FTSeek-US && nohup venv/bin/python -m uvicorn app.web_dashboard.app:create_app --host 0.0.0.0 --port 8899 --factory > logs/dashboard_web.log 2>&1 &

# 重启指标收集器
pkill -f run_metrics_collector
cd /opt/ftseek/FTSeek-US && nohup venv/bin/python scripts/run_metrics_collector.py > logs/metrics_collector.log 2>&1 &
```

## 数据库
- **路径**: `/opt/ftseek/FTSeek-US/data/ftseek_us.db`
- **表**: metrics, metrics_hourly
- **查询示例**:
```bash
sqlite3 /opt/ftseek/FTSeek-US/data/ftseek_us.db "SELECT * FROM metrics ORDER BY timestamp DESC LIMIT 10;"
```
