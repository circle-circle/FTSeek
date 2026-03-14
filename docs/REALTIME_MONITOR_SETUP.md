# FTSeek 实时行情监控配置说明

## 📋 功能概述

实时监控股港行情，策略触发后推送到钉钉群，人工确认后执行交易。

| 功能 | 说明 |
|------|------|
| **实时行情订阅** | 通过 OpenD 订阅 1 分钟 K 线 |
| **策略实时计算** | 均线/RSI/MACD 多策略并行 |
| **钉钉确认推送** | 带确认/忽略按钮 |
| **人工确认执行** | 先期模拟模式，后期可切实盘 |

---

## 🚀 快速开始

### 1. 检查 OpenD 状态

```bash
# 检查 OpenD 是否运行
ps aux | grep FutuOpenD

# 检查端口
ss -tlnp | grep 11111
```

### 2. 启动实时行情监控

```bash
cd /opt/ftseek/FTSeek
export $(cat .env | grep -v '^#' | xargs)

# 测试模式 (前台运行)
python3 scripts/realtime_monitor.py

# 后台运行
nohup python3 scripts/realtime_monitor.py >> logs/realtime_monitor.log 2>&1 &

# 或使用启动脚本
./scripts/start_realtime_monitor.sh
```

### 3. 交易执行 (人工确认)

收到钉钉推送后，点击确认按钮会自动调用执行器：

```bash
# 模拟模式执行买入
python3 scripts/trade_executor_manual.py --signal-id xxx --action BUY --mode paper

# 实盘模式执行卖出
python3 scripts/trade_executor_manual.py --signal-id xxx --action SELL --mode real
```

---

## ⚙️ 配置说明

### 环境变量 (.env)

```bash
# 富途配置
FUTU_HOST=127.0.0.1
FUTU_PORT=11111
FUTU_PASSWORD=你的交易密码

# OpenD 配置
OPEND_HOST=127.0.0.1
OPEND_PORT=11111

# 钉钉推送
DINGTALK_WEBHOOK_URL=https://...
DINGTALK_SECRET=SEC...

# 交易模式 (paper=模拟，real=实盘)
FTSEEK_TRADING_MODE=paper

# 安全 Token
API_TOKEN=xxx
```

### 关注股票

编辑 `scripts/realtime_monitor.py`:

```python
WATCHLIST = [
    'HK.00700',  # 腾讯控股
    'HK.09988',  # 阿里巴巴
    'HK.09992',  # 泡泡玛特
    'HK.02390',  # 智谱 AI
    'HK.03690',  # 美团
]
```

### 策略配置

```python
STRATEGIES_CONFIG = {
    'ma_cross': {
        'enabled': True,
        'fast_period': 5,
        'slow_period': 20,
    },
    'rsi_rebound': {
        'enabled': True,
        'rsi_period': 14,
        'oversold_threshold': 30,
    },
    'macd': {
        'enabled': True,
        'fast_period': 12,
        'slow_period': 26,
        'signal_period': 9,
    },
}
```

---

## 📱 钉钉推送流程

### 1. 策略触发信号

```
🟢 交易信号确认

腾讯控股 (HK.00700)

方向：确认买入
策略：均线交叉
价格：380.50 HKD
数量：100 股
原因：MA5(382.00) 上穿 MA20(378.50), 金叉买入

🕒 时间：2026-03-02 10:30:00
📊 模式：模拟

[✅ 确认买入] [❌ 忽略]
```

### 2. 点击确认按钮

- 点击 **确认买入** → 调用执行器下单
- 点击 **忽略** → 跳过该信号

### 3. 执行结果推送

```
✅ 交易执行结果

腾讯控股 (HK.00700)

方向：买入
状态：成功
模式：模拟

订单 ID: PAPER_xxx
价格：380.50 HKD
数量：100 股

🕒 时间：2026-03-02 10:31:00
```

---

## 🔧 系统服务配置

### 创建 systemd 服务

```bash
sudo tee /etc/systemd/system/ftseek-realtime.service > /dev/null <<'EOF'
[Unit]
Description=FTSeek Realtime Monitor
After=network.target futu-opend.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ftseek/FTSeek
EnvironmentFile=/opt/ftseek/FTSeek/.env
ExecStart=/usr/bin/python3 scripts/realtime_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 重载 systemd
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start ftseek-realtime

# 设置开机自启
sudo systemctl enable ftseek-realtime

# 查看状态
sudo systemctl status ftseek-realtime

# 查看日志
sudo journalctl -u ftseek-realtime -f
```

---

## 📊 信号状态

| 状态 | 说明 |
|------|------|
| `pending_confirmation` | 待人工确认 |
| `executed` | 已执行 |
| `failed` | 执行失败 |
| `ignored` | 已忽略 |

---

## 📝 常用命令

### 查看实时日志

```bash
tail -f /opt/ftseek/FTSeek/logs/realtime_monitor.log
```

### 查询待确认信号

```bash
python3 scripts/query_signals.py --status pending_confirmation --limit 10
```

### 手动执行信号

```bash
# 查询信号 ID
python3 scripts/query_signals.py --stock HK0700 --limit 5

# 执行交易
python3 scripts/trade_executor_manual.py --signal-id xxx --action BUY
```

### 切换交易模式

```bash
# 模拟模式
export FTSEEK_TRADING_MODE=paper

# 实盘模式
export FTSEEK_TRADING_MODE=real
```

---

## ⚠️ 注意事项

### 模拟模式 (paper)

- ✅ 不实际下单
- ✅ 生成模拟订单 ID
- ✅ 适合测试和验证

### 实盘模式 (real)

- ⚠️ 实际下单交易
- ⚠️ 需要正确配置交易密码
- ⚠️ 请谨慎使用

### 安全建议

1. **先期使用模拟模式** 验证策略
2. **小额测试** 确认流程正常
3. **定期检查** 信号和执行记录
4. **设置止损** 控制风险

---

## 🚨 故障排查

### OpenD 连接失败

```bash
# 检查 OpenD 状态
ps aux | grep FutuOpenD

# 检查端口
ss -tlnp | grep 11111

# 重启 OpenD
cd /opt/ftseek/Futu_OpenD_*
./FutuOpenD
```

### 钉钉推送失败

```bash
# 测试推送
python3 scripts/test_dingtalk_enhanced.py

# 检查 Webhook 配置
cat .env | grep DINGTALK
```

### 策略无信号

1. 检查 K 线数据是否正常
2. 调整策略参数
3. 查看日志确认策略运行状态

---

## 📈 监控指标

### 信号统计

```bash
# 今日信号数
python3 scripts/query_signals.py --from $(date +%Y-%m-%d) | wc -l

# 待确认信号
python3 scripts/query_signals.py --status pending_confirmation
```

### 执行统计

```bash
# 已执行信号
python3 scripts/query_signals.py --status executed

# 成功率
python3 -c "
from app.storage.database import Database
db = Database()
with db.get_connection() as conn:
    c = conn.cursor()
    c.execute(\"SELECT status, COUNT(*) FROM signals GROUP BY status\")
    for row in c.fetchall():
        print(f'{row[0]}: {row[1]}')
"
```

---

*最后更新：2026-03-02 | FTSeek V3*
