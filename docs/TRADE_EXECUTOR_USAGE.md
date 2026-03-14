# 交易执行器使用文档

## 概述

交易执行器 (`TradeExecutor`) 是 FTSeek v3.0 的核心组件，负责带人工确认的交易执行流程。

## 核心功能

### 1. 信号提交

```python
from app.core.trade_executor import get_executor
from app.strategy.base import Signal, SignalAction

# 获取执行器实例
executor = get_executor()

# 创建信号
signal = Signal(
    id="unique_signal_id",
    code="HK.00700",
    name="腾讯控股",
    action=SignalAction.BUY,
    price=350.0,
    quantity=100,
    reason="技术面突破 + 情绪面利好",
    strategy="融合 v2",
    extra={
        'technical_score': 0.75,
        'sentiment_score': 0.80,
        'confidence': 0.78
    }
)

# 提交信号 (推送到钉钉，启动超时定时器)
success = executor.submit_signal(signal)
```

### 2. 用户确认回调

当用户在钉钉点击"✅ 确认执行"按钮时：

```python
# 处理确认
success = executor.handle_confirm(
    signal_id="unique_signal_id",
    user_id="dingtalk_user_id"  # 可选
)

# 成功后：
# 1. 状态变更为 confirmed
# 2. 取消超时定时器
# 3. 自动执行订单
# 4. 记录交易到数据库
# 5. 推送执行结果
```

### 3. 用户延期回调

当用户在钉钉点击"⏰ 延期 15 分钟"按钮时：

```python
success = executor.handle_defer(
    signal_id="unique_signal_id",
    user_id="dingtalk_user_id"
)

# 成功后：
# 1. 过期时间延长 15 分钟
# 2. 重置超时定时器
# 3. 推送确认消息
```

### 4. 用户忽略回调

当用户在钉钉点击"❌ 忽略"按钮时：

```python
success = executor.handle_ignore(
    signal_id="unique_signal_id",
    user_id="dingtalk_user_id"
)

# 成功后：
# 1. 状态变更为 ignored
# 2. 取消超时定时器
# 3. 推送确认消息
```

### 5. 风控拒绝

当风控系统拒绝信号时：

```python
success = executor.handle_reject(
    signal_id="unique_signal_id",
    reason="置信度过低"  # 拒绝原因
)

# 成功后：
# 1. 状态变更为 rejected
# 2. 取消超时定时器
# 3. 推送风控预警
```

## 状态机

```
                    ┌─────────────────┐
                    │     pending     │
                    │   (待确认)       │
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│   confirmed   │   │    ignored    │   │    expired    │
│   (已确认)    │   │   (已忽略)    │   │   (已过期)    │
└───────┬───────┘   └───────────────┘   └───────────────┘
        │
        ▼
┌───────────────┐   ┌───────────────┐
│   executed    │   │   rejected    │
│   (已执行)    │   │  (风控拒绝)   │
└───────────────┘   └───────────────┘
```

### 状态说明

| 状态 | 说明 | 触发条件 |
|------|------|----------|
| `pending` | 待确认 | 信号提交后初始状态 |
| `confirmed` | 已确认 | 用户点击确认按钮 |
| `executed` | 已执行 | 订单执行成功 |
| `ignored` | 已忽略 | 用户点击忽略按钮 |
| `expired` | 已过期 | 15 分钟超时未确认 |
| `rejected` | 风控拒绝 | 风控检查不通过 |

## 超时处理

- **超时时间**: 15 分钟 (可在 `config.SIGNAL_EXPIRE_SECONDS` 配置)
- **自动过期**: 超时后自动标记为 `expired`
- **定时器管理**: 确认/忽略/延期后自动取消定时器
- **延期**: 每次延期增加 15 分钟

## 订单执行

### 执行流程

```python
from app.core.trade_executor import Order, OrderStatus

# 创建订单
order = Order(
    id="order_001",
    signal_id="signal_001",
    code="HK.00700",
    name="腾讯控股",
    action=SignalAction.BUY,
    price=350.0,
    quantity=100,
    strategy="融合 v2"
)

# 执行订单
success = executor.execute_order(order)

# 成功后：
# 1. 调用富途 API 下单
# 2. 更新订单状态为 submitted
# 3. 创建交易记录
# 4. 更新信号状态为 executed
```

### 富途集成

当前使用模拟实现，实际部署时需要替换为真实的富途 OpenD API 调用：

```python
# TODO: 在 _submit_futu_order 方法中实现
from futu import *

# 初始化交易上下文
trade_ctx = OpenTradeContext(config_path=xxx, pwd=xxx)

# 下单
ret_code, ret_data = trade_ctx.place_order(
    price=order.price,
    qty=order.quantity,
    code=order.code,
    trd_side=TrdSide.BUY if order.action == SignalAction.BUY else TrdSide.SELL,
    order_type=OrderType.NORMAL
)

if ret_code == 0:
    return ret_data['order_id']
else:
    return None
```

## 交易记录

### 记录交易

