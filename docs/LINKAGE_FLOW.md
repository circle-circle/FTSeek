# FTSeek v2.1 钉钉 - 富途 OpenD 联动流程

**核心流程**: 钉钉按钮点击 → FastAPI 回调 → TradeExecutor → 富途 OpenD → 模拟下单

---

## 📊 完整联动流程图

```
┌─────────────────────────────────────────────────────────────────────┐
│                         FTSeek v2.1 联动架构                         │
└─────────────────────────────────────────────────────────────────────┘

1️⃣ 信号生成阶段
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  富途 OpenD   │────▶│  策略引擎    │────▶│  融合引擎    │
│  L2 行情数据  │     │  技术指标    │     │  技术 + 情绪   │
└──────────────┘     └──────────────┘     └──────────────┘
                                                │
                                                ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  新闻抓取    │◀────│  情绪分析    │◀────│  信号融合    │
│  新浪财经    │     │  情绪评分    │     │  概率计算    │
└──────────────┘     └──────────────┘     └──────────────┘
                                                │
                                                ▼
                                        ┌──────────────┐
                                        │  Signal 对象  │
                                        │  + 概率 + 等级 │
                                        └──────────────┘
                                                │
                                                ▼
2️⃣ 钉钉推送阶段
┌─────────────────────────────────────────────────────────────────────┐
│                        钉钉 ActionCard 消息                          │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  🔴 HK.00700 BUY 信号                                        │   │
│  │  概率：买入 87.5% / 卖出 12.5%                               │   │
│  │  置信度：83%  等级：recommended                              │   │
│  │  [✅ 确认买入]  [⏰ 延期 15 分钟]  [❌ 忽略]                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                                │
                                                │ 用户点击按钮
                                                ▼
3️⃣ 回调处理阶段
┌─────────────────────────────────────────────────────────────────────┐
│                     FastAPI 回调服务 (server.py)                     │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐        │
│  │  /api/confirm│     │  /api/defer  │     │  /api/reject │        │
│  │  确认执行    │     │  延期处理    │     │  忽略信号    │        │
│  └──────────────┘     └──────────────┘     └──────────────┘        │
│         │                    │                    │                 │
│         ▼                    ▼                    ▼                 │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │              SignalStore (信号管理器)                    │       │
│  │  • 验证信号是否存在                                     │       │
│  │  • 验证信号是否过期                                     │       │
│  │  • 验证信号是否已确认 (防重放)                           │       │
│  │  • 更新信号状态                                         │       │
│  └─────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────┘
                                                │
                                                │ 确认执行
                                                ▼
4️⃣ 交易执行阶段
┌─────────────────────────────────────────────────────────────────────┐
│                      TradeExecutor (executor.py)                    │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐        │
│  │  市场判断    │────▶│  参数准备    │────▶│  安全检查    │        │
│  │  HK / US     │     │  价格/数量   │     │  金额限制    │        │
│  └──────────────┘     └──────────────┘     └──────────────┘        │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────┐        │
│  │           富途 OpenD API (OpenSecTradeContext)           │        │
│  │  trd_ctx.place_order(                                    │        │
│  │      code=signal.code,                                   │        │
│  │      price=signal.price,                                 │        │
│  │      qty=signal.quantity,                                │        │
│  │      trd_side=TrdSide.BUY/SELL,                          │        │
│  │      order_type=OrderType.MARKET/NORMAL,                 │        │
│  │      trd_env=TrdEnv.SIMULATE/REAL  ← 模拟/真实环境        │        │
│  │  )                                                        │        │
│  └─────────────────────────────────────────────────────────┘        │
└─────────────────────────────────────────────────────────────────────┘
                                                │
                                                ▼
5️⃣ 结果返回阶段
┌─────────────────────────────────────────────────────────────────────┐
│                        执行结果推送                                 │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐        │
│  │  成功        │     │  失败        │     │  数据库记录  │        │
│  │  ✅ 执行成功 │     │  ❌ 执行失败 │     │  signals 表   │        │
│  │  订单号：xxx │     │  原因：xxx   │     │  trades 表    │        │
│  └──────────────┘     └──────────────┘     └──────────────┘        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🔧 关键代码位置

### 1. 钉钉按钮生成 (app/notifier/dingtalk.py)

```python
def send_signal(self, signal: Signal) -> bool:
    # 生成三个按钮的 URL
    confirm_url = (
        f"{config.CALLBACK_EXTERNAL_URL}/api/confirm"
        f"?signal_id={signal.id}&token={config.API_TOKEN}"
    )
    defer_url = (
        f"{config.CALLBACK_EXTERNAL_URL}/api/defer"
        f"?signal_id={signal.id}&token={config.API_TOKEN}"
    )
    reject_url = (
        f"{config.CALLBACK_EXTERNAL_URL}/api/reject"
        f"?signal_id={signal.id}&token={config.API_TOKEN}"
    )
    
    # 构建 ActionCard 消息
    payload = {
        "msgtype": "actionCard",
        "actionCard": {
            "btns": [
                {"title": "✅ 确认执行", "actionURL": confirm_url},
                {"title": "⏰ 延期 15 分钟", "actionURL": defer_url},
                {"title": "❌ 忽略", "actionURL": reject_url},
            ],
        },
    }
