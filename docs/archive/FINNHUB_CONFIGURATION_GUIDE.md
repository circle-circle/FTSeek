# FTSeek-US Finnhub 配置指南

**创建日期**: 2026-03-24 20:10  
**状态**: ✅ 可用

---

## 一、Finnhub 注册

### 步骤 1: 注册账号
访问 https://finnhub.io/

1. 点击右上角 "Sign Up"
2. 填写邮箱和密码
3. 验证邮箱

### 步骤 2: 获取 API Key
1. 登录账号
2. 进入 Dashboard
3. 点击 "API Key"
4. 复制 API Key (类似：`xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`)

### 步骤 3: 配置到 .env.us
编辑 `/opt/ftseek/FTSeek-US/.env.us`:

```bash
# Finnhub API 配置
FINNHUB_API_KEY=your_api_key_here
```

### 步骤 4: 安装依赖
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
pip install finnhub-python
```

### 步骤 5: 测试连接
```bash
python3.8 -c "
import finnhub
import os
from dotenv import load_dotenv
load_dotenv('.env.us')

api_key = os.getenv('FINNHUB_API_KEY')
api = finnhub.Client(api_key=api_key)

# 测试获取 AAPL 报价
quote = api.quote('AAPL')
print(f'AAPL: ${quote[\"c\"]:.2f}')
"
```

---

## 二、监控脚本使用

### 启动监控
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
python3.8 scripts/us_stock_monitor_finnhub.py
```

### 后台运行
```bash
nohup python3.8 scripts/us_stock_monitor_finnhub.py > logs/monitor_finnhub.log 2>&1 &

# 查看进程
ps aux | grep us_stock_monitor

# 查看日志
tail -f logs/monitor_finnhub.log

# 停止进程
pkill -f us_stock_monitor_finnhub
```

### 定时任务 (systemd)
创建 `/etc/systemd/system/ftseek_us_finnhub.service`:

```ini
[Unit]
Description=FTSeek-US Stock Monitor (Finnhub)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ftseek/FTSeek-US
Environment="PATH=/opt/ftseek/FTSeek-US/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/ftseek/FTSeek-US/venv/bin/python3.8 /opt/ftseek/FTSeek-US/scripts/us_stock_monitor_finnhub.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动:
```bash
systemctl daemon-reload
systemctl enable ftseek_us_finnhub
systemctl start ftseek_us_finnhub
systemctl status ftseek_us_finnhub
```

---

## 三、配置说明

### 持仓配置
编辑 `scripts/us_stock_monitor_finnhub.py`:

```python
POSITIONS = {
    "AAPL": {
        "cost": 170.0,      # 成本价
        "qty": 100,         # 持仓数量
        "add_times": 0,     # 已补仓次数
        "last_add_price": 170.0,  # 上次补仓价格
    },
    "MSFT": {
        "cost": 400.0,
        "qty": 50,
        "add_times": 0,
        "last_add_price": 400.0,
    },
}
```

### 止损止盈配置
编辑 `.env.us`:

```bash
# 止损止盈：放宽幅度 (中长线)
DEFAULT_STOP_LOSS=0.10        # -10%
DEFAULT_TAKE_PROFIT_1=0.15    # +15%
DEFAULT_TAKE_PROFIT_2=0.25    # +25%
DEFAULT_TAKE_PROFIT_3=0.40    # +40%
```

### 补仓配置
编辑 `.env.us`:

```bash
# 补仓配置
ADD_POSITION_RATIO=0.05       # 下跌 5% 补仓
MAX_ADD_POSITION_TIMES=3      # 最多补仓 3 次
```

### 查询间隔
编辑脚本:

```python
QUERY_INTERVAL = 60  # 每分钟查询一次 (Finnhub 免费 60 次/分钟)
```

---

## 四、告警说明

### 告警类型
| 类型 | 触发条件 | 级别 |
|------|----------|------|
| **STOP_LOSS** | 当前价 ≤ 止损价 | CRITICAL |
| **STOP_LOSS_WARNING** | 当前价 ≤ 止损价×1.02 | WARN |
| **TAKE_PROFIT_1** | 当前价 ≥ 第一止盈价 | INFO |
| **TAKE_PROFIT_2** | 当前价 ≥ 第二止盈价 | INFO |
| **TAKE_PROFIT_3** | 当前价 ≥ 第三止盈价 | INFO |
| **ADD_POSITION** | 当前价 ≤ 上次补仓价×0.95 | WARN |

### 告警示例
```
⚠️ STOP_LOSS - AAPL
级别：CRITICAL

