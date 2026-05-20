# FTSeek-US 模拟盘监控配置说明

**版本**: 方案 B (修复后策略)  
**更新日期**: 2026-03-27

---

## 📋 策略配置

### 核心参数

| 参数 | 值 | 说明 |
|------|-----|------|
| **ATR 止损倍数** | 2.0× | 动态止损，适应不同波动率 |
| **移动止损激活** | 5% | 盈利≥5% 后激活移动止损 |
| **移动止损 ATR** | 2.0× | 移动止损距离 |
| **止盈档位** | 三档 | +15% / +25% / +40% |
| **信号频率** | 4 小时 | 符合中长线定位 |
| **信心阈值** | 0.65 | 高质量信号过滤 |

### 交易限制

| 限制 | 值 | 说明 |
|------|-----|------|
| **单日最大交易** | 5 笔 | 避免过度交易 |
| **单票日交易上限** | 2 笔 | 避免重复交易 |
| **连续亏损熔断** | 2 笔 | 连亏 2 笔暂停 24 小时 |
| **单品种仓位** | ≤30% | 仓位分散 |
| **总仓位** | ≤80% | 保留 20% 现金 |

---

## 🚀 启动监控

### 方式 1: 使用启动脚本（推荐）

```bash
# 启动模拟盘监控
cd /opt/ftseek/FTSeek-US
bash scripts/start_simulated_monitor.sh

# 查看监控状态
tail -f logs/simulated_monitor.log

# 停止监控
bash scripts/stop_simulated_monitor.sh
```

### 方式 2: 手动启动

```bash
cd /opt/ftseek/FTSeek-US
python3 scripts/us_stock_monitor.py
```

### 方式 3: systemd 服务（生产环境）

```bash
# 创建服务
sudo nano /etc/systemd/system/ftseek-us-monitor.service

# 服务配置
[Unit]
Description=FTSeek-US Simulated Trading Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/ftseek/FTSeek-US
Environment="PATH=/opt/ftseek/FTSeek-US/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/opt/ftseek/FTSeek-US/venv/bin/python3 scripts/us_stock_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target

# 启动服务
sudo systemctl daemon-reload
sudo systemctl enable ftseek-us-monitor
sudo systemctl start ftseek-us-monitor

# 查看状态
sudo systemctl status ftseek-us-monitor
```

---

## 📊 监控股票池

### 当前持仓

| 股票代码 | 持仓数量 | 平均成本 | 止损价 | 目标价 | 建仓日期 |
|---------|---------|---------|--------|--------|---------|
| US.TSLA | 100 | $395.00 | $375.25 (-5%) | $434.50 (+10%) | 2026-03-26 |

### 观察列表

| 股票代码 | 公司名称 | 行业 | 关注理由 |
|---------|---------|------|---------|
| AAPL | 苹果 | 消费电子 | 科技巨头 |
| MSFT | 微软 | 云计算/AI | 科技巨头 |
| NVDA | 英伟达 | AI 芯片 | 科技巨头 |
| GOOGL | 谷歌 | 搜索/AI | 科技巨头 |
| AMZN | 亚马逊 | 电商/云计算 | 科技巨头 |
| META | Meta | 社交媒体 | 科技巨头 |
| BABA | 阿里巴巴 | 电商/云 | 中概股 |
| PDD | 拼多多 | 电商 | 中概股 |

---

## 🔔 告警推送

### 飞书 Webhook

- **Webhook URL**: `https://open.feishu.cn/open-apis/bot/v2/hook/de20f742-1e2b-414c-8874-6473f48eca82`
- **安全模式**: 关键字验证
- **关键字**: `FTSeek`、`交易`

### 告警类型

| 级别 | 触发条件 | 推送内容 |
|------|---------|---------|
| **INFO** | 正常交易、持仓更新 | 交易执行、持仓盈亏 |
| **WARN** | 接近止损、时间止损提醒 | 风险提示 |
| **CRITICAL** | 触发止损、大幅回撤 | 紧急告警 |

### 推送时间

- **小时报告**: 美股交易时段每小时整点 (22:00-23:00, 00:00-03:00)
- **实时告警**: 触发止损/止盈立即推送
- **VIX 告警**: VIX>25 预警，VIX>40 紧急告警
- **持仓跌幅**: 单只持仓跌幅>5% 预警，>8% 紧急告警

---

## 📈 监控指标

### 实时指标

- 持仓股票实时价格（15 分钟延迟）
- 浮动盈亏
- 距离止损/止盈价格

### 定期检查

- 每 5 分钟：查询最新价格
- 每小时：生成交易报告
- 每日：合规性检查

### 风险控制

- VIX 指数监控
- 持仓跌幅监控
- 账户整体回撤监控
- 交易频率限制检查

---

## 📁 文件位置

| 文件 | 路径 | 说明 |
|------|------|------|
| **启动脚本** | `scripts/start_simulated_monitor.sh` | 启动监控 |
| **停止脚本** | `scripts/stop_simulated_monitor.sh` | 停止监控 |
| **监控脚本** | `scripts/us_stock_monitor.py` | 主监控逻辑 |
| **配置文件** | `.env.us` | 环境变量配置 |
| **日志文件** | `logs/simulated_monitor.log` | 运行日志 |
| **PID 文件** | `logs/simulated_monitor.pid` | 进程 ID |

---

## 🔧 故障排查

### 问题 1: 监控无法启动

```bash
# 检查 Python 环境
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
python3 --version

# 检查依赖
pip install yfinance requests python-dotenv

# 检查日志
tail -f logs/simulated_monitor.log
```

### 问题 2: 飞书推送失败

```bash
# 测试飞书 Webhook
cd /opt/ftseek/FTSeek-US
python3 scripts/test_feishu_push.py

# 检查 Webhook 配置
cat .env.us | grep FEISHU
```

### 问题 3: 行情数据异常

```bash
# 检查 Yahoo Finance 连接
python3 -c "import yfinance as yf; print(yf.Ticker('AAPL').fast_info.last_price)"

# 检查 OpenD 服务
pgrep -f opend
```

---

## 📊 性能评估

### 预期表现（基于回测）

| 指标 | 预期值 | 说明 |
|------|-------|------|
| **年化收益率** | ~9.5% | 基于 1 年回测 |
| **最大回撤** | ~65% | 可通过仓位控制降低 |
| **胜率** | ~53% | 超过 50% |
| **盈亏比** | ~1.30 | 盈利交易平均收益更高 |
| **夏普比率** | ~0.95 | 风险调整后收益 |

### 观察期（2 周）

- **第 1 周**: 监控信号质量和推送准确性
- **第 2 周**: 统计胜率和盈亏比
- **2 周后**: 根据实际表现微调参数

---

## 🎯 下一步

### 模拟盘观察期（2 周）

1. **每日检查**:
   - 查看日志文件
   - 检查飞书推送
   - 监控持仓盈亏

2. **每周复盘**:
   - 统计交易胜率
   - 分析亏损交易
   - 调整参数（如需要）

3. **2 周后评估**:
   - 胜率 > 45% → 继续优化
   - 胜率 > 50% → 考虑实盘
   - 胜率 < 40% → 重新调整策略

### 实盘部署条件

- ✅ 模拟盘胜率 > 50%（2 周观察）
- ✅ 最大回撤 < 40%
- ✅ 夏普比率 > 0.8
- ✅ 飞书推送准确率 > 95%

---

**祝交易顺利！** 🇺🇸📈