```

### 2. 回调处理 (app/server.py)

```python
@app.get("/api/confirm")
async def confirm_signal(signal_id: str, token: str):
    # 1. 验证 Token
    _check_token(token)
    
    # 2. 从 SignalStore 获取并确认信号
    signal = signal_store.confirm(signal_id)
    if not signal:
        return "信号无效或已过期"
    
    # 3. 调用 TradeExecutor 执行交易
    result = _executor.execute(signal)
    
    # 4. 标记信号已执行
    signal_store.mark_executed(signal_id)
    
    # 5. 推送执行结果到钉钉
    if _notifier:
        _notifier.send_execution_result(signal, result["success"], result["message"])
    
    return "交易已执行"
```

### 3. 交易执行 (app/trader/executor.py)

```python
def execute(self, signal: Signal) -> dict:
    # 1. 判断市场 (港股/美股)
    market = _detect_market(signal.code)
    
    # 2. 获取交易上下文
    trd_ctx = self._get_trd_ctx(market)
    
    # 3. 设置交易环境 (模拟/真实)
    trd_env = TrdEnv.SIMULATE if config.TRADE_ENV == 1 else TrdEnv.REAL
    
    # 4. 准备订单参数
    trd_side = TrdSide.BUY if signal.action == SignalAction.BUY else TrdSide.SELL
    qty = self._get_qty(signal)
    order_type = OrderType.MARKET if config.DEFAULT_ORDER_TYPE == "MARKET" else OrderType.NORMAL
    
    # 5. 安全检查 (金额限制)
    if signal.price * qty > config.MAX_ORDER_AMOUNT:
        return {"success": False, "message": "超过金额限制"}
    
    # 6. 调用富途 API 下单
    ret, data = trd_ctx.place_order(
        price=signal.price,
        qty=qty,
        code=signal.code,
        trd_side=trd_side,
        order_type=order_type,
        trd_env=trd_env,
    )
    
    # 7. 返回结果
    if ret == 0:
        order_id = str(data.iloc[0]["order_id"])
        return {"success": True, "order_id": order_id}
    else:
        return {"success": False, "message": str(data)}
```

---

## 🧪 联动测试脚本

### 测试 1: 模拟完整流程

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN=test123
export FTSEEK_DEV_MODE=1

# 运行集成测试
python3 test_integration.py
```

### 测试 2: 手动测试单一下单

```python
from app.trader.executor import TradeExecutor
from app.strategy.base import Signal, SignalAction

# 创建交易执行器
executor = TradeExecutor()

# 创建测试信号
signal = Signal(
    code='HK.00700',
    name='腾讯控股',
    action=SignalAction.BUY,
    price=520.0,
    quantity=100,
    strategy='test'
)

# 执行交易 (模拟环境)
result = executor.execute(signal)
print(f"下单结果：{result}")
```

---

## 📋 配置检查清单

