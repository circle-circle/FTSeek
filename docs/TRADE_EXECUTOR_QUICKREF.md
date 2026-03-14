# 交易执行器快速参考

## 快速开始

```python
from app.core.trade_executor import get_executor
from app.strategy.base import Signal, SignalAction

executor = get_executor()

signal = Signal(
    code="HK.00700",
    action=SignalAction.BUY,
    price=350.0,
    quantity=100
)

executor.submit_signal(signal)
```

## API 速查

| 方法 | 说明 | 返回值 |
|------|------|--------|
| `submit_signal(signal)` | 提交信号到钉钉 | `bool` |
| `handle_confirm(signal_id, user_id)` | 处理确认 | `bool` |
| `handle_defer(signal_id, user_id)` | 处理延期 | `bool` |
| `handle_ignore(signal_id, user_id)` | 处理忽略 | `bool` |
| `handle_reject(signal_id, reason)` | 处理风控拒绝 | `bool` |
| `execute_order(order)` | 执行订单 | `bool` |
| `record_trade(trade)` | 记录交易 | `bool` |

## 状态枚举

```python
from app.core.trade_executor import SignalStatus

SignalStatus.PENDING    # 待确认
SignalStatus.CONFIRMED  # 已确认
SignalStatus.EXECUTED   # 已执行
SignalStatus.IGNORED    # 已忽略
SignalStatus.EXPIRED    # 已过期
SignalStatus.REJECTED   # 风控拒绝
```

## 回调路由示例

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/confirm")
async def confirm(signal_id: str, token: str):
    from app.core.trade_executor import get_executor
    executor = get_executor()
    return {"success": executor.handle_confirm(signal_id)}

@app.get("/api/defer")
async def defer(signal_id: str, token: str):
    from app.core.trade_executor import get_executor
    executor = get_executor()
    return {"success": executor.handle_defer(signal_id)}

@app.get("/api/reject")
async def reject(signal_id: str, token: str):
    from app.core.trade_executor import get_executor
    executor = get_executor()
    return {"success": executor.handle_ignore(signal_id)}
```

## 运行测试

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export FTSEEK_DEV_MODE=1

# 所有测试
python3 -m unittest discover -s tests -p 'test_executor*.py'

# 单个测试文件
python3 tests/test_trade_executor.py
python3 tests/test_executor_timeout.py
python3 tests/test_executor_integration.py
```

## 配置文件

```bash
# .env 或环境变量
export API_TOKEN=your_secure_token
export DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
export DINGTALK_SECRET=your_secret
export SIGNAL_EXPIRE_SECONDS=300  # 5 分钟
```

## 常见问题

**Q: 信号提交失败？**  
A: 检查钉钉 Webhook 配置和 API_TOKEN

**Q: 确认无响应？**  
A: 检查回调路由和 token 验证

**Q: 超时未触发？**  
A: 检查定时器状态 `executor._timers`

**Q: 数据库记录缺失？**  
A: 检查数据库文件权限和连接

## 文件位置

- 主代码：`app/core/trade_executor.py`
- 使用文档：`docs/TRADE_EXECUTOR_USAGE.md`
- 测试文件：`tests/test_executor*.py`
- 完成报告：`docs/plans/task8-completion-report.md`
