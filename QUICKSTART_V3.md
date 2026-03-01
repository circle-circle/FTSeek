# FTSeek V3 快速启动指南

> 5 分钟快速上手 FTSeek V3 | 2026-03-01

## 🚀 快速开始

### 1️⃣ 环境准备

```bash
# 进入项目目录
cd /opt/ftseek/FTSeek

# 检查 Python 版本 (需要 3.8+)
python3 --version

# 安装依赖
pip3 install -r requirements.txt
```

### 2️⃣ 配置环境变量

```bash
# 复制示例配置
cp .env.example .env

# 编辑配置文件
vim .env
```

**必填配置:**

```bash
# 🔐 安全 Token (必须生成)
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

# 📈 富途牛牛配置
FUTU_PASSWORD=你的交易密码
FUTU_HOST=127.0.0.1
FUTU_PORT=11111

# 📱 钉钉推送 (可选)
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN
DINGTALK_SECRET=你的加签密钥

# ⚠️ 风控配置
RISK_MAX_DAILY_TRADES=10
RISK_MAX_DAILY_LOSS=5000
RISK_MAX_DRAWDOWN=0.15
```

### 3️⃣ 初始化数据库

```bash
python3 scripts/init_db.py
```

### 4️⃣ 验证安装

```bash
# 测试模块导入
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

python3 -c "from app.risk.risk_manager import RiskManager; print('✓ 风控 OK')"
python3 -c "from app.strategies import *; print('✓ 策略 OK')"
python3 -c "from app.core.trade_executor import TradeExecutor; print('✓ 交易 OK')"

# 运行测试
python3 scripts/test_reports.py
python3 scripts/test_risk_monitor.py
```

### 5️⃣ 启动服务

```bash
# 方式 1: 使用启动脚本
./start.sh

# 方式 2: 直接运行
python3 app/main.py

# 方式 3: systemd 服务 (生产环境)
systemctl start ftseek
```

## 📁 项目结构

```
FTSeek/
├── app/                    # 应用代码
│   ├── core/              # 核心模块
│   ├── strategies/        # 策略引擎
│   ├── risk/              # 风控系统
│   ├── analyzers/         # 分析器
│   ├── reporters/         # 报告生成
│   ├── notifier/          # 通知推送
│   └── storage/           # 数据存储
├── config/                 # 配置文件
├── scripts/                # 工具脚本
├── data/                   # 数据文件
├── logs/                   # 日志文件
└── docs/                   # 文档
```

## ⚙️ 核心配置

### 策略配置 (`config/strategy_config.yaml`)

```yaml
strategies:
  price_break:
    enabled: true
    weight: 0.5
  rsi_rebound:
    enabled: true
    weight: 0.3
  ma_cross:
    enabled: true
    weight: 0.2
```

### 风控配置 (`config/risk_config.yaml`)

```yaml
risk_management:
  max_daily_trades: 10
  max_daily_loss: 5000
  max_drawdown: 0.15
  max_position_ratio: 0.8
```

## 📊 常用命令

### 交易相关

```bash
# 查看持仓
python3 scripts/show_positions.py

# 执行交易
python3 scripts/execute_trade.py --stock HK.00700 --action buy --qty 100

# 查看订单
python3 scripts/show_orders.py
```

### 报告相关

```bash
# 生成日报
python3 scripts/generate_daily_report.py

# 生成周报
python3 scripts/generate_weekly_report.py

# 生成月报
python3 scripts/generate_monthly_report.py
```

### 监控相关

```bash
# 查看实时风控
python3 scripts/show_risk_status.py

# 查看策略信号
tail -f logs/strategy_signals.log

# 查看交易日志
tail -f logs/trades.log
```

## 📱 钉钉推送配置

### 1. 创建钉钉机器人

1. 打开钉钉群 → 群设置 → 智能助手 → 添加机器人
2. 选择"自定义"机器人
3. 设置 webhook 和加签密钥
4. 安全设置：添加 IP 白名单 (可选)

### 2. 配置环境变量

```bash
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
DINGTALK_SECRET=你的加签密钥
```

### 3. 测试推送

```bash
python3 scripts/test_dingtalk.py
```

## 🧪 测试与回测

### 运行测试

```bash
# 单元测试
python3 scripts/test_reports.py
python3 scripts/test_risk_monitor.py

# 集成测试
python3 test_integration.py
```

### 策略回测

```bash
# 运行回测示例
python3 backtest_example.py

# 参数优化
python3 optimize_example.py
```

## 📈 监控与日志

### 日志文件

| 日志 | 路径 | 说明 |
|------|------|------|
| 交易日志 | `logs/trades.log` | 所有交易记录 |
| 策略日志 | `logs/strategy_signals.log` | 策略信号 |
| 风控日志 | `logs/risk_monitor.log` | 风控事件 |
| 系统日志 | `logs/system.log` | 系统运行 |

### 实时监控

```bash
# 实时查看所有日志
tail -f logs/*.log

# 查看今日错误
grep "ERROR" logs/*.log | grep "$(date +%Y-%m-%d)"
```

## ⚠️ 注意事项

### 安全提示

1. **API_TOKEN**: 每次启动前生成，不要硬编码
2. **交易密码**: 不要提交到版本控制
3. **配置文件**: 生产环境使用独立配置

### 风控建议

1. **新手**: 从模拟盘开始，熟悉后再实盘
2. **资金**: 首次投入不超过总资金的 10%
3. **参数**: 使用保守型风控配置
4. **监控**: 开启钉钉推送，实时监控

### 常见问题

**Q: 启动时提示 API_TOKEN 未设置？**
```bash
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
```

**Q: 富途连接失败？**
- 检查富途牛牛是否运行
- 检查 host/port 配置
- 检查交易密码是否正确

**Q: 钉钉推送失败？**
- 检查 webhook URL 是否正确
- 检查加签密钥是否配置
- 检查网络连接

## 📚 更多文档

- [升级指南](docs/UPGRADE_TO_V3.md) - 从 V2 升级到 V3
- [风控配置](docs/RISK_CONFIG.md) - 风控系统详解
- [策略配置](docs/STRATEGY_CONFIG.md) - 三策略引擎详解
- [README](README.md) - 项目总览

## 🎯 下一步

1. ✅ 完成快速启动
2. 📖 阅读策略配置文档
3. ⚙️ 调整风控参数
4. 🧪 运行回测验证
5. 💰 小资金试运行
6. 📊 监控表现并优化

---

*最后更新：2026-03-01 | 版本：V3.0.0*
