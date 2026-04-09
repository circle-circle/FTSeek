# FTSeek 配置中心使用指南

## 配置架构 (v5.0 - 2026-04-09 收敛后)

```
┌─────────────────────────────────────────────────────────┐
│                    FTSeek 配置体系                        │
├─────────────────────────────────────────────────────────┤
│  .env                  config/config.yaml               │
│  ├─ API_TOKEN          ├─ 策略配置                      │
│  ├─ DINGTALK_SECRET    ├─ 交易配置                      │
│  ├─ FUTU_LOGIN_ACCOUNT ├─ 风控配置                      │
│  └─ ...                ├─ 通知配置                      │
│                        └─ 监控配置                      │
└─────────────────────────────────────────────────────────┘
         ↓                        ↓
         └────────────────────────┘
                    ConfigCenter
                 (统一加载器/热重载)
```

## 两层配置说明

### 1. `.env` - 敏感环境变量

存储不能提交到版本控制的敏感信息：

```bash
# API 鉴权
API_TOKEN=your_secret_token

# 钉钉通知
DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
DINGTALK_SECRET=xxx

# 富途账号
FUTU_LOGIN_ACCOUNT=your_account
FUTU_LOGIN_PWD=your_password

# 交易环境
TRADE_ENV=1           # 0=实盘，1=模拟
ALLOW_REAL_TRADING=false
```

### 2. `config/config.yaml` - 唯一配置源

所有其他配置项都在此文件中：

```yaml
version: "2.0.0"

trading:
  mode: paper
  max_daily_trades: 10
  max_single_position: 0.20

risk_limits:
  min_confidence: 0.45
  max_drawdown: 0.12

strategy:
  confluence_v2:
    enabled: true
    min_factors: 3
```

## 快速开始

### 访问配置

```python
# 方式 1: 通过 ConfigCenter
from config.config_center import get_config_center
config = get_config_center()
print(config.get('trading.mode'))

# 方式 2: 通过 app.config
from app.config import config_center
print(config_center.get('risk_limits.max_drawdown'))

# 方式 3: 通过 ConfigContainer (依赖注入)
from app.core.config_container import ConfigContainer
container = ConfigContainer()
print(container.get('strategy.confluence_v2.enabled'))
```

### 配置热重载

```python
from config.config_center import get_config_center

config = get_config_center()

# 手动检查重载
if config.reload_if_changed():
    print("配置已更新")

# 自动监控 (每 60 秒检查)
config.start_auto_watch(interval=60)
```

## 已清理的冗余文件

以下文件已合并到 `config.yaml`，不再使用：

| 已删除文件 | 合并到 config.yaml 路径 |
|-----------|------------------------|
| risk_config.yaml | risk_limits, stop_loss, take_profit |
| strategy_params.json | strategy |
| dynamic_exit_params.yaml | dynamic_exit |
| position_strategy.yaml | trading.max_* |
| app_config.yaml | trading, server, logging |
| system_config.yaml | base, monitoring |

## 保留的独立配置文件

以下文件独立存在，未被合并：

| 文件 | 用途 |
|------|------|
| alert_rules.yaml | 告警规则配置 |
| trading_rules_20260402.yaml | 交易规则 |
| watchlist.json | 监控股票列表 |
| backtest_presets.json | 回测预设 |
| accounts.yaml | 账户配置 |
| version.yaml | 版本信息 |

## 环境变量覆盖

可以通过环境变量覆盖配置文件中的值：

```bash
export TRADING_MODE=real
export MAX_DAILY_TRADES=5
export CONSENSUS_THRESHOLD=0.7
```

## 配置校验

```python
from config.config_center import get_config_center

config = get_config_center()
config.load()

# 检查配置有效性
if config.is_valid():
    print("✅ 配置有效")
else:
    for error in config.get_validation_errors():
        print(f"❌ {error}")
```

## 最佳实践

1. **敏感信息放 .env** - 不要将密码、token 等提交到 git
2. **使用 config.yaml** - 所有其他配置使用统一文件
3. **启用热重载** - 开发环境开启自动监控配置变更
4. **配置变更日志** - 记录重要的配置修改

---

更新时间：2026-04-09
版本：v5.0 (配置收敛后)
