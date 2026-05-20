# API 限流监控系统

## 概述

API 限流监控系统用于防止 TwelveData API 超限 (免费版 800 次/天),并在接近限额时自动切换到备用数据源 Finnhub。

## 功能特性

1. **实时记录 API 调用**: 自动记录 TwelveData、Finnhub、AlphaVantage 的调用次数
2. **智能预警**: 700 次时发送钉钉预警
3. **自动切换**: 800 次时自动切换到 Finnhub
4. **钉钉告警**: 预警和切换都会推送钉钉消息
5. **每日自动重置**: 每天 00:00 自动重置计数器

## 架构设计

```
┌─────────────────────────────────────────────────────────┐
│                   FTSeek-US 系统                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────────┐      ┌──────────────────┐        │
│  │ TwelveDataProvider│      │  FinnhubProvider │        │
│  └────────┬─────────┘      └────────┬─────────┘        │
│           │                          │                   │
│           │ 记录调用                  │ 记录调用           │
│           ▼                          ▼                   │
│  ┌─────────────────────────────────────────────────┐    │
│  │          APILimitMonitor (监控器)                │    │
│  │  - 记录 API 调用次数                               │    │
│  │  - 检查阈值 (700/800)                            │    │
│  │  - 发送钉钉告警                                  │    │
│  │  - 切换数据源                                    │    │
│  └─────────────────────────────────────────────────┘    │
│           │                                              │
│           ▼                                              │
│  ┌──────────────────┐      ┌──────────────────┐        │
│  │  状态文件         │      │  钉钉推送         │        │
│  │  api_limit_      │      │  DingTalkSender   │        │
│  │  state.json      │      │                   │        │
│  └──────────────────┘      └──────────────────┘        │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## 阈值配置

| 阈值 | 次数 | 动作 |
|------|------|------|
| 预警阈值 | 700 | 发送钉钉预警消息 |
| 限额阈值 | 800 | 切换到 Finnhub + 发送钉钉告警 |

## 数据源切换逻辑

```python
# TwelveDataProvider 内部检查
if API_LIMIT_MONITOR_ENABLED and not should_use_twelvedata():
    logger.warning("已切换到备用数据源，跳过")
    return None
```

当达到 800 次限额后:
- `should_use_twelvedata()` 返回 `False`
- TwelveData 调用被跳过
- 系统自动使用 Finnhub 作为数据源

## 使用方法

### 1. 手动检查状态

```bash
cd /opt/ftseek/FTSeek-US
python scripts/api_limit_monitor.py check
```

### 2. 查看详细状态 (JSON)

```bash
python scripts/api_limit_monitor.py status
```

### 3. 手动重置状态

```bash
python scripts/api_limit_monitor.py reset
```

### 4. 测试钉钉推送

```bash
python scripts/api_limit_monitor.py test
```

### 5. 定时任务 (已配置)

```bash
# 每 5 分钟自动检查和告警
*/5 * * * * cd /opt/ftseek/FTSeek-US && /opt/ftseek/FTSeek-US/venv/bin/python3 scripts/api_limit_monitor.py check >> logs/api_limit_monitor.log 2>&1
```

## 集成方式

### TwelveDataProvider 集成

```python
# 在 __init__ 中导入
from scripts.api_limit_monitor import should_use_twelvedata, record_twelvedata_call

# 在 get_quote 中检查
if API_LIMIT_MONITOR_ENABLED and not should_use_twelvedata():
    return None  # 已切换，跳过

# 记录调用
if API_LIMIT_MONITOR_ENABLED:
    record_twelvedata_call(1)
```

### FinnhubProvider 集成

```python
# 在 __init__ 中导入
from scripts.api_limit_monitor import record_finnhub_call

# 记录调用
if API_LIMIT_MONITOR_ENABLED:
    record_finnhub_call(1)
```

## 状态文件

位置：`/opt/ftseek/FTSeek-US/data/api_limit_state.json`

```json
{
  "last_reset_date": "2026-03-30",
  "twelvedata_calls": 0,
  "finnhub_calls": 0,
  "alphavantage_calls": 0,
  "current_source": "twelvedata",
  "warning_sent": false,
  "limit_reached_sent": false,
  "last_warning_time": null,
  "last_switch_time": null
}
```

## 告警消息示例

### 预警消息 (700 次)

```
## ⚠️ TwelveData API 用量预警

**当前用量**: 700 / 800 次
**剩余配额**: 100 次
**使用比例**: 87.5%

**建议操作**:
- 减少非必要 API 调用
- 增加缓存时间
- 准备切换到 Finnhub
```

### 限额告警 (800 次)

```
## 🚨 TwelveData API 限额已用尽

**当前用量**: 800 / 800 次
**已切换数据源**: twelvedata → Finnhub

**影响**:
- 技术指标可能不可用 (Finnhub 不支持)
- 实时报价正常

**恢复时间**: 次日 00:00 自动重置
```

## 监控指标

| 指标 | 说明 | 阈值 |
|------|------|------|
| TwelveData 调用次数 | 当日累计调用 | 700 预警，800 切换 |
| Finnhub 调用次数 | 当日累计调用 | 60 次/分钟 |
| AlphaVantage 调用次数 | 当日累计调用 | 500 次/天 |
| 当前数据源 | twelvedata / finnhub | - |

## 故障排查

### 问题：告警未发送

1. 检查钉钉配置:
   ```bash
   grep DINGTALK /opt/ftseek/FTSeek-US/.env.us
   ```

2. 检查状态文件:
   ```bash
   cat /opt/ftseek/FTSeek-US/data/api_limit_state.json
   ```

3. 手动测试推送:
   ```bash
   python scripts/api_limit_monitor.py test
   ```

### 问题：未自动切换

1. 检查日志:
   ```bash
   tail -f logs/api_limit_monitor.log
   ```

2. 检查 cron 任务:
   ```bash
   crontab -l | grep api_limit
   ```

3. 手动触发检查:
   ```bash
   python scripts/api_limit_monitor.py check
   ```

## 最佳实践

1. **增加缓存时间**: 在接近限额时，增加数据缓存时间减少调用
2. **批量获取**: 使用 `get_batch_quotes` 代替多次 `get_quote`
3. **监控日志**: 定期检查 `logs/api_limit_monitor.log`
4. **每日检查**: 通过钉钉日报了解 API 使用情况

## 相关文件

- 监控脚本：`/opt/ftseek/FTSeek-US/scripts/api_limit_monitor.py`
- 状态文件：`/opt/ftseek/FTSeek-US/data/api_limit_state.json`
- TwelveData 提供商：`/opt/ftseek/FTSeek-US/app/data/twelvedata_provider.py`
- Finnhub 提供商：`/opt/ftseek/FTSeek-US/app/data/finnhub_provider.py`
- 日志文件：`/opt/ftseek/FTSeek-US/logs/api_limit_monitor.log`

## 验收标准验证

✅ **实时记录 API 调用**
- 每次调用 TwelveData/Finnhub 都会记录到状态文件
- 验证：`python scripts/api_limit_monitor.py status`

✅ **700 次时发送预警**
- 达到 700 次自动发送钉钉预警
- 验证：模拟 700 次调用，检查钉钉消息

✅ **800 次时自动切换数据源**
- 达到 800 次自动切换到 Finnhub
- TwelveData 调用被跳过
- 验证：模拟 800 次调用，检查 `current_source`

✅ **告警推送到钉钉**
- 预警和切换都会推送钉钉
- 验证：检查钉钉消息历史

---

*最后更新：2026-03-30*
