# FTSeek v2.1 钉钉 - 富途 OpenD 联动指南

**完整流程**: 信号生成 → 钉钉推送 → 点击确认 → 富途下单 → 结果返回

---

## 📊 联动架构

```
┌─────────────────────────────────────────────────────────────┐
│                    FTSeek v2.1 联动架构                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  富途 OpenD ────→ 策略引擎 ────→ 融合引擎                   │
│  (L2 行情)       (技术指标)    (技术 + 情绪)                 │
│                                       │                     │
│                                       ▼                     │
│                                  信号生成                   │
│                                  (概率 + 等级)               │
│                                       │                     │
│                                       ▼                     │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              钉钉推送 (ActionCard)                   │   │
│  │  🔴 HK.00700 BUY 信号                                │   │
│  │  买入概率：87.5%  卖出概率：12.5%                    │   │
│  │  [✅ 确认买入] [⏰ 延期] [❌ 忽略]                      │   │
│  └─────────────────────────────────────────────────────┘   │
│                                       │                     │
│                                       │ 用户点击            │
│                                       ▼                     │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              FastAPI 回调服务                        │   │
│  │  /api/confirm  → 验证 → TradeExecutor               │   │
│  └─────────────────────────────────────────────────────┘   │
│                                       │                     │
│                                       ▼                     │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              富途 OpenD API                          │   │
│  │  place_order(code, price, qty, side, type, env)     │   │
│  └─────────────────────────────────────────────────────┘   │
│                                       │                     │
│                                       ▼                     │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              结果返回                                │   │
│  │  ✅ 执行成功 订单号：MO123456                        │   │
│  │  ❌ 执行失败 原因：xxx                               │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 🔧 配置步骤

### 步骤 1: 环境变量配置

```bash
# 编辑 .env 文件
vim /opt/ftseek/FTSeek/.env

# 添加以下配置:
API_TOKEN=test123
FTSEEK_DEV_MODE=1

# 钉钉配置 (已配置)
DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=cddea8919223640d4f3bd59e507373cae0df476df5f541fcca5c8921eab73121
DINGTALK_SECRET=SEC177f5321e91e689a1a2134e4414ae49e7b7f2cddc63775613effbf371f388f78

# 富途 OpenD 配置
OPEND_HOST=127.0.0.1
OPEND_PORT=11111
TRADE_ENV=1  # 1=模拟环境，0=真实环境
```

### 步骤 2: 启动 OpenD

```bash
# 检查 OpenD 是否运行
ps aux | grep FutuOpenD

# 如果未运行，启动 OpenD
/opt/ftseek/Futu_OpenD_9.6.5618_Centos7/Futu_OpenD_9.6.5618_Centos7/FutuOpenD &

# 验证启动成功
pgrep -f FutuOpenD
```

### 步骤 3: 启动 FTSeek

```bash
cd /opt/ftseek/FTSeek

# 加载环境变量
source .env

# 启动系统
./run.sh
```

---

## 🧪 手动测试完整流程

### 测试 1: 创建模拟信号并推送

```bash
cd /opt/ftseek/FTSeek
source .env

python3 -c "
from app.strategy.base import Signal, SignalAction
from app.notifier.dingtalk import DingTalkNotifier
from app import config
import time

# 创建模拟看涨信号
signal = Signal(
    id=f'test_{int(time.time())}',
    code='HK.00700',
    name='腾讯控股',
    action=SignalAction.BUY,
    price=520.0,
    quantity=100,
    strategy='fusion_v2',
    reason='RSI 超卖 + MACD 金叉 + 情绪正面',
    extra={
        'direction': 'BUY',
        'buy_probability': 0.875,
        'sell_probability': 0.125,
        'confidence': 0.83,
        'signal_level': 'recommended'
    }
)

# 推送到钉钉
notifier = DingTalkNotifier()
result = notifier.send_signal(signal)

print(f'信号 ID: {signal.id}')
print(f'推送结果：{\"成功\" if result else \"失败\"}')
print(f'请在钉钉中查看消息并点击按钮')
"
```

### 测试 2: 模拟回调确认

```bash
python3 -c "
from app.signal_store import signal_store
from app.trader.executor import TradeExecutor

# 替换为实际信号 ID
signal_id = 'test_1234567890'

# 确认信号
signal = signal_store.confirm(signal_id)
if signal:
    print(f'信号确认成功')
    print(f'标的：{signal.code}')
    print(f'方向：{signal.action.value}')
    
    # 执行交易
    executor = TradeExecutor()
    result = executor.execute(signal)
    
    print(f'下单结果：{result}')
    
    # 标记已执行
    signal_store.mark_executed(signal_id)
    print(f'信号已标记为已执行')
