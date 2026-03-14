# FTSeek 策略监控配置说明

## 📋 功能概述

FTSeek 现在支持以下自动化功能：

| 功能 | 执行时间 | 说明 |
|------|----------|------|
| **策略信号监控** | 交易日 9-16 点 每 15 分钟 | 实时监控港股，生成买卖信号并推送钉钉 |
| **新闻汇总推送** | 交易日 10-15 点 每小时 | 抓取财经新闻并汇总推送 |
| **风险监控推送** | 交易日 10/12/14/16 点 | 监控账户风险和持仓 |
| **收盘统计** | 交易日 16:30 | 每日交易统计和盈亏分析 |
| **策略回测周报** | 每周一 09:00 | 过去 7 天策略收益率报告 |
| **周报** | 每周六 09:00 | 周度交易总结 |
| **月报** | 每月 1 日 09:00 | 月度交易总结 |

---

## 🎯 策略信号监控

### 关注的股票

默认关注以下港股：

```
HK.00700  腾讯控股
HK.09988  阿里巴巴
HK.09992  泡泡玛特
HK.02390  智谱 AI
HK.03690  美团
```

### 启用的策略

```yaml
ma_cross:      # 均线交叉策略
  fast_period: 5
  slow_period: 20

rsi_rebound:   # RSI 反弹策略
  rsi_period: 14
  oversold_threshold: 30

macd:          # MACD 策略
  fast_period: 12
  slow_period: 26
  signal_period: 9
```

### 修改配置

编辑 `/opt/ftseek/FTSeek/scripts/strategy_monitor.py`:

```python
# 修改关注股票
WATCHLIST = [
    'HK.00700',  # 添加/删除股票
    'HK.09988',
    # ...
]

# 修改策略配置
STRATEGIES_CONFIG = {
    'ma_cross': {
        'enabled': True,  # False 禁用
        'fast_period': 5,
        'slow_period': 20,
    },
    # ...
}
```

---

## 📊 收益率回测

### 查看历史信号

```bash
cd /opt/ftseek/FTSeek
python3 scripts/query_signals.py --limit 50
```

### 生成回测报告

```bash
# 过去 30 天
python3 scripts/backtest_report.py --days 30

# 周报 (7 天)
python3 scripts/backtest_report.py --weekly

# 月报 (30 天)
python3 scripts/backtest_report.py --monthly
```

### 导出信号数据

```bash
# 导出为 JSON
python3 scripts/query_signals.py --format json --output signals.json

# 导出特定股票
python3 scripts/query_signals.py --stock HK0700 --format csv --output tencent.csv
```

---

## 🔧 手动测试

### 测试策略监控

```bash
cd /opt/ftseek/FTSeek
export $(cat .env | grep -v '^#' | xargs)
python3 scripts/strategy_monitor.py
```

### 测试回测报告

```bash
cd /opt/ftseek/FTSeek
export $(cat .env | grep -v '^#' | xargs)
python3 scripts/backtest_report.py --days 7
```

### 查看日志

```bash
# 策略监控日志
tail -f /opt/ftseek/FTSeek/logs/strategy_monitor.log

# 回测日志
tail -f /opt/ftseek/FTSeek/logs/backtest.log

# 所有日志
ls -lh /opt/ftseek/FTSeek/logs/
```

---

## ⚙️ Crontab 配置

查看当前定时任务：

```bash
crontab -l
```

编辑定时任务：

```bash
crontab -e
```

---

## 📱 钉钉推送

所有信号和报告都会推送到配置的钉钉群：

- 买卖信号：实时推送 (带策略分析)
- 新闻汇总：每小时推送
- 风险监控：定时推送
- 回测报告：每周一推送

### 推送格式

**买入信号示例:**
```
🟢 买入信号 - 腾讯控股 (HK.00700)

策略：均线交叉
价格：380.50 HKD
原因：MA5(382.00) 上穿 MA20(378.50), 金叉买入

🤖 AI 分析
综合评估：情绪评分 +0.50
建议：buy
影响程度：7/10
```

**回测报告示例:**
```
📊 FTSeek 策略回测报告

统计周期：过去 7 天

核心指标
| 指标 | 数值 |
|------|------|
| 总信号数 | 15 条 |
| 交易次数 | 8 笔 |
| 胜率 | 62.5% |
| 总收益 | +3.25% |
| 平均收益 | +0.41% |
```

---

## 🚨 故障排查

### 信号未推送

1. 检查钉钉 Webhook 配置
2. 查看日志：`tail logs/strategy_monitor.log`
3. 测试推送：`python3 scripts/test_dingtalk_enhanced.py`

### 富途 API 连接失败

1. 检查 OpenD 是否运行：`ps aux | grep FutuOpenD`
2. 检查端口：`ss -tlnp | grep 11111`
3. 重启 OpenD

### 策略无信号

1. 检查 K 线数据是否正常获取
2. 调整策略参数 (可能当前行情不满足条件)
3. 查看日志确认策略运行状态

---

*最后更新：2026-03-02 | FTSeek V3*
