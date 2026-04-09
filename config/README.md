# FTSeek 配置中心使用指南

## 概述

配置中心 (ConfigCenter) 是 FTSeek v2.0 的核心组件，用于统一管理所有配置文件，解决配置分散、重复、冲突等问题。

## 主要特性

- **统一加载**: 自动加载并合并多个配置文件
- **点式访问**: 支持 `config.trading.mode` 风格访问
- **热重载**: 检测文件变更并自动重载
- **配置校验**: 启动时自动校验配置合理性
- **环境变量覆盖**: 支持使用环境变量覆盖配置文件
- **变更通知**: 支持配置变更回调通知

## 快速开始

### 1. 基本使用

```python
from config.config_center import get_config_center

# 获取配置中心单例
config = get_config_center()

# 访问配置
print(config.trading.mode)              # 交易模式
print(config.risk_limits.max_drawdown)  # 最大回撤
print(config.strategy.weights)          # 策略权重
```

### 2. 使用 get 方法

```python
# 支持嵌套路径访问
mode = config.get('trading.mode', 'paper')
drawdown = config.get('risk_limits.max_drawdown', 0.15)

# 获取整个字典
trading_config = config.get('trading')
```

### 3. 配置热重载

```python
# 检查并重载变更的配置
if config.reload_if_changed():
    print("配置已重新加载")

# 或者启动自动监控
config.start_auto_watch(interval=30)  # 每 30 秒检查一次
```

### 4. 配置变更通知

```python
from config.config_center import ConfigChangeEvent

def on_change(event: ConfigChangeEvent):
    print(f"配置变更：{event.key}")
    print(f"旧值：{event.old_value}")
    print(f"新值：{event.new_value}")

# 注册回调
config.on_change(on_change)
```

## 配置结构

### 统一配置文件 (config.yaml)

```yaml
version: "2.0.0"

# 基础配置
base:
  environment: production
  log_level: INFO

# 富途配置
futu:
  host: "127.0.0.1"
  port: 11111
  trade_env: 1

# 交易配置
trading:
  mode: paper
  max_daily_trades: 10
  max_single_position: 0.20

# 风控配置
risk_limits:
  min_confidence: 0.45
  max_drawdown: 0.12
  max_daily_loss: 0.03

# 策略配置
strategy:
  enabled:
    primary: "confluence_v2"
  weights:
    confluence_v2: 1.5
    supertrend: 1.0
```

### 配置优先级

配置文件按以下顺序加载和合并（后面的覆盖前面的）：

1. `config.yaml` - 统一配置文件（最高优先级）
2. `unified_config.yaml` - 旧版主配置
3. `alert_rules.yaml` - 告警规则
4. `trading_rules_*.yaml` - 交易规则

## 环境变量覆盖

可以通过环境变量覆盖配置文件中的值：

```bash
# 交易模式
export TRADING_MODE=real

# 风控参数
export MAX_DAILY_TRADES=5
export MAX_DRAWDOWN=0.10

# 钉钉通知
export DINGTALK_WEBHOOK_URL=xxx
export DINGTALK_SECRET=xxx
```

## 配置校验

配置中心会自动校验配置的合理性：

```python
# 检查配置是否有效
if config.is_valid():
    print("配置有效")
else:
    print("配置有错误:")
    for error in config.get_validation_errors():
        print(f"  - {error}")

# 获取校验摘要
print(config.get_validation_summary())
# 输出：⚠️ 校验通过：3 个警告
```

## 向后兼容

旧的 `config` 模块接口仍然可用：

```python
# 旧代码仍然可以工作
from config import config as old_config

print(old_config.TRADE_ENV)
print(old_config.WATCH_LIST)

# 新代码推荐使用配置中心
from config.config_center import get_config_center

config = get_config_center()
print(config.trading.mode)
```

## 配置迁移

如果你有旧的分散配置文件，可以使用迁移工具：

```bash
# 备份现有配置并迁移到新格式
python config/migrate_to_unified.py
```

迁移工具会：
1. 备份所有现有配置到 `config/backups/时间戳/`
2. 合并配置到新文件
3. 验证配置完整性
4. 提供回滚功能

## 最佳实践

### 1. 使用环境变量存储敏感信息

```yaml
# config.yaml
futu:
  login_account: "${FUTU_LOGIN_ACCOUNT}"  # 从环境变量读取
  login_password: "${FUTU_LOGIN_PWD}"
```

### 2. 生产环境使用统一配置

```bash
# 使用统一的 config.yaml，便于管理
cp config/config.yaml config/config.prod.yaml
```

### 3. 启用配置热重载

```python
# 启动时开启自动监控
config.start_auto_watch(interval=60)
```

### 4. 配置变更日志

```python
def log_change(event: ConfigChangeEvent):
    logger.info(f"配置变更：{event.key} = {event.new_value}")

config.on_change(log_change)
```

## 常见问题

### Q: 配置加载失败怎么办？

A: 检查校验错误和警告：
```python
print(config.get_validation_errors())
print(config.get_validation_warnings())
```

### Q: 如何添加自定义配置项？

A: 直接在 `config.yaml` 中添加，然后使用点式访问：
```yaml
# config.yaml
custom:
  my_feature:
    enabled: true
    threshold: 0.5
```
```python
enabled = config.get('custom.my_feature.enabled')
```

### Q: 配置热重载不生效？

A: 确保：
1. 配置文件确实已修改（哈希值变化）
2. 自动监控已启动：`config.start_auto_watch()`
3. 检查日志输出

## API 参考

### ConfigCenter 类

| 方法 | 说明 |
|------|------|
| `load(force=False)` | 加载配置文件 |
| `get(key, default)` | 获取配置值 |
| `get_dict()` | 获取完整配置字典 |
| `reload_if_changed()` | 检查并重载变更 |
| `start_auto_watch(interval)` | 启动自动监控 |
| `on_change(callback)` | 注册变更回调 |
| `is_valid()` | 配置是否有效 |
| `get_validation_errors()` | 获取错误列表 |
| `get_validation_warnings()` | 获取警告列表 |

## 配置项完整列表

### 交易配置 (trading.*)
- `trading.mode` - 交易模式 (paper/real)
- `trading.max_daily_trades` - 日最大交易次数
- `trading.max_single_position` - 单标的最大仓位
- `trading.max_total_position` - 总仓位上限
- `trading.max_open_positions` - 最大持仓数

### 风控配置 (risk_limits.*)
- `risk_limits.min_confidence` - 最小置信度
- `risk_limits.max_drawdown` - 最大回撤
- `risk_limits.max_daily_loss` - 日亏损限制
- `risk_limits.max_weekly_loss` - 周亏损限制
- `risk_limits.consecutive_loss_limit` - 连续亏损限制

### 策略配置 (strategy.*)
- `strategy.enabled.primary` - 主力策略
- `strategy.weights` - 策略权重
- `strategy.consensus.threshold` - 共识阈值
- `strategy.ai.weight` - AI 权重

### 通知配置 (notification.*)
- `notification.dingtalk.enabled` - 钉钉通知开关
- `notification.dingtalk.webhook_url` - Webhook URL
- `notification.dingtalk.secret` - 签名密钥

---

更新时间：2026-04-05
版本：v2.0