else:
    print(f'信号不存在或已过期')
"
```

### 测试 3: 直接下单测试

```bash
python3 -c "
from app.trader.executor import TradeExecutor
from app.strategy.base import Signal, SignalAction

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

# 执行下单
result = executor.execute(signal)

print(f'下单结果:')
print(f'  成功：{result[\"success\"]}')
print(f'  订单号：{result.get(\"order_id\", \"N/A\")}')
print(f'  消息：{result[\"message\"]}')
"
```

---

## 📋 实际运行流程

### 1. 系统自动运行

```bash
# 后台启动 FTSeek
cd /opt/ftseek/FTSeek
nohup python3 app/main.py > logs/ftseek.log 2>&1 &

# 查看日志
tail -f logs/ftseek.log
```

### 2. 等待信号生成

系统会自动:
- 从富途 OpenD 获取 L2 行情
- 计算技术指标 (RSI, MACD, MA, VOL, BB)
- 抓取新闻并分析情绪
- 融合生成信号 (60% 技术 + 40% 情绪)
- 推送到钉钉

### 3. 钉钉接收消息

您会在钉钉中看到:
```
🔴 HK.00700 BUY 信号

标的：HK.00700 腾讯控股
方向：BUY
概率：买入 87.5% / 卖出 12.5%
置信度：83%
等级：recommended

[✅ 确认买入] [⏰ 延期 15 分钟] [❌ 忽略]
```

### 4. 点击确认按钮

点击 "✅ 确认买入" 后:
- 浏览器打开确认页面
- 系统验证 Token 和信号
- 调用 TradeExecutor 下单
- 富途 OpenD 执行交易
- 返回订单结果

### 5. 查看执行结果

钉钉收到结果推送:
```
[交易结果] ✅ 执行成功
标的：HK.00700
方向：BUY
价格：HK$520.00
数量：100
订单号：MO123456789
```

---

## 🔍 查看订单

### 富途 APP 查看

1. 打开富途牛牛 APP
2. 进入 交易 → 模拟交易
3. 查看 当日委托 或 持仓
4. 确认订单状态

### 数据库查询

```bash
sqlite3 data/ftseek.db "
SELECT id, code, action, price, status, confirmed_at, executed_at 
FROM signals 
ORDER BY created_at DESC 
LIMIT 10;
"

sqlite3 data/ftseek.db "
SELECT id, signal_id, code, action, entry_price, quantity, order_id, status
FROM trades 
ORDER BY entry_time DESC 
LIMIT 10;
"
```

---

## ⚠️ 注意事项

### 1. OpenD 配置

确保 OpenD 配置文件中已设置:
- 富途账号和密码
- 模拟交易权限已开通
- 监听地址和端口正确

### 2. 网络配置

- CALLBACK_EXTERNAL_URL 必须可外网访问
- 钉钉机器人需要能回调到 FastAPI 服务
- 建议使用域名或公网 IP

### 3. 安全配置

- API_TOKEN 必须保密
- 使用 HTTPS (生产环境)
- 设置防火墙规则

### 4. 金额限制

- 默认单笔最大 50000
- 可根据需要调整 MAX_ORDER_AMOUNT
- 模拟盘建议设置较小金额

---

## 📞 故障排查

### Q1: 钉钉消息收到但按钮无响应

**检查**:
```bash
# 查看 CALLBACK_EXTERNAL_URL 配置
cat .env | grep CALLBACK

# 测试回调接口
curl "http://your-server:8899/health"
```

### Q2: 下单失败

**检查**:
```bash
# OpenD 是否运行
ps aux | grep FutuOpenD

# 查看日志
tail -f logs/ftseek.log | grep -E "(order|trade|error)"

# 测试富途连接
python3 -c "
from futu import OpenSecTradeContext, TrdMarket
ctx = OpenSecTradeContext(host='127.0.0.1', port=11111, filter_trdmarket=TrdMarket.HK)
print(ctx.get_global_state())
"
```

### Q3: 信号未生成

**检查**:
```bash
# 查看策略日志
tail -f logs/ftseek.log | grep -E "(signal|strategy|fusion)"

# 检查监控股票列表
cat config/watchlist.json

# 检查指标计算
python3 test_modular.py
```

---

## ✅ 验收清单

- [ ] OpenD 已启动并连接
- [ ] FTSeek 已启动
- [ ] 钉钉推送正常
- [ ] 三按钮可点击
- [ ] 回调接口可访问
- [ ] 下单成功 (模拟环境)
- [ ] 订单在富途 APP 可见
- [ ] 数据库记录完整

---

**联动流程已就绪，可以开始实盘测试!** 🎉
