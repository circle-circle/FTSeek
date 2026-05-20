# FTSeek-US 美股行情监控指南

**更新日期**: 2026-03-24  
**状态**: ✅ 可用

---

## 一、功能说明

### 核心功能
1. **实时行情获取**: 通过富途接口或 Yahoo Finance 获取美股价格
2. **定期查询**: 按设定间隔自动查询监控股票池
3. **止损止盈计算**: 根据持仓成本自动计算止损止盈价格
4. **告警推送**: 触发止损止盈时通过钉钉推送告警

### 数据源优先级
| 数据源 | 延迟 | 权限要求 | 状态 |
|--------|------|----------|------|
| 富途 OpenD | 实时 | 需要美股行情权限 | ⚠️ 需配置 |
| Yahoo Finance | 15 分钟 | 无需权限 | ✅ 可用 |

---

## 二、快速开始

### 1. 安装依赖
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate

# 安装 Yahoo Finance (备用数据源)
pip install yfinance
```

### 2. 配置持仓
编辑 `scripts/us_stock_monitor.py`:

```python
# 持仓成本 (根据实际持仓修改)
POSITIONS = {
    "AAPL": {"cost": 170.0, "qty": 100},
    "MSFT": {"cost": 400.0, "qty": 50},
    "NVDA": {"cost": 800.0, "qty": 30},
}
```

### 3. 配置止损止盈
编辑 `.env.us`:

```bash
# 止损止盈：放宽幅度
DEFAULT_STOP_LOSS=0.10        # -10% (中长线放宽)
DEFAULT_TAKE_PROFIT_1=0.15    # +15%
DEFAULT_TAKE_PROFIT_2=0.25    # +25%
DEFAULT_TAKE_PROFIT_3=0.40    # +40%
```

### 4. 运行监控
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
python3.8 scripts/us_stock_monitor.py
```

---

## 三、测试

### 测试行情获取
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
python3.8 scripts/test_us_monitor.py
```

### 预期输出
```
================================================================================
  FTSeek-US 美股行情测试
================================================================================

测试股票：['AAPL', 'MSFT', 'NVDA']

📡 使用 Yahoo Finance 获取行情 (15 分钟延迟)
获取 AAPL 行情...
  ✅ AAPL:
     当前价：$175.00
     涨跌：+2.50 (+1.45%)
     开盘：$173.00
     最高：$176.00
     最低：$172.50
     昨收：$172.50

================================================================================
  测试完成
================================================================================
```

---

## 四、告警说明

### 告警类型
| 类型 | 触发条件 | 级别 |
|------|----------|------|
| **STOP_LOSS** | 当前价 ≤ 止损价 | CRITICAL |
| **TAKE_PROFIT_1** | 当前价 ≥ 第一止盈价 | INFO |
| **TAKE_PROFIT_2** | 当前价 ≥ 第二止盈价 | INFO |
| **TAKE_PROFIT_3** | 当前价 ≥ 第三止盈价 | INFO |

### 告警示例
```
⚠️ STOP_LOSS - AAPL
级别：CRITICAL

AAPL 触发止损！当前价 $165.00, 止损价 $153.00

✅ TAKE_PROFIT_1 - MSFT
级别：INFO

MSFT 触发第一止盈！当前价 $460.00, 目标价 $460.00
```

---

## 五、定时任务配置

### 方案 1: systemd 服务 (推荐)

创建服务文件 `/etc/systemd/system/ftseek_us_monitor.service`:

```ini
[Unit]
Description=FTSeek-US Stock Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ftseek/FTSeek-US
Environment="PATH=/opt/ftseek/FTSeek-US/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/opt/ftseek/FTSeek-US/venv/bin/python3.8 /opt/ftseek/FTSeek-US/scripts/us_stock_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务:
```bash
systemctl daemon-reload
systemctl enable ftseek_us_monitor
systemctl start ftseek_us_monitor
systemctl status ftseek_us_monitor
```

### 方案 2: cron 定时任务

编辑 crontab:
```bash
crontab -e
```

添加任务 (每分钟执行):
```bash
* * * * * cd /opt/ftseek/FTSeek-US && source venv/bin/activate && python3.8 scripts/us_stock_monitor.py >> logs/monitor.log 2>&1
```

