# Decision API V2 - 精简 API 端点

提供必要的人工干预和数据查询接口。

## 快速开始

### 启动服务器

```bash
cd /opt/ftseek/FTSeek
export API_KEY=your-secret-key  # 必须设置
export API_PORT=8900  # 可选，默认 8900
PYTHONPATH=/opt/ftseek/FTSeek:$PYTHONPATH python app/v4/api/decision_api.py
```

### 测试端点

```bash
# 健康检查 (无需认证)
curl http://localhost:8900/api/v2/health

# 查询持仓 (需要认证)
curl -H "X-API-Key: your-secret-key" http://localhost:8900/api/v2/positions

# 查询绩效
curl -H "X-API-Key: your-secret-key" "http://localhost:8900/api/v2/performance?code=0700.HK&days=30"

# 切换模式
curl -X POST -H "X-API-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"mode": "PAPER"}' http://localhost:8900/api/v2/mode/0700.HK

# 手动平仓
curl -X POST -H "X-API-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"quantity": 100}' http://localhost:8900/api/v2/manual_exit/0700.HK
```

## API 端点

### 1. GET /api/v2/positions

查询所有持仓。

**请求头:**
- `X-API-Key`: API 密钥 (必需)

**响应:**
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

### 2. GET /api/v2/performance

查询绩效统计。

**请求头:**
- `X-API-Key`: API 密钥 (必需)

**查询参数:**
- `code`: 股票代码 (必需)
- `days`: 统计天数 (可选，默认 30)

**响应:**
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

### 3. POST /api/v2/mode/{code}

切换交易模式 (需要人工确认)。

**请求头:**
- `X-API-Key`: API 密钥 (必需)
- `Content-Type`: application/json

**路径参数:**
- `code`: 股票代码

**请求体:**
```json
{
    "mode": "PAPER"  // PAPER, HYBRID, LIVE, DISABLED
}
```

**响应:**
```json
{
    "code": "0700.HK",
    "old_mode": "HYBRID",
    "new_mode": "PAPER",
    "confirmed_by": "user",
    "timestamp": "2026-03-12T16:45:00"
}
```

### 4. POST /api/v2/manual_exit/{code}

手动平仓 (人工干预)。

**请求头:**
- `X-API-Key`: API 密钥 (必需)
- `Content-Type`: application/json

**路径参数:**
- `code`: 股票代码

**请求体 (可选):**
```json
{
    "quantity": 100  // 可选，默认全部
}
```

**响应:**
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

## 认证

所有端点 (除了 `/api/v2/health`) 都需要 API Key 认证。

**方式:** 在请求头中添加 `X-API-Key`

```bash
curl -H "X-API-Key: your-secret-key" http://localhost:8900/api/v2/positions
```

**错误响应:**
- 401: 缺失或无效的 API Key
- 400: 请求参数错误
- 404: 端点不存在
- 500: 服务器内部错误
- 503: 服务不可用 (引擎未初始化)

## 测试

运行单元测试:

```bash
cd /opt/ftseek/FTSeek
python -m pytest tests/v4/test_decision_api.py -v
```

## 依赖

- Flask
- DecisionEngine (Task 7)
- PerformanceTracker
- TradingDatabase

## 安全注意事项

1. **必须设置环境变量 `API_KEY`** - 不要使用默认值
2. 生产环境建议使用 HTTPS
3. 考虑添加 IP 白名单
4. 定期轮换 API Key
5. 监控 API 使用日志

## 文件结构

```
app/v4/api/
  └── decision_api.py      # API 实现
tests/v4/
  └── test_decision_api.py # API 测试
```
