# FTSeek-US 双数据源监控配置指南

**创建日期**: 2026-03-24 20:25  
**状态**: ✅ 可用

---

## 一、数据源说明

### TwelveData
- **免费版限制**: 800 次/天，8 次/分钟
- **API**: `https://api.twelvedata.com/price`
- **优势**: 数据准确，支持全球市场
- **劣势**: 每日有限制

### Finnhub
- **免费版限制**: 60 次/分钟
- **API**: `https://finnhub.io/api/v1/quote`
- **优势**: 每分钟调用次数多
- **劣势**: 无每日限制但有分钟限制

### 轮询策略
```
优先使用：Finnhub (60 次/分钟)
备用数据：TwelveData (8 次/分钟，800 次/天)

策略:
1. 优先使用 Finnhub
2. Finnhub 限流时切换到 TwelveData
3. TwelveData 限流时等待
4. 自动切换，确保监控不中断
```

---

## 二、API Key 配置

### 已配置 API Keys
```bash
# TwelveData API Key
TWELVEDATA_API_KEY=c359be1c550f4c2b8240eaf62fcb8536

# Finnhub API Key
FINNHUB_API_KEY=d604po9r01qihi8ofon0d604po9r01qihi8ofong
```

### 配置文件
编辑 `/opt/ftseek/FTSeek-US/.env.us`:

```bash
# 行情数据源 API Keys
TWELVEDATA_API_KEY=your_twelvedata_key
FINNHUB_API_KEY=your_finnhub_key
```

---

## 三、请求频率控制

### 限流设置
```python
# TwelveData: 8 次/分钟
twelvedata_limiter = RateLimiter(max_calls=8, period_seconds=60)

# Finnhub: 60 次/分钟
finnhub_limiter = RateLimiter(max_calls=60, period_seconds=60)
```

### 使用示例
```python
# 监控股票：8 只
# 查询间隔：60 秒
# 每小时请求：8 × 60 = 480 次/小时

# TwelveData 限制：800 次/天 ≈ 33 次/小时
# Finnhub 限制：60 次/分钟 = 3600 次/小时

# 策略：
# - 优先使用 Finnhub (充足)
# - TwelveData 作为备用 (每小时最多 30 次)
# - 确保不触发限流
```

### 监控剩余次数
```bash
[2026-03-24 22:30:00] 📡 获取行情... (TwelveData 剩余：7 次，Finnhub 剩余：52 次)
```

---

## 四、使用方法

### 启动监控
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
python3.8 scripts/us_stock_monitor_dual.py
```

### 后台运行
```bash
nohup python3.8 scripts/us_stock_monitor_dual.py > logs/monitor_dual.log 2>&1 &

# 查看进程
ps aux | grep us_stock_monitor_dual

# 查看日志
tail -f logs/monitor_dual.log

# 停止进程
pkill -f us_stock_monitor_dual
```

### 定时任务 (systemd)
创建 `/etc/systemd/system/ftseek_us_dual.service`:

```ini
[Unit]
Description=FTSeek-US Stock Monitor (Dual Source)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ftseek/FTSeek-US
Environment="PATH=/opt/ftseek/FTSeek-US/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/ftseek/FTSeek-US/venv/bin/python3.8 /opt/ftseek/FTSeek-US/scripts/us_stock_monitor_dual.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动:
```bash
systemctl daemon-reload
systemctl enable ftseek_us_dual
systemctl start ftseek_us_dual
systemctl status ftseek_us_dual
```

---

## 五、配置说明

### 持仓配置
编辑 `scripts/us_stock_monitor_dual.py`:

```python
POSITIONS = {
    "AAPL": {"cost": 170.0, "qty": 100, "add_times": 0},
    "MSFT": {"cost": 400.0, "qty": 50, "add_times": 0},
    "NVDA": {"cost": 800.0, "qty": 30, "add_times": 0},
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
QUERY_INTERVAL = 60  # 每分钟查询一次
```

**注意**: 
- 不要设置过短 (< 60 秒)，避免触发限流
- 8 只股票 × 60 秒 = 480 次/小时，在安全范围内

---

## 六、告警说明

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

## 七、故障排查

### 问题 1: API Key 无效
```bash
# 测试 TwelveData
curl "https://api.twelvedata.com/price?symbol=AAPL&apikey=c359be1c550f4c2b8240eaf62fcb8536"

# 测试 Finnhub
curl "https://finnhub.io/api/v1/quote?symbol=AAPL&token=d604po9r01qihi8ofon0d604po9r01qihi8ofong"
```

### 问题 2: 触发限流
```
错误：429 Too Many Requests

解决:
1. 增加查询间隔 (QUERY_INTERVAL)
2. 减少监控股票数量
3. 检查限流器是否正常工作
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

## 八、API 使用统计

### 查看使用量
```bash
# TwelveData
curl "https://api.twelvedata.com/account?apikey=c359be1c550f4c2b8240eaf62fcb8536"

# Finnhub
# 免费版无使用量查询，需自行记录
```

### 日志统计
```bash
# 查看每小时请求次数
grep "获取行情" logs/monitor_dual.log | wc -l
```

---

## 九、文档索引

| 文档 | 路径 |
|------|------|
| 双数据源监控指南 | `docs/DUAL_SOURCE_MONITOR_GUIDE.md` |
| Finnhub 配置指南 | `docs/FINNHUB_CONFIGURATION_GUIDE.md` |
| 富途权限解决方案 | `docs/FUTU_US_MARKET_SOLUTION.md` |
| 监控指南 | `docs/US_STOCK_MONITOR_GUIDE.md` |

---

## 十、总结

### 当前状态
| 功能 | 状态 | 说明 |
|------|------|------|
| TwelveData API | ✅ | 已配置 API Key |
| Finnhub API | ✅ | 已配置 API Key |
| 双数据源轮询 | ✅ | 已实现 |
| 限流控制 | ✅ | 已实现 |
| 监控脚本 | ✅ | 已创建 |
| 止损止盈逻辑 | ✅ | 已实现 |
| 补仓逻辑 | ✅ | 已实现 |
| 钉钉告警 | ✅ | 已测试通过 |

### 下一步
1. ✅ API Key 已配置
2. ✅ 监控脚本已创建
3. ⏳ 测试运行
4. ⏳ 部署定时任务

---

**文档版本**: 1.0  
**最后更新**: 2026-03-24  
**维护人**: AI Assistant