```python
from app.core.trade_executor import Trade
from datetime import datetime

trade = Trade(
    id="trade_001",
    signal_id="signal_001",
    order_id="order_001",
    code="HK.00700",
    name="腾讯控股",
    action=SignalAction.BUY,
    entry_price=350.0,
    quantity=100,
    strategy="融合 v2",
    entry_time=datetime.now()
)

success = executor.record_trade(trade)
```

### 查询交易

```python
from app.storage.database import get_database

db = get_database()

# 获取未平仓交易
open_trades = db.get_open_trades()

# 获取指定时间段的交易
trades = db.get_trades_by_period(
    start_date="2026-03-01",
    end_date="2026-03-31"
)
```

## 回调函数

可以设置状态变化回调：

```python
def on_status_change(signal_id: str, new_status: SignalStatus):
    print(f"信号 {signal_id} 状态变更为 {new_status}")

executor.on_status_change = on_status_change
```

## 完整示例

```python
from app.core.trade_executor import get_executor, SignalStatus
from app.strategy.base import Signal, SignalAction

# 初始化
executor = get_executor()

# 设置回调
executor.on_status_change = lambda sid, status: print(f"{sid}: {status}")

# 创建信号
signal = Signal(
    code="HK.00700",
    name="腾讯控股",
    action=SignalAction.BUY,
    price=350.0,
    quantity=100,
    reason="技术面突破",
    strategy="融合 v2"
)

# 提交信号
if executor.submit_signal(signal):
    print(f"信号已提交：{signal.id}")
    
    # 等待用户确认...
    # 用户在钉钉点击按钮后，会自动调用 handle_confirm/handle_ignore/handle_defer
    
else:
    print("信号提交失败")
```

## 与钉钉集成

### 回调路由

钉钉按钮点击后会回调到 FastAPI 服务：

```python
# FastAPI 路由示例
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/confirm")
async def confirm_signal(signal_id: str, token: str):
    if token != config.API_TOKEN:
        return {"error": "Unauthorized"}
    
    executor = get_executor()
    success = executor.handle_confirm(signal_id)
    return {"success": success}

@app.get("/api/defer")
async def defer_signal(signal_id: str, token: str):
    if token != config.API_TOKEN:
        return {"error": "Unauthorized"}
    
    executor = get_executor()
    success = executor.handle_defer(signal_id)
    return {"success": success}

@app.get("/api/reject")
async def reject_signal(signal_id: str, token: str):
    if token != config.API_TOKEN:
        return {"error": "Unauthorized"}
    
    executor = get_executor()
    success = executor.handle_ignore(signal_id)  # 忽略即拒绝
    return {"success": success}
```

## 测试

### 运行单元测试

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export FTSEEK_DEV_MODE=1

# 运行所有测试
python3 -m pytest tests/test_trade_executor.py -v

# 运行超时测试
python3 tests/test_executor_timeout.py

# 运行集成测试
python3 tests/test_executor_integration.py
```

### 测试覆盖率

```bash
# 安装 coverage
pip3 install coverage

# 运行测试并生成报告
coverage run -m pytest tests/test_trade_executor.py
coverage report -m
coverage html
```

## 配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| 信号过期时间 | `SIGNAL_EXPIRE_SECONDS` | 300 (5 分钟) | 信号有效期 |
| API Token | `API_TOKEN` | 必须设置 | 回调鉴权 |
| 钉钉 Webhook | `DINGTALK_WEBHOOK_URL` | 必须设置 | 推送地址 |
| 钉钉 Secret | `DINGTALK_SECRET` | 可选 | 加签密钥 |

## 注意事项

1. **线程安全**: 使用 `_lock` 保护共享状态
2. **超时清理**: 定期调用 `cleanup()` 清理资源
3. **异常处理**: 所有公开方法都有 try-except 保护
4. **日志记录**: 关键操作都有详细日志
5. **幂等性**: 确认/忽略/延期操作都是幂等的

## 故障排查

### 常见问题

1. **信号提交失败**
   - 检查钉钉 Webhook 配置
   - 检查数据库连接
   - 查看日志：`logs/ftseek.log`

2. **确认无响应**
   - 检查 API Token 是否正确
   - 检查回调路由是否配置
   - 查看钉钉机器人日志

3. **超时未触发**
   - 检查定时器是否被意外取消
   - 检查线程是否正常运行
   - 查看 executor._timers 状态

4. **数据库记录缺失**
   - 检查数据库文件权限
   - 检查表结构是否完整
   - 查看数据库操作日志

## 性能优化建议

1. **批量处理**: 多个信号可以批量提交
2. **异步执行**: 订单执行可以异步化
3. **缓存优化**: 使用 Redis 缓存信号状态
4. **连接池**: 使用数据库连接池
5. **定时清理**: 定期清理过期信号和已完成交易

## 下一步

- [ ] 集成真实的富途 OpenD API
- [ ] 添加订单状态轮询
- [ ] 实现自动止盈止损
- [ ] 添加交易绩效统计
- [ ] 支持实盘模式