### OpenD 配置
- [ ] OpenD 已安装并启动
- [ ] OpenD 配置文件中已设置富途账号密码
- [ ] OPEND_HOST 和 OPEND_PORT 配置正确
- [ ] 模拟交易权限已开通

### 系统配置
- [ ] TRADE_ENV=1 (模拟环境)
- [ ] API_TOKEN 已设置
- [ ] DINGTALK_WEBHOOK_URL 已配置
- [ ] CALLBACK_EXTERNAL_URL 可外网访问

### 安全检查
- [ ] MAX_ORDER_AMOUNT 设置合理 (默认 50000)
- [ ] DEFAULT_ORDER_QTY_HK 设置合理 (默认 100)
- [ ] DEFAULT_ORDER_QTY_US 设置合理 (默认 1)

---

## 🔐 安全机制

### 1. Token 验证
- 所有回调请求必须携带 API_TOKEN
- 使用 constant-time 比较防止时序攻击

### 2. 信号防重放
- 每个信号只能确认一次
- 确认后标记为 executed，防止重复执行

### 3. 信号有效期
- 默认 15 分钟过期
- 过期信号无法确认

### 4. 金额限制
- 单笔订单金额上限检查
- 超过限额自动拒绝

### 5. 频率限制
- 单股每日最多 3 次信号
- 全局每日最多 20 次信号

---

## 📊 数据库记录

### signals 表
```sql
INSERT INTO signals (
    id, code, action, price, quantity, strategy,
    technical_score, sentiment_score, confidence,
    created_at, expire_at, confirmed_at, executed_at, status
) VALUES (
    'demo_001', 'HK.00700', 'BUY', 520.0, 100, 'fusion_v2',
    0.85, 0.60, 0.83,
    '2026-02-27 17:00:00', '2026-02-27 17:15:00', 
    '2026-02-27 17:08:00', '2026-02-27 17:08:30', 'executed'
);
```

### trades 表
```sql
INSERT INTO trades (
    id, signal_id, code, action, entry_price, quantity,
    pnl, pnl_percent, entry_time, status, order_id
) VALUES (
    'trade_001', 'demo_001', 'HK.00700', 'BUY', 520.0, 100,
    0, 0, '2026-02-27 17:08:30', 'open', 'MO123456789'
);
```

---

## 🎯 模拟盘验证流程

### 第 1 步：启动 OpenD
```bash
# 检查 OpenD 状态
/opt/ftseek/Futu_OpenD_9.6.5618_Centos7/Futu_OpenD_9.6.5618_Centos7/FutuOpenD
```

### 第 2 步：启动 FTSeek
```bash
cd /opt/ftseek/FTSeek
source .env
./run.sh
```

### 第 3 步：等待信号生成
- 系统自动监控配置的股票
- 生成信号后推送到钉钉

### 第 4 步：点击确认按钮
- 在钉钉中点击 "✅ 确认执行"
- 等待执行结果推送

### 第 5 步：查看订单
- 富途 APP → 模拟交易 → 订单记录
- 查看订单是否成功

---

## 📞 故障排查

### Q1: 点击按钮无响应
**检查**:
- CALLBACK_EXTERNAL_URL 是否可外网访问
- API_TOKEN 是否正确
- 网络是否通畅

### Q2: 下单失败
**检查**:
- OpenD 是否运行
- 模拟交易权限是否开通
- 日志：`tail -f logs/ftseek.log`

### Q3: 信号已过期
**解决**:
- 点击 "⏰ 延期 15 分钟" 按钮
- 或等待下一个信号

---

## ✅ 联动验收标准

| 步骤 | 预期结果 | 验证方法 |
|------|----------|----------|
| 信号生成 | 钉钉收到推送 | 查看钉钉消息 |
| 点击确认 | 按钮可点击 | 点击测试 |
| 回调处理 | 返回确认页面 | 浏览器显示 ✅ |
| 下单执行 | 富途收到订单 | 查看订单记录 |
| 结果推送 | 钉钉收到结果 | 查看执行结果 |
| 数据库记录 | 信号和交易已保存 | 查询 SQLite |

---

**联动流程已就绪，可以开始模拟盘验证!** 🎉
