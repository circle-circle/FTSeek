# 统一决策引擎用户指南

> 📘 FTSeek V4 交易决策系统使用手册  
> 版本：1.0.0 | 最后更新：2026-03-12

---

## 📖 目录

- [功能介绍](#功能介绍)
- [快速开始](#快速开始)
- [交易模式说明](#交易模式说明)
- [API 使用指南](#api 使用指南)
- [常见问题](#常见问题)

---

## 🎯 功能介绍

统一决策引擎是 FTSeek V4 的核心组件，提供智能化的交易决策和执行能力。

### 核心功能

| 功能 | 说明 |
|------|------|
| **信号限流** | 每只股票最多 2 个信号/小时，避免过度交易 |
| **模拟交易** | 纸面交易验证策略有效性，零风险测试 |
| **自动止盈止损** | 收益≥5% 自动止盈，亏损≥3% 自动止损 |
| **市场时段管理** | 港股/美股独立运行，自动识别交易时段 |
| **定时报告** | 日报/周报自动推送，交易汇总一目了然 |
| **人工干预** | 支持手动切换模式、手动平仓 |

### 系统架构

```
信号生成 → 限流检查 → 时段检查 → 模式路由 → 交易执行 → 报告推送
   ↓           ↓           ↓           ↓           ↓           ↓
策略引擎   频率控制   市场时段   PAPER/LIVE  模拟/实盘   钉钉通知
```

---

## 🚀 快速开始

### 1. 环境准备

确保已安装 Python 3.8+ 和必要依赖：

```bash
cd /opt/ftseek/FTSeek
pip3 install apscheduler flask pytz
```

### 2. 配置环境变量

编辑 `.env` 文件，配置必要的环境变量：

```bash
# API 认证密钥
export API_TOKEN="your-secret-token-here"

# 钉钉推送 (可选)
export DINGTALK_WEBHOOK="https://oapi.dingtalk.com/robot/send?access_token=xxx"
export DINGTALK_SECRET="your-secret"

# 富途配置
export FUTU_PASSWORD="your-password"
export FUTU_HOST="127.0.0.1"
export FUTU_PORT="11111"
```

### 3. 部署引擎

运行部署脚本：

```bash
bash scripts/deploy_trading_engine.sh
```

部署完成后，服务将自动启动：
- **定时任务调度器**：后台运行，负责日报/周报推送
- **API 服务器**：监听 8900 端口，提供人工干预接口

### 4. 验证部署

```bash
# 健康检查
curl http://localhost:8900/api/v2/health

# 预期输出:
# {"status":"ok","timestamp":"2026-03-12T18:00:00"}
```

---

## 📊 交易模式说明

系统支持四种交易模式，可 per-stock 配置：

### PAPER (模拟模式)

- **用途**: 策略验证、新手入门
- **执行**: 所有交易均为纸面交易，不产生真实订单
- **适用**: 新策略测试、市场观察期

```bash
# 切换到模拟模式
curl -X POST http://localhost:8900/api/v2/mode/0700.HK \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"mode": "PAPER"}'
```

### HYBRID (混合模式)

- **用途**: 渐进式实盘
- **执行**: 高置信度信号 (>0.8) 走实盘，低置信度走模拟
- **适用**: 策略验证通过后，逐步过渡到实盘

```bash
# 切换到混合模式
curl -X POST http://localhost:8900/api/v2/mode/0700.HK \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"mode": "HYBRID"}'
```

### LIVE (实盘模式)

- **用途**: 真实交易
- **执行**: 所有信号均执行真实订单
- **适用**: 策略成熟、风险可控
- **注意**: ⚠️ 需人工确认切换，谨慎使用！

```bash
# 切换到实盘模式 (需人工确认)
curl -X POST http://localhost:8900/api/v2/mode/0700.HK \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"mode": "LIVE"}'
```

### DISABLED (禁用模式)

- **用途**: 临时停止交易
- **执行**: 不处理任何信号
- **适用**: 市场异常、系统维护

```bash
# 禁用交易
curl -X POST http://localhost:8900/api/v2/mode/0700.HK \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"mode": "DISABLED"}'
```

### 模式切换建议

| 场景 | 推荐模式 | 说明 |
|------|---------|------|
| 新策略上线 | PAPER | 先模拟运行 3 天，验证有效性 |
| 连续 3 天收益>5% | HYBRID | 可考虑切换到混合模式 |
| 策略成熟稳定 | LIVE | 实盘交易，但仍需监控 |
| 市场异常波动 | DISABLED | 暂停交易，观望为主 |

---

## 🔌 API 使用指南

### 认证方式

所有 API 端点 (除健康检查外) 需要 API Key 认证：

```bash
# 在请求头中添加
-H "X-API-Key: your-api-key"
```

API Key 从环境变量 `API_TOKEN` 或 `API_KEY` 读取。

### API 端点

#### 1. 健康检查

```bash
GET /api/v2/health
```

**响应示例**:
```json
{
  "status": "ok",
  "timestamp": "2026-03-12T18:00:00"
}
```

#### 2. 查询所有持仓

```bash
GET /api/v2/positions
```

**响应示例**:
```json
{
  "positions": [
    {
      "code": "0700.HK",
      "quantity": 100,
      "avg_cost": 382.40,
      "current_price": 401.52,
      "pnl": 1912.00,
      "pnl_pct": 0.05
    }
  ],
  "total_count": 1
}
```

#### 3. 查询绩效统计

```bash
GET /api/v2/performance?code=0700.HK&days=30
```

**参数**:
- `code`: 股票代码 (必需)
- `days`: 统计天数 (可选，默认 30)

**响应示例**:
```json
{
  "code": "0700.HK",
  "total_trades": 12,
  "winning_trades": 8,
  "win_rate": 0.67,
  "total_pnl": 8500.00,
  "total_return": 0.085,
  "max_drawdown": -0.042
}
```

#### 4. 切换交易模式

```bash
POST /api/v2/mode/{code}
Content-Type: application/json
X-API-Key: your-api-key

{
  "mode": "PAPER"  // PAPER/HYBRID/LIVE/DISABLED
}
```

**响应示例**:
```json
{
  "code": "0700.HK",
  "old_mode": "HYBRID",
  "new_mode": "PAPER",
  "confirmed_by": "user",
  "timestamp": "2026-03-12T16:45:00"
}
```

#### 5. 手动平仓

```bash
POST /api/v2/manual_exit/{code}
Content-Type: application/json
X-API-Key: your-api-key

{
  "quantity": 100  // 可选，默认全部平仓
}
```

**响应示例**:
```json
{
  "code": "0700.HK",
  "action": "SELL",
  "quantity": 100,
  "price": 401.52,
  "pnl": 1912.00,
  "status": "executed"
}
```

### Python 示例

```python
import requests

API_BASE = "http://localhost:8900"
API_KEY = "your-api-key"

headers = {"X-API-Key": API_KEY}

# 查询持仓
response = requests.get(f"{API_BASE}/api/v2/positions", headers=headers)
positions = response.json()
print(f"当前持仓：{len(positions['positions'])} 只股票")

# 查询绩效
response = requests.get(
    f"{API_BASE}/api/v2/performance",
    headers=headers,
    params={"code": "0700.HK", "days": 7}
)
perf = response.json()
print(f"周收益率：{perf['total_return']:.2%}")

# 切换模式
response = requests.post(
    f"{API_BASE}/api/v2/mode/0700.HK",
    headers=headers,
    json={"mode": "PAPER"}
)
print(f"模式切换：{response.json()['new_mode']}")

# 手动平仓
response = requests.post(
    f"{API_BASE}/api/v2/manual_exit/0700.HK",
    headers=headers,
    json={"quantity": 100}
)
print(f"平仓结果：{response.json()['status']}")
```

---

## ❓ 常见问题

### Q1: 如何查看当前交易模式？

```bash
# 查询持仓时会显示模式信息
curl http://localhost:8900/api/v2/positions \
  -H "X-API-Key: your-api-key"
```

或查看数据库：

```bash
cd /opt/ftseek/FTSeek
sqlite3 data/trading_decision.db "SELECT * FROM trading_modes;"
```

### Q2: 信号被限流了怎么办？

限流规则：
- 每只股票最多 2 个信号/小时
- 同方向信号去重
- 卖出后 15 分钟冷静期

如果信号被限流，说明交易频率过高。建议：
1. 检查策略信号质量
2. 调整策略参数，减少信号频率
3. 等待限流窗口过去

### Q3: 如何止盈止损？

系统自动监控持仓，触发条件自动卖出：
- **止盈**: 收益率≥5%
- **止损**: 亏损≥3%
- **超时**: 持仓超 24 小时

也可手动平仓：

```bash
curl -X POST http://localhost:8900/api/v2/manual_exit/0700.HK \
  -H "X-API-Key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Q4: 日报/周报什么时候推送？

- **港股日报**: 周一至周五 16:30 HKT (收盘后 30 分钟)
- **美股日报**: 周一至周五 04:30 EST (收盘后 30 分钟)
- **周报**: 周日 20:00 Asia/Shanghai

推送格式为钉钉消息，需配置 `DINGTALK_WEBHOOK`。

### Q5: 如何停止服务？

```bash
# 停止定时任务调度器
kill $(cat /opt/ftseek/FTSeek/logs/scheduler.pid)

# 停止 API 服务器
kill $(cat /opt/ftseek/FTSeek/logs/api_server.pid)
```

### Q6: 日志在哪里查看？

```bash
# 调度器日志
tail -f /opt/ftseek/FTSeek/logs/scheduler.log

# API 服务器日志
tail -f /opt/ftseek/FTSeek/logs/api_server.log

# 决策引擎日志
tail -f /opt/ftseek/FTSeek/logs/decision_engine.log
```

### Q7: 模拟交易和实盘交易的区别？

| 项目 | 模拟交易 (PAPER) | 实盘交易 (LIVE) |
|------|----------------|----------------|
| 资金 | 虚拟资金 | 真实资金 |
| 成交 | 按实时价格模拟 | 真实订单成交 |
| 滑点 | 默认 0.5% | 实际市场滑点 |
| 风险 | 无风险 | 有亏损风险 |
| 用途 | 策略验证 | 真实盈利 |

### Q8: 如何备份交易数据？

```bash
# 备份数据库
cp /opt/ftseek/FTSeek/data/trading_decision.db \
   /opt/ftseek/FTSeek/data/trading_decision.db.backup.$(date +%Y%m%d)

# 备份日志
tar -czf /opt/ftseek/FTSeek/logs.backup.$(date +%Y%m%d).tar.gz \
   /opt/ftseek/FTSeek/logs/
```

---

## 📞 技术支持

如有问题，请查看：
- [运维指南](docs/trading_decision_ops_guide.md) - 系统架构、故障排查
- [项目文档](docs/) - 完整文档列表

---

*最后更新：2026-03-12 | 版本：1.0.0*