### 方案 3: 后台运行

```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
nohup python3.8 scripts/us_stock_monitor.py > logs/monitor.log 2>&1 &

# 查看进程
ps aux | grep us_stock_monitor

# 停止进程
pkill -f us_stock_monitor
```

---

## 六、富途行情权限配置

### 问题
测试显示"无权限获取 US.AAPL 的行情"

### 解决方案

#### 方案 1: 开通富途美股行情权限
1. 登录富途牛牛 APP
2. 进入"我的" → "行情权限"
3. 开通"美股实时行情"
4. 重启 OpenD

#### 方案 2: 使用 Yahoo Finance (当前方案)
- 无需额外配置
- 自动 fallback 到 Yahoo Finance
- 15 分钟延迟，适合中长线监控

#### 方案 3: 使用其他免费数据源
```bash
# 编辑 .env.us
DATA_SOURCE=alpha_vantage
ALPHA_VANTAGE_API_KEY=your_key

# 或
DATA_SOURCE=finnhub
FINNHUB_API_KEY=your_key
```

---

## 七、日志查看

### 实时监控日志
```bash
tail -f logs/monitor.log
```

### 查看告警记录
```bash
grep "触发" logs/monitor.log | tail -20
```

### 查看钉钉推送日志
```bash
grep "钉钉推送" logs/monitor.log | tail -20
```

---

## 八、故障排查

### 问题 1: 无法获取行情
```bash
# 检查数据源配置
cat .env.us | grep DATA_SOURCE

# 测试 Yahoo Finance
python3.8 -c "import yfinance; print(yf.Ticker('AAPL').fast_info.last_price)"

# 检查网络连接
ping finance.yahoo.com
```

### 问题 2: 钉钉推送失败
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

### 问题 3: 止损止盈计算错误
```bash
# 检查持仓配置
grep "POSITIONS" scripts/us_stock_monitor.py

# 检查止损止盈配置
cat .env.us | grep -E "STOP_LOSS|TAKE_PROFIT"
```

---

## 九、最佳实践

### 1. 监控频率
- **短线交易**: 30-60 秒
- **中线交易**: 5-15 分钟
- **长线交易**: 1-4 小时

### 2. 止损止盈设置
- **保守**: 止损 -8%, 止盈 +12%/+20%/+30%
- **中等**: 止损 -10%, 止盈 +15%/+25%/+40% (当前配置)
- **激进**: 止损 -15%, 止盈 +20%/+35%/+50%

### 3. 告警级别
- **CRITICAL**: 止损触发，需要立即处理
- **WARN**: 接近止损/止盈，需要关注
- **INFO**: 止盈触发，记录即可

### 4. 数据源选择
- **实时性要求高**: 富途 OpenD (需权限)
- **中长线监控**: Yahoo Finance (免费)
- **生产环境**: 建议配置多个数据源 fallback

---

## 十、文档索引

| 文档 | 路径 |
|------|------|
| 监控指南 | `docs/US_STOCK_MONITOR_GUIDE.md` |
| LLM API 修复报告 | `docs/LLM_API_FIX_REPORT_20260324.md` |
| 配置总结 | `docs/FINAL_CONFIGURATION_20260324.md` |
| 验证报告 | `docs/TRADING_VALIDATION_REPORT_20260324.md` |

---

## 十一、总结

### 当前状态
| 功能 | 状态 | 说明 |
|------|------|------|
| 行情获取 | ✅ | Yahoo Finance (15 分钟延迟) |
| 止损止盈计算 | ✅ | 自动计算 |
| 告警推送 | ✅ | 钉钉推送 |
| 定时任务 | ✅ | systemd/cron/后台运行 |
| 富途实时行情 | ⚠️ | 需要开通权限 |

### 下一步
1. ✅ 测试监控脚本
2. ✅ 配置持仓和止损止盈
3. ✅ 启动定时任务
4. ⚠️ 考虑开通富途美股行情权限 (可选)

---

**文档版本**: 1.0  
**最后更新**: 2026-03-24  
**维护人**: AI Assistant
