# FTSeek 配置文档

**版本**: 2.0.0  
**更新时间**: 2026-04-05  
**生成时间**: 2026-04-11 16:01:02

---

## 配置概览

| 指标 | 数值 |
|-----|------|
| 顶级配置项 | 37 |
| 总配置键数 | 905 |
| 最大嵌套深度 | 5 |
| 重复配置组 | 6 |

---

## 配置章节

### 基础配置

**描述**: 环境、日志级别、调试模式等基础设置

**配置项** (4个):

- **version**: `2.0.0`
- **updated**: `2026-04-05`
- **description**: `FTSeek 量化交易系统 - 统一配置`
- **base**: 3 个子配置项

### 交易连接

**描述**: 富途OpenD连接配置、API服务器

**配置项** (2个):

- **futu**: 10 个子配置项
- **server**: 7 个子配置项

### 交易规则

**描述**: 股票池、仓位限制、订单配置

**配置项** (3个):

- **trading**: 16 个子配置项
- **markets**: 2 个子配置项
- **watchlist**: 3 个子配置项

### 风控体系

**描述**: 五层风控体系：信号级→订单级→仓位级→账户级→系统级

**配置项** (5个):

- **risk_limits**: 11 个子配置项
- **stop_loss**: 4 个子配置项
- **take_profit**: 6 个子配置项
- **circuit_breaker**: 2 个子配置项
- **loss_pause**: 3 个子配置项

### 策略引擎

**描述**: 共识引擎、策略参数、动态退出、信号冷却

**配置项** (5个):

- **strategy**: 28 个子配置项
- **dynamic_exit**: 5 个子配置项
- **signal_cooldown**: 3 个子配置项
- **four_step**: 4 个子配置项
- **smart_push**: 5 个子配置项

### 通知告警

**描述**: 钉钉通知、监控告警规则

**配置项** (2个):

- **notification**: 2 个子配置项
- **monitoring**: 3 个子配置项

### 数据持久化

**描述**: SQLite数据库、缓存配置

**配置项** (2个):

- **database**: 4 个子配置项
- **persistence**: 6 个子配置项

### AI与智能

**描述**: AI情绪分析、信号质量评分

**配置项** (2个):

- **ai**: 9 个子配置项
- **quality_scorer**: 4 个子配置项

### 调度与定时

**描述**: 定时任务、调度器配置

**配置项** (1个):

- **schedule**: 4 个子配置项

### 日志与调试

**描述**: 日志配置、特性开关

**配置项** (2个):

- **logging**: 10 个子配置项
- **features**: 7 个子配置项

### 回测与优化

**描述**: 回测参数、优化配置

**配置项** (1个):

- **backtest**: 2 个子配置项

### 配置管理

**描述**: 配置迁移、版本管理

**配置项** (1个):

- **migration**: 4 个子配置项

## ⚠️ 重复配置警告

以下配置项在多个位置定义，请确保值一致：

| 位置1 | 位置2 | 值1 | 值2 | 一致性 |
|------|------|-----|-----|--------|
| trading.max_single_position | risk_limits.max_single_position | 0.2 | 0.2 | ✅ |
| trading.max_total_position | risk_limits.max_total_position | 0.6 | 0.6 | ✅ |
| trading.max_daily_trades | risk_limits.max_daily_trades | 10 | 10 | ✅ |
| trading.max_daily_loss | risk_limits.max_daily_loss | 0.03 | 0.03 | ✅ |
| trading.max_weekly_loss | risk_limits.max_weekly_loss | 0.08 | 0.08 | ✅ |
| trading.min_signal_confidence | risk_limits.min_confidence | 0.5 | 0.5 | ✅ |