AAPL 触发止损！当前价 $152.00, 止损价 $153.00

💰 ADD_POSITION - AAPL
级别：WARN

AAPL 触发补仓！下跌 5% 触发补仓
当前价：$161.50
触发价：$161.50
已补仓：1/3 次
```

---

## 五、中长线交易逻辑

### 止盈策略
```
成本价：$100

第一止盈：$115 (+15%) - 卖出 30%
第二止盈：$125 (+25%) - 卖出 30%
第三止盈：$140 (+40%) - 卖出 40%
```

### 补仓策略
```
成本价：$100

第一次补仓：$95 (下跌 5%) - 补仓 1/3
第二次补仓：$90.25 (再跌 5%) - 补仓 1/3
第三次补仓：$85.74 (再跌 5%) - 补仓 1/3

最多补仓 3 次，避免无限补仓
```

### 止损策略
```
成本价：$100
止损价：$90 (-10%)

触发止损后:
1. 发送 CRITICAL 告警
2. 建议手动平仓或自动平仓
```

---

## 六、Finnhub API 限制

### 免费计划
- **调用限制**: 60 次/分钟
- **推荐查询间隔**: 60 秒 (监控 8 只股票)
- **数据延迟**: 实时

### 付费计划
- **Starter**: $59/月 (600 次/分钟)
- **Professional**: $169/月 (3000 次/分钟)

### 优化建议
1. **查询间隔**: 根据持仓数量调整
   - 8 只股票 → 60 秒
   - 16 只股票 → 120 秒

2. **交易时间**: 只在美东时间 9:30-16:00 查询
   - 北京时间：22:30-05:00

3. **缓存数据**: 本地缓存最新价格，减少 API 调用

---

## 七、故障排查

### 问题 1: API Key 无效
```bash
# 检查配置
cat .env.us | grep FINNHUB

# 测试 API
python3.8 -c "
import finnhub
api = finnhub.Client(api_key='your_key')
print(api.quote('AAPL'))
"
```

### 问题 2: 达到调用限制
```
错误：429 Too Many Requests

解决:
1. 增加查询间隔 (QUERY_INTERVAL)
2. 减少监控股票数量
3. 升级付费计划
```

### 问题 3: 钉钉推送失败
```bash
# 检查配置
cat .env.us | grep DINGTALK

# 测试推送
python3.8 -c "
from app.observability.alert_manager import AlertManager
import os
from dotenv import load_dotenv
load_dotenv('.env.us')
manager = AlertManager(
    provider='dingtalk',
    dingtalk_webhook=os.getenv('DINGTALK_WEBHOOK_URL_US'),
    dingtalk_secret=os.getenv('DINGTALK_SECRET_US')
)
manager.send_info(title='测试', content='测试推送')
"
```

---

## 八、与富途对比

| 功能 | 富途 OpenD | Finnhub |
|------|----------|---------|
| **数据延迟** | 实时 (需权限) | 实时 |
| **费用** | 可能收费 | 免费 (60 次/分钟) |
| **开通难度** | 需要开通权限 | 注册即可 |
| **调用限制** | 无限制 | 60 次/分钟 |
| **数据范围** | 全球市场 | 全球市场 |
| **集成难度** | 低 (已有代码) | 中 (需要适配) |
| **推荐度** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |

---

## 九、文档索引

| 文档 | 路径 |
|------|------|
| Finnhub 配置指南 | `docs/FINNHUB_CONFIGURATION_GUIDE.md` |
| 富途权限解决方案 | `docs/FUTU_US_MARKET_SOLUTION.md` |
| 监控指南 | `docs/US_STOCK_MONITOR_GUIDE.md` |
| 监控总结 | `docs/US_STOCK_MONITOR_SUMMARY.md` |

---

## 十、总结

### 当前状态
| 功能 | 状态 | 说明 |
|------|------|------|
| Finnhub 注册 | ⏳ | 需要用户注册 |
| API Key 配置 | ⏳ | 需要配置到.env.us |
| 监控脚本 | ✅ | 已创建 |
| 止损止盈逻辑 | ✅ | 已实现 |
| 补仓逻辑 | ✅ | 已实现 |
| 钉钉告警 | ✅ | 已测试通过 |

### 下一步
1. ⏳ 注册 Finnhub 账号
2. ⏳ 获取 API Key
3. ⏳ 配置到 .env.us
4. ⏳ 安装 finnhub-python
5. ⏳ 测试运行

---

**文档版本**: 1.0  
**最后更新**: 2026-03-24  
**维护人**: AI Assistant
