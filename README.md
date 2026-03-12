# FTSeek V4 - 智能量化交易系统

> 🤖 基于富途牛牛的自动化量化交易平台 | V4.0.0

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/status-production-green.svg)]()

## 📖 目录

- [简介](#简介)
- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [统一决策引擎 (V4 新增)](#统一决策引擎-v4 新增)
- [系统架构](#系统架构)
- [策略引擎](#策略引擎)
- [风控系统](#风控系统)
- [文档](#文档)
- [测试状态](#测试状态)

## 🎯 简介

FTSeek 是一个基于富途牛牛 API 的智能量化交易系统，支持自动化的股票交易、实时监控和风险管理。V3 版本进行了全面重构，采用模块化架构，提供更强大的策略引擎和风控系统。

**适用场景:**
- ✅ 港股自动化交易
- ✅ 多策略组合投资
- ✅ 实时风险监控
- ✅ 交易报告生成
- ✅ 钉钉实时推送

## ⭐ 核心特性

### 🧠 三策略引擎

| 策略 | 类型 | 原理 | 适用场景 |
|------|------|------|----------|
| **PriceBreak** | 突破策略 | 价格突破区间边界 | 趋势行情 |
| **RSIRebound** | 反弹策略 | RSI 超卖反弹 | 震荡行情 |
| **MACross** | 均线策略 | 均线金叉死叉 | 中长期趋势 |

支持策略信号融合，综合多个策略判断，提高决策准确性。

### 🛡️ 风控系统

- **交易前风控**: 交易次数、仓位比例、可用资金检查
- **交易中风控**: 实时盈亏监控、回撤计算、异常检测
- **交易后风控**: 日终统计、连续亏损评估、熔断决策

**风控指标:**
- 每日最大交易次数
- 每日最大亏损金额
- 最大回撤限制
- 持仓比例限制
- 连续亏损熔断

### 📊 报告系统

- **日报**: 每日交易统计、盈亏分析
- **周报**: 周度表现、策略归因
- **月报**: 月度总结、夏普比率、索提诺比率

### 📱 钉钉推送

- 实时交易通知
- 定时风险监控
- 熔断紧急警报
- 定期报告推送

## 🚀 快速开始

### 1. 环境准备

```bash
cd /opt/ftseek/FTSeek
pip3 install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
vim .env
```

**必填配置:**
```bash
# 安全 Token
export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

# 富途配置
FUTU_PASSWORD=你的交易密码
FUTU_HOST=127.0.0.1
FUTU_PORT=11111

# 钉钉推送 (可选)
DINGTALK_WEBHOOK=你的 webhook
DINGTALK_SECRET=你的加签密钥
```

### 3. 初始化并启动

```bash
python3 scripts/init_db.py
./start.sh
```

详细指南请查看 [QUICKSTART_V3.md](QUICKSTART_V3.md)

## 🎯 统一决策引擎 (V4 新增)

> V4 版本新增统一决策引擎，提供智能化交易决策和自动执行能力

### 核心功能

| 功能 | 说明 |
|------|------|
| **信号限流** | 每只股票最多 2 个信号/小时，避免过度交易 |
| **模拟交易** | 纸面交易验证策略有效性，零风险测试 |
| **自动止盈止损** | 收益≥5% 自动止盈，亏损≥3% 自动止损 |
| **市场时段管理** | 港股/美股独立运行，自动识别交易时段 |
| **定时报告** | 日报/周报自动推送，交易汇总一目了然 |

### 交易模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| **PAPER** | 模拟模式，所有交易为纸面交易 | 策略验证、新手入门 |
| **HYBRID** | 混合模式，高置信度走实盘 | 渐进式实盘 |
| **LIVE** | 实盘模式，真实交易 | 策略成熟稳定 |
| **DISABLED** | 禁用模式，暂停交易 | 市场异常、系统维护 |

### 快速部署

```bash
# 一键部署统一决策引擎
bash scripts/deploy_trading_engine.sh

# 验证部署
curl http://localhost:8900/api/v2/health
```

### API 端点

```bash
# 查询持仓
curl -H "X-API-Key: your-key" http://localhost:8900/api/v2/positions

# 查询绩效
curl -H "X-API-Key: your-key" "http://localhost:8900/api/v2/performance?code=0700.HK"

# 切换模式
curl -X POST -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{"mode": "PAPER"}' \
  http://localhost:8900/api/v2/mode/0700.HK

# 手动平仓
curl -X POST -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://localhost:8900/api/v2/manual_exit/0700.HK
```

### 文档

- 📘 [用户指南](docs/trading_decision_user_guide.md) - 功能介绍、API 使用、常见问题
- 🔧 [运维指南](docs/trading_decision_ops_guide.md) - 系统架构、监控、故障排查

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────┐
│                    FTSeek V4                         │
├─────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │  策略引擎   │  │  风控系统   │  │  交易执行   │  │
│  │  Strategy   │  │    Risk     │  │   Trade     │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │  新闻分析   │  │  报告生成   │  │  钉钉推送   │  │
│  │   News      │  │  Reporter   │  │  DingTalk   │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
├─────────────────────────────────────────────────────┤
│              统一决策引擎 (V4 新增)                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │  信号限流   │  │  模拟交易   │  │  止盈止损   │  │
│  │ RateLimiter │  │PaperTrader  │  │ExitManager  │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
├─────────────────────────────────────────────────────┤
│                   富途牛牛 API                        │
└─────────────────────────────────────────────────────┘
```

### 目录结构

```
FTSeek/
├── app/
│   ├── core/              # 核心模块
│   │   ├── trade_executor.py    # 交易执行器
│   │   └── order_manager.py     # 订单管理
│   ├── strategies/        # 策略引擎
│   │   ├── __init__.py
│   │   ├── price_break.py       # 突破策略
│   │   ├── rsi_rebound.py       # 反弹策略
│   │   └── ma_cross.py          # 均线策略
│   ├── risk/              # 风控系统
│   │   ├── risk_manager.py      # 风控管理器
│   │   └── risk_monitor.py      # 风险监控
│   ├── analyzers/         # 分析器
│   │   └── news_analyzer.py     # 新闻分析
│   ├── reporters/         # 报告生成
│   │   ├── daily_stats.py       # 日报
│   │   ├── weekly.py            # 周报
│   │   └── monthly.py           # 月报
│   ├── notifier/          # 通知推送
│   │   └── dingtalk.py          # 钉钉推送
│   └── storage/           # 数据存储
│       └── database.py          # 数据库
├── config/                # 配置文件
├── scripts/               # 工具脚本
├── data/                  # 数据文件
├── logs/                  # 日志文件
└── docs/                  # 文档
```

## 📈 策略引擎

### 策略配置

在 `config/strategy_config.yaml` 中配置：

```yaml
strategies:
  price_break:
    enabled: true
    weight: 0.5           # 权重 50%
    params:
      lookback_period: 20
      break_threshold: 0.02
      
  rsi_rebound:
    enabled: true
    weight: 0.3           # 权重 30%
    params:
      rsi_period: 14
      oversold_threshold: 30
      
  ma_cross:
    enabled: true
    weight: 0.2           # 权重 20%
    params:
      short_period: 5
      long_period: 20
```

详细说明请查看 [STRATEGY_CONFIG.md](docs/STRATEGY_CONFIG.md)

## 🛡️ 风控系统

### 风控配置

在 `config/risk_config.yaml` 中配置：

```yaml
risk_management:
  max_daily_trades: 10          # 每日最大交易次数
  max_daily_loss: 5000          # 每日最大亏损 (HKD)
  max_drawdown: 0.15            # 最大回撤 (15%)
  max_position_ratio: 0.8       # 最大持仓比例
  
  monitor:
    enabled: true
    push_times: ["10:00", "12:00", "14:00", "16:00"]
```

详细说明请查看 [RISK_CONFIG.md](docs/RISK_CONFIG.md)

## 📚 文档

### V4 统一决策引擎

| 文档 | 说明 |
|------|------|
| [docs/trading_decision_user_guide.md](docs/trading_decision_user_guide.md) | 用户指南 - 功能介绍、API 使用 |
| [docs/trading_decision_ops_guide.md](docs/trading_decision_ops_guide.md) | 运维指南 - 监控、故障排查 |

### 核心文档

| 文档 | 说明 |
|------|------|
| [QUICKSTART_V3.md](QUICKSTART_V3.md) | 快速启动指南 |
| [docs/UPGRADE_TO_V3.md](docs/UPGRADE_TO_V3.md) | V2 到 V3 升级指南 |
| [docs/RISK_CONFIG.md](docs/RISK_CONFIG.md) | 风控配置详解 |
| [docs/STRATEGY_CONFIG.md](docs/STRATEGY_CONFIG.md) | 策略配置详解 |

## ✅ 测试状态

### 模块测试

```bash
# 风控管理器
python3 -c "from app.risk.risk_manager import RiskManager"
# ✓ 风控管理器导入成功

# 新闻分析器
python3 -c "from app.analyzers.news_analyzer import NewsAnalyzer"
# ✓ 新闻分析器导入成功

# 三策略引擎
python3 -c "from app.strategies import *"
# ✓ 三策略引擎导入成功

# 交易执行器
python3 -c "from app.core.trade_executor import TradeExecutor"
# ✓ 交易执行器导入成功

# 钉钉推送
python3 -c "from app.notifier.dingtalk import DingTalkNotifier"
# ✓ 钉钉推送导入成功

# 收盘统计
python3 -c "from app.reporters.daily_stats import DailyStatsReporter"
# ✓ 收盘统计导入成功
```

### 集成测试

```bash
# 报告生成测试
python3 scripts/test_reports.py
# ✅ 所有测试通过!

# 风险监控测试
python3 scripts/test_risk_monitor.py
# 🎉 所有测试通过!
```

## 🔧 开发

### 运行测试

```bash
# 单元测试
python3 scripts/test_reports.py
python3 scripts/test_risk_monitor.py

# 集成测试
python3 test_integration.py
```

### 代码风格

- 遵循 PEP 8 规范
- 使用 type hints
- 编写单元测试
- 提交前运行测试

## 📄 许可证

MIT License - 详见 [LICENSE](LICENSE) 文件

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📞 支持

如有问题，请查看文档或提交 Issue。

---

*最后更新：2026-03-12 | 版本：V4.0.0*
